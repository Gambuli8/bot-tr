"""
core/price_action_engine.py
Motor de decisión basado en estructura de mercado + liquidity sweeps.
Diseñado para operar en TF 1h con contexto macro en 4h.

Premisas:
- NO usamos osciladores rezagados (MACD, RSI, BB) como gatillos.
- ATR sólo se usa para dimensionar el SL.
- Sólo operamos a favor de la estructura del 4h.
- El gatillo es una "trampa" de liquidez en 1h:
    LONG: precio rompe un swing low previo (caza stops) y la vela cierra por encima.
    SHORT: lo opuesto.
- Confirmación de volumen institucional: vela del sweep > 1.5x media móvil(20).
"""

from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd
import ta

from logs.logger import logger
from config.settings import Settings


# ─────────────────────────────────────────
#  Tipos
# ─────────────────────────────────────────

Direction = Literal["LONG", "SHORT"]
Structure = Literal["BULL", "BEAR", "RANGE"]


@dataclass
class Swing:
    ts: pd.Timestamp
    idx: int
    kind: Literal["high", "low"]
    price: float


@dataclass
class PriceActionDecision:
    """Decisión del PriceActionEngine. Compatible con TechnicalDecision pero
    expone SL/TP en precio absoluto (no en pct), porque los calculamos con
    el ATR y la mecha del sweep — no con porcentajes del entry."""
    accion: Literal["COMPRAR", "VENDER", "ESPERAR"]
    direction: Direction
    confianza: float
    razon: str
    entry_price: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    # Para compatibilidad con TechnicalDecision en código viejo:
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    advertencias: list[str] = field(default_factory=list)


# ─────────────────────────────────────────
#  Detección matemática de swings
# ─────────────────────────────────────────

def find_swings(df: pd.DataFrame, n: int = 3) -> list[Swing]:
    """
    Fractal de Williams: un swing high es el máximo de su ventana ±n velas.
    Vectorizado con rolling().
    """
    if len(df) < 2 * n + 1:
        return []

    h = df["high"]
    l = df["low"]

    # Para cada vela i, comparar contra ventanas izquierda y derecha.
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
    """
    BULL si HH + HL (últimos 2 highs ↑ y últimos 2 lows ↑).
    BEAR si LH + LL.
    RANGE en cualquier otro caso.
    """
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
#  Liquidity Sweep detector
# ─────────────────────────────────────────

@dataclass
class SweepSignal:
    direction: Direction
    sweep_extreme: float        # mecha extrema del barrido (low o high)
    swept_level: float          # nivel del swing previo
    candle_close: float
    candle_volume_ratio: float


def detect_liquidity_sweep(
    df_1h_so_far: pd.DataFrame,
    swings_1h: list[Swing],
    structure_4h: Structure,
    vol_mult: float = 1.5,
    vol_window: int = 20,
) -> Optional[SweepSignal]:
    """
    Examina la ÚLTIMA vela cerrada de df_1h_so_far. Sólo dispara señal a favor
    de la estructura macro (4h).

    Para LONG (estructura BULL):
        - Buscar el último swing low *confirmado* antes de la vela actual.
        - La vela hizo low < swing_low y close > swing_low.
        - vol(vela) > vol_mult × MA(volumen, vol_window).

    Para SHORT (estructura BEAR): simétrico.
    """
    if structure_4h not in ("BULL", "BEAR"):
        return None
    if len(df_1h_so_far) < vol_window + 5:
        return None

    last = df_1h_so_far.iloc[-1]
    last_ts = df_1h_so_far.index[-1]

    # Confirmación de volumen (excluye la vela actual del cálculo de la media)
    vol_ma = df_1h_so_far["volume"].iloc[-(vol_window + 1):-1].mean()
    if vol_ma <= 0:
        return None
    vol_ratio = float(last["volume"]) / float(vol_ma)
    if vol_ratio < vol_mult:
        return None

    if structure_4h == "BULL":
        # Último swing low confirmado *antes* de la vela actual
        prev_swings = [s for s in swings_1h if s.kind == "low" and s.ts < last_ts]
        if not prev_swings:
            return None
        last_swing_low = prev_swings[-1].price
        if float(last["low"]) < last_swing_low and float(last["close"]) > last_swing_low:
            return SweepSignal(
                direction="LONG",
                sweep_extreme=float(last["low"]),
                swept_level=last_swing_low,
                candle_close=float(last["close"]),
                candle_volume_ratio=vol_ratio,
            )
        return None

    # BEAR
    prev_swings = [s for s in swings_1h if s.kind == "high" and s.ts < last_ts]
    if not prev_swings:
        return None
    last_swing_high = prev_swings[-1].price
    if float(last["high"]) > last_swing_high and float(last["close"]) < last_swing_high:
        return SweepSignal(
            direction="SHORT",
            sweep_extreme=float(last["high"]),
            swept_level=last_swing_high,
            candle_close=float(last["close"]),
            candle_volume_ratio=vol_ratio,
        )
    return None


# ─────────────────────────────────────────
#  ATR helper (1h)
# ─────────────────────────────────────────

def compute_atr(df: pd.DataFrame, window: int = 14) -> float:
    """ATR de la última vela cerrada del df."""
    if len(df) < window + 2:
        return 0.0
    atr_series = ta.volatility.AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=window,
    ).average_true_range()
    last = float(atr_series.iloc[-1])
    return last if pd.notna(last) else 0.0


