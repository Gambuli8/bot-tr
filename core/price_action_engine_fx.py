"""
core/price_action_engine_fx.py
Port a FOREX del PriceActionEngine validado en cripto (liquidity sweep A FAVOR
de la estructura HTF). NO modifica core/price_action_engine.py — es un módulo
hermano dedicado a forex.

Diferencias vs el motor de cripto:
- pip_size en vez de % (EUR/USD pip = 0.0001). SL/TP bounds en PIPS, no en % del
  entry (en forex los movimientos son ~0.1-1% — los bounds en % no tienen sentido).
- ATR calculado a mano (la lib `ta` no compila en este entorno).
- "Volumen" = tick-count por vela (en forex retail no hay volumen real; el conteo
  de ticks es el proxy estándar de actividad/interés institucional).
- Sin dependencia de `ta` ni del logger global (para que el backtest sea liviano).

La lógica de detección (fractal Williams + estructura HH/HL + sweep a favor) es
idéntica a la del motor de cripto: sólo cambian el dimensionamiento y las unidades.
"""

from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd


Direction = Literal["LONG", "SHORT"]
Structure = Literal["BULL", "BEAR", "RANGE"]

PIP = 0.0001  # EUR/USD


@dataclass
class Swing:
    ts: pd.Timestamp
    idx: int
    kind: Literal["high", "low"]
    price: float


@dataclass
class FxDecision:
    accion: Literal["COMPRAR", "VENDER", "ESPERAR"]
    direction: Direction
    confianza: float
    razon: str
    entry_price: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    sl_pips: float = 0.0
    tp_pips: float = 0.0
    advertencias: list[str] = field(default_factory=list)


# ─────────────────────────────────────────
#  Swings + estructura (idéntico al motor de cripto)
# ─────────────────────────────────────────

def find_swings(df: pd.DataFrame, n: int = 3) -> list[Swing]:
    """Fractal de Williams: swing high = máximo de la ventana ±n velas."""
    if len(df) < 2 * n + 1:
        return []

    h = df["high"]
    l = df["low"]

    left_high_max = h.rolling(n).max().shift(1)
    right_high_max = h.shift(-n).rolling(n).max()
    is_swing_high = (h > left_high_max) & (h > right_high_max)

    left_low_min = l.rolling(n).min().shift(1)
    right_low_min = l.shift(-n).rolling(n).min()
    is_swing_low = (l < left_low_min) & (l < right_low_min)

    swings: list[Swing] = []
    for i in range(n, len(df) - n):
        ts = df.index[i]
        if bool(is_swing_high.iloc[i]):
            swings.append(Swing(ts=ts, idx=i, kind="high", price=float(h.iloc[i])))
        if bool(is_swing_low.iloc[i]):
            swings.append(Swing(ts=ts, idx=i, kind="low", price=float(l.iloc[i])))
    return swings


def determine_structure(swings: list[Swing]) -> Structure:
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "RANGE"

    last2_highs = highs[-2:]
    last2_lows = lows[-2:]
    hh = last2_highs[1].price > last2_highs[0].price
    hl = last2_lows[1].price > last2_lows[0].price
    lh = last2_highs[1].price < last2_highs[0].price
    ll = last2_lows[1].price < last2_lows[0].price

    if hh and hl:
        return "BULL"
    if lh and ll:
        return "BEAR"
    return "RANGE"


# ─────────────────────────────────────────
#  Liquidity sweep (idéntico, vol = tick-count proxy)
# ─────────────────────────────────────────

@dataclass
class SweepSignal:
    direction: Direction
    sweep_extreme: float
    swept_level: float
    candle_close: float
    candle_volume_ratio: float


def detect_liquidity_sweep(
    df_so_far: pd.DataFrame,
    swings: list[Swing],
    structure_htf: Structure,
    vol_mult: float = 1.5,
    vol_window: int = 20,
) -> Optional[SweepSignal]:
    if structure_htf not in ("BULL", "BEAR"):
        return None
    if len(df_so_far) < vol_window + 5:
        return None

    last = df_so_far.iloc[-1]
    last_ts = df_so_far.index[-1]

    vol_ma = df_so_far["volume"].iloc[-(vol_window + 1):-1].mean()
    if vol_ma <= 0:
        return None
    vol_ratio = float(last["volume"]) / float(vol_ma)
    if vol_ratio < vol_mult:
        return None

    if structure_htf == "BULL":
        prev = [s for s in swings if s.kind == "low" and s.ts < last_ts]
        if not prev:
            return None
        swept = prev[-1].price
        if float(last["low"]) < swept and float(last["close"]) > swept:
            return SweepSignal("LONG", float(last["low"]), swept,
                               float(last["close"]), vol_ratio)
        return None

    prev = [s for s in swings if s.kind == "high" and s.ts < last_ts]
    if not prev:
        return None
    swept = prev[-1].price
    if float(last["high"]) > swept and float(last["close"]) < swept:
        return SweepSignal("SHORT", float(last["high"]), swept,
                           float(last["close"]), vol_ratio)
    return None


