"""
core/scalping_engine.py
Motor de scalping de alta frecuencia para TF 5m.

Estrategia: Bollinger Band Squeeze + Volatility Expansion + Volume Spike.

Lógica:
1. Calculamos BB(20, 2) y trackeamos su width.
2. Detectamos "squeeze": BB width en el percentil bajo (p20) de las últimas N velas
   → compresión = energía acumulada por liberar.
3. Detectamos "expansion": una vela rompe la banda (close > upper o close < lower)
   tras un squeeze reciente.
4. Confirmación de volumen institucional: vol > 2 × MA(volume, 20).
5. ATR ajusta SL/TP:
   - SL = 1.0 × ATR (tight, para scalping)
   - TP = 1.5 × ATR (R:R 1.5)

Diseñado para Futures USDT-M con leverage 10x. Las salidas son agresivas
y cortas para que la ganancia cubra el spread + fees rápidamente.
"""

from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd
import ta

from logs.logger import logger
from config.settings import Settings


Direction = Literal["LONG", "SHORT"]


@dataclass
class ScalpDecision:
    accion: Literal["COMPRAR", "VENDER", "ESPERAR"]
    direction: Direction
    confianza: float
    razon: str
    entry_price: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    advertencias: list[str] = field(default_factory=list)


# ─────────────────────────────────────────
#  Cálculos
# ─────────────────────────────────────────

def _bb_width(df: pd.DataFrame, window: int = 20, dev: int = 2) -> pd.Series:
    bb = ta.volatility.BollingerBands(close=df["close"], window=window, window_dev=dev)
    upper = bb.bollinger_hband()
    lower = bb.bollinger_lband()
    middle = bb.bollinger_mavg()
    return (upper - lower) / middle


def _bb_components(df: pd.DataFrame, window: int = 20, dev: int = 2):
    bb = ta.volatility.BollingerBands(close=df["close"], window=window, window_dev=dev)
    return bb.bollinger_hband(), bb.bollinger_mavg(), bb.bollinger_lband()


def _atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    return ta.volatility.AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=window,
    ).average_true_range()


# ─────────────────────────────────────────
#  Engine
# ─────────────────────────────────────────

