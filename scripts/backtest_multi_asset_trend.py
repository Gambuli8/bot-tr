"""
scripts/backtest_multi_asset_trend.py
Aplica la MISMA regla validada (SMA long-only diario) a un universo de majors
cripto, y arma un BASKET equal-weight. Probar la misma regla en muchas monedas
es la prueba de robustez más fuerte: si funciona en varias, es edge real, no
curve-fit. El basket diversifica → mejor Sharpe / menor DD que una sola moneda.

Uso:
    python scripts/backtest_multi_asset_trend.py --years 5 --sma 150 --leverage 1
"""

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd

from scripts.backtest_price_action import fetch_history
from scripts.backtest_trend_daily import sma_trend, simulate, metrics

UNIVERSE = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--sma", type=int, default=150)
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--fee", type=float, default=0.0008)
    ap.add_argument("--coins", type=str, default=",".join(UNIVERSE))
    args = ap.parse_args()

    coins = [c.strip() for c in args.coins.split(",")]
    days = int(args.years * 365)
    data = {}
    for c in coins:
        try:
            df = fetch_history(c, "1d", days + 60)
            if len(df) > args.sma + 60:
                data[c] = df
                print(f"  {c}: {len(df)} velas ({df.index[0].date()}→{df.index[-1].date()})")
        except Exception as e:
            print(f"  {c}: error {e}")

    # Rango común (intersección de fechas) para el basket.
    common = None
    for df in data.values():
        idx = set(df.index)
        common = idx if common is None else (common & idx)
    common = sorted(common)
    print(f"\nRango común: {common[0].date()}→{common[-1].date()} ({len(common)} días), "
          f"SMA{args.sma} LO, {args.leverage}x, fee {args.fee:.3%}\n")

    n = len(data)
    sleeve_cap = 1000.0 / n
    basket_eq = pd.Series(0.0, index=pd.DatetimeIndex(common))
    all_trades = []
    rows = []
    for c, df in data.items():
        df = df.loc[df.index.isin(common)]
        pos = sma_trend(df, args.sma, False)
        eqc, tr = simulate(df, pos, args.leverage, args.fee, initial=sleeve_cap)
        m = metrics(df, eqc, tr, initial=sleeve_cap)
        rows.append((c, metrics(df, eqc, tr, initial=sleeve_cap)))
        # alinear sleeve al índice común
        s = pd.Series(eqc, index=df.index).reindex(common, method="ffill").fillna(sleeve_cap)
        basket_eq += s
        all_trades += tr

    hdr = f"{'Activo':<12}{'Ret%':>9}{'CAGR%':>8}{'Sharpe':>8}{'MaxDD%':>8}{'PF':>6}{'N':>5}"
    print(hdr); print("-" * len(hdr))
    for c, m in rows:
        print(f"{c:<12}{m['ret']:>+9.0f}{m['cagr']:>+8.1f}{m['sharpe']:>8.2f}"
              f"{m['dd']:>8.1f}{m['pf']:>6.2f}{m['n']:>5}")
    print("-" * len(hdr))

    # Métricas del basket sobre la curva total.
    bdf = pd.DataFrame(index=pd.DatetimeIndex(common))
    bdf["close"] = basket_eq.values  # truco para reusar metrics: tratamos equity como "precio"
    final = basket_eq.iloc[-1]; initial = 1000.0
    ret = (final / initial - 1) * 100
    daysd = max((common[-1] - common[0]).days, 1)
    cagr = ((final / initial) ** (365.25 / daysd) - 1) * 100
    dret = basket_eq.pct_change().dropna()
    sharpe = dret.mean() / dret.std() * math.sqrt(365) if dret.std() > 0 else 0
    dn = dret[dret < 0].std()
    sortino = dret.mean() / dn * math.sqrt(365) if dn and dn > 0 else 0
    dd = ((basket_eq.cummax() - basket_eq) / basket_eq.cummax()).max() * 100
    pnls = [t["pnl_usdt"] for t in all_trades]
    wins = [x for x in pnls if x > 0]; losses = [x for x in pnls if x <= 0]
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else 99.0
    # retorno mensual del basket
    mret = (basket_eq.resample("1ME").last().pct_change().dropna() * 100)
    last12 = ((1 + mret.iloc[-12:] / 100).prod() - 1) * 100 if len(mret) >= 12 else float("nan")
    print(f"\n{'BASKET eq-weight':<12}{ret:>+9.0f}{cagr:>+8.1f}{sharpe:>8.2f}{dd:>8.1f}{pf:>6.2f}{len(all_trades):>5}")
    print(f"\nBasket — Sortino {sortino:.2f} | meses positivos {(mret>0).mean()*100:.0f}% | "
          f"últimos 12m {last12:+.1f}% | mejor mes {mret.max():+.0f}% | peor mes {mret.min():+.0f}%")


if __name__ == "__main__":
    main()
