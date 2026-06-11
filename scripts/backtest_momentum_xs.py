"""
scripts/backtest_momentum_xs.py
MOMENTUM CROSS-SECTIONAL (top-K) sobre una canasta cripto (diario, close-only).

Idea: en cada rebalanceo, rankear los activos por su retorno de los últimos
`lookback` días y mantener SOLO los `k` más fuertes (equal-weight), siempre que
su momentum sea positivo y estén por encima de su SMA de tendencia (filtro de
régimen). Capital que no entra queda en cash. Costos por turnover (fee+slip).
Mark-to-market diario. Long-only (spot cripto).

Esto es complementario al trend-following por breakout: en vez de tomar toda
señal, concentra el capital en los líderes relativos -> suele mejorar el Calmar.

Uso:
  python scripts/backtest_momentum_xs.py --lookback 90 --k 4 --rebal 7 --by-year
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd

from scripts.crypto_cm_data import load_basket


def run_momentum(prices: pd.DataFrame, *, lookback=90, k=4, rebal=7,
                 trend_len=100, capital=10000.0, fee_side=0.0005, slip=0.0005,
                 abs_filter=True):
    assets = list(prices.columns)
    dates = prices.index
    px = prices
    mom = px / px.shift(lookback) - 1.0          # retorno lookback
    sma = px.rolling(trend_len).mean()
    ret = px.pct_change().fillna(0.0)

    weights = pd.Series(0.0, index=assets)        # peso objetivo actual
    equity = capital
    eq_curve = []
    peak = capital
    maxdd = 0.0
    turnover_tot = 0.0
    daily_rets = []

    for di, dt in enumerate(dates):
        # retorno del día segun pesos vigentes (decididos ayer)
        day_ret = float((weights * ret.iloc[di]).sum())
        equity *= (1.0 + day_ret)
        daily_rets.append(day_ret)
        peak = max(peak, equity)
        maxdd = max(maxdd, (peak - equity) / peak if peak > 0 else 0)
        eq_curve.append((dt, equity))

        # rebalanceo
        if di >= lookback and di % rebal == 0:
            m = mom.iloc[di]
            s = sma.iloc[di]
            elig = []
            for a in assets:
                if np.isnan(m[a]) or np.isnan(px[a].iloc[di]):
                    continue
                if abs_filter and not (m[a] > 0 and px[a].iloc[di] > s[a]):
                    continue
                elig.append((a, m[a]))
            elig.sort(key=lambda x: x[1], reverse=True)
            top = [a for a, _ in elig[:k]]
            new_w = pd.Series(0.0, index=assets)
            if top:
                for a in top:
                    new_w[a] = 1.0 / k     # equal-weight; resto en cash si <k
            # costo por turnover
            turn = float((new_w - weights).abs().sum())
            turnover_tot += turn
            cost = turn * (fee_side + slip)
            equity *= (1.0 - cost)
            weights = new_w

    eq = pd.Series([e for _, e in eq_curve], index=[d for d, _ in eq_curve])
    dr = pd.Series(daily_rets, index=eq.index)
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / capital) ** (1 / yrs) - 1 if yrs > 0 else 0
    sharpe = (dr.mean() / dr.std() * np.sqrt(365)) if dr.std() > 0 else 0
    return dict(eq=eq, cagr=cagr * 100, dd=maxdd * 100,
                calmar=(cagr * 100) / (maxdd * 100) if maxdd > 0 else float("nan"),
                sharpe=sharpe, turnover=turnover_tot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", nargs="+", default=[
        "btc", "eth", "ltc", "bch", "xrp", "etc", "doge", "xlm", "xmr", "zec",
        "dash", "eos", "trx", "ada", "link", "xtz", "neo", "sol"])
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--lookback", type=int, default=90)
    ap.add_argument("--k", type=int, default=4)
    ap.add_argument("--rebal", type=int, default=7)
    ap.add_argument("--by-year", action="store_true")
    a = ap.parse_args()

    px = load_basket(a.assets, start=a.start)
    m = run_momentum(px, lookback=a.lookback, k=a.k, rebal=a.rebal)
    print(f"\nMomentum X-S  lookback={a.lookback} k={a.k} rebal={a.rebal}  "
          f"({len(px.columns)} activos)")
    print(f"  CAGR={m['cagr']:+.1f}%  maxDD={m['dd']:.1f}%  "
          f"Calmar={m['calmar']:.2f}  Sharpe={m['sharpe']:.2f}  "
          f"turnover={m['turnover']:.0f}")
    if a.by_year:
        eq = m["eq"]
        for yr, sub in eq.groupby(eq.index.year):
            r = (sub.iloc[-1] / sub.iloc[0] - 1) * 100
            pk = sub.cummax(); dd = ((pk - sub) / pk).max() * 100
            print(f"    {yr}: ret={r:>+7.1f}%  DD={dd:>4.1f}%")


if __name__ == "__main__":
    main()
