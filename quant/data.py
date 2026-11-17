"""Fase 1: descarga OHLCV con CCXT, repara velas faltantes y guarda en SQLite."""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd

from quant import config as C

log = logging.getLogger("quant.data")

NETWORK_ERRORS = (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout, ccxt.DDoSProtection)


def make_exchange(host: str = C.BINANCE_DATA_HOST) -> ccxt.binance:
    """Cliente CCXT de Binance spot, solo endpoints públicos (sin claves)."""
    ex = ccxt.binance({"enableRateLimit": True, "timeout": 20_000, "options": {"defaultType": "spot",
                                                                              "fetchMarkets": {"types": ["spot"]},
                                                                              "fetchCurrencies": False}})
    if host != "api.binance.com":
        ex.urls["api"]["public"] = f"https://{host}/api/v3"
    # CCXT ignora las variables de proxy del sistema: si existen, se las pasamos explícitamente.
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        ex.https_proxy = proxy
    return ex


def with_retry(fn, *args, attempts: int = 6, base_delay: float = 2.0, **kwargs):
    """Reintenta errores de red con backoff exponencial. Los errores de exchange (lógicos) no se reintentan."""
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except NETWORK_ERRORS as e:
            if i == attempts - 1:
                raise
            delay = min(base_delay * 2 ** i, 120)
            log.warning("network_error", extra={"err": f"{type(e).__name__}: {str(e)[:160]}", "retry_in_s": delay})
            time.sleep(delay)


def fetch_ohlcv_range(ex, since_ms: int, until_ms: int, symbol: str = C.SYMBOL, timeframe: str = C.TIMEFRAME,
                      limit: int = 1000) -> pd.DataFrame:
    """Pagina fetch_ohlcv desde since_ms hasta until_ms (exclusivo). Devuelve solo velas CERRADAS."""
    rows: list[list] = []
    cursor = since_ms
    while cursor < until_ms:
        batch = with_retry(ex.fetch_ohlcv, symbol, timeframe, since=cursor, limit=limit)
        if not batch:
            break
        rows.extend(batch)
        last = batch[-1][0]
        if last <= cursor:
            break
        cursor = last + 1
        if len(batch) < limit:
            break
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).drop_duplicates("ts")
    bar_ms = ex.parse_timeframe(timeframe) * 1000
    now_ms = int(time.time() * 1000)
    df = df[(df["ts"] < until_ms) & (df["ts"] + bar_ms <= now_ms)]  # fuera la vela en formación
    return df.sort_values("ts").reset_index(drop=True)


def repair_gaps(df: pd.DataFrame, bar_ms: int = C.BAR_MS) -> tuple[pd.DataFrame, int]:
    """Reindexa a una grilla regular. Las velas faltantes se rellenan planas (OHLC = cierre previo, volumen 0)
    y quedan marcadas `synthetic=1`: las estrategias no generan señales sobre ellas."""
    if df.empty:
        return df.assign(synthetic=pd.Series(dtype=int)), 0
    grid = np.arange(df["ts"].iloc[0], df["ts"].iloc[-1] + bar_ms, bar_ms, dtype=np.int64)
    out = df.set_index("ts").reindex(grid)
    missing = out["close"].isna()
    out["close"] = out["close"].ffill()
    for col in ("open", "high", "low"):
        out[col] = out[col].fillna(out["close"])
    out["volume"] = out["volume"].fillna(0.0)
    out["synthetic"] = missing.astype(int)
    out.index.name = "ts"
    return out.reset_index(), int(missing.sum())


# ── SQLite ──────────────────────────────────────────────────────────────────────────────────────────────
def _conn(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS ohlcv (symbol TEXT, timeframe TEXT, ts INTEGER, open REAL, high REAL,
                   low REAL, close REAL, volume REAL, synthetic INTEGER, PRIMARY KEY (symbol, timeframe, ts))""")
    return con


def save(df: pd.DataFrame, db: Path = C.DB_PATH, symbol: str = C.SYMBOL, timeframe: str = C.TIMEFRAME) -> None:
    with _conn(db) as con:
        con.executemany("INSERT OR REPLACE INTO ohlcv VALUES (?,?,?,?,?,?,?,?,?)",
                        [(symbol, timeframe, int(r.ts), r.open, r.high, r.low, r.close, r.volume, int(r.synthetic))
                         for r in df.itertuples(index=False)])


def load(db: Path = C.DB_PATH, symbol: str = C.SYMBOL, timeframe: str = C.TIMEFRAME) -> pd.DataFrame:
    with _conn(db) as con:
        df = pd.read_sql_query("SELECT ts, open, high, low, close, volume, synthetic FROM ohlcv "
                               "WHERE symbol=? AND timeframe=? ORDER BY ts", con, params=(symbol, timeframe))
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def sync(years: int = C.HISTORY_YEARS, db: Path = C.DB_PATH, ex=None) -> pd.DataFrame:
    """Descarga incremental: si la base ya tiene datos, baja solo lo nuevo. Re-repara la serie completa."""
    ex = ex or make_exchange()
    existing = load(db)
    now_ms = int(time.time() * 1000)
    start = now_ms - years * 365 * 24 * 3600 * 1000
    start -= start % C.BAR_MS
    if not existing.empty and existing["ts"].iloc[0] <= start + C.BAR_MS:
        start = int(existing["ts"].iloc[-1]) + C.BAR_MS
    fresh = fetch_ohlcv_range(ex, start, now_ms)
    merged = pd.concat([existing[["ts", "open", "high", "low", "close", "volume"]], fresh]) \
        .drop_duplicates("ts", keep="last").sort_values("ts")
    repaired, n_missing = repair_gaps(merged)
    save(repaired, db)
    log.info("ohlcv_synced", extra={"rows": len(repaired), "new": len(fresh), "synthetic": n_missing})
    return load(db)
