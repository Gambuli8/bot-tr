"""
scripts/backtest_trend.py
Estrategia TREND-FOLLOWING (Donchian breakout estilo Turtle) sobre cripto.
La hipótesis del repo: el edge está en CAPTURAR las tendencias fuertes de
BTC/SOL, no en scalpear. Esto lo testea de frente, con costos reales y a lo
largo de TODA la historia disponible (BTC: 2012-2025, varios bull y bear).

Reglas (pocas, fijas — sin optimizar por período):
  - Entrada LONG: close rompe el máximo de los últimos N velas (canal Donchian).
    Entrada SHORT: close rompe el mínimo de los últimos N velas.
  - Filtro de tendencia: long solo si close > SMA(trend_len); short solo si <.
  - Salida: canal opuesto de M velas (M<N) O stop ATR (k×ATR) — lo que toque
    primero. Trailing del stop a favor de la posición.
  - Sizing por riesgo: unidades tal que perder el stop = risk% del capital.
  - Costos: fee por lado sobre el notional (default 0.05% futuros) + slippage.
  - long_only opcional (spot). Compounding.

Uso:
  python scripts/backtest_trend.py --symbol BTC --tf 1d --n 20 --exit-n 10
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

from scripts.crypto_data import load_1min, resample


def atr(df, n):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def run(df, *, n=20, exit_n=10, trend_len=100, atr_len=20, atr_stop=3.0,
        risk=0.01, capital=10000.0, fee_side=0.0005, slip=0.0005,
        long_only=False):
    c = df["close"].values
    h = df["high"].values
    lo = df["low"].values
    nbar = len(df)

    don_hi = pd.Series(h).rolling(n).max().shift(1).values   # canal entrada (sin la actual)
    don_lo = pd.Series(lo).rolling(n).min().shift(1).values
    ex_hi = pd.Series(h).rolling(exit_n).max().shift(1).values
    ex_lo = pd.Series(lo).rolling(exit_n).min().shift(1).values
    sma = pd.Series(c).rolling(trend_len).mean().shift(1).values
    a = atr(df, atr_len).shift(1).values

    cap = capital
    peak = capital
    maxdd = 0.0
    pos = 0           # 1 long, -1 short, 0 flat
    entry = stop = units = 0.0
    trades = []       # (pnl_net, win)
    eq_curve = []
    start = max(n, exit_n, trend_len, atr_len) + 1

    for i in range(start, nbar):
        price = c[i]
        # ---- gestión de posición abierta ----
        if pos != 0:
            # trailing stop ATR
            if pos == 1:
                stop = max(stop, price - atr_stop * a[i])
                hit_stop = lo[i] <= stop
                hit_chan = price < ex_lo[i]
                if hit_stop or hit_chan:
                    exit_px = stop if hit_stop else price
                    exit_px *= (1 - slip)
                    pnl = units * (exit_px - entry) - fee_side * units * (entry + exit_px)
                    cap += pnl
                    trades.append((pnl, pnl > 0))
                    pos = 0
            else:
                stop = min(stop, price + atr_stop * a[i])
                hit_stop = h[i] >= stop
                hit_chan = price > ex_hi[i]
                if hit_stop or hit_chan:
                    exit_px = stop if hit_stop else price
                    exit_px *= (1 + slip)
                    pnl = units * (entry - exit_px) - fee_side * units * (entry + exit_px)
                    cap += pnl
                    trades.append((pnl, pnl > 0))
                    pos = 0

        # ---- nueva entrada ----
        if pos == 0 and not np.isnan(don_hi[i]) and not np.isnan(sma[i]) and not np.isnan(a[i]):
            long_sig = price > don_hi[i] and price > sma[i]
            short_sig = (not long_only) and price < don_lo[i] and price < sma[i]
            if long_sig:
                entry = price * (1 + slip)
                stop = entry - atr_stop * a[i]
                sl_dist = entry - stop
                if sl_dist > 0:
                    units = (risk * cap) / sl_dist
                    pos = 1
            elif short_sig:
                entry = price * (1 - slip)
                stop = entry + atr_stop * a[i]
                sl_dist = stop - entry
                if sl_dist > 0:
                    units = (risk * cap) / sl_dist
                    pos = -1

        peak = max(peak, cap)
        maxdd = max(maxdd, (peak - cap) / peak)
        eq_curve.append((df.index[i], cap))

    # métricas
    years = (df.index[-1] - df.index[start]).total_seconds() / (365.25 * 86400)
    nt = len(trades)
    wins = [p for p, w in trades if w]
    losses = [p for p, w in trades if not w]
    gp = sum(wins); gl = -sum(losses)
    pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    wr = (len(wins) / nt * 100) if nt else 0.0
    total_ret = (cap / capital - 1) * 100
    cagr = ((cap / capital) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    return dict(cap=cap, ret=total_ret, cagr=cagr, pf=pf, wr=wr, dd=maxdd * 100,
                n=nt, years=years, eq=eq_curve)


def per_year(df, **kw):
    """Retorno por año natural, reiniciando capital cada año (para ver
    consistencia, no compounding multianual)."""
    out = {}
    for yr, sub in df.groupby(df.index.year):
        if len(sub) < kw.get("trend_len", 100) + 50:
            continue
        m = run(sub, **{k: v for k, v in kw.items() if k != "eq"})
        out[yr] = m
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--exit-n", type=int, default=10)
    ap.add_argument("--trend-len", type=int, default=100)
    ap.add_argument("--atr-stop", type=float, default=3.0)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--no-cost", action="store_true")
    ap.add_argument("--by-year", action="store_true")
    args = ap.parse_args()

    df1 = load_1min(args.symbol)
    df = resample(df1, args.tf)
    kw = dict(n=args.n, exit_n=args.exit_n, trend_len=args.trend_len,
              atr_stop=args.atr_stop, risk=args.risk, long_only=args.long_only)
    if args.no_cost:
        kw.update(fee_side=0.0, slip=0.0)

    tag = "SIN COSTOS" if args.no_cost else "costos (0.05%/lado+slip)"
    print(f"\nTREND-FOLLOWING {args.symbol} {args.tf} | Donchian {args.n}/{args.exit_n} "
          f"| trend SMA{args.trend_len} | stop {args.atr_stop}×ATR | "
          f"{'long-only' if args.long_only else 'long+short'} | {tag}\n")

    m = run(df, **kw)
    print(f"GLOBAL ({m['years']:.1f} años): retorno_total={m['ret']:+.0f}%  "
          f"CAGR={m['cagr']:+.1f}%  PF={m['pf']:.2f}  WR={m['wr']:.0f}%  "
          f"maxDD={m['dd']:.1f}%  trades={m['n']}")

    if args.by_year:
        print("\nPor año (capital reiniciado, mismo set de reglas):")
        py = per_year(df, **kw)
        pos = 0
        for yr, mm in sorted(py.items()):
            flag = "✅" if mm["ret"] > 0 else "  "
            if mm["ret"] > 0:
                pos += 1
            print(f"  {yr}: ret={mm['ret']:>+7.1f}%  PF={mm['pf']:>4.2f}  "
                  f"WR={mm['wr']:>3.0f}%  DD={mm['dd']:>4.1f}%  n={mm['n']:>3} {flag}")
        print(f"  → años positivos: {pos}/{len(py)}")


if __name__ == "__main__":
    main()
