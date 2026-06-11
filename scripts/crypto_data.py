"""
scripts/crypto_data.py
Loader de OHLCV cripto desde datasets en GitHub (allowlisted; Binance está
bloqueado por el allowlist de red). Fuente BTC: ff137/bitstamp-btcusd-minute-data
(BTC/USD 1-min Bitstamp, 2012-2025, limpio).

API:
  load_1min(symbol="BTC") -> DataFrame 1min (index UTC, OHLCV)
  resample(df, "1h"|"4h"|"1d"|...) -> DataFrame OHLCV
"""
import gzip
import pickle
import urllib.request
from pathlib import Path

import pandas as pd

CACHE = Path("/tmp/cryptocache")
CACHE.mkdir(parents=True, exist_ok=True)

SOURCES = {
    "BTC": ("https://raw.githubusercontent.com/ff137/bitstamp-btcusd-minute-data/"
            "main/data/historical/btcusd_bitstamp_1min_2012-2025.csv.gz"),
}


def _gz_path(sym): return CACHE / f"{sym.lower()}_1min.csv.gz"
def _pkl_path(sym): return CACHE / f"{sym.lower()}_1min.pkl"


def load_1min(symbol="BTC", use_cache=True) -> pd.DataFrame:
    sym = symbol.upper()
    pkl = _pkl_path(sym)
    if use_cache and pkl.exists():
        with open(pkl, "rb") as f:
            return pickle.load(f)
    gz = _gz_path(sym)
    if not gz.exists():
        if sym not in SOURCES:
            raise ValueError(f"sin fuente para {sym}")
        print(f"[crypto_data] bajando {sym} 1min...", flush=True)
        urllib.request.urlretrieve(SOURCES[sym], gz)
    print(f"[crypto_data] parseando {sym} 1min...", flush=True)
    with gzip.open(gz, "rt") as f:
        df = pd.read_csv(f)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    df = df[~df.index.duplicated(keep="first")].sort_index()
    with open(pkl, "wb") as f:
        pickle.dump(df, f)
    print(f"[crypto_data] {len(df)} velas 1min {sym} "
          f"({df.index[0].date()} → {df.index[-1].date()})", flush=True)
    return df


def resample(df1m: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h",
            "2h": "2h", "4h": "4h", "12h": "12h", "1d": "1D"}[tf]
    g = df1m.resample(rule)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    }).dropna()
    return out
