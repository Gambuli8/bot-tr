"""
core/smc_engine.py
Motor SMC/ICT automatizado: Barridas de Liquidez + Inverse Fair Value Gaps (IFVG).
TF base estricto: 15m (decisión de arquitectura — 5m/3m destruyen el edge por
ruido + fees, ya validado). Sin discrecionalidad humana: todo es matemático.

NOTA HONESTA: un primo de esto (liquidity sweeps en 1h, PriceActionEngine) NO
sobrevivió el WFA de 5 años. Este motor debe pasar el MISMO gauntlet (5a + WFA +
costos reales) antes de considerarse para plata real. Un backtest de 30d no valida.

Pipeline:
  1. Daily bias (TF superior, EMA50/200) → filtra dirección (solo long si bull).
  2. FVGs de 3 velas (vectorizado).
  3. Barrida de liquidez (mecha supera swing previo, cuerpo cierra adentro).
  4. IFVG: una vela cierra del otro lado de un FVG opuesto activo → inversión = gatillo.
  5. Consolidación de FVGs múltiples en zonas (anti-ruido).
  6. SL en la mecha de la vela de inversión, TP en swing opuesto, filtro R:R + fees.

Devuelve el df con columnas: signal_long, signal_short, smc_sl, smc_tp.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import ta


@dataclass
class SMCParams:
    bias_tf: str = "4h"            # TF del daily bias macro
    bias_ema_fast: int = 50
    bias_ema_slow: int = 200
    swing_lookback: int = 20       # velas 15m para swing high/low (TP y liquidez)
    sweep_lookback: int = 20       # velas para detectar la barrida
    sweep_within: int = 6          # la barrida debe ser de las últimas N velas
    fvg_merge_atr: float = 0.5     # fusiona FVGs cuyo gap < 0.5×ATR (anti-ruido)
    fvg_max_age: int = 96          # un FVG caduca tras N velas (~1 día en 15m)
    atr_period: int = 14
    min_rr: float = 1.5            # R:R mínimo
    fee_maker: float = 0.0002      # entrada maker 0.02%
    fee_taker: float = 0.0005      # salida taker 0.05% (SL a mercado)
    max_fee_to_gain: float = 0.30  # rechaza si fees > 30% de la ganancia bruta


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    return ta.volatility.AverageTrueRange(
        df["high"], df["low"], df["close"], window=n
    ).average_true_range()


def detect_fvg(df: pd.DataFrame):
    """
    FVG de 3 velas (gap de la vela i respecto de la i-2):
      - Bull FVG: low[i] > high[i-2]    → zona [bottom=high[i-2], top=low[i]]
      - Bear FVG: high[i] < low[i-2]    → zona [bottom=high[i], top=low[i-2]]
    Vectorizado. Devuelve (bull, bear, high2, low2).
    """
    high2 = df["high"].shift(2)
    low2 = df["low"].shift(2)
    bull = df["low"] > high2
    bear = df["high"] < low2
    return bull, bear, high2, low2


def detect_sweep(df: pd.DataFrame, n: int):
    """
    Barrida de liquidez (absorción): la mecha supera el swing de las últimas n
    velas, pero el cuerpo CIERRA dentro de la estructura.
      - Bull sweep (de lows): low[i] < min(low[i-n..i-1]) AND close[i] > ese min.
      - Bear sweep (de highs): high[i] > max(high[i-n..i-1]) AND close[i] < ese max.
    Vectorizado.
    """
    prev_low = df["low"].rolling(n).min().shift(1)
    prev_high = df["high"].rolling(n).max().shift(1)
    bull_sweep = (df["low"] < prev_low) & (df["close"] > prev_low)
    bear_sweep = (df["high"] > prev_high) & (df["close"] < prev_high)
    return bull_sweep, bear_sweep


def higher_tf_bias(df: pd.DataFrame, p: SMCParams) -> pd.Series:
    """Daily bias desde TF superior (EMA50/200), alineado al 15m SIN lookahead
    (la señal del TF mayor recién está disponible al cierre de su vela → shift)."""
    h = df["close"].resample(p.bias_tf).last().dropna()
    ef = h.ewm(span=p.bias_ema_fast, adjust=False).mean()
    es = h.ewm(span=p.bias_ema_slow, adjust=False).mean()
    bias = pd.Series(0, index=h.index)
    bias[ef > es] = 1
    bias[ef < es] = -1
    return bias.shift(1).reindex(df.index, method="ffill").fillna(0)


def _consolidate(fvgs: list, atr: float, merge_atr: float) -> list:
    """Fusiona FVGs del mismo tipo cuyas zonas se solapan o están a < merge_atr×ATR.
    Resuelve el ruido de múltiples vacíos adyacentes → zonas de ineficiencia únicas."""
    if not fvgs:
        return []
    zones = sorted(({"bottom": f["bottom"], "top": f["top"]} for f in fvgs),
                   key=lambda z: z["bottom"])
    merged = [dict(zones[0])]
    tol = merge_atr * atr
    for z in zones[1:]:
        if z["bottom"] <= merged[-1]["top"] + tol:
            merged[-1]["top"] = max(merged[-1]["top"], z["top"])
            merged[-1]["bottom"] = min(merged[-1]["bottom"], z["bottom"])
        else:
            merged.append(dict(z))
    return merged


def _viable(entry: float, stop: float, target: float, side: str, p: SMCParams) -> bool:
    """Filtro de viabilidad: R:R mínimo + las fees no se comen la ganancia.
    fee/gross es leverage-independiente (ambos escalan igual con el apalancamiento)."""
    if side == "long":
        risk, reward = entry - stop, target - entry
    else:
        risk, reward = stop - entry, entry - target
    if risk <= 0 or reward <= 0:
        return False
    if reward / risk < p.min_rr:
        return False
    gross = reward / entry                       # ganancia bruta (fracción de precio)
    roundtrip_fee = p.fee_maker + p.fee_taker
    if gross <= 0 or roundtrip_fee / gross > p.max_fee_to_gain:
        return False
    return True


def compute_signals(df: pd.DataFrame, p: Optional[SMCParams] = None) -> pd.DataFrame:
    """
    Recibe OHLCV de 15m, devuelve el df con signal_long, signal_short, smc_sl, smc_tp.
    Detección vectorizada + un forward-loop que mantiene la matriz de FVGs activos
    y emite señal en la inversión (IFVG).
    """
    p = p or SMCParams()
    df = df.copy()
    atr = _atr(df, p.atr_period)
    bull_fvg, bear_fvg, high2, low2 = detect_fvg(df)
    bull_sweep, bear_sweep = detect_sweep(df, p.sweep_lookback)
    bias = higher_tf_bias(df, p)
    swing_high = df["high"].rolling(p.swing_lookback).max()
    swing_low = df["low"].rolling(p.swing_lookback).min()

    n = len(df)
    close, high, low = df["close"].values, df["high"].values, df["low"].values
    sh, sl = swing_high.values, swing_low.values
    atrv, biasv = atr.values, bias.values
    bfvg, ufvg = bear_fvg.values, bull_fvg.values
    h2, l2 = high2.values, low2.values
    bsw, ssw = bull_sweep.values, bear_sweep.values

    active_bear, active_bull = [], []   # FVGs activos (bear→IFVG long; bull→IFVG short)
    sig_long = np.zeros(n, bool)
    sig_short = np.zeros(n, bool)
    sl_arr = np.full(n, np.nan)
    tp_arr = np.full(n, np.nan)
    last_bull_sweep = last_bear_sweep = -10 ** 9

    for i in range(2, n):
        # 1) registrar FVGs nuevos y barridas
        if bfvg[i]:
            active_bear.append({"top": l2[i], "bottom": high[i], "formed": i})
        if ufvg[i]:
            active_bull.append({"top": low[i], "bottom": h2[i], "formed": i})
        if bsw[i]:
            last_bull_sweep = i
        if ssw[i]:
            last_bear_sweep = i
        # 2) caducar FVGs viejos
        active_bear = [f for f in active_bear if i - f["formed"] <= p.fvg_max_age]
        active_bull = [f for f in active_bull if i - f["formed"] <= p.fvg_max_age]
        if np.isnan(atrv[i]):
            continue
        # 3) consolidar (anti-ruido)
        zones_bear = _consolidate(active_bear, atrv[i], p.fvg_merge_atr)
        zones_bull = _consolidate(active_bull, atrv[i], p.fvg_merge_atr)

        # 4) IFVG LONG: bias bull + barrida reciente de lows + cierre POR ENCIMA
        #    del top de un FVG bajista (inversión) que recién ocurre en esta vela.
        if biasv[i] == 1 and (i - last_bull_sweep) <= p.sweep_within:
            for z in zones_bear:
                if close[i] > z["top"] and close[i - 1] <= z["top"]:
                    entry, stop, target = close[i], low[i], sh[i]
                    if _viable(entry, stop, target, "long", p):
                        sig_long[i] = True
                        sl_arr[i] = stop
                        tp_arr[i] = target
                    active_bear = [f for f in active_bear if f["top"] > z["top"]]
                    break

        # 5) IFVG SHORT: simétrico (bias bear + barrida de highs + cierre por DEBAJO
        #    del bottom de un FVG alcista).
        if biasv[i] == -1 and (i - last_bear_sweep) <= p.sweep_within:
            for z in zones_bull:
                if close[i] < z["bottom"] and close[i - 1] >= z["bottom"]:
                    entry, stop, target = close[i], high[i], sl[i]
                    if _viable(entry, stop, target, "short", p):
                        sig_short[i] = True
                        sl_arr[i] = stop
                        tp_arr[i] = target
                    active_bull = [f for f in active_bull if f["bottom"] < z["bottom"]]
                    break

    df["signal_long"] = sig_long
    df["signal_short"] = sig_short
    df["smc_sl"] = sl_arr
    df["smc_tp"] = tp_arr
    return df


class SMCEngine:
    """Wrapper para uso en vivo: corre compute_signals sobre el df 15m."""

    def __init__(self, settings=None, params: Optional[SMCParams] = None):
        self.settings = settings
        self.p = params or SMCParams()

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        return compute_signals(df, self.p)

    def last_signal(self, df: pd.DataFrame) -> dict:
        """Devuelve la señal de la última vela: {dir, entry, sl, tp} o {} si no hay."""
        out = compute_signals(df, self.p)
        row = out.iloc[-1]
        if bool(row["signal_long"]):
            return {"dir": "LONG", "entry": float(row["close"]),
                    "sl": float(row["smc_sl"]), "tp": float(row["smc_tp"])}
        if bool(row["signal_short"]):
            return {"dir": "SHORT", "entry": float(row["close"]),
                    "sl": float(row["smc_sl"]), "tp": float(row["smc_tp"])}
        return {}
