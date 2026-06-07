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


def _parse_hours(spec: str) -> set[int]:
    """Acepta '6-11' o '6,7,8,9,10,11' o '' (vacío = no skip)."""
    spec = (spec or "").strip()
    if not spec:
        return set()
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif chunk:
            out.add(int(chunk))
    return {h for h in out if 0 <= h <= 23}


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
        # Skip hours UTC (formato: "6-11" o "6,7,8,9,10,11"). Validado en
        # aud F: cortar 06-12 UTC multiplica el retorno 10× en backtest 60d.
        self.skip_hours: set[int] = _parse_hours(
            getattr(settings, "scalp_skip_hours_utc", "")
        )
        self._last_close_idx: int = -10**9
        # Cache de indicadores: evita recalcular BB y ATR sobre el subset en
        # cada vela. Se invalida cuando cambia el df (id() distinto).
        self._cache_df_id: int = -1
        self._cache_upper: Optional[pd.Series] = None
        self._cache_middle: Optional[pd.Series] = None
        self._cache_lower: Optional[pd.Series] = None
        self._cache_bbw: Optional[pd.Series] = None
        self._cache_atr: Optional[pd.Series] = None
        self._cache_vol_ma: Optional[pd.Series] = None
        logger.info(
            "ScalpingEngine inicializado (BB squeeze + expansion + volume spike, TF base)"
        )

    def _ensure_cache(self, df: pd.DataFrame) -> None:
        """Pre-calcula BB y ATR sobre el df completo si no están en cache."""
        if id(df) == self._cache_df_id and self._cache_upper is not None:
            return
        bb = ta.volatility.BollingerBands(close=df["close"],
                                           window=self.bb_window,
                                           window_dev=self.bb_dev)
        self._cache_upper = bb.bollinger_hband()
        self._cache_middle = bb.bollinger_mavg()
        self._cache_lower = bb.bollinger_lband()
        self._cache_bbw = (self._cache_upper - self._cache_lower) / self._cache_middle
        self._cache_atr = ta.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"],
            window=self.atr_window,
        ).average_true_range()
        self._cache_vol_ma = df["volume"].rolling(self.bb_window).mean().shift(1)
        self._cache_df_id = id(df)

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

        # Skip de horario (filtro validado en aud F: cortar 06-12 UTC mejora 10×)
        if self.skip_hours:
            hour_utc = df.index[current_idx].hour
            if hour_utc in self.skip_hours:
                return self._wait(f"hora {hour_utc:02d}h UTC en skip_hours")

        # Datos suficientes
        warm = max(self.squeeze_lookback, self.bb_window + 5, self.atr_window + 5)
        if current_idx < warm:
            return self._wait("warm-up incompleto")

        # Pre-cálculo cacheado de indicadores (evita O(n²) en backtests largos).
        self._ensure_cache(df)
        upper = self._cache_upper
        middle = self._cache_middle
        lower = self._cache_lower
        bbw = self._cache_bbw
        atr_series = self._cache_atr
        vol_ma_series = self._cache_vol_ma

        # Squeeze: BB width actual debajo del percentil-N de las últimas squeeze_lookback velas
        recent_widths = bbw.iloc[max(0, current_idx - self.squeeze_lookback + 1): current_idx + 1].dropna()
        if len(recent_widths) < self.squeeze_lookback // 2:
            return self._wait("widths insuficientes")
        squeeze_threshold = float(recent_widths.quantile(self.squeeze_percentile / 100.0))

        # Volumen (MA shift(1) → excluye la vela actual del MA)
        vol_ma = float(vol_ma_series.iloc[current_idx]) if pd.notna(vol_ma_series.iloc[current_idx]) else 0.0
        if vol_ma <= 0:
            return self._wait("volumen MA inválido")

        # ATR
        atr_now = float(atr_series.iloc[current_idx]) if pd.notna(atr_series.iloc[current_idx]) else 0.0
        if atr_now <= 0:
            return self._wait("ATR no disponible")

        last = df.iloc[current_idx]
        prev = df.iloc[current_idx - 1]
        close = float(last["close"])
        prev_close = float(prev["close"])

        # Buscar squeeze EN LAS VELAS PREVIAS (no en la vela de expansión).
        # Las 10 velas anteriores a la actual.
        prev_bbw = bbw.iloc[max(0, current_idx - 10): current_idx]
        was_in_squeeze = bool((prev_bbw <= squeeze_threshold).any())
        if not was_in_squeeze:
            return self._wait(f"sin squeeze previo (umbral {squeeze_threshold:.4f})")

        upper_now = float(upper.iloc[current_idx])
        lower_now = float(lower.iloc[current_idx])
        vol_now = float(last["volume"])
        vol_ratio = vol_now / vol_ma

        # Volumen mínimo
        if vol_ratio < self.vol_spike_mult:
            return self._wait(
                f"vol {vol_ratio:.2f}x < {self.vol_spike_mult:.1f}x (sin spike)"
            )

        # Detectar BREAKOUT (close por encima/debajo de banda)
        # Requerimos que la vela actual ROMPA y que la anterior NO lo hubiera roto aún
        breakout_up = close > upper_now and prev_close <= float(upper.iloc[current_idx - 1])
        breakout_down = close < lower_now and prev_close >= float(lower.iloc[current_idx - 1])

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
