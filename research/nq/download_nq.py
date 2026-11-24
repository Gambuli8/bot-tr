"""
Baja velas de 1 minuto de NQ (Nasdaq-100 E-mini, contrato continuo de CME) desde Databento.

- Fuente: dataset GLBX.MDP3 (CME Globex), símbolo continuo NQ.v.0 (el vencimiento con más volumen).
  MNQ cotiza exactamente al mismo precio que NQ; sólo cambia el valor del punto ($2 en vez de $20),
  y NQ tiene más historia (MNQ existe desde mayo de 2019).
- Antes de bajar nada muestra el COSTO en USD (se descuenta del crédito gratis de Databento) y pide
  confirmación. Con --yes no pregunta.
- Guarda un archivo por año en data/nq/ (los años completos ya bajados no se vuelven a pedir).

La API key va en la variable de entorno DATABENTO_API_KEY o en el .env de la raíz del repo:
    DATABENTO_API_KEY=db-xxxxxxxx
Nunca la pegues en un chat ni la subas al repo (.env está en .gitignore).

Uso:
    python research/nq/download_nq.py                    # 2016 → ayer
    python research/nq/download_nq.py --start 2019-01-01
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "nq"
DATASET = "GLBX.MDP3"
SYMBOL = "NQ.v.0"
SCHEMA = "ohlcv-1m"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def year_ranges(start: date, end: date) -> list[tuple[int, str, str]]:
    out = []
    for y in range(start.year, end.year + 1):
        a = max(start, date(y, 1, 1))
        b = min(end, date(y + 1, 1, 1))
        if a < b:
            out.append((y, a.isoformat(), b.isoformat()))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None, help="por defecto: ayer")
    ap.add_argument("--yes", action="store_true", help="no pedir confirmación del costo")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key.startswith("db-"):
        raise SystemExit("Falta DATABENTO_API_KEY (empieza con db-). Ponela en el .env de la raíz del repo.")

    import databento as db
    import pandas as pd

    client = db.Historical(key)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    ranges = year_ranges(date.fromisoformat(args.start), end)
    OUT.mkdir(parents=True, exist_ok=True)

    pending = []
    for y, a, b in ranges:
        path = OUT / f"nq_1m_{y}.csv.gz"
        complete = path.exists() and b == date(y + 1, 1, 1).isoformat()
        if complete:
            print(f"  {y}: ya bajado")
            continue
        pending.append((y, a, b, path))
    if not pending:
        print("Todo bajado. Nada que hacer.")
        return

    total = 0.0
    for y, a, b, _ in pending:
        cost = client.metadata.get_cost(dataset=DATASET, symbols=[SYMBOL], stype_in="continuous",
                                        schema=SCHEMA, start=a, end=b)
        total += cost
        print(f"  {y}: {a} → {b} · costo US$ {cost:,.2f}")
    print(f"\nCosto total: US$ {total:,.2f} (sale del crédito de tu cuenta de Databento)")
    if not args.yes and input("¿Bajar? (s/n): ").strip().lower() not in ("s", "si", "sí", "y", "yes"):
        print("Cancelado, no se gastó nada.")
        return

    for y, a, b, path in pending:
        print(f"  bajando {y}...", flush=True)
        store = client.timeseries.get_range(dataset=DATASET, symbols=[SYMBOL], stype_in="continuous",
                                            schema=SCHEMA, start=a, end=b)
        df = store.to_df()
        if df.empty:
            print(f"  {y}: sin datos")
            continue
        df = df.reset_index()[["ts_event", "instrument_id", "open", "high", "low", "close", "volume"]]
        df["ts_event"] = (df["ts_event"] - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)  # ms UTC
        df.rename(columns={"ts_event": "ts"}).to_csv(path, index=False, compression="gzip")
        print(f"  {y}: {len(df):,} velas → {path.relative_to(ROOT)}")
    print("\nListo. Siguiente paso: python research/nq/nq_backtest.py")


if __name__ == "__main__":
    main()
