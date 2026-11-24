"""
Carga y prepara las velas de 1 minuto de NQ bajadas con download_nq.py.

- Ajuste por vencimientos (back-adjust): en cada cambio de contrato se suma la diferencia de precio a todo
  lo anterior, así no aparecen saltos falsos en los niveles diarios. Los precios recientes quedan reales.
- Horario de Nueva York (America/New_York, con cambio de hora incluido).
- Sesión de futuros: de 18:00 a 17:00 ET. La "fecha de sesión" es el día en que cierra.
- RTH (horario de la bolsa): 9:30 a 16:00 ET.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

NY = "America/New_York"
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "nq"


def load_raw(folder: Path = DATA) -> pd.DataFrame:
    files = sorted(folder.glob("nq_1m_*.csv.gz"))
    if not files:
        raise SystemExit(f"No hay datos en {folder}. Corré primero: python research/nq/download_nq.py")
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def back_adjust(df: pd.DataFrame) -> pd.DataFrame:
    """Suma a todo lo anterior a cada vencimiento la diferencia (apertura nueva − cierre viejo)."""
    df = df.copy()
    roll = df["instrument_id"].ne(df["instrument_id"].shift()) & df.index.to_series().gt(0)
    gap = np.where(roll, df["open"] - df["close"].shift(), 0.0)
    # offset[i] = suma de los saltos de los vencimientos POSTERIORES a la vela i
    offset = np.cumsum(gap[::-1])[::-1] - gap
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] + offset
    df.attrs["rolls"] = int(roll.sum())
    return df


@dataclass
class NQ:
    """Velas de 1 minuto listas para simular (arrays de numpy) + índices por sesión."""
    m1: pd.DataFrame            # índice: hora ET; columnas open high low close volume ts(ms UTC)
    ts: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    session: np.ndarray         # fecha de sesión (datetime64[D]) de cada vela
    minute: np.ndarray          # minutos desde las 00:00 ET

    def at_or_after(self, ts_ms: int) -> int:
        return int(np.searchsorted(self.ts, ts_ms, side="left"))


def prepare(df: pd.DataFrame) -> NQ:
    df = back_adjust(df)
    idx = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(NY)
    m1 = df.set_index(pd.DatetimeIndex(idx))[["ts", "open", "high", "low", "close", "volume"]]
    session = (m1.index + pd.Timedelta(hours=6)).date          # 18:00 ET → sesión del día siguiente
    minute = (m1.index.hour * 60 + m1.index.minute).to_numpy()
    return NQ(m1, m1["ts"].to_numpy(np.int64), m1["open"].to_numpy(float), m1["high"].to_numpy(float),
              m1["low"].to_numpy(float), m1["close"].to_numpy(float),
              np.array(session, dtype="datetime64[D]"), minute)


def resample(m1: pd.DataFrame, rule: str) -> list[dict]:
    """Velas para el motor de bot/strategy.py: [{time(ms UTC de apertura), open, high, low, close, volume}]."""
    agg = m1.resample(rule, label="left", closed="left").agg(
        {"ts": "first", "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    start_ms = (agg.index.tz_convert("UTC") - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)
    return [{"time": int(t), "open": o, "high": h, "low": l, "close": c, "volume": v}
            for t, o, h, l, c, v in zip(start_ms, agg["open"], agg["high"], agg["low"], agg["close"], agg["volume"])]


def daily_sessions(nq: NQ) -> pd.DataFrame:
    """Vela diaria por sesión (18:00 → 17:00 ET). time = ms UTC de la primera vela de la sesión."""
    g = nq.m1.assign(session=nq.session).groupby("session")
    d = pd.DataFrame({"time": g["ts"].first(), "open": g["open"].first(), "high": g["high"].max(),
                      "low": g["low"].min(), "close": g["close"].last(), "volume": g["volume"].sum()})
    d.index = pd.to_datetime(d.index)
    tr = np.maximum(d["high"] - d["low"], np.maximum((d["high"] - d["close"].shift()).abs(),
                                                     (d["low"] - d["close"].shift()).abs()))
    d["atr14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["atr14_prev"] = d["atr14"].shift()        # ATR conocido ANTES de que empiece la sesión
    return d


def rth_bounds(nq: NQ) -> dict:
    """{fecha de sesión: (índice de la vela 9:30, índice de la última vela antes de las 16:00)} sólo RTH."""
    mask = (nq.minute >= 9 * 60 + 30) & (nq.minute < 16 * 60)
    idx = np.flatnonzero(mask)
    out = {}
    if len(idx) == 0:
        return out
    sess = nq.session[idx]
    cuts = np.flatnonzero(sess[1:] != sess[:-1]) + 1
    for part in np.split(idx, cuts):
        out[pd.Timestamp(nq.session[part[0]])] = (int(part[0]), int(part[-1]))
    return out
