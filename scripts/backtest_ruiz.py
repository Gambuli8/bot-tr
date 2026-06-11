"""
scripts/backtest_ruiz.py
Backtest de la estrategia "pullback EMA50 + Fibonacci" de Alex Ruiz (TradingLab),
mecanizada fielmente a las reglas públicas:

  - Tendencia en 4H: precio vs EMA50(4H) + pendiente de la EMA.
  - Impulso/zona en 1H: última pierna swing (fractal) L->H (o H->L), con
    retroceso de Fibonacci. Zona de entrada ~0.618 (banda 0.50-0.705).
    Confluencia opcional con EMA50(1H).
  - Gatillo en 5m: cruce de la EMA9(5m) a favor de la tendencia dentro de la zona.
  - Stop: nivel 0.75 de Fibonacci (apenas más profundo que la entrada).
  - Take profit: R:R fijo (default 1.6, como reporta el curso).
  - Riesgo 1% por trade, compounding. Costos retail reales (spread+comisión+swap).

Sin lookahead: features de 1H/4H disponibles recién al cierre de su vela; pivotes
fractales confirmados con retardo. Una posición a la vez; un trade por pierna.

Uso:
    python scripts/backtest_ruiz.py --pair EURUSD --year 2019 --rr 1.6
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

from scripts.fx_data import load_5m, resample

PIP_MAP = {"USDJPY": 0.01, "EURJPY": 0.01, "GBPJPY": 0.01}
def pip_of(pair): return PIP_MAP.get(pair.upper(), 0.0001)


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def fractal_pivots(df, n=2):
    """Devuelve dos Series alineadas al index 1H: last_sh_val, last_sh_time,
    last_sl_val, last_sl_time — el último swing high/low CONFIRMADO disponible
    en cada barra (confirmación con retardo de n barras, sin lookahead)."""
    high = df["high"].values
    low = df["low"].values
    idx = df.index
    m = len(df)
    is_sh = np.zeros(m, bool)
    is_sl = np.zeros(m, bool)
    for i in range(n, m - n):
        win_h = high[i - n:i + n + 1]
        win_l = low[i - n:i + n + 1]
        if high[i] == win_h.max() and (win_h.argmax() == n):
            is_sh[i] = True
        if low[i] == win_l.min() and (win_l.argmin() == n):
            is_sl[i] = True
    sh_val = np.full(m, np.nan); sh_t = np.full(m, np.nan)
    sl_val = np.full(m, np.nan); sl_t = np.full(m, np.nan)
    last_shv = np.nan; last_sht = np.nan
    last_slv = np.nan; last_slt = np.nan
    for i in range(m):
        # un pivote en j se confirma (disponible) en j+n
        j = i - n
        if j >= 0:
            if is_sh[j]:
                last_shv = high[j]; last_sht = idx[j].value
            if is_sl[j]:
                last_slv = low[j]; last_slt = idx[j].value
        sh_val[i] = last_shv; sh_t[i] = last_sht
        sl_val[i] = last_slv; sl_t[i] = last_slt
    return (pd.Series(sh_val, idx), pd.Series(sh_t, idx),
            pd.Series(sl_val, idx), pd.Series(sl_t, idx))


def build_context(df5, pair):
    """Construye arrays a nivel 5m con la tendencia 4H y la pierna/zona 1H,
    disponibles sin lookahead (indexados al cierre de cada vela superior)."""
    df1 = resample(df5, "1h")
    df4 = resample(df5, "4h")

    ema50_4 = ema(df4["close"], 50)
    slope_4 = ema50_4 - ema50_4.shift(10)
    trend4 = pd.Series(0, index=df4.index)
    trend4[(df4["close"] > ema50_4) & (slope_4 > 0)] = 1
    trend4[(df4["close"] < ema50_4) & (slope_4 < 0)] = -1
    # disponible al cierre de la vela 4H -> index + 4h
    trend4.index = df4.index + pd.Timedelta(hours=4)

    ema50_1 = ema(df1["close"], 50)
    atr1 = atr(df1, 14)
    shv, sht, slv, slt = fractal_pivots(df1, n=2)
    feat1 = pd.DataFrame({
        "ema50_1": ema50_1, "atr1": atr1,
        "shv": shv, "sht": sht, "slv": slv, "slt": slt,
    }, index=df1.index)
    feat1.index = df1.index + pd.Timedelta(hours=1)  # disponible al cierre 1H

    ema9_5 = ema(df5["close"], 9)

    ctx = pd.DataFrame(index=df5.index)
    ctx["trend4"] = trend4.reindex(df5.index, method="ffill")
    f1 = feat1.reindex(df5.index, method="ffill")
    for c in feat1.columns:
        ctx[c] = f1[c]
    ctx["ema9"] = ema9_5
    ctx["o"] = df5["open"]; ctx["h"] = df5["high"]
    ctx["l"] = df5["low"]; ctx["c"] = df5["close"]
    ctx["spread"] = df5["spread"]
    return ctx


def run(df5, pair, rr=1.6, risk=0.01, capital=10000.0,
        fib_lo=0.50, fib_hi=0.705, fib_sl=0.75, leg_atr_mult=0.8,
        leg_fresh_hours=30, max_hold_bars=864, no_cost=False,
        use_ema_confluence=False, sl_mode="fib", sl_buf_atr=0.25):
    pip = pip_of(pair)
    eff_spread_floor = 0.0 if no_cost else 0.8 * pip
    commission = 0.0 if no_cost else 0.6 * pip
    swap = 0.0 if no_cost else 0.3 * pip

    ctx = build_context(df5, pair)
    t = ctx.index.asi8  # nanosegundos int64 (consistente con sht/slt = .value)
    o = ctx["o"].values; h = ctx["h"].values; lo = ctx["l"].values; c = ctx["c"].values
    ema9 = ctx["ema9"].values; spr = ctx["spread"].values
    trend = ctx["trend4"].values
    ema50_1 = ctx["ema50_1"].values; atr1 = ctx["atr1"].values
    shv = ctx["shv"].values; sht = ctx["sht"].values
    slv = ctx["slv"].values; slt = ctx["slt"].values

    n = len(ctx)
    cap = capital
    peak = capital; maxdd = 0.0
    eq = capital
    trades = []  # (pnl_net, win_bool)
    last_leg = None
    fresh_ns = leg_fresh_hours * 3600 * 1_000_000_000

    i = 1
    while i < n - 1:
        tr = trend[i]
        if tr == 0 or np.isnan(c[i]) or np.isnan(ema9[i]) or np.isnan(ema9[i - 1]):
            i += 1; continue

        L = slv[i]; H = shv[i]; Lt = slt[i]; Ht = sht[i]
        a = atr1[i]
        if np.isnan(L) or np.isnan(H) or np.isnan(a) or a <= 0:
            i += 1; continue
        R = H - L
        if R < leg_atr_mult * a:
            i += 1; continue

        entry = sl = tp = None; side = 0
        if tr == 1 and Ht > Lt:  # up-leg L->H, retroceso a la baja
            if (t[i] - Ht) > fresh_ns:
                i += 1; continue
            zlo = H - fib_hi * R; zhi = H - fib_lo * R
            in_zone = zlo <= c[i] <= zhi
            if use_ema_confluence:
                in_zone = in_zone and (c[i] <= ema50_1[i] * 1.001)
            cross_up = c[i - 1] <= ema9[i - 1] and c[i] > ema9[i]
            leg_id = (Lt, Ht)
            if in_zone and cross_up and leg_id != last_leg:
                side = 1
                entry = c[i]
                sl = (H - fib_sl * R) if sl_mode == "fib" else (L - sl_buf_atr * a)
                if sl >= entry:
                    i += 1; continue
                tp = entry + rr * (entry - sl)
                last_leg = leg_id
        elif tr == -1 and Lt > Ht:  # down-leg H->L, retroceso al alza
            if (t[i] - Lt) > fresh_ns:
                i += 1; continue
            zlo = L + fib_lo * R; zhi = L + fib_hi * R
            in_zone = zlo <= c[i] <= zhi
            if use_ema_confluence:
                in_zone = in_zone and (c[i] >= ema50_1[i] * 0.999)
            cross_dn = c[i - 1] >= ema9[i - 1] and c[i] < ema9[i]
            leg_id = (Ht, Lt)
            if in_zone and cross_dn and leg_id != last_leg:
                side = -1
                entry = c[i]
                sl = (L + fib_sl * R) if sl_mode == "fib" else (H + sl_buf_atr * a)
                if sl <= entry:
                    i += 1; continue
                tp = entry - rr * (sl - entry)
                last_leg = leg_id

        if side == 0:
            i += 1; continue

        # sizing por distancia al stop
        sl_dist = abs(entry - sl)
        units = (risk * cap) / sl_dist
        eff_spread = max(spr[i] if not np.isnan(spr[i]) else 0.0, eff_spread_floor)
        cost = units * (eff_spread + commission)

        # gestionar salida barra a barra
        exit_price = None; win = False; j = i + 1
        end = min(n, i + 1 + max_hold_bars)
        nights = 0; prev_day = None
        while j < end:
            if side == 1:
                if lo[j] <= sl:
                    exit_price = sl; win = False; break
                if h[j] >= tp:
                    exit_price = tp; win = True; break
            else:
                if h[j] >= sl:
                    exit_price = sl; win = False; break
                if lo[j] <= tp:
                    exit_price = tp; win = True; break
            j += 1
        if exit_price is None:
            j = min(end - 1, n - 1)
            exit_price = c[j]
            win = (exit_price > entry) if side == 1 else (exit_price < entry)

        nights = max(int((t[j] - t[i]) / (86400 * 1e9)), 0)
        pnl_gross = units * (exit_price - entry) * side
        pnl_net = pnl_gross - cost - units * swap * nights
        cap += pnl_net
        trades.append((pnl_net, pnl_net > 0))
        peak = max(peak, cap)
        maxdd = max(maxdd, (peak - cap) / peak)
        i = j + 1

    # métricas
    days = (ctx.index[-1] - ctx.index[0]).total_seconds() / 86400.0
    nt = len(trades)
    wins = [p for p, w in trades if w]
    losses = [p for p, w in trades if not w]
    gp = sum(p for p in wins); gl = -sum(p for p in losses)
    pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    wr = (len(wins) / nt * 100) if nt else 0.0
    ret = (cap / capital - 1) * 100
    tpm = nt / (days / 30.0) if days > 0 else 0
    return dict(ret=ret, pf=pf, wr=wr, dd=maxdd * 100, n=nt, tpm=tpm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="EURUSD")
    ap.add_argument("--year", type=int, default=2019)
    ap.add_argument("--rr", type=float, default=1.6)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--no-cost", action="store_true")
    ap.add_argument("--ema-confluence", action="store_true")
    args = ap.parse_args()
    df5 = load_5m(args.pair, args.year)
    m = run(df5, args.pair, rr=args.rr, risk=args.risk,
            no_cost=args.no_cost, use_ema_confluence=args.ema_confluence)
    tag = "SIN COSTOS" if args.no_cost else "costos retail"
    print(f"{args.pair} {args.year} | RR={args.rr} | {tag}: "
          f"ret={m['ret']:+.2f}%  PF={m['pf']:.2f}  WR={m['wr']:.0f}%  "
          f"DD={m['dd']:.1f}%  n={m['n']}  trades/mes={m['tpm']:.1f}")


if __name__ == "__main__":
    main()
