"""
core/mtf_context.py
Contexto Multi-Timeframe: tendencia y fuerza en TFs superiores (1h, 4h).
Lo usa el TechnicalEngine para descartar setups intraday que vayan contra
la tendencia macro — filtro estándar en trading institucional.
"""

from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd
import ta


Trend = Literal["BULL", "BEAR", "LATERAL"]


@dataclass
class MTFContext:
    """Snapshot de tendencia y fuerza en TFs superiores."""
    trend_1h: Trend
    trend_4h: Trend
    adx_1h: float
    adx_4h: float
    ema50_1h: float
    ema200_1h: float
    ema50_4h: float
    ema200_4h: float
    price_1h: float
    price_4h: float


def compute_trend(df: pd.DataFrame, ema_fast: int = 50, ema_slow: int = 200) -> tuple[Trend, float, float, float]:
    """
    Devuelve (trend, ema_fast, ema_slow, last_close) sobre el df dado.
    Asume df tiene columnas OHLCV. La tendencia usa el cierre de la última
    vela cerrada (iloc[-1] si ya cerró, iloc[-2] si está formándose).
    """
    if len(df) < ema_slow + 5:
        return "LATERAL", 0.0, 0.0, float(df["close"].iloc[-1]) if len(df) else 0.0

    df = df.copy()
    df["ema_fast"] = ta.trend.EMAIndicator(close=df["close"], window=ema_fast).ema_indicator()
    df["ema_slow"] = ta.trend.EMAIndicator(close=df["close"], window=ema_slow).ema_indicator()

    last = df.iloc[-1]
    ema_f = float(last["ema_fast"])
    ema_s = float(last["ema_slow"])
    close = float(last["close"])

    if close > ema_s and ema_f > ema_s:
        return "BULL", ema_f, ema_s, close
    if close < ema_s and ema_f < ema_s:
        return "BEAR", ema_f, ema_s, close
    return "LATERAL", ema_f, ema_s, close


def compute_adx(df: pd.DataFrame, window: int = 14) -> float:
    """ADX de la última vela cerrada."""
    if len(df) < window + 5:
        return 0.0
    try:
        s = ta.trend.ADXIndicator(
            high=df["high"], low=df["low"], close=df["close"], window=window,
        ).adx()
        last = float(s.iloc[-1])
        return last if pd.notna(last) else 0.0
    except Exception:
        return 0.0


def build_mtf_context(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> MTFContext:
    """Construye un MTFContext a partir de dos DataFrames OHLCV (1h y 4h)."""
    t1, ef1, es1, p1 = compute_trend(df_1h)
    t4, ef4, es4, p4 = compute_trend(df_4h)
    return MTFContext(
        trend_1h=t1,
        trend_4h=t4,
        adx_1h=round(compute_adx(df_1h), 2),
        adx_4h=round(compute_adx(df_4h), 2),
        ema50_1h=round(ef1, 2),
        ema200_1h=round(es1, 2),
        ema50_4h=round(ef4, 2),
        ema200_4h=round(es4, 2),
        price_1h=round(p1, 2),
        price_4h=round(p4, 2),
    )


def confluence_allows(
    direction: Literal["LONG", "SHORT"],
    mtf: MTFContext,
    strict_4h: bool = False,
) -> tuple[bool, str]:
    """
    Devuelve (allowed, motivo).
    LONG permitido si trend_1h no es BEAR (LATERAL o BULL).
    SHORT permitido si trend_1h no es BULL.
    Si strict_4h: también exigir que 4h no contradiga.
    """
    if direction == "LONG":
        if mtf.trend_1h == "BEAR":
            return False, f"1h en tendencia BEAR (p {mtf.price_1h:.0f} < EMA200 {mtf.ema200_1h:.0f})"
        if strict_4h and mtf.trend_4h == "BEAR":
            return False, f"4h en tendencia BEAR"
    else:  # SHORT
        if mtf.trend_1h == "BULL":
            return False, f"1h en tendencia BULL (p {mtf.price_1h:.0f} > EMA200 {mtf.ema200_1h:.0f})"
        if strict_4h and mtf.trend_4h == "BULL":
            return False, f"4h en tendencia BULL"
    return True, "MTF OK"
