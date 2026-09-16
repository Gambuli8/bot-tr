"""
scripts/backtest_funding_neutral.py
Valida la estrategia DELTA-NEUTRAL (funding capture): long spot + short perp,
neutral al precio, cobra el funding rate cuando es positivo.

Datos: histórico real de funding rate de Binance (cada 8h).
Modela: yield bruto (funding cobrado), neto de fees, y dos variantes:
  - always-on: mantiene la posición siempre (cobra positivo, paga negativo).
  - positive-only: solo activa cuando el funding viene positivo (evita pagar).
Métricas: yield anualizado, retorno mes a mes, % meses positivos, drawdown.

NOTA honesta: el PnL de precio es ~0 (delta-neutral). El drawdown real viene de
rachas de funding negativo + basis noise. Asumimos notional = capital (uso
eficiente con leverage bajo en la pata perp); con split 50/50 el yield es ~la mitad.

Uso:
    python scripts/backtest_funding_neutral.py --years 3
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import ccxt
import pandas as pd


def fetch_funding(symbol, days):
    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    end = ex.milliseconds()
    since = end - days * 24 * 60 * 60 * 1000
    out = []
    cursor = since
    while cursor < end:
        batch = ex.fetch_funding_rate_history(symbol, since=cursor, limit=1000)
        if not batch:
            break
        out.extend(batch)
        cursor = batch[-1]["timestamp"] + 1
        if len(batch) < 1000:
            break
    s = pd.Series({pd.Timestamp(r["timestamp"], unit="ms"): float(r["fundingRate"]) for r in out})
    return s[~s.index.duplicated()].sort_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT:USDT")
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--fee", type=float, default=0.0006, help="fee round-trip por switch (ambas patas)")
    args = ap.parse_args()

    days = int(args.years * 365)
    print(f"Bajando funding de {args.symbol} (~{args.years}a)...")
    f = fetch_funding(args.symbol, days)
    print(f"  {len(f)} registros de 8h ({f.index[0].date()}→{f.index[-1].date()})\n")

    periods_per_year = 3 * 365

    # Variante 1: always-on (cobra positivo, paga negativo)
    gross_ann = f.mean() * periods_per_year * 100
    pct_pos = (f > 0).mean() * 100

    # Retorno por período = fundingRate (received si short perp). Yield sobre notional.
    ret_alwayson = f.copy()
    # Variante 2: positive-only (solo cobra cuando funding>0; 0 si no, menos fee al cambiar)
    active = f > 0
    ret_posonly = f.where(active, 0.0)
    # fee cuando cambia de activo↔inactivo
    switches = active.ne(active.shift()).sum()

    def monthly(ret_series, fee_total=0.0):
        eq = (1 + ret_series).cumprod()
        m = eq.resample("1ME").last().pct_change().dropna() * 100
        total = (eq.iloc[-1] - 1) * 100 - fee_total * 100
        return m, total

    m_all, tot_all = monthly(ret_alwayson)
    m_pos, tot_pos = monthly(ret_posonly, fee_total=switches * args.fee)

    def stats(m, tot, label):
        ann = ((1 + tot / 100) ** (1 / args.years) - 1) * 100
        # drawdown sobre equity mensual
        eq = (1 + m / 100).cumprod()
        dd = ((eq.cummax() - eq) / eq.cummax()).max() * 100
        print(f"=== {label} ===")
        print(f"  Yield anualizado:   {ann:+.1f}%/año")
        print(f"  Retorno total {args.years:.0f}a: {tot:+.1f}%")
        print(f"  Meses positivos:    {(m>0).mean()*100:.0f}%")
        print(f"  Mes promedio:       {m.mean():+.2f}%  | mediana {m.median():+.2f}%")
        print(f"  Mejor / peor mes:   {m.max():+.2f}% / {m.min():+.2f}%")
        print(f"  Max drawdown:       {dd:.1f}%")
        print()

    print(f"Funding bruto anualizado promedio: {gross_ann:+.1f}%/año | % períodos positivos: {pct_pos:.0f}%\n")
    stats(m_all, tot_all, "ALWAYS-ON (long spot + short perp siempre)")
    stats(m_pos, tot_pos, f"POSITIVE-ONLY (activa solo con funding>0, {switches} switches)")

    print("Últimos 12 meses (always-on), mes a mes:")
    for ts, v in m_all.iloc[-12:].items():
        bar = ("+" if v >= 0 else "-") * min(int(abs(v) * 20) + 1, 30)
        print(f"  {ts.strftime('%Y-%m')}  {v:>+6.2f}%  {bar}")


if __name__ == "__main__":
    main()
