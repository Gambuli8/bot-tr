"""
core/pullback_scalp_engine.py
Motor de scalping de PULLBACK-CONTINUACIÓN para intradía (TF base 5m).

═══════════════════════════════════════════════════════════════════════════
POR QUÉ ESTE MOTOR EXISTE (lectura obligatoria antes de tocarlo)
═══════════════════════════════════════════════════════════════════════════
El ScalpingEngine v1 (BB squeeze + breakout) fue DESCARTADO en la Fase 1 tras
Monte Carlo + Walk-Forward: 2/7 ventanas OS positivas, −17.74% retorno OS,
esperanza matemática ≈ 0 con fees reales. Ver docs/BACKTESTS.md (2026-06-07).

La auditoría dejó hallazgos concretos sobre POR QUÉ falló en BTC 5m:
  1. El breakout NO tiene edge direccional (~50% WR) → pagás fee completa
     a cambio de un coin-flip.
  2. Los breakouts obligan a entrar "a mercado" (taker) persiguiendo precio.
  3. Volatilidad muy baja (BTC, ATR ~0.15%) → el fee fijo se come el TP.
  4. EU AM (06-12 UTC) destruye breakouts con whipsaw de HFT.
  5. Spikes de volumen muy grandes (>4×) son ruido que revierte.

Este motor está diseñado para ATACAR cada uno de esos puntos:
  1. Edge direccional: SÓLO opera A FAVOR de la tendencia (EMA fast/slow stack
     + ADX). No adivina dirección; la hereda de la tendencia.
  2. Entrada maker: compra el PULLBACK (el precio viene a vos, no lo perseguís)
     → fill con orden Limit Post-Only = fee maker, no taker.
  3. Captura > fee: gate de EV neto + piso de TP (TP bruto ≥ N× fee round-trip).
     Pensado para activos con ATR% > BTC (SOL, AVAX…), donde el movimiento
     supera holgadamente la comisión.
  4. Filtro de sesión (skip_hours_utc).
  5. Guard de spike de volumen (rechaza la vela de reclaim si vol > N× MA).

⚠️  NO VALIDADO TODAVÍA. Igual que v1, este motor NO va a dinero real hasta
    pasar el mismo gauntlet: backtest + WFA con fees reales. Comando:
        python scripts/backtest_pullback.py --symbol SOL/USDT --days 90
    Recomendación: validar primero en SOL/USDT (más ATR% que BTC = mejor
    ratio movimiento/fee), no en BTC.
═══════════════════════════════════════════════════════════════════════════

Estrategia (single-TF 5m, agnóstico de moneda):
  - Tendencia: EMA(fast) sobre EMA(slow) y precio del lado correcto + ADX ≥ min.
  - Pullback: el precio retrocede y toca la EMA(fast) (imán dinámico) con RSI
    en zona de reset (no sobreventa total — es un respiro dentro de tendencia).
  - Reclaim: la vela actual cierra de vuelta del lado de la tendencia, de forma
    impulsiva → la tendencia se reanuda. Ahí entramos.
  - SL: ajustado (entramos en soporte/resistencia dinámica) = R:R sano.
  - TP: múltiplo del riesgo, con piso para que el fee nunca lo coma.
"""

from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd
import ta

from logs.logger import logger
from config.settings import Settings
from core.scalping_engine import ScalpDecision  # reutilizamos el shape de decisión

Direction = Literal["LONG", "SHORT"]


