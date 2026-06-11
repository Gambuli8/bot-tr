"""
scripts/fetch_clean.py
Baja + parsea + LIMPIA datos forex de varios pares/años, dejando solo la pickle
5m (chica) en /tmp/fxcache. Borra zip + dir extraído tras construir cada pickle
para no llenar el disco.

Uso: python scripts/fetch_clean.py --pairs EURUSD GBPUSD ... --years 2016 2017 2018
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.fx_data import load_5m, _zip_path, _extract_dir, _pkl_path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", nargs="+", required=True)
    ap.add_argument("--years", nargs="+", type=int, required=True)
    args = ap.parse_args()
    for pair in args.pairs:
        for yr in args.years:
            pkl = _pkl_path(pair, yr)
            if pkl.exists():
                print(f"[skip] {pair} {yr} ya tiene pickle", flush=True)
                continue
            try:
                df = load_5m(pair, yr, use_cache=True)
                print(f"[ok] {pair} {yr}: {len(df)} velas 5m", flush=True)
            except Exception as e:
                print(f"[FALLO] {pair} {yr}: {e}", flush=True)
            finally:
                # limpieza: borrar zip + extraído (la pickle queda)
                z = _zip_path(pair, yr)
                ed = _extract_dir(pair, yr)
                if z.exists():
                    z.unlink()
                if ed.exists():
                    shutil.rmtree(ed, ignore_errors=True)

if __name__ == "__main__":
    main()