# ─────────────────────────────────────────
#  ATR a mano (sin `ta`)
# ─────────────────────────────────────────

def compute_atr(df: pd.DataFrame, window: int = 14) -> float:
    """ATR (Wilder) de la última vela cerrada. Implementación manual."""
    if len(df) < window + 2:
        return 0.0
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    # EMA de Wilder ≈ ewm(alpha=1/window)
    atr = tr.ewm(alpha=1.0 / window, adjust=False).mean()
    last = float(atr.iloc[-1])
    return last if pd.notna(last) else 0.0


# ─────────────────────────────────────────
#  Engine forex
# ─────────────────────────────────────────

class PriceActionEngineFX:
    """
    Mismo decisor que el de cripto, dimensionado para forex.

    Parámetros (con defaults; overridables vía settings con prefijo pafx_*):
      - fractal_n
      - vol_mult       confirmación de tick-volume del sweep
      - atr_sl_mult    SL = mecha_sweep ± atr_sl_mult × ATR(htf-trigger)
      - tp_rr          TP en múltiplo del riesgo
      - sl_min_pips    SL mínimo en pips (filtro de ruido)
      - sl_max_pips    SL máximo en pips (filtro de sweeps demasiado profundos)
      - pip_size       0.0001 para EUR/USD
    """

    def __init__(self, settings):
        g = lambda k, d: getattr(settings, k, d)
        self.fractal_n: int = int(g("pafx_fractal_n", 3))
        self.vol_mult: float = float(g("pafx_vol_mult", 1.5))
        self.atr_sl_mult: float = float(g("pafx_atr_sl_mult", 1.5))
        self.tp_rr: float = float(g("pafx_tp_rr", 2.5))
        self.sl_min_pips: float = float(g("pafx_sl_min_pips", 8.0))
        self.sl_max_pips: float = float(g("pafx_sl_max_pips", 60.0))
        self.pip_size: float = float(g("pafx_pip_size", PIP))
        self.atr_window: int = int(g("pafx_atr_window", 14))

    def analyze(self, df_trig: pd.DataFrame, df_htf: pd.DataFrame) -> FxDecision:
        """df_trig = TF de gatillo (ej. 1h), df_htf = TF de contexto (ej. 4h).
        Ambos terminan en la última vela CERRADA."""
        if len(df_trig) < 30 or len(df_htf) < 30:
            return self._wait("data insuficiente")

        swings_htf = find_swings(df_htf, n=self.fractal_n)
        structure = determine_structure(swings_htf)
        if structure == "RANGE":
            return self._wait("HTF en rango")

        swings_trig = find_swings(df_trig, n=self.fractal_n)
        if len(swings_trig) < 2:
            return self._wait("pocos swings en trigger TF")

        sweep = detect_liquidity_sweep(df_trig, swings_trig, structure,
                                       vol_mult=self.vol_mult)
        if sweep is None:
            return self._wait(f"sin sweep (HTF={structure})")

        atr = compute_atr(df_trig, window=self.atr_window)
        if atr <= 0:
            return self._wait("ATR no disponible")

        entry = float(df_trig.iloc[-1]["close"])

        if sweep.direction == "LONG":
            sl = sweep.sweep_extreme - self.atr_sl_mult * atr
            risk = entry - sl
            if risk <= 0:
                return self._wait("riesgo no positivo")
            tp = entry + self.tp_rr * risk
        else:
            sl = sweep.sweep_extreme + self.atr_sl_mult * atr
            risk = sl - entry
            if risk <= 0:
                return self._wait("riesgo no positivo")
            tp = entry - self.tp_rr * risk

        sl_pips = abs(entry - sl) / self.pip_size
        if sl_pips < self.sl_min_pips:
            return self._wait(f"SL muy apretado ({sl_pips:.1f} pips)")
        if sl_pips > self.sl_max_pips:
            return self._wait(f"SL excesivo ({sl_pips:.1f} pips)")
        tp_pips = abs(tp - entry) / self.pip_size

        accion = "COMPRAR" if sweep.direction == "LONG" else "VENDER"
        confianza = min(0.95, 0.7 + 0.1 * (sweep.candle_volume_ratio - self.vol_mult))
        razon = (f"Sweep {sweep.direction} | HTF={structure} | "
                 f"swept {sweep.swept_level:.5f}→close {sweep.candle_close:.5f} | "
                 f"vol {sweep.candle_volume_ratio:.2f}x | SL {sl_pips:.1f}p R:R 1:{self.tp_rr:.1f}")

        return FxDecision(
            accion=accion,
            direction=sweep.direction,
            confianza=round(confianza, 3),
            razon=razon,
            entry_price=entry,
            stop_loss_price=sl,
            take_profit_price=tp,
            sl_pips=round(sl_pips, 1),
            tp_pips=round(tp_pips, 1),
        )

    def _wait(self, motivo: str) -> FxDecision:
        return FxDecision("ESPERAR", "LONG", 0.2, motivo)