class PullbackScalpEngine:
    """
    Decisor de scalping por pullback-continuación. Mismo contrato que
    ScalpingEngine: analyze(df, current_idx) -> ScalpDecision sobre la última
    vela cerrada. Drop-in para el backtest y para main_strategy.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ema_fast: int = getattr(settings, "pbs_ema_fast", 21)
        self.ema_slow: int = getattr(settings, "pbs_ema_slow", 200)
        self.adx_window: int = getattr(settings, "pbs_adx_window", 14)
        self.adx_min: float = getattr(settings, "pbs_adx_min", 18.0)
        self.rsi_window: int = getattr(settings, "pbs_rsi_window", 14)
        self.rsi_pullback_long: float = getattr(settings, "pbs_rsi_pullback_long", 45.0)
        self.rsi_pullback_short: float = getattr(settings, "pbs_rsi_pullback_short", 55.0)
        self.pullback_lookback: int = getattr(settings, "pbs_pullback_lookback", 6)
        self.touch_atr_tol: float = getattr(settings, "pbs_touch_atr_tol", 0.5)
        self.atr_window: int = getattr(settings, "pbs_atr_window", 14)
        self.atr_min_pct: float = getattr(settings, "pbs_atr_min_pct", 0.0012)
        self.atr_max_pct: float = getattr(settings, "pbs_atr_max_pct", 0.0060)
        self.vol_window: int = getattr(settings, "pbs_vol_window", 20)
        self.vol_spike_max: float = getattr(settings, "pbs_vol_spike_max", 4.0)
        self.sl_atr_mult: float = getattr(settings, "pbs_sl_atr_mult", 1.1)
        self.tp_rr: float = getattr(settings, "pbs_tp_rr", 1.6)
        self.sl_min_pct: float = getattr(settings, "pbs_sl_min_pct", 0.0015)
        self.sl_max_pct: float = getattr(settings, "pbs_sl_max_pct", 0.02)
        self.cooldown_bars: int = getattr(settings, "pbs_cooldown_bars", 3)
        # ─ Gates de fees (el corazón "que las comisiones no coman el profit") ─
        self.max_fee_to_gain: float = getattr(settings, "pbs_max_fee_to_gain", 0.15)
        self.fee_floor_mult: float = getattr(settings, "pbs_fee_floor_mult", 6.0)
        self.assumed_winrate: float = getattr(settings, "pbs_assumed_winrate", 0.55)
        self.min_net_ev_pct: float = getattr(settings, "pbs_min_net_ev_pct", 0.0)

        self.skip_hours: set[int] = _parse_hours(
            getattr(settings, "scalp_skip_hours_utc", "")
        )
        self._last_close_idx: int = -10**9

        # Cache de indicadores por id(df) (evita O(n²) en backtests largos).
        self._cache_df_id: int = -1
        self._ema_f: Optional[pd.Series] = None
        self._ema_s: Optional[pd.Series] = None
        self._atr: Optional[pd.Series] = None
        self._rsi: Optional[pd.Series] = None
        self._adx: Optional[pd.Series] = None
        self._vol_ma: Optional[pd.Series] = None
        logger.info(
            "PullbackScalpEngine inicializado (trend-pullback, maker-first, fee-gated)"
        )

    # ── compat con el loop de main_strategy ──
    @property
    def is_safe_mode(self) -> bool:
        return False

    def reset_circuit_breaker(self) -> None:
        pass

    def mark_close(self, idx: int) -> None:
        self._last_close_idx = idx

    def _ensure_cache(self, df: pd.DataFrame) -> None:
        if id(df) == self._cache_df_id and self._ema_f is not None:
            return
        close, high, low = df["close"], df["high"], df["low"]
        self._ema_f = ta.trend.EMAIndicator(close=close, window=self.ema_fast).ema_indicator()
        self._ema_s = ta.trend.EMAIndicator(close=close, window=self.ema_slow).ema_indicator()
        self._atr = ta.volatility.AverageTrueRange(
            high=high, low=low, close=close, window=self.atr_window,
        ).average_true_range()
        self._rsi = ta.momentum.RSIIndicator(close=close, window=self.rsi_window).rsi()
        try:
            self._adx = ta.trend.ADXIndicator(
                high=high, low=low, close=close, window=self.adx_window,
            ).adx()
        except Exception:
            self._adx = pd.Series([0.0] * len(df), index=df.index)
        # MA de volumen excluyendo la vela actual (shift 1).
        self._vol_ma = df["volume"].rolling(self.vol_window).mean().shift(1)
        self._cache_df_id = id(df)

    def analyze(self, df: pd.DataFrame, current_idx: int) -> ScalpDecision:
        # Cooldown anti-ruido tras cierre.
        if (current_idx - self._last_close_idx) < self.cooldown_bars:
            return self._wait(f"cooldown ({self.cooldown_bars} velas)")

        # Skip de sesión (EU AM u horas que el WFA marque tóxicas).
        if self.skip_hours:
            hour_utc = df.index[current_idx].hour
            if hour_utc in self.skip_hours:
                return self._wait(f"hora {hour_utc:02d}h UTC en skip_hours")

        warm = max(self.ema_slow + 5, self.atr_window + 5, self.pullback_lookback + 2)
        if current_idx < warm:
            return self._wait("warm-up incompleto")

        self._ensure_cache(df)
        i = current_idx
        ema_f = _val(self._ema_f, i)
        ema_s = _val(self._ema_s, i)
        atr = _val(self._atr, i)
        rsi = _val(self._rsi, i)
        adx = _val(self._adx, i)
        vol_ma = _val(self._vol_ma, i)
        if min(ema_f, ema_s, atr) <= 0 or vol_ma <= 0:
            return self._wait("indicadores no disponibles (warm-up)")

        last = df.iloc[i]
        prev = df.iloc[i - 1]
        close = float(last["close"])
        open_ = float(last["open"])
        prev_close = float(prev["close"])
        ema_f_prev = _val(self._ema_f, i - 1)

        # ── Régimen de volatilidad (auditoría v1: edge en vol media) ──
        atr_pct = atr / close
        if atr_pct < self.atr_min_pct:
            return self._wait(f"ATR% {atr_pct:.3%} < piso {self.atr_min_pct:.2%} (fees comen el TP)")
        if atr_pct > self.atr_max_pct:
            return self._wait(f"ATR% {atr_pct:.3%} > techo {self.atr_max_pct:.2%} (SL random)")

        # ── Fuerza de tendencia ──
        if adx < self.adx_min:
            return self._wait(f"ADX {adx:.1f} < {self.adx_min:.0f} (lateral, sin fuerza)")

        # ── Guard de spike: vela de reclaim no debe ser explosión de ruido ──
        vol_ratio = float(last["volume"]) / vol_ma
        if vol_ratio > self.vol_spike_max:
            return self._wait(f"vol {vol_ratio:.1f}× > {self.vol_spike_max:.0f}× (spike de ruido)")

        # Ventana del pullback (velas previas a la actual).
        lo = max(0, i - self.pullback_lookback)
        window = df.iloc[lo:i]
        rsi_window = self._rsi.iloc[lo:i]

        bull = close > ema_s and ema_f > ema_s
        bear = close < ema_s and ema_f < ema_s

        direction: Optional[Direction] = None

        if bull:
            # Pullback: tocó la EMA rápida por abajo y RSI hizo un respiro.
            touched = bool((window["low"] <= ema_f * (1 + self.touch_atr_tol * atr_pct)).any())
            rsi_dip = bool((rsi_window <= self.rsi_pullback_long).any())
            # Reclaim impulsivo: la vela anterior estaba en/bajo la EMA, esta
            # cierra por encima con cuerpo alcista.
            reclaim = (prev_close <= ema_f_prev) and (close > ema_f) and (close > open_)
            if touched and rsi_dip and reclaim:
                direction = "LONG"
        elif bear:
            touched = bool((window["high"] >= ema_f * (1 - self.touch_atr_tol * atr_pct)).any())
            rsi_pop = bool((rsi_window >= self.rsi_pullback_short).any())
            reclaim = (prev_close >= ema_f_prev) and (close < ema_f) and (close < open_)
            if touched and rsi_pop and reclaim:
                direction = "SHORT"

        if direction is None:
            return self._wait("sin setup de pullback-continuación")

        # ── Niveles ──
        if direction == "LONG":
            sl_price = close - self.sl_atr_mult * atr
            risk = close - sl_price
            tp_price = close + self.tp_rr * risk
            accion = "COMPRAR"
        else:
            sl_price = close + self.sl_atr_mult * atr
            risk = sl_price - close
            tp_price = close - self.tp_rr * risk
            accion = "VENDER"

        if risk <= 0:
            return self._wait("riesgo no positivo")

        sl_pct = abs(close - sl_price) / close
        tp_pct = abs(tp_price - close) / close
        if sl_pct < self.sl_min_pct:
            return self._wait(f"SL muy apretado ({sl_pct:.3%})")
        if sl_pct > self.sl_max_pct:
            return self._wait(f"SL excesivo ({sl_pct:.3%})")

        # ─────────────────────────────────────────────────────────────
        #  GATES DE FEE — que la comisión no se coma el profit
        # ─────────────────────────────────────────────────────────────
        maker = getattr(self.settings, "commission_pct_per_side", 0.0002)
        taker = getattr(self.settings, "commission_taker_pct", 0.0005)
        # Round-trip optimista (entrada maker + salida TP maker).
        rt_fee_maker = 2 * maker
        # 1) El fee no puede ser > max_fee_to_gain del bruto esperado.
        if tp_pct <= 0:
            return self._wait("TP inválido")
        fee_to_gain = rt_fee_maker / tp_pct
        if fee_to_gain > self.max_fee_to_gain:
            return self._wait(
                f"TP corto: fee sería {fee_to_gain:.0%} del bruto (límite {self.max_fee_to_gain:.0%})"
            )
        # 2) Piso absoluto: el TP bruto debe ser ≥ N× el fee round-trip.
        if tp_pct < self.fee_floor_mult * rt_fee_maker:
            return self._wait(
                f"TP {tp_pct:.3%} < piso {self.fee_floor_mult:.0f}× fee ({self.fee_floor_mult * rt_fee_maker:.3%})"
            )
        # 3) EV neto con winrate conservador. Escenario realista: gana → salida
        #    maker (TP limit); pierde → salida taker (SL market).
        p = self.assumed_winrate
        net_win = tp_pct - 2 * maker
        net_loss = sl_pct + maker + taker
        ev_pct = p * net_win - (1 - p) * net_loss
        if ev_pct <= self.min_net_ev_pct:
            return self._wait(
                f"EV neto {ev_pct:.3%} ≤ {self.min_net_ev_pct:.3%} (no compensa el fee)"
            )

        razon = (
            f"Pullback-continuación {direction} | ADX {adx:.0f} | "
            f"ATR {atr_pct:.2%} | R:R 1:{self.tp_rr:.1f} | "
            f"EV neto {ev_pct:.2%} | fee/bruto {fee_to_gain:.0%}"
        )
        confianza = min(0.95, 0.55 + 0.01 * (adx - self.adx_min) + 2.0 * ev_pct)
        logger.info(f"🎯 Pullback Engine → {accion} {direction} | {razon}")

        return ScalpDecision(
            accion=accion,
            direction=direction,
            confianza=round(max(0.0, confianza), 3),
            razon=razon,
            entry_price=round(close, 4),
            stop_loss_price=round(sl_price, 4),
            take_profit_price=round(tp_price, 4),
            stop_loss_pct=round(sl_pct, 5),
            take_profit_pct=round(tp_pct, 5),
            advertencias=["Scalping pullback — maker-first, fee-gated"],
        )

    def _wait(self, motivo: str) -> ScalpDecision:
        return ScalpDecision(
            accion="ESPERAR", direction="LONG", confianza=0.0,
            razon=motivo, advertencias=[],
        )


# ─────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────

def _val(series: Optional[pd.Series], i: int) -> float:
    if series is None:
        return 0.0
    v = series.iloc[i]
    return float(v) if pd.notna(v) else 0.0


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
