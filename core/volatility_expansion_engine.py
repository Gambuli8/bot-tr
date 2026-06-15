"""
core/volatility_expansion_engine.py
Volatility Expansion Trend Engine — motor INDEPENDIENTE (no toca Price Action).

HIPÓTESIS A VALIDAR (no a defender):
  "BTC recompensa más el seguimiento de tendencia + expansión de volatilidad que
   la reversión por liquidity sweeps."

LÓGICA (todo confirmado al cierre, sin anticipación):
  - Macro 4H: EMA50 vs EMA200. BULL si EMA50>EMA200, BEAR si <. Si la separación
    es insignificante (mercado lateral) → no operar.
  - Compresión 1H: ATR(14) actual < ratio × ATR promedio de N días (mercado
    comprimido = energía por liberar).
  - Breakout 1H: cierre rompe el máx (LONG) / mín (SHORT) de las últimas K velas.
  - Volumen 1H: volumen > mult × media(20).
  - Entrada a mercado a favor de la macro. SL = sl_atr×ATR, TP = tp_atr×ATR.

DISEÑO: la generación de señales es VECTORIZADA (compute_signals) → O(n), apta
para backtests de años. analyze() es para uso en vivo (1 llamada por ciclo).
Anti-lookahead: el macro 4H se alinea por TIEMPO DE DISPONIBILIDAD (la vela 4h
recién aporta su valor cuando cerró), vía merge_asof.
"""

from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd
import ta


Direction = Literal["LONG", "SHORT"]


@dataclass
class VEParams:
    ema_fast: int = 50
    ema_slow: int = 200
    min_ema_sep_pct: float = 0.001     # separación mínima EMA50/EMA200 (0.1%) p/ no-lateral
    atr_period: int = 14
    compression_lookback_bars: int = 720   # 30 días de 1h
    compression_ratio: float = 0.70
    breakout_lookback: int = 24
    vol_period: int = 20
    vol_mult: float = 1.5
    sl_atr: float = 1.5
    tp_atr: float = 3.0

    @classmethod
    def from_settings(cls, s) -> "VEParams":
        g = lambda k, d: getattr(s, k, d)
        return cls(
            ema_fast=g("ve_ema_fast", 50),
            ema_slow=g("ve_ema_slow", 200),
            min_ema_sep_pct=g("ve_min_ema_sep_pct", 0.001),
            atr_period=g("ve_atr_period", 14),
            compression_lookback_bars=g("ve_compression_lookback_bars", 720),
            compression_ratio=g("ve_compression_ratio", 0.70),
            breakout_lookback=g("ve_breakout_lookback", 24),
            vol_period=g("ve_vol_period", 20),
            vol_mult=g("ve_vol_mult", 1.5),
            sl_atr=g("ve_sl_atr", 1.5),
            tp_atr=g("ve_tp_atr", 3.0),
        )


@dataclass
class VEDecision:
    """Compatible con el Sim del backtester (mismos campos que PriceActionDecision)."""
    accion: Literal["COMPRAR", "VENDER", "ESPERAR"]
    direction: Direction
    confianza: float
    razon: str
    entry_price: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    advertencias: list = field(default_factory=list)


def _atr(df: pd.DataFrame, window: int) -> pd.Series:
    return ta.volatility.AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=window,
    ).average_true_range()


def _macro_4h(df_4h: pd.DataFrame, p: VEParams) -> pd.DataFrame:
    """Devuelve df_4h con columna 'macro' (1=BULL, -1=BEAR, 0=lateral) y la marca
    de tiempo a partir de la cual ese valor está DISPONIBLE (cierre del 4h)."""
    d = df_4h.copy()
    d["ema_f"] = ta.trend.EMAIndicator(close=d["close"], window=p.ema_fast).ema_indicator()
    d["ema_s"] = ta.trend.EMAIndicator(close=d["close"], window=p.ema_slow).ema_indicator()
    sep = (d["ema_f"] - d["ema_s"]) / d["close"]
    macro = pd.Series(0, index=d.index)
    macro[(d["ema_f"] > d["ema_s"]) & (sep >= p.min_ema_sep_pct)] = 1
    macro[(d["ema_f"] < d["ema_s"]) & (-sep >= p.min_ema_sep_pct)] = -1
    d["macro"] = macro
    # El valor de una vela 4h (open=index) se conoce recién al cerrar: index + 4h.
    d["avail"] = d.index + pd.Timedelta(hours=4)
    return d[["macro", "avail"]].dropna()


