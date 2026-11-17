"""Fase 2: tres estrategias base, solo LONG (spot, sin apalancamiento).

Contrato común — cada estrategia recibe OHLCV y devuelve un DataFrame alineado con columnas:
  entry      bool   la vela t CERRÓ con condición de entrada → se compra en la apertura de t+1
  exit       bool   la vela t cerró con condición de salida  → se vende en la apertura de t+1
  stop_dist  float  distancia del stop inicial en precio (fija el tamaño por riesgo)
  trail      float  nivel de stop dinámico válido para la vela t+1 (NaN = sin trailing). El stop sólo sube.

Toda la información usada en t es de velas ≤ t (sin mirar al futuro). El MISMO código corre en el backtest
y en el bot en vivo.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Callable

import numpy as np
import pandas as pd


# ── Indicadores ─────────────────────────────────────────────────────────────────────────────────────────
def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _frame(df, entry, exit_, stop_dist, trail=None) -> pd.DataFrame:
    synthetic = df.get("synthetic", pd.Series(0, index=df.index)).astype(bool)
    out = pd.DataFrame({"entry": entry.fillna(False).astype(bool) & ~synthetic,
                        "exit": exit_.fillna(False).astype(bool),
                        "stop_dist": stop_dist,
                        "trail": trail if trail is not None else np.nan}, index=df.index)
    out.loc[out["stop_dist"].isna(), "entry"] = False
    return out


# ── 1. Rompimiento de volatilidad (canal Donchian) ──────────────────────────────────────────────────────
def breakout(df: pd.DataFrame, n_entry: int = 40, n_exit: int = 20, atr_mult: float = 2.5) -> pd.DataFrame:
    """Compra el cierre por encima del máximo de las n_entry velas previas. El stop sigue al mínimo de las
    n_exit velas (trailing intrabar). Stop inicial: atr_mult × ATR(14)."""
    hh = df["high"].rolling(n_entry).max().shift(1)
    ll = df["low"].rolling(n_exit).min()
    entry = df["close"] > hh
    return _frame(df, entry, pd.Series(False, index=df.index), atr_mult * atr(df), trail=ll)


# ── 2. Cruce de tendencia EMA ───────────────────────────────────────────────────────────────────────────
def ema_cross(df: pd.DataFrame, fast: int = 20, slow: int = 100, atr_mult: float = 3.0) -> pd.DataFrame:
    """Compra cuando la EMA rápida cruza por encima de la lenta; vende al cruce contrario. Stop: atr_mult × ATR."""
    f, s = ema(df["close"], fast), ema(df["close"], slow)
    above = f > s
    entry = above & ~above.shift(1, fill_value=False)
    exit_ = ~above & s.notna()
    return _frame(df, entry, exit_, atr_mult * atr(df))


# ── 3. Reversión a la media RSI (con filtro de tendencia) ───────────────────────────────────────────────
def rsi_reversion(df: pd.DataFrame, n: int = 4, entry_lvl: float = 20, exit_lvl: float = 65,
                  trend: int = 200, atr_mult: float = 3.0) -> pd.DataFrame:
    """Compra sobreventa (RSI(n) < entry_lvl) sólo si el cierre está sobre la SMA(trend); sale con
    RSI > exit_lvl. Stop: atr_mult × ATR."""
    r = rsi(df["close"], n)
    uptrend = df["close"] > df["close"].rolling(trend).mean()
    return _frame(df, (r < entry_lvl) & uptrend, r > exit_lvl, atr_mult * atr(df))


# ── Registro + grillas de parámetros ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class StrategySpec:
    name: str
    label: str
    fn: Callable[..., pd.DataFrame]
    grid: dict[str, list]
    default: dict

    def combos(self) -> list[dict]:
        keys = list(self.grid)
        out = [dict(zip(keys, vals)) for vals in product(*self.grid.values())]
        if self.name == "ema_cross":
            out = [p for p in out if p["fast"] * 2 <= p["slow"]]
        return out


STRATEGIES: dict[str, StrategySpec] = {
    "breakout": StrategySpec(
        "breakout", "Rompimiento de volatilidad (Donchian)", breakout,
        grid={"n_entry": [20, 30, 40, 55, 70, 90], "n_exit": [10, 15, 20, 30], "atr_mult": [2.0, 3.0]},
        default={"n_entry": 40, "n_exit": 20, "atr_mult": 2.5}),
    "ema_cross": StrategySpec(
        "ema_cross", "Cruce de tendencia EMA", ema_cross,
        grid={"fast": [10, 20, 30, 50], "slow": [50, 100, 150, 200], "atr_mult": [2.0, 3.0, 4.0]},
        default={"fast": 20, "slow": 100, "atr_mult": 3.0}),
    "rsi_reversion": StrategySpec(
        "rsi_reversion", "Reversión a la media RSI", rsi_reversion,
        grid={"n": [2, 4, 7, 14], "entry_lvl": [15, 25, 35], "exit_lvl": [55, 70], "atr_mult": [2.0, 3.0]},
        default={"n": 4, "entry_lvl": 20, "exit_lvl": 65, "atr_mult": 3.0}),
}


def signals(name: str, df: pd.DataFrame, params: dict) -> pd.DataFrame:
    return STRATEGIES[name].fn(df, **params)
