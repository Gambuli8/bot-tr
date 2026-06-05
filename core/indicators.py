"""
core/indicators.py
Cálculo de indicadores técnicos sobre DataFrames OHLCV.
Estrategia híbrida: Trend Following (Donchian) + filtros (RSI, MACD, EMA).
Usa la librería 'ta' compatible con Python 3.12+
"""

from dataclasses import dataclass
from typing import Literal
import pandas as pd
import ta
from logs.logger import logger
from config.settings import Settings


@dataclass
class MarketSnapshot:
    """Foto completa del mercado en un instante dado. Es lo que recibe Claude."""
    # Precio
    price: float
    price_change_1h: float
    price_change_24h: float

    # RSI
    rsi: float
    rsi_prev: float

    # MACD
    macd_line: float
    macd_signal: float
    macd_histogram: float
    macd_crossover: bool

    # Medias móviles
    ema50: float
    ema200: float
    price_vs_ema50: float
    price_vs_ema200: float

    # Bollinger Bands
    bb_upper: float
    bb_middle: float
    bb_lower: float
    bb_width: float

    # Donchian Channel (señal principal de trend-following)
    donchian_high: float           # Máximo de últimas N velas
    donchian_low: float            # Mínimo de últimas N velas
    donchian_mid: float
    breakout_up: bool              # Esta vela rompió el máximo del canal (señal LONG)
    breakout_down: bool            # Esta vela rompió el mínimo del canal (señal SHORT)
    distance_to_high_pct: float    # % del precio respecto al techo del canal
    distance_to_low_pct: float     # % del precio respecto al piso del canal

    # ATR
    atr: float
    atr_pct: float

    # ADX (fuerza de tendencia)
    adx: float

    # Tendencia
    trend: Literal["BULL", "BEAR", "LATERAL"]
    trend_strength: float

    # Volumen
    volume_current: float
    volume_avg_20: float
    volume_ratio: float

    # Metadatos
    symbol: str
    timeframe: str
    candles_available: int
    is_warmed_up: bool


class IndicatorEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def calculate(self, df: pd.DataFrame) -> MarketSnapshot:
        if len(df) < self.settings.warmup_candles:
            logger.warning(
                f"Warm-up incompleto: {len(df)}/{self.settings.warmup_candles} velas"
            )
        df = df.copy()
        df = self._add_all_indicators(df)
        return self._build_snapshot(df)

    def _add_all_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        # RSI
        df["rsi"] = ta.momentum.RSIIndicator(
            close=df["close"], window=self.settings.rsi_period
        ).rsi()

        # MACD
        macd = ta.trend.MACD(
            close=df["close"],
            window_fast=self.settings.macd_fast,
            window_slow=self.settings.macd_slow,
            window_sign=self.settings.macd_signal,
        )
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()

        # EMAs
        df["ema50"] = ta.trend.EMAIndicator(
            close=df["close"], window=self.settings.ema_fast
        ).ema_indicator()
        df["ema200"] = ta.trend.EMAIndicator(
            close=df["close"], window=self.settings.ema_slow
        ).ema_indicator()

        # Bollinger Bands
        bb = ta.volatility.BollingerBands(close=df["close"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_middle"] = bb.bollinger_mavg()
        df["bb_lower"] = bb.bollinger_lband()

        # Donchian Channel — señal principal de trend-following
        donchian = ta.volatility.DonchianChannel(
            high=df["high"], low=df["low"], close=df["close"],
            window=self.settings.donchian_period,
        )
        df["donchian_high"] = donchian.donchian_channel_hband()
        df["donchian_low"] = donchian.donchian_channel_lband()
        df["donchian_mid"] = donchian.donchian_channel_mband()

        # ATR
        df["atr"] = ta.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"],
            window=self.settings.atr_period,
        ).average_true_range()

        # ADX (fuerza de tendencia, 0-100; <20 lateral, >25 trending)
        df["adx"] = ta.trend.ADXIndicator(
            high=df["high"], low=df["low"], close=df["close"],
            window=self.settings.adx_period,
        ).adx()

        # Volumen promedio
        df["volume_avg_20"] = df["volume"].rolling(20).mean()

        return df

    def _build_snapshot(self, df: pd.DataFrame) -> MarketSnapshot:
        last = df.iloc[-2]                  # Última vela cerrada
        prev = df.iloc[-3]                  # Vela anterior
        current_price = float(df.iloc[-1]["close"])

        # Cambios de precio
        candles_1h = max(1, 60 // self._timeframe_minutes())
        candles_24h = max(1, 96)
        price_1h_ago = (
            float(df.iloc[-candles_1h]["close"])
            if len(df) >= candles_1h else current_price
        )
        price_24h_ago = (
            float(df.iloc[-candles_24h]["close"])
            if len(df) >= candles_24h else current_price
        )
        price_change_1h = ((current_price - price_1h_ago) / price_1h_ago) * 100
        price_change_24h = ((current_price - price_24h_ago) / price_24h_ago) * 100

        # MACD crossover en la última vela cerrada
        macd_crossover = (
            float(last["macd"]) > float(last["macd_signal"]) and
            float(prev["macd"]) <= float(prev["macd_signal"])
        )

        ema50 = float(last["ema50"])
        ema200 = float(last["ema200"])
        price_vs_ema50 = ((current_price - ema50) / ema50) * 100
        price_vs_ema200 = ((current_price - ema200) / ema200) * 100

        bb_upper = float(last["bb_upper"])
        bb_lower = float(last["bb_lower"])
        bb_middle = float(last["bb_middle"])
        bb_width = ((bb_upper - bb_lower) / bb_middle) * 100

        # Donchian — la clave de la nueva estrategia
        # Usamos el valor de la PENÚLTIMA vela cerrada como referencia
        # para que no se "auto-incluya" en su propio máximo
        donchian_high = float(df.iloc[-3]["donchian_high"])
        donchian_low = float(df.iloc[-3]["donchian_low"])
        donchian_mid = float(df.iloc[-3]["donchian_mid"])

        # Breakout: el precio actual supera el máximo / rompe el mínimo previo
        breakout_up = current_price > donchian_high
        breakout_down = current_price < donchian_low
        distance_to_high_pct = ((current_price - donchian_high) / donchian_high) * 100
        distance_to_low_pct = ((current_price - donchian_low) / donchian_low) * 100

        atr = float(last["atr"])
        atr_pct = (atr / current_price) * 100

        adx_val = float(last["adx"]) if not pd.isna(last["adx"]) else 0.0

        trend, strength = self._determine_trend(current_price, ema50, ema200)

        volume_current = float(last["volume"])
        volume_avg = (
            float(last["volume_avg_20"])
            if not pd.isna(last["volume_avg_20"]) else volume_current
        )
        volume_ratio = volume_current / volume_avg if volume_avg > 0 else 1.0

        is_warmed_up = len(df) >= self.settings.warmup_candles

        snapshot = MarketSnapshot(
            price=current_price,
            price_change_1h=round(price_change_1h, 3),
            price_change_24h=round(price_change_24h, 3),
            rsi=round(float(last["rsi"]), 2),
            rsi_prev=round(float(prev["rsi"]), 2),
            macd_line=round(float(last["macd"]), 4),
            macd_signal=round(float(last["macd_signal"]), 4),
            macd_histogram=round(float(last["macd_hist"]), 4),
            macd_crossover=macd_crossover,
            ema50=round(ema50, 2),
            ema200=round(ema200, 2),
            price_vs_ema50=round(price_vs_ema50, 3),
            price_vs_ema200=round(price_vs_ema200, 3),
            bb_upper=round(bb_upper, 2),
            bb_middle=round(bb_middle, 2),
            bb_lower=round(bb_lower, 2),
            bb_width=round(bb_width, 3),
            donchian_high=round(donchian_high, 2),
            donchian_low=round(donchian_low, 2),
            donchian_mid=round(donchian_mid, 2),
            breakout_up=breakout_up,
            breakout_down=breakout_down,
            distance_to_high_pct=round(distance_to_high_pct, 3),
            distance_to_low_pct=round(distance_to_low_pct, 3),
            atr=round(atr, 2),
            atr_pct=round(atr_pct, 4),
            adx=round(adx_val, 2),
            volume_current=round(volume_current, 4),
            volume_avg_20=round(volume_avg, 4),
            volume_ratio=round(volume_ratio, 3),
            trend=trend,
            trend_strength=round(strength, 3),
            symbol=self.settings.symbol,
            timeframe=self.settings.timeframe,
            candles_available=len(df),
            is_warmed_up=is_warmed_up,
        )

        logger.debug(
            f"Snapshot | Price: ${current_price:,.2f} | RSI: {snapshot.rsi} | "
            f"Trend: {trend} | Breakout: {breakout_up} | "
            f"Donchian high: ${donchian_high:,.2f}"
        )

        return snapshot

    def _determine_trend(
        self, price: float, ema50: float, ema200: float
    ) -> tuple[str, float]:
        dist_ema200 = abs((price - ema200) / ema200) * 100
        if price > ema200 and ema50 > ema200:
            return "BULL", min(dist_ema200 / 5.0, 1.0)
        elif price < ema200 and ema50 < ema200:
            return "BEAR", min(dist_ema200 / 5.0, 1.0)
        else:
            return "LATERAL", max(0.1, 1.0 - (dist_ema200 / 2.0))

    def _timeframe_minutes(self) -> int:
        mapping = {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                   "1h": 60, "4h": 240, "1d": 1440}
        return mapping.get(self.settings.timeframe, 15)
