"""
scripts/fx_data.py
Loader de datos forex de Dukascopy desde GitHub (única fuente disponible en el
entorno con allowlist). Reconstruido para la TAREA 1 (price-action forex).

Repos públicos: FX-Data/FX-Data-<PAIR>-DS, con branches por año `<PAIR>-<año>`.
Cada branch trae ticks en CSV por hora: PAIR/<año>/<mes>/YYYY-MM-DD--HHh_ticks.csv
Formato de cada línea:  2019.01.02 22:03:54.650,bid,ask,bidVol,askVol  (UTC)

El loader:
  1. Baja el zip del branch del año vía codeload (cacheado en /tmp).
  2. Lo descomprime (cacheado en /tmp).
  3. Agrega los ticks a velas 5m sobre precio MID = (bid+ask)/2, guardando:
       - open/high/low/close (mid)
       - volume   = tick-count de la vela (proxy de actividad; no hay vol real en FX retail)
       - spread   = spread medio real de la vela, en precio (ask-bid)
  4. Cachea el DataFrame 5m resultante (pickle) para no reparsear.

Como cada archivo cubre exactamente 1 hora y los bins de 5m están hora-alineados,
se resamplea archivo por archivo (memoria acotada a 1 hora de ticks).

API:
  load_5m(pair="EURUSD", year=2019) -> pd.DataFrame  (index UTC, cols OHLC+volume+spread)
  resample(df5m, "1h"|"4h"|"15m"...) -> pd.DataFrame  (OHLC mid + volume + spread medio)
"""

import io
import os
import pickle
import subprocess
import sys
import zipfile
from pathlib import Path

import pandas as pd

CACHE_DIR = Path("/tmp/fxcache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TS_FMT = "%Y.%m.%d %H:%M:%S.%f"


def _zip_path(pair: str, year: int) -> Path:
    return CACHE_DIR / f"{pair}-{year}.zip"


def _extract_dir(pair: str, year: int) -> Path:
    return CACHE_DIR / f"{pair}-{year}-extracted"


def _pkl_path(pair: str, year: int) -> Path:
    return CACHE_DIR / f"{pair}_{year}_5m.pkl"


def _download_zip(pair: str, year: int) -> Path:
    zp = _zip_path(pair, year)
    if zp.exists() and zp.stat().st_size > 1_000_000:
        return zp
    url = (f"https://codeload.github.com/FX-Data/FX-Data-{pair}-DS/"
           f"zip/refs/heads/{pair}-{year}")
    print(f"[fx_data] bajando {url} ...", file=sys.stderr)
    # curl con reintentos exponenciales
    for attempt in range(5):
        rc = subprocess.run(
            ["curl", "-sfL", "-o", str(zp), url]
        ).returncode
        if rc == 0 and zp.exists() and zp.stat().st_size > 1_000_000:
            print(f"[fx_data] OK {zp.stat().st_size/1e6:.0f} MB", file=sys.stderr)
            return zp
        wait = 2 ** (attempt + 1)
        print(f"[fx_data] reintento {attempt+1} en {wait}s (rc={rc})", file=sys.stderr)
        subprocess.run(["sleep", str(wait)])
    raise RuntimeError(f"No se pudo bajar {url}")


def _extract(pair: str, year: int) -> Path:
    ed = _extract_dir(pair, year)
    marker = ed / ".done"
    if marker.exists():
        return ed
    ed.mkdir(parents=True, exist_ok=True)
    zp = _download_zip(pair, year)
    print(f"[fx_data] descomprimiendo {zp.name} ...", file=sys.stderr)
    with zipfile.ZipFile(zp) as z:
        z.extractall(ed)
    marker.write_text("ok")
    return ed


def _hourly_files(pair: str, year: int) -> list[Path]:
    ed = _extract(pair, year)
    # raíz: FX-Data-<PAIR>-DS-<PAIR>-<year>/<PAIR>/<year>/<mes>/*.csv
    files = sorted(ed.rglob(f"{pair}/{year}/*/*_ticks.csv"))
    return files


def _resample_file(path: Path) -> pd.DataFrame | None:
    """Lee 1 CSV de ticks horario y devuelve velas 5m (mid OHLC + vol + spread)."""
    try:
        df = pd.read_csv(
            path, header=None,
            names=["ts", "bid", "ask", "bidv", "askv"],
            usecols=["ts", "bid", "ask"],
            dtype={"bid": "float64", "ask": "float64"},
        )
    except Exception:
        return None
    if df.empty:
        return None
    df["ts"] = pd.to_datetime(df["ts"], format=TS_FMT, errors="coerce")
    df = df.dropna(subset=["ts"])
    if df.empty:
        return None
    df["mid"] = (df["bid"] + df["ask"]) / 2.0
    df["spread"] = df["ask"] - df["bid"]
    df = df.set_index("ts")
    g = df.resample("5min")
    out = pd.DataFrame({
        "open": g["mid"].first(),
        "high": g["mid"].max(),
        "low": g["mid"].min(),
        "close": g["mid"].last(),
        "volume": g["mid"].size(),
        "spread": g["spread"].mean(),
    })
    out = out.dropna(subset=["open"])
    out = out[out["volume"] > 0]
    return out


def load_5m(pair: str = "EURUSD", year: int = 2019, use_cache: bool = True) -> pd.DataFrame:
    """Velas 5m MID del año, con volumen (tick-count) y spread medio real."""
    pkl = _pkl_path(pair, year)
    if use_cache and pkl.exists():
        with open(pkl, "rb") as f:
            return pickle.load(f)

    files = _hourly_files(pair, year)
    if not files:
        raise RuntimeError(f"No se encontraron CSVs para {pair} {year}")
    print(f"[fx_data] parseando {len(files)} archivos horarios de {pair} {year}...",
          file=sys.stderr)
    parts = []
    for i, fp in enumerate(files):
        bar = _resample_file(fp)
        if bar is not None and not bar.empty:
            parts.append(bar)
        if (i + 1) % 500 == 0:
            print(f"[fx_data]   {i+1}/{len(files)}", file=sys.stderr)
    df = pd.concat(parts).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    with open(pkl, "wb") as f:
        pickle.dump(df, f)
    print(f"[fx_data] {len(df)} velas 5m {pair} {year} (cache: {pkl})", file=sys.stderr)
    return df


def resample(df5m: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Reagrega velas 5m a un TF mayor. spread = media del spread de las sub-velas."""
    rule = {"5m": "5min", "15m": "15min", "30m": "30min",
            "1h": "1h", "4h": "4h", "1d": "1D"}[tf]
    if tf == "5m":
        return df5m.copy()
    g = df5m.resample(rule)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
        "spread": g["spread"].mean(),
    })
    return out.dropna(subset=["open"])


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="EURUSD")
    ap.add_argument("--year", type=int, default=2019)
    args = ap.parse_args()
    d = load_5m(args.pair, args.year)
    print(d.head())
    print(d.tail())
    print(f"\nVelas 5m: {len(d)}  rango: {d.index[0]} → {d.index[-1]}")
    print(f"spread medio: {(d['spread'].mean()/0.0001):.2f} pips | "
          f"mediano: {(d['spread'].median()/0.0001):.2f} pips")
    print(f"volumen medio (ticks/5m): {d['volume'].mean():.0f}")
