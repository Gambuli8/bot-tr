"""
core/donchian_engine.py
Motor de tendencia: Breakout de Canales de Donchian en TF 15m.

Estrategia (baseline validado + capa de régimen):
1. Canal Donchian(N=20) calculado sobre las N velas PREVIAS (shift(1) →
   cero lookahead: la vela actual nunca participa de su propio canal).
2. Entrada LONG : close cruza por ENCIMA del canal superior previo.
   Entrada SHORT: close cruza por DEBAJO del canal inferior previo.
   "Cruza" = la vela anterior todavía no había roto (breakout fresco,
   evita re-disparar la señal en cada vela de un mismo impulso).
3. Confirmación de volumen: volume >= dc_vol_ratio_min × MA(volume, 20)
   (la MA excluye la vela actual vía shift(1)).
4. Filtro de régimen (Capa 2): ADX(14) >= dc_adx_min (default 20).
   Mercado lateral = sin trades.
5. Salidas:
   - SL inicial: banda opuesta del canal ("channel"), 1.5 × ATR ("atr"),
     o el MÁS CERCANO de ambos ("tighter", default — capea el riesgo y
     garantiza que el R:R mínimo sea alcanzable).
   - TP dinámico: dc_tp_atr_mult × ATR (default 3.0 → R:R 1:2 vs 1.5 ATR).
   - Guard duro: si TP < min_risk_reward × SL, la señal se descarta.

Interfaz idéntica a ScalpingEngine: analyze(df, current_idx) sobre el df
OHLCV completo — MainStrategy la despacha igual y el backtest la reusa
sin tocar nada.
"""

from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd
import ta

from logs.logger import logger
from config.settings import Settings


Direction = Literal["LONG", "SHORT"]
SLMode = Literal["channel", "atr", "tighter"]


@dataclass
class DonchianDecision:
    """Misma superficie que ScalpDecision/TradeDecision — compatible con OrderManager."""
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


