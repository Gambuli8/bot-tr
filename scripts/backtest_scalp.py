"""
scripts/backtest_scalp.py
SCALPING intradía sobre BTC/USD 1-minuto (Bitstamp, datos reales de GitHub).
Costos REALISTAS de Binance Futures: taker 0.04%/lado + slippage por cruce de
spread. Leverage configurable. Margen fijo por operación.

Objetivo: ver el numero REAL del scalping con leverage, sin humo.
"""
import sys, gzip
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np

GZ = Path("/tmp/cryptocache/btc1min.csv.gz")


def load_1min(last_n=700_000):
    raw = gzip.decompress(GZ.read_bytes())
    lines = raw.split(b"\n")
    hdr = lines[0]
    body = [l for l in lines[1:] if l]
    body = body[-last_n:]
    ts = np.empty(len(body)); op = np.empty(len(body)); hi = np.empty(len(body))
    lo = np.empty(len(body)); cl = np.empty(len(body))
    for i, l in enumerate(body):
        p = l.split(b",")
        ts[i] = float(p[0]); op[i] = float(p[1]); hi[i] = float(p[2])
        lo[i] = float(p[3]); cl[i] = float(p[4])
    return ts, op, hi, lo, cl


def scalp(ts, op, hi, lo, cl, *, lb=15, tp=0.003, sl=0.002, hold=30,
          fee=0.0004, slip=0.0002, lev=10, margin=25.0, capital=300.0):
    n = len(cl)
    roll_hi = np.full(n, np.nan); roll_lo = np.full(n, np.nan)
    # maximos/minimos de las ultimas lb velas (excluyendo la actual)
    import pandas as pd
    s = pd.Series(cl)
    roll_hi = s.rolling(lb).max().shift(1).values
    roll_lo = s.rolling(lb).min().shift(1).values

    equity = capital
    cost_frac = (fee + slip) * 2          # round-trip sobre el notional
    eq_curve = [capital]
    trades = []
    liqs = 0
    i = lb + 1
    while i < n - 1:
        if equity < margin:               # sin plata para el margen
            break
        price = cl[i]
        side = 0
        if not np.isnan(roll_hi[i]) and price > roll_hi[i]:
            side = 1
        elif not np.isnan(roll_lo[i]) and price < roll_lo[i]:
            side = -1
        if side == 0:
            i += 1; continue
        entry = price
        notional = margin * lev
        units = notional / entry
        tp_px = entry * (1 + side * tp)
        sl_px = entry * (1 - side * sl)
        liq_px = entry * (1 - side * (1/lev - cost_frac))   # liquidacion aprox
        exit_px = None; reason = None
        j = i + 1
        end = min(i + hold, n - 1)
        while j <= end:
            h = hi[j]; lw = lo[j]
            if side == 1:
                if lw <= liq_px: exit_px, reason = liq_px, "liq"; break
                if lw <= sl_px:  exit_px, reason = sl_px, "sl"; break
                if h >= tp_px:   exit_px, reason = tp_px, "tp"; break
            else:
                if h >= liq_px: exit_px, reason = liq_px, "liq"; break
                if h >= sl_px:  exit_px, reason = sl_px, "sl"; break
                if lw <= tp_px: exit_px, reason = tp_px, "tp"; break
            j += 1
        if exit_px is None:
            exit_px, reason = cl[end], "time"
        gross = units * (exit_px - entry) * side
        fees = cost_frac * notional
        pnl = gross - fees
        if reason == "liq":
            pnl = -margin                 # perdes el margen entero
            liqs += 1
        equity += pnl
        trades.append((pnl, reason))
        eq_curve.append(equity)
        i = j + 1                          # seguir despues de cerrar

    eq = np.array(eq_curve)
    wins = sum(1 for p, _ in trades if p > 0)
    peak = np.maximum.accumulate(eq); dd = ((peak - eq) / peak).max() * 100 if len(eq) else 0
    days = (ts[-1] - ts[lb]) / 86400
    return dict(final=equity, ntr=len(trades), wr=100*wins/max(len(trades),1),
                dd=dd, liqs=liqs, days=days,
                tot_ret=(equity/capital-1)*100,
                avg=np.mean([p for p,_ in trades]) if trades else 0)


if __name__ == "__main__":
    ts, op, hi, lo, cl = load_1min(700_000)
    import datetime as dt
    print(f"Datos: {dt.datetime.utcfromtimestamp(ts[0]).date()} -> "
          f"{dt.datetime.utcfromtimestamp(ts[-1]).date()}  ({len(cl):,} velas 1min)\n")
    print("Binance Futures: taker 0.04%/lado + slip 0.02%/lado, 10x, margen $25, cap $300\n")
    grid = [(15,0.003,0.002,30),(10,0.002,0.0015,20),(20,0.005,0.003,60),
            (5,0.0015,0.001,10),(30,0.006,0.004,90),(15,0.004,0.0025,45)]
    for lb,tp,sl,hold in grid:
        r = scalp(ts,op,hi,lo,cl, lb=lb,tp=tp,sl=sl,hold=hold)
        print(f"lb={lb:>2} tp={tp*100:.2f}% sl={sl*100:.2f}% hold={hold:>2}m | "
              f"trades={r['ntr']:>5} WR={r['wr']:4.0f}% liq={r['liqs']:>3} | "
              f"$300->${r['final']:>7,.0f} ({r['tot_ret']:>+7.1f}%) DD={r['dd']:4.0f}% "
              f"avg/trade=${r['avg']:+.2f}")
