"""
scripts/wfa_multi_asset_trend.py
Prueba de robustez cross-sectional: corre la MISMA regla de trend-following con
CONFIG FIJA (sin optimizar = sin curve-fit, sin switching) sobre un universo
amplio de cripto, y reporta por activo:
  - métricas full-sample (Ret/CAGR/Sharpe/MaxDD/PF)
  - consistencia out-of-sample por ventanas de 180d (% ventanas positivas, PF mediano)
  - veredicto por activo

La pregunta: ¿el edge de trend es propio de BTC o es una propiedad general de
las majors? Si una regla fija pasa en muchos activos sin tocar parámetros, es
edge estructural, no ruido de un período/moneda.

Uso:
    python scripts/wfa_multi_asset_trend.py --sma 150 --leverage 1
    python scripts/wfa_multi_asset_trend.py --sma 100 --leverage 1 --years 5
"""

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from scripts.backtest_price_action import fetch_history
from scripts.backtest_trend_daily import sma_trend, simulate, metrics

UNIVERSE = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
            "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT",
            "DOT/USDT", "TRX/USDT", "BCH/USDT", "ATOM/USDT", "MATIC/USDT"]


def rolling_os(df, pos, leverage, fee, os_days=180):
    """Consistencia OS: corta en ventanas de os_days y mide cada una (config fija)."""
    n = len(df)
    rets, pfs = [], []
    s = 0
    while s + os_days <= n:
        sub = df.iloc[s:s + os_days]
        p = pos.iloc[s:s + os_days]
        eq, tr = simulate(sub, p, leverage, fee)
        m = metrics(sub, eq, tr)
        rets.append(m["ret"]); pfs.append(m["pf"])
        s += os_days
    if not rets:
        return None
    pos_w = sum(1 for r in rets if r > 0)
    return dict(nwin=len(rets), pos=pos_w, frac=pos_w / len(rets),
                total=sum(rets), pf_med=statistics.median(pfs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sma", type=int, default=150)
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--fee", type=float, default=0.0008)
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--coins", type=str, default=",".join(UNIVERSE))
    args = ap.parse_args()

    coins = [c.strip() for c in args.coins.split(",")]
    days = int(args.years * 365)
    print(f"Regla FIJA: SMA{args.sma} long-only diario | {args.leverage}x | fee {args.fee:.3%}/lado | ~{args.years}a\n")

    hdr = (f"{'Activo':<12}{'Vel':>5}{'Ret%':>8}{'CAGR%':>7}{'Sharpe':>7}{'MaxDD%':>7}"
           f"{'PF':>6}{'  | OSpos':>9}{'OStot%':>8}{'OSpf':>6}  Veredicto")
    print(hdr); print("-" * len(hdr))
    passed = []
    rows = []
    for c in coins:
        try:
            df = fetch_history(c, "1d", days + 60)
        except Exception as e:
            print(f"{c:<12} error fetch: {e}")
            continue
        if len(df) < args.sma + 250:
            print(f"{c:<12}{len(df):>5}  histórico insuficiente (listado tardío)")
            continue
        pos = sma_trend(df, args.sma, False)
        eq, tr = simulate(df, pos, args.leverage, args.fee)
        m = metrics(df, eq, tr)
        os = rolling_os(df, pos, args.leverage, args.fee)
        # veredicto por activo: full-sample PF>1.3 Y consistencia OS >=55% Y OS total>0
        ok = m["pf"] > 1.3 and os and os["frac"] >= 0.55 and os["total"] > 0 and m["sharpe"] > 0.5
        if ok:
            passed.append(c)
        verdict = "✅" if ok else "❌"
        osstr = f"{os['pos']}/{os['nwin']}" if os else "-"
        print(f"{c:<12}{len(df):>5}{m['ret']:>+8.0f}{m['cagr']:>+7.1f}{m['sharpe']:>7.2f}"
              f"{m['dd']:>7.0f}{m['pf']:>6.2f}{osstr:>9}{os['total'] if os else 0:>+8.0f}"
              f"{os['pf_med'] if os else 0:>6.2f}  {verdict}")
        rows.append((c, m, os))
    print("-" * len(hdr))
    print(f"\nActivos que PASAN la regla fija: {len(passed)}/{len(rows)}  →  {', '.join(passed) if passed else '(ninguno)'}")
    if rows:
        med_sharpe = statistics.median([r[1]["sharpe"] for r in rows])
        med_pf = statistics.median([r[1]["pf"] for r in rows])
        print(f"Sharpe mediano del universo: {med_sharpe:.2f} | PF mediano: {med_pf:.2f}")
        print("\nLectura: si PASAN muchos → edge estructural de trend en cripto (robusto).")
        print("Si pasa solo 1-2 → es ruido/curve-fit por activo, NO desplegable.")


if __name__ == "__main__":
    main()