class ScalpingEngine:
    """
    Decisor para scalping. Recibe el df 5m y devuelve una decisión sobre la
    última vela cerrada.

    Parámetros (con defaults sensatos; overridables vía settings):
      - bb_window (20), bb_dev (2)
      - squeeze_lookback (100) → ventana donde se evalúa el percentil del BB width
      - squeeze_percentile (20) → debajo de este percentil = squeeze
      - vol_spike_mult (2.0) → volumen mínimo de la vela de breakout
      - atr_window (14)
      - sl_atr_mult (1.0)
      - tp_atr_mult (1.5)
      - sl_min_pct (0.001) / sl_max_pct (0.02) → bounds del SL en % del entry
      - cooldown_bars (3) → mínimo de velas entre cierre y reentrada (anti-ruido)
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.bb_window: int = getattr(settings, "scalp_bb_window", 20)
        self.bb_dev: int = getattr(settings, "scalp_bb_dev", 2)
        self.squeeze_lookback: int = getattr(settings, "scalp_squeeze_lookback", 100)
        self.squeeze_percentile: float = getattr(settings, "scalp_squeeze_pct", 20.0)
        self.vol_spike_mult: float = getattr(settings, "scalp_vol_spike", 2.0)
        self.atr_window: int = getattr(settings, "scalp_atr_window", 14)
        self.sl_atr_mult: float = getattr(settings, "scalp_sl_atr_mult", 1.0)
        self.tp_atr_mult: float = getattr(settings, "scalp_tp_atr_mult", 1.5)
        self.sl_min_pct: float = getattr(settings, "scalp_sl_min_pct", 0.001)
        self.sl_max_pct: float = getattr(settings, "scalp_sl_max_pct", 0.02)
        self.cooldown_bars: int = getattr(settings, "scalp_cooldown_bars", 3)
        self._last_close_idx: int = -10**9
        logger.info(
            "ScalpingEngine inicializado (BB squeeze + expansion + volume spike, TF base)"
        )

    @property
    def is_safe_mode(self) -> bool:
        return False

    def reset_circuit_breaker(self) -> None:
        pass

    def mark_close(self, idx: int) -> None:
        """Para el cooldown: indicar el índice de la última vela donde cerramos."""
        self._last_close_idx = idx

    def analyze(self, df: pd.DataFrame, current_idx: int) -> ScalpDecision:
        """
        df: DataFrame OHLCV completo (5m), con índice temporal.
        current_idx: índice de la última vela cerrada que estamos evaluando.

        Devuelve una decisión sobre si abrir o esperar.
        """
        # Anti-ruido: cooldown post-cierre
        if (current_idx - self._last_close_idx) < self.cooldown_bars:
            return self._wait(f"cooldown ({self.cooldown_bars} velas)")

        # Datos suficientes
        warm = max(self.squeeze_lookback, self.bb_window + 5, self.atr_window + 5)
        if current_idx < warm:
            return self._wait("warm-up incompleto")

        # Subset hasta la vela actual (inclusive)
        sub = df.iloc[: current_idx + 1]
        if len(sub) < warm:
            return self._wait("data insuficiente")

        # BB components
        upper, middle, lower = _bb_components(sub, self.bb_window, self.bb_dev)
        bbw = (upper - lower) / middle

        # Squeeze: BB width actual debajo del percentil-N de las últimas squeeze_lookback velas
        recent_widths = bbw.iloc[-self.squeeze_lookback:].dropna()
        if len(recent_widths) < self.squeeze_lookback // 2:
            return self._wait("widths insuficientes")
        squeeze_threshold = float(recent_widths.quantile(self.squeeze_percentile / 100.0))

        # Volumen
        vol_ma = sub["volume"].iloc[-(self.bb_window + 1):-1].mean()
        if vol_ma <= 0:
            return self._wait("volumen MA inválido")

        # ATR
        atr_series = _atr(sub, self.atr_window)
        atr_now = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else 0.0
        if atr_now <= 0:
            return self._wait("ATR no disponible")

        last = sub.iloc[-1]
        prev = sub.iloc[-2]
        close = float(last["close"])
        prev_close = float(prev["close"])

        # Buscar squeeze EN LAS VELAS PREVIAS (no en la vela de expansión).
        # Si las últimas 10 velas estuvieron en squeeze, la 11° (actual) podría ser la expansión.
        prev_bbw = bbw.iloc[-11:-1]
        was_in_squeeze = bool((prev_bbw <= squeeze_threshold).any())
        if not was_in_squeeze:
            return self._wait(f"sin squeeze previo (umbral {squeeze_threshold:.4f})")

        upper_now = float(upper.iloc[-1])
        lower_now = float(lower.iloc[-1])
        vol_now = float(last["volume"])
        vol_ratio = vol_now / vol_ma

        # Volumen mínimo
        if vol_ratio < self.vol_spike_mult:
            return self._wait(
                f"vol {vol_ratio:.2f}x < {self.vol_spike_mult:.1f}x (sin spike)"
            )

        # Detectar BREAKOUT (close por encima/debajo de banda)
        # Requerimos que la vela actual ROMPA y que la anterior NO lo hubiera roto aún
        breakout_up = close > upper_now and prev_close <= float(upper.iloc[-2])
        breakout_down = close < lower_now and prev_close >= float(lower.iloc[-2])

        if breakout_up:
            sl_price = close - self.sl_atr_mult * atr_now
            tp_price = close + self.tp_atr_mult * atr_now
            direction: Direction = "LONG"
            accion = "COMPRAR"
        elif breakout_down:
            sl_price = close + self.sl_atr_mult * atr_now
            tp_price = close - self.tp_atr_mult * atr_now
            direction = "SHORT"
            accion = "VENDER"
        else:
            return self._wait("sin breakout limpio de BB")

        # Bounds del SL en %
        sl_pct = abs(close - sl_price) / close
        if sl_pct < self.sl_min_pct:
            return self._wait(f"SL muy apretado ({sl_pct:.3%})")
        if sl_pct > self.sl_max_pct:
            return self._wait(f"SL excesivo ({sl_pct:.3%}) — vela demasiado volátil")

        tp_pct = abs(tp_price - close) / close

        # ─── FILTRO DE MARGEN MÍNIMO OPERATIVO ───
        # Si el costo de la comisión round-trip (entrada + salida) representa
        # más del max_fee_to_gain_ratio del TP esperado, NO operamos. La
        # operación nace "comida por fees" y no compensa.
        #
        # Fees notional:  fee_in + fee_out = 2 * commission_pct_per_side * notional
        # Ganancia bruta esperada: tp_pct * notional
        # Ratio: 2 * fee / tp_pct
        max_ratio = getattr(self.settings, "scalp_max_fee_to_gain", 0.20)
        fee_per_side = getattr(self.settings, "commission_pct_per_side", 0.0002)
        roundtrip_fee_pct = 2 * fee_per_side
        if tp_pct <= 0:
            return self._wait("TP inválido")
        fee_to_gain = roundtrip_fee_pct / tp_pct
        if fee_to_gain > max_ratio:
            return self._wait(
                f"TP demasiado corto: fees serían {fee_to_gain:.1%} del bruto "
                f"(límite {max_ratio:.0%})"
            )

        razon = (
            f"BB squeeze→expansion {direction} | "
            f"vol {vol_ratio:.2f}x | "
            f"ATR {atr_now/close*100:.2f}% | "
            f"R:R 1:{self.tp_atr_mult/self.sl_atr_mult:.1f}"
        )
        # Confianza: 0.6 base, sube con volumen
        confianza = min(0.95, 0.6 + 0.05 * (vol_ratio - self.vol_spike_mult))
        logger.info(f"⚡ Scalp Engine → {accion} {direction} | {razon}")

        return ScalpDecision(
            accion=accion,
            direction=direction,
            confianza=round(confianza, 3),
            razon=razon,
            entry_price=round(close, 2),
            stop_loss_price=round(sl_price, 2),
            take_profit_price=round(tp_price, 2),
            stop_loss_pct=round(sl_pct, 5),
            take_profit_pct=round(tp_pct, 5),
            advertencias=["Scalping TF base — alta frecuencia"],
        )

    def _wait(self, motivo: str) -> ScalpDecision:
        return ScalpDecision(
            accion="ESPERAR",
            direction="LONG",
            confianza=0.0,
            razon=motivo,
            advertencias=[],
        )