class DonchianEngine:
    """
    Decisor Donchian breakout. Recibe el df OHLCV completo (15m) y evalúa
    la última vela cerrada. Todos los indicadores se pre-calculan una sola
    vez por df (cache por id()) → O(1) por vela en backtests largos.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.period: int = int(getattr(settings, "dc_period", 20))
        self.vol_ratio_min: float = float(getattr(settings, "dc_vol_ratio_min", 1.0))
        self.adx_min: float = float(getattr(settings, "dc_adx_min", 20.0))
        self.adx_period: int = int(getattr(settings, "dc_adx_period", 14))
        self.atr_period: int = int(getattr(settings, "dc_atr_period", 14))
        self.sl_atr_mult: float = float(getattr(settings, "dc_sl_atr_mult", 1.5))
        self.tp_atr_mult: float = float(getattr(settings, "dc_tp_atr_mult", 3.0))
        self.sl_mode: SLMode = str(getattr(settings, "dc_sl_mode", "tighter")).lower()  # type: ignore[assignment]
        self.min_rr: float = float(getattr(settings, "min_risk_reward", 2.0))

        if self.sl_mode not in ("channel", "atr", "tighter"):
            logger.warning(f"dc_sl_mode inválido ({self.sl_mode}); uso 'tighter'")
            self.sl_mode = "tighter"

        # Cache de indicadores (invalidado cuando cambia el df).
        self._cache_df_id: int = -1
        self._upper: Optional[pd.Series] = None       # Donchian high de las N velas previas
        self._lower: Optional[pd.Series] = None       # Donchian low de las N velas previas
        self._atr: Optional[pd.Series] = None
        self._adx: Optional[pd.Series] = None
        self._vol_ma: Optional[pd.Series] = None      # MA(volume, 20) excluyendo la vela actual

        logger.info(
            f"DonchianEngine inicializado | N={self.period} | ADX>={self.adx_min:.0f} | "
            f"vol>={self.vol_ratio_min:.1f}x | SL={self.sl_mode} "
            f"({self.sl_atr_mult}×ATR) | TP={self.tp_atr_mult}×ATR"
        )

    # ── compat con la interfaz de ClaudeAgent/ScalpingEngine ──

    @property
    def is_safe_mode(self) -> bool:
        return False

    def reset_circuit_breaker(self) -> None:
        pass

    # ── indicadores ──

    def _ensure_cache(self, df: pd.DataFrame) -> None:
        if id(df) == self._cache_df_id and self._upper is not None:
            return
        # shift(1): el canal de la vela i es el max/min de las N velas ANTERIORES.
        self._upper = df["high"].rolling(self.period).max().shift(1)
        self._lower = df["low"].rolling(self.period).min().shift(1)
        self._atr = ta.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=self.atr_period,
        ).average_true_range()
        self._adx = ta.trend.ADXIndicator(
            high=df["high"], low=df["low"], close=df["close"], window=self.adx_period,
        ).adx()
        self._vol_ma = df["volume"].rolling(20).mean().shift(1)
        self._cache_df_id = id(df)

    def warmup_bars(self) -> int:
        """Velas mínimas para que canal, ATR, ADX y MA de volumen sean válidos."""
        return max(self.period + 2, self.adx_period * 2 + 5, self.atr_period + 5, 22)

    # ── decisión ──

    def analyze(self, df: pd.DataFrame, current_idx: int) -> DonchianDecision:
        """
        df: DataFrame OHLCV completo con índice temporal.
        current_idx: índice posicional de la última vela CERRADA a evaluar.
        """
        if current_idx < self.warmup_bars():
            return self._wait("warm-up incompleto")

        self._ensure_cache(df)
        assert self._upper is not None  # para el type-checker

        upper_now = self._value_at(self._upper, current_idx)
        lower_now = self._value_at(self._lower, current_idx)
        upper_prev = self._value_at(self._upper, current_idx - 1)
        lower_prev = self._value_at(self._lower, current_idx - 1)
        atr_now = self._value_at(self._atr, current_idx)
        adx_now = self._value_at(self._adx, current_idx)
        vol_ma = self._value_at(self._vol_ma, current_idx)

        if None in (upper_now, lower_now, upper_prev, lower_prev) or not atr_now or atr_now <= 0:
            return self._wait("indicadores no disponibles (NaN)")
        if vol_ma is None or vol_ma <= 0:
            return self._wait("MA de volumen inválida")

        bar = df.iloc[current_idx]
        prev = df.iloc[current_idx - 1]
        close = float(bar["close"])
        prev_close = float(prev["close"])
        volume = float(bar["volume"])

        # Capa 2 — régimen: sin fuerza de tendencia no se opera.
        if adx_now is None or adx_now < self.adx_min:
            return self._wait(
                f"ADX {adx_now or 0:.1f} < {self.adx_min:.0f} (mercado lateral)"
            )

        # Breakout FRESCO: rompe ahora y la vela anterior no había roto su canal.
        breakout_up = close > upper_now and prev_close <= upper_prev
        breakout_down = close < lower_now and prev_close >= lower_prev
        if not breakout_up and not breakout_down:
            return self._wait(
                f"sin breakout (close {close:,.2f} ∈ [{lower_now:,.2f}, {upper_now:,.2f}])"
            )

        # Confirmación de volumen.
        vol_ratio = volume / vol_ma
        if vol_ratio < self.vol_ratio_min:
            return self._wait(
                f"vol {vol_ratio:.2f}x < {self.vol_ratio_min:.1f}x (breakout sin participación)"
            )

        direction: Direction = "LONG" if breakout_up else "SHORT"
        sl_price = self._initial_stop(direction, close, upper_now, lower_now, atr_now)
        sl_pct = abs(close - sl_price) / close
        tp_pct = (self.tp_atr_mult * atr_now) / close
        tp_price = close * (1 + tp_pct) if direction == "LONG" else close * (1 - tp_pct)

        if sl_pct <= 0:
            return self._wait("SL degenerado (distancia 0)")

        # Guard de asimetría: el trade nace con R:R >= min_rr o no nace.
        # Epsilon: con SL=1.5×ATR y TP=3×ATR el cociente puede dar 1.999...
        rr = tp_pct / sl_pct
        if rr < self.min_rr - 1e-9:
            return self._wait(
                f"R:R 1:{rr:.2f} < 1:{self.min_rr:.1f} (SL {self.sl_mode} demasiado lejos)"
            )

        accion = "COMPRAR" if direction == "LONG" else "VENDER"
        band = upper_now if direction == "LONG" else lower_now
        razon = (
            f"[Donchian {self.period}] breakout {direction} de ${band:,.2f} | "
            f"vol {vol_ratio:.2f}x | ADX {adx_now:.1f} | R:R 1:{rr:.1f}"
        )
        # Confianza: 0.70 base; premia participación de volumen y fuerza de régimen.
        confianza = min(0.90, 0.70 + 0.05 * (vol_ratio - self.vol_ratio_min)
                        + 0.05 * max(0.0, (adx_now - self.adx_min) / 20.0))
        logger.info(f"📐 Donchian Engine → {accion} {direction} | {razon}")

        return DonchianDecision(
            accion=accion,
            direction=direction,
            confianza=round(confianza, 3),
            razon=razon,
            entry_price=round(close, 2),
            stop_loss_price=round(sl_price, 2),
            take_profit_price=round(tp_price, 2),
            stop_loss_pct=round(sl_pct, 5),
            take_profit_pct=round(tp_pct, 5),
            advertencias=[],
        )

    def _initial_stop(
        self, direction: Direction, close: float,
        upper: float, lower: float, atr: float,
    ) -> float:
        """SL inicial según dc_sl_mode: banda opuesta, ATR, o el más cercano."""
        atr_stop = close - self.sl_atr_mult * atr if direction == "LONG" \
            else close + self.sl_atr_mult * atr
        channel_stop = lower if direction == "LONG" else upper
        if self.sl_mode == "channel":
            return channel_stop
        if self.sl_mode == "atr":
            return atr_stop
        # "tighter": el stop MÁS CERCANO al precio (menor riesgo por trade).
        if direction == "LONG":
            return max(atr_stop, channel_stop)
        return min(atr_stop, channel_stop)

    @staticmethod
    def _value_at(series: Optional[pd.Series], idx: int) -> Optional[float]:
        if series is None or idx < 0 or idx >= len(series):
            return None
        v = series.iloc[idx]
        return float(v) if pd.notna(v) else None

    def _wait(self, motivo: str) -> DonchianDecision:
        return DonchianDecision(
            accion="ESPERAR",
            direction="LONG",
            confianza=0.0,
            razon=motivo,
            advertencias=[],
        )
