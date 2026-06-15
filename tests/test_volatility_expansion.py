"""
tests/test_volatility_expansion.py
Cubre core/volatility_expansion_engine.compute_signals: estructura, dominio de la
señal, y que un breakout alcista en macro BULL + compresión + volumen dispara
signal=1 (y NO dispara contra la macro → no lookahead/leakage de dirección).
"""

import numpy as np
import pandas as pd

from core.volatility_expansion_engine import VEParams, compute_signals


def _mk_1h(n, base=100.0):
    # Rango chico pero NO cero (ATR>0) para que la compresión sea medible.
    idx = pd.date_range("2025-01-01", periods=n, freq="1h")
    close = np.full(n, base, dtype=float)
    df = pd.DataFrame({
        "open": close, "high": close + 0.3, "low": close - 0.3, "close": close,
        "volume": np.full(n, 100.0),
    }, index=idx)
    return df


def _mk_4h_bull(n_1h):
    # 4h alcista: EMA50>EMA200. close creciente fuerte y sostenido.
    n4 = n_1h // 4 + 5
    idx = pd.date_range("2024-12-20", periods=n4, freq="4h")
    close = np.linspace(50.0, 100.0, n4)
    df = pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close,
        "volume": np.full(n4, 100.0),
    }, index=idx)
    return df


def test_estructura_y_dominio():
    df1 = _mk_1h(300)
    df4 = _mk_4h_bull(300)
    p = VEParams(ema_slow=20, ema_fast=10, compression_lookback_bars=50,
                 breakout_lookback=24)
    sig = compute_signals(df1, df4, p)
    assert list(sig.columns) == ["close", "atr", "signal"]
    assert len(sig) == len(df1)
    assert set(pd.unique(sig["signal"])).issubset({-1, 0, 1})


def test_breakout_bull_dispara_long():
    n = 300
    df1 = _mk_1h(n, base=100.0)
    # Crear compresión (rango chico) en las últimas ~60 velas y un breakout en la última.
    # ATR bajo: high≈low≈close planos; en la última vela, breakout de volumen y precio.
    df1.iloc[-1, df1.columns.get_loc("high")] = 130.0
    df1.iloc[-1, df1.columns.get_loc("close")] = 129.0
    df1.iloc[-1, df1.columns.get_loc("volume")] = 1000.0  # > 1.5× media(20)=100
    df4 = _mk_4h_bull(n)
    p = VEParams(ema_slow=20, ema_fast=10, compression_lookback_bars=50,
                 compression_ratio=2.0,  # afloja compresión para el test
                 breakout_lookback=24, vol_mult=1.5)
    sig = compute_signals(df1, df4, p)
    assert sig["signal"].iloc[-1] == 1   # LONG a favor de la macro BULL


def test_no_dispara_short_en_bull():
    # Aunque haya breakout a la baja, en macro BULL no debe abrir SHORT.
    n = 300
    df1 = _mk_1h(n, base=100.0)
    df1.iloc[-1, df1.columns.get_loc("low")] = 70.0
    df1.iloc[-1, df1.columns.get_loc("close")] = 71.0
    df1.iloc[-1, df1.columns.get_loc("volume")] = 1000.0
    df4 = _mk_4h_bull(n)
    p = VEParams(ema_slow=20, ema_fast=10, compression_lookback_bars=50,
                 compression_ratio=2.0, breakout_lookback=24)
    sig = compute_signals(df1, df4, p)
    assert sig["signal"].iloc[-1] != -1   # nunca SHORT contra macro BULL