def compute_signals(df_1h: pd.DataFrame, df_4h: pd.DataFrame,
                    p: Optional[VEParams] = None) -> pd.DataFrame:
    """
    VECTORIZADO. Devuelve un DataFrame indexado como df_1h con columnas:
      close, atr, signal (1 LONG / -1 SHORT / 0 nada).
    El runner aplica los múltiplos de SL/TP sobre 'atr' (así se barren TP sin
    recomputar señales).
    """
    p = p or VEParams()
    # Normalizar índices a ns para evitar mismatch de unidades (ms/us) en el align.
    df1 = df_1h.copy()
    df1.index = pd.DatetimeIndex(df1.index).astype("datetime64[ns]")
    df4 = df_4h.copy()
    df4.index = pd.DatetimeIndex(df4.index).astype("datetime64[ns]")

    out = pd.DataFrame(index=df1.index)
    out["close"] = df1["close"]

    # ATR(14) y compresión vs promedio de N días.
    atr = _atr(df1, p.atr_period)
    out["atr"] = atr
    atr_avg = atr.rolling(p.compression_lookback_bars).mean()
    # Compresión medida en la vela PREVIA al breakout: el breakout en sí expande
    # la volatilidad, así que medir el ATR de la propia vela de ruptura sería
    # contradictorio. shift(1) = "el mercado venía comprimido ANTES de romper".
    compressed = atr.shift(1) < (p.compression_ratio * atr_avg.shift(1))

    # Breakout: cierre rompe el extremo de las K velas PREVIAS (excluye la actual).
    prev_high = df1["high"].rolling(p.breakout_lookback).max().shift(1)
    prev_low = df1["low"].rolling(p.breakout_lookback).min().shift(1)
    breakout_up = df1["close"] > prev_high
    breakout_down = df1["close"] < prev_low

    # Volumen > mult × media(20) previa.
    vol_avg = df1["volume"].rolling(p.vol_period).mean().shift(1)
    vol_ok = df1["volume"] > (p.vol_mult * vol_avg)

    # Macro 4H alineado SIN lookahead: reindex por TIEMPO DE DISPONIBILIDAD
    # (la vela 4h recién aporta su valor cuando cerró = open+4h) con ffill.
    macro4 = _macro_4h(df4, p)
    ms = pd.Series(macro4["macro"].values,
                   index=pd.DatetimeIndex(macro4["avail"].values).astype("datetime64[ns]"))
    ms = ms[~ms.index.duplicated(keep="last")].sort_index()
    macro = ms.reindex(df1.index, method="ffill")

    long_sig = (macro == 1) & compressed & breakout_up & vol_ok
    short_sig = (macro == -1) & compressed & breakout_down & vol_ok

    signal = pd.Series(0, index=df1.index)
    signal[long_sig.fillna(False)] = 1
    signal[short_sig.fillna(False)] = -1
    out["signal"] = signal
    return out


class VolatilityExpansionEngine:
    """Uso en vivo: analyze(df_1h, df_4h) → VEDecision sobre la última vela cerrada."""

    def __init__(self, settings):
        self.settings = settings
        self.p = VEParams.from_settings(settings)

    @property
    def is_safe_mode(self) -> bool:
        return False

    def reset_circuit_breaker(self) -> None:
        pass

    def analyze(self, df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> VEDecision:
        need = max(self.p.compression_lookback_bars + 5, self.p.ema_slow + 5)
        if len(df_1h) < need or len(df_4h) < self.p.ema_slow + 5:
            return self._wait("warm-up incompleto")
        sig = compute_signals(df_1h, df_4h, self.p)
        last = sig.iloc[-1]
        s = int(last["signal"])
        if s == 0:
            return self._wait("sin señal (macro/compresión/breakout/volumen)")
        atr = float(last["atr"])
        entry = float(last["close"])
        if atr <= 0:
            return self._wait("ATR inválido")
        if s == 1:
            sl = entry - self.p.sl_atr * atr
            tp = entry + self.p.tp_atr * atr
            direction, accion = "LONG", "COMPRAR"
        else:
            sl = entry + self.p.sl_atr * atr
            tp = entry - self.p.tp_atr * atr
            direction, accion = "SHORT", "VENDER"
        sl_pct = abs(entry - sl) / entry
        tp_pct = abs(tp - entry) / entry
        return VEDecision(
            accion=accion, direction=direction, confianza=0.7,
            razon=(f"VolExpansion {direction} | breakout {self.p.breakout_lookback}v | "
                   f"ATR comprimido | R:R 1:{self.p.tp_atr/self.p.sl_atr:.1f}"),
            entry_price=round(entry, 2),
            stop_loss_price=round(sl, 2), take_profit_price=round(tp, 2),
            stop_loss_pct=round(sl_pct, 5), take_profit_pct=round(tp_pct, 5),
            advertencias=["Volatility Expansion Trend Engine"],
        )

    def _wait(self, motivo: str) -> VEDecision:
        return VEDecision("ESPERAR", "LONG", 0.0, motivo)
