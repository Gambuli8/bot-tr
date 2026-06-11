"""
scripts/backtest_trend_port.py
PORTAFOLIO trend-following multi-activo (cripto, diario, close-only).
La diversificación entre tendencias descorrelacionadas es donde el
trend-following pasa de "ok" a "muy bueno": curva más suave, mejor Calmar.

Motor por activo: Donchian breakout sobre cierres + filtro SMA de tendencia +
trailing stop k×ATR(proxy close). Sizing por riesgo (perder stop = risk% del
equity total). Posiciones concurrentes (diversificación). Mark-to-market diario.
Costos: fee%/lado + slippage. Reporta apalancamiento bruto máximo.

Uso:
  python scripts/backtest_trend_port.py --assets btc eth sol bnb ada avax link \
      --n 20 --exit-n 10 --risk 0.01 --by-year
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


def _indicators(px, n, exit_n, trend_len, vol_len):
    don_hi = px.rolling(n).max().shift(1)
    don_lo = px.rolling(n).min().shift(1)
    ex_hi = px.rolling(exit_n).max().shift(1)
    ex_lo = px.rolling(exit_n).min().shift(1)
    sma = px.rolling(trend_len).mean().shift(1)
    tr = px.diff().abs()
    atr = tr.ewm(span=vol_len, adjust=False).mean().shift(1)
    return don_hi, don_lo, ex_hi, ex_lo, sma, atr


def run_portfolio(prices: pd.DataFrame, *, n=20, exit_n=10, trend_len=100,
                  vol_len=20, atr_stop=4.0, risk=0.01, capital=10000.0,
                  fee_side=0.0005, slip=0.0005, long_only=True,
                  max_notional_mult=3.0, max_positions=99):
    assets = list(prices.columns)
    ind = {a: _indicators(prices[a], n, exit_n, trend_len, vol_len) for a in assets}
    dates = prices.index

    cash = capital
    pos = {}  # asset -> dict(units, side, entry, stop)
    eq_curve = []
    trades = []
    peak = capital
    maxdd = 0.0
    max_lev = 0.0

    for di, dt in enumerate(dates):
        # marcar a mercado
        unreal = 0.0
        gross = 0.0
        for a, p in list(pos.items()):
            px = prices[a].iloc[di]
            if np.isnan(px):
                # datos del activo terminaron: cerrar al último precio conocido
                exit_px = p["mark"]
                pnl = p["units"] * (exit_px - p["entry"]) * p["side"] \
                    - fee_side * p["units"] * (p["entry"] + exit_px)
                cash += pnl; trades.append((pnl, pnl > 0)); del pos[a]
                continue
            p["mark"] = px
            unreal += p["units"] * p["side"] * (px - p["entry"])
            gross += p["units"] * px
        equity = cash + unreal
        max_lev = max(max_lev, gross / equity if equity > 0 else 0)
        peak = max(peak, equity)
        maxdd = max(maxdd, (peak - equity) / peak if peak > 0 else 0)
        eq_curve.append((dt, equity))

        # gestionar / abrir por activo
        for a in assets:
            px = prices[a].iloc[di]
            if np.isnan(px):
                continue
            don_hi, don_lo, ex_hi, ex_lo, sma, atr = ind[a]
            dh = don_hi.iloc[di]; dl = don_lo.iloc[di]
            xh = ex_hi.iloc[di]; xl = ex_lo.iloc[di]
            sm = sma.iloc[di]; at = atr.iloc[di]

            if a in pos:
                p = pos[a]
                if p["side"] == 1:
                    p["stop"] = max(p["stop"], px - atr_stop * at) if not np.isnan(at) else p["stop"]
                    if px <= p["stop"] or (not np.isnan(xl) and px < xl):
                        exit_px = px * (1 - slip)
                        pnl = p["units"] * (exit_px - p["entry"]) \
                            - fee_side * p["units"] * (p["entry"] + exit_px)
                        cash += pnl; trades.append((pnl, pnl > 0)); del pos[a]
                else:
                    p["stop"] = min(p["stop"], px + atr_stop * at) if not np.isnan(at) else p["stop"]
                    if px >= p["stop"] or (not np.isnan(xh) and px > xh):
                        exit_px = px * (1 + slip)
                        pnl = p["units"] * (p["entry"] - exit_px) \
                            - fee_side * p["units"] * (p["entry"] + exit_px)
                        cash += pnl; trades.append((pnl, pnl > 0)); del pos[a]

            if (a not in pos and len(pos) < max_positions and not np.isnan(dh)
                    and not np.isnan(sm) and not np.isnan(at) and at > 0):
                long_sig = px > dh and px > sm
                short_sig = (not long_only) and px < dl and px < sm
                side = 1 if long_sig else (-1 if short_sig else 0)
                if side != 0:
                    entry = px * (1 + slip * side)
                    stop = entry - side * atr_stop * at
                    sl_dist = abs(entry - stop)
                    if sl_dist > 0:
                        units = (risk * equity) / sl_dist
                        # cap de notional por posición (realismo spot/lev acotado)
                        cap_notional = max_notional_mult / max(len(assets), 1) * equity
                        if units * entry > cap_notional:
                            units = cap_notional / entry
                        pos[a] = dict(units=units, side=side, entry=entry,
                                      stop=stop, mark=entry)

    # cerrar lo abierto al final
    for a, p in list(pos.items()):
        px = prices[a].iloc[-1]
        exit_px = px * (1 - slip * p["side"])
        pnl = p["units"] * (exit_px - p["entry"]) * p["side"] \
            - fee_side * p["units"] * (p["entry"] + exit_px)
        cash += pnl; trades.append((pnl, pnl > 0))
    equity = cash

    years = (dates[-1] - dates[0]).days / 365.25
    nt = len(trades)
    wins = [p for p, w in trades if w]; losses = [p for p, w in trades if not w]
    gp = sum(wins); gl = -sum(losses)
    pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0)
    wr = (len(wins) / nt * 100) if nt else 0
    ret = (equity / capital - 1) * 100
    cagr = ((equity / capital) ** (1 / years) - 1) * 100 if years > 0 else 0
    calmar = cagr / maxdd / 100 if maxdd > 0 else 0
    return dict(equity=equity, ret=ret, cagr=cagr, pf=pf, wr=wr, dd=maxdd * 100,
                n=nt, years=years, calmar=calmar, max_lev=max_lev,
                eq=pd.Series({t: v for t, v in eq_curve}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", nargs="+",
                    default=["btc", "eth", "sol", "bnb", "ada", "avax", "link"])
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--exit-n", type=int, default=10)
    ap.add_argument("--trend-len", type=int, default=100)
    ap.add_argument("--atr-stop", type=float, default=4.0)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--long-short", action="store_true")
    ap.add_argument("--no-cost", action="store_true")
    ap.add_argument("--by-year", action="store_true")
    args = ap.parse_args()

    prices = load_basket(args.assets, start=args.start)
    print(f"\nBasket: {list(prices.columns)}  | {prices.index[0].date()} → "
          f"{prices.index[-1].date()}  ({len(prices)} días)")
    kw = dict(n=args.n, exit_n=args.exit_n, trend_len=args.trend_len,
              atr_stop=args.atr_stop, risk=args.risk, long_only=not args.long_short)
    if args.no_cost:
        kw.update(fee_side=0.0, slip=0.0)
    tag = "SIN COSTOS" if args.no_cost else "costos 0.05%/lado+slip"
    print(f"Donchian {args.n}/{args.exit_n} | SMA{args.trend_len} | stop {args.atr_stop}×ATR "
          f"| {'long+short' if args.long_short else 'long-only'} | risk {args.risk:.0%} | {tag}\n")

    m = run_portfolio(prices, **kw)
    print(f"PORTAFOLIO ({m['years']:.1f} años): CAGR={m['cagr']:+.1f}%  "
          f"ret_total={m['ret']:+.0f}%  maxDD={m['dd']:.1f}%  Calmar={m['calmar']:.2f}  "
          f"PF={m['pf']:.2f}  WR={m['wr']:.0f}%  trades={m['n']}  lev_max={m['max_lev']:.1f}×")

    if args.by_year:
        print("\nPor año (equity del portafolio):")
        eq = m["eq"]
        for yr, sub in eq.groupby(eq.index.year):
            r = (sub.iloc[-1] / sub.iloc[0] - 1) * 100
            pk = sub.cummax(); dd = ((pk - sub) / pk).max() * 100
            flag = "✅" if r > 0 else "  "
            print(f"  {yr}: ret={r:>+7.1f}%  DD_intra={dd:>4.1f}% {flag}")


if __name__ == "__main__":
    main()
