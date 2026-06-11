"""
scripts/crypto_cm_data.py
Loader de precios diarios desde el dataset comunitario de Coin Metrics en GitHub
(allowlisted; Binance/CryptoDataDownload bloqueados por el allowlist de red).

Repo: coinmetrics/data  → csv/<asset>.csv (diario, columna ReferenceRate = precio
USD compuesto). Solo close diario (no hay OHLC intradía), suficiente para un
sistema de tendencia diario sobre cierres.

API:
  load_close(asset="btc", start="2020-01-01") -> Series (index date, precio USD)
  load_basket([...]) -> DataFrame (columnas = activos, index = fechas comunes)
"""
import io
import urllib.request
from pathlib import Path

import pandas as pd

CACHE = Path("/tmp/cryptocache/cm")
CACHE.mkdir(parents=True, exist_ok=True)
BASE = "https://raw.githubusercontent.com/coinmetrics/data/master/csv/"


def load_close(asset: str, start="2020-01-01", use_cache=True) -> pd.Series:
    a = asset.lower()
    csv = CACHE / f"{a}.csv"
    if not (use_cache and csv.exists()):
        url = BASE + f"{a}.csv"
        print(f"[cm] bajando {a}.csv ...", flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=60).read()
        csv.write_bytes(data)
    df = pd.read_csv(csv, usecols=lambda c: c in (
        "time", "PriceUSD", "ReferenceRateUSD", "ReferenceRate"))
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time")
    px = None
    for col in ("PriceUSD", "ReferenceRateUSD", "ReferenceRate"):  # prioridad
        if col in df.columns and df[col].notna().sum() > 100:
            px = df[col]; break
    if px is None:
        return pd.Series(dtype=float, name=a)
    px = px.dropna()
    px = px[px.index >= pd.Timestamp(start)]
    px.name = a
    return px


def load_basket(assets, start="2020-01-01") -> pd.DataFrame:
    cols = {}
    for a in assets:
        try:
            s = load_close(a, start=start)
            if len(s) > 100:
                cols[a] = s
            else:
                print(f"[cm] {a}: muy pocos datos ({len(s)}), descartado")
        except Exception as e:
            print(f"[cm] {a}: FALLO {e}")
    df = pd.DataFrame(cols).sort_index()
    return df