# ─────────────────────────────────────────
#  Engine principal
# ─────────────────────────────────────────

class PriceActionEngine:
    """
    Decisor basado en estructura + liquidity sweep.

    Parámetros calibrables (vienen de settings con defaults):
      - fractal_n: ventana del fractal Williams (3 = clásico)
      - vol_mult: confirmación de volumen institucional (1.5 = razonable)
      - atr_sl_mult: SL = sweep_extreme ± atr_sl_mult × ATR (1.5 = pedido)
      - tp_rr: TP = entry ± tp_rr × (entry - SL) (2.5 = pedido)
      - sl_min_pct, sl_max_pct: bounds de SL en % del entry para evitar ruido/extremos
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        # Defaults (overridables via settings si querés exponerlos al .env)
        self.fractal_n: int = getattr(settings, "pa_fractal_n", 3)
        self.vol_mult: float = getattr(settings, "pa_vol_mult", 1.5)
        self.atr_sl_mult: float = getattr(settings, "pa_atr_sl_mult", 1.5)
        self.tp_rr: float = getattr(settings, "pa_tp_rr", 2.5)
        self.sl_min_pct: float = getattr(settings, "pa_sl_min_pct", 0.003)
        self.sl_max_pct: float = getattr(settings, "pa_sl_max_pct", 0.05)
        logger.info("PriceActionEngine inicializado (1h trigger + 4h structure)")

    @property
    def is_safe_mode(self) -> bool:
        return False

    def reset_circuit_breaker(self) -> None:
        pass

    def analyze(
        self,
        df_1h: pd.DataFrame,
        df_4h: pd.DataFrame,
    ) -> PriceActionDecision:
        """
        Pipeline completo. Llamar al cierre de cada vela 1h.
        df_1h y df_4h deben terminar con la última vela cerrada (no la en formación).
        """
        if len(df_1h) < 30 or len(df_4h) < 30:
            return self._wait("data insuficiente")

        # 1) Estructura macro en 4h
        swings_4h = find_swings(df_4h, n=self.fractal_n)
        structure_4h = determine_structure(swings_4h)
        if structure_4h == "RANGE":
            return self._wait("4h en rango (sin estructura clara)")

        # 2) Swings en 1h (para encontrar el nivel barrido)
        swings_1h = find_swings(df_1h, n=self.fractal_n)
        if len(swings_1h) < 2:
            return self._wait("pocos swings en 1h")

        # 3) Detección de liquidity sweep en la última vela cerrada de 1h
        sweep = detect_liquidity_sweep(
            df_1h, swings_1h, structure_4h, vol_mult=self.vol_mult,
        )
        if sweep is None:
            return self._wait(f"sin sweep (estructura 4h: {structure_4h})")

        # 4) Sizing del SL/TP con ATR de 1h
        atr_1h = compute_atr(df_1h, window=14)
        if atr_1h <= 0:
            return self._wait("ATR no disponible")

        entry = float(df_1h.iloc[-1]["close"])

        if sweep.direction == "LONG":
            sl_price = sweep.sweep_extreme - self.atr_sl_mult * atr_1h
            risk = entry - sl_price
            if risk <= 0:
                return self._wait("riesgo no positivo (ATR muy chico)")
            tp_price = entry + self.tp_rr * risk
        else:  # SHORT
            sl_price = sweep.sweep_extreme + self.atr_sl_mult * atr_1h
            risk = sl_price - entry
            if risk <= 0:
                return self._wait("riesgo no positivo (ATR muy chico)")
            tp_price = entry - self.tp_rr * risk

        # 5) Sanity checks sobre el SL en % del entry
        sl_pct = abs(entry - sl_price) / entry
        if sl_pct < self.sl_min_pct:
            return self._wait(f"SL muy apretado ({sl_pct:.2%}) — riesgo de ruido")
        if sl_pct > self.sl_max_pct:
            return self._wait(f"SL excesivo ({sl_pct:.2%}) — sweep demasiado profundo")

        tp_pct = abs(tp_price - entry) / entry

        razon = (
            f"Liquidity sweep en {sweep.direction} | "
            f"4h={structure_4h} | "
            f"swept ${sweep.swept_level:,.2f} → close ${sweep.candle_close:,.2f} | "
            f"vol {sweep.candle_volume_ratio:.2f}x | "
            f"R:R 1:{self.tp_rr:.1f}"
        )

        accion = "COMPRAR" if sweep.direction == "LONG" else "VENDER"
        # Confianza: arrancamos en 0.7, sumamos por volumen excesivo
        confianza = min(0.95, 0.7 + 0.1 * (sweep.candle_volume_ratio - self.vol_mult))

        logger.info(f"🎯 PA Engine → {accion} {sweep.direction} | {razon}")

        return PriceActionDecision(
            accion=accion,
            direction=sweep.direction,
            confianza=round(confianza, 3),
            razon=razon,
            entry_price=round(entry, 2),
            stop_loss_price=round(sl_price, 2),
            take_profit_price=round(tp_price, 2),
            stop_loss_pct=round(sl_pct, 4),
            take_profit_pct=round(tp_pct, 4),
            advertencias=["Motor Price Action — TF 1h, contexto 4h"],
        )

    def _wait(self, motivo: str) -> PriceActionDecision:
        return PriceActionDecision(
            accion="ESPERAR",
            direction="LONG",
            confianza=0.2,
            razon=motivo,
            advertencias=[],
        )
