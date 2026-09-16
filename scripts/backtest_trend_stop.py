"""
scripts/backtest_trend_stop.py
Motor Trend Diario Long-Only con leverage 3x, compounding total, stop ATR
(capeado para que el DD de cuenta por trade <= max_acct_dd) y trailing flexible.

Entrada: cross-up de la SMA (close cruza por encima). Sólo LONG.
Salida: (a) stop ATR, (b) trailing, (c) cross-down de la SMA. Lo que ocurra primero.
Compounding: cada entrada usa equity_actual × leverage (notional dinámico).
Costos reales (taker+slippage por lado). Fills: gap-down rellena al open.
MaxDD calculado sobre la curva de equity DIARIA (incluye drawdown intra-trade).

Uso:
    python scripts/backtest_trend_stop.py --years 5 --sma 100 --leverage 3 \
        --atr-mult 2.5 --max-acct-dd 0.12 --trail-atr 3 --trail-activate 0.10
    python scripts/backtest_trend_stop.py --years 5 --compare
    python scripts/backtest_trend_stop.py --years 5 --wfa
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
import ta

from scripts.backtest_price_action import fetch_history


def simulate(df, sma_n, leverage, fee, atr_mult, max_acct_dd, trail_atr,
             trail_activate, initial=1000.0):
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    o = df["open"].values
    sma = df["close"].rolling(sma_n).mean().values
    atr = ta.volatility.AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=14
    ).average_true_range().values
    above = c > sma

    eq = initial
    equity = np.empty(len(df)); equity[0] = eq
    in_pos = False
    entry = stop = high_since = 0.0
    entry_eq = 0.0
    trades = []
    for i in range(1, len(df)):
        if in_pos:
            high_since = max(high_since, h[i])
            # trailing: una vez extendida la ganancia, asegurar subiendo el stop
            if trail_atr > 0 and not np.isnan(atr[i]) and (high_since / entry - 1) >= trail_activate:
                new_stop = high_since - trail_atr * atr[i]
                if new_stop > stop:
                    stop = new_stop
            exit_price = None
            reason = None
            if l[i] <= stop:                       # stop / trailing tocado
                exit_price = stop if o[i] >= stop else o[i]   # gap-down → fill al open
                reason = "stop"
            elif not above[i]:                      # cross-down de la SMA
                exit_price = c[i]
                reason = "signal"
            if exit_price is not None:
                eq *= (1 + leverage * (exit_price / c[i - 1] - 1))
                eq -= eq * leverage * fee
                trades.append({"exit_ts": df.index[i], "pnl_usdt": eq - entry_eq, "reason": reason})
                in_pos = False
            else:
                eq *= (1 + leverage * (c[i] / c[i - 1] - 1))
        else:
            if above[i] and not above[i - 1] and not np.isnan(atr[i]) and atr[i] > 0:
                eq -= eq * leverage * fee
                in_pos = True
                entry = c[i]; entry_eq = eq; high_since = c[i]
                if atr_mult > 0:
                    stop_dist = min(atr_mult * atr[i], (max_acct_dd / leverage) * entry)
                else:
                    stop_dist = (max_acct_dd / leverage) * entry  # solo cap %
                stop = entry - stop_dist
        equity[i] = eq
    return equity, trades


def metrics(df, equity, trades, initial=1000.0):
    final = equity[-1]
    ret = (final / initial - 1) * 100
    days = max((df.index[-1] - df.index[0]).days, 1)
    cagr = ((final / initial) ** (365.25 / days) - 1) * 100 if final > 0 else -100
    eq = pd.Series(equity, index=df.index)
    dret = eq.pct_change().dropna()
    sharpe = dret.mean() / dret.std() * math.sqrt(365) if dret.std() > 0 else 0
    peak = eq.cummax(); dd = ((peak - eq) / peak).max() * 100
    pnls = [t["pnl_usdt"] for t in trades]
    wins = [x for x in pnls if x > 0]; losses = [x for x in pnls if x <= 0]
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else (99.0 if wins else 0.0)
    wr = len(wins) / len(pnls) * 100 if pnls else 0
    stops = sum(1 for t in trades if t["reason"] == "stop")
    return dict(ret=ret, cagr=cagr, sharpe=sharpe, dd=dd, pf=pf, wr=wr,
                n=len(pnls), final=final, stops=stops)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--sma", type=int, default=100)
    ap.add_argument("--leverage", type=float, default=3.0)
    ap.add_argument("--fee", type=float, default=0.0008)
    ap.add_argument("--atr-mult", type=float, default=2.5)
    ap.add_argument("--max-acct-dd", type=float, default=0.12)
    ap.add_argument("--trail-atr", type=float, default=3.0)
    ap.add_argument("--trail-activate", type=float, default=0.10)
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--wfa", action="store_true")
    ap.add_argument("--monthly", action="store_true", help="rentabilidad mes a mes, últimos 12m")
    ap.add_argument("--equity-points", action="store_true", help="imprime equity mensual")
    args = ap.parse_args()

    days = int(args.years * 365)
    print(f"Bajando ~{args.years}a de {args.symbol} (1D)...")
    df = fetch_history(args.symbol, "1d", days + 60)
    print(f"  {len(df)} velas ({df.index[0].date()}→{df.index[-1].date()}) lev {args.leverage}x fee {args.fee:.3%}\n")

    def run(label, **kw):
        p = dict(sma_n=args.sma, leverage=args.leverage, fee=args.fee,
                 atr_mult=args.atr_mult, max_acct_dd=args.max_acct_dd,
                 trail_atr=args.trail_atr, trail_activate=args.trail_activate)
        p.update(kw)
        eqc, tr = simulate(df, **p)
        m = metrics(df, eqc, tr)
        print(f"{label:<28}Ret {m['ret']:>+8.0f}%  CAGR {m['cagr']:>+6.1f}%  "
              f"MaxDD {m['dd']:>5.1f}%  PF {m['pf']:>5.2f}  Sharpe {m['sharpe']:>4.2f}  "
              f"N {m['n']:>3} (stops {m['stops']})")
        return eqc, tr, m

    if args.compare:
        print("COMPARACIÓN (3x, compounding, SMA100):")
        run("Sin stop ATR (solo SMA)", atr_mult=0.0, max_acct_dd=0.99, trail_atr=0.0)
        run("Stop directiva (12% cta)", atr_mult=2.5, max_acct_dd=0.12, trail_atr=3.0)
        run("Stop ATR amplio (3.5x)", atr_mult=3.5, max_acct_dd=0.30, trail_atr=4.0)
        run("Stop medio (atr2.5 cap20%)", atr_mult=2.5, max_acct_dd=0.20, trail_atr=3.0)
        return

    if args.wfa:
        run_wfa(df, args)
        return

    if args.monthly:
        run_monthly(df, args)
        return

    eqc, tr, m = run("Config directiva", )
    if args.equity_points:
        eq = pd.Series(eqc, index=df.index).resample("1ME").last().dropna()
        print("\nEquity mensual:")
        for ts, v in eq.items():
            print(f"  {ts.strftime('%Y-%m')}  {v:,.0f}")


def run_monthly(df, args, months=12):
    """Rentabilidad mes a mes de los últimos `months`, para varios leverages.
    Usa la config validada: SMA cross + stop AMPLIO de cola (no el ajustado)."""
    # Stop amplio de cola: no whipsea, solo protege la cola.
    atr_mult, cap, trail = 3.5, 0.30, 4.0
    levs = [1.0, 1.5, 2.0]
    print(f"Config: SMA{args.sma} long-only, salida por cruce, stop amplio "
          f"(3.5 ATR / cap 30%), compounding. Últimos {months} meses.\n")

    # Resumen por leverage
    print(f"{'Lev':>5}{'Ret 12m%':>10}{'Ret 6m%':>9}{'MaxDD%':>8}{'PF':>6}"
          f"{'mes+%':>7}{'mes prom%':>10}{'mejor%':>8}{'peor%':>8}")
    print("-" * 71)
    series_for_detail = None
    for lev in levs:
        eqc, tr = simulate(df, args.sma, lev, args.fee, atr_mult, cap, trail, 0.10)
        eq = pd.Series(eqc, index=df.index)
        mret = eq.resample("1ME").last().pct_change().dropna() * 100
        m12 = mret.iloc[-months:]
        m6 = mret.iloc[-6:]
        # totales compuestos
        tot12 = ((1 + m12 / 100).prod() - 1) * 100
        tot6 = ((1 + m6 / 100).prod() - 1) * 100
        # MaxDD sobre los últimos 12m de equity diaria
        cut = eq.index[-1] - pd.Timedelta(days=months * 31)
        eqw = eq[eq.index >= cut]
        dd = ((eqw.cummax() - eqw) / eqw.cummax()).max() * 100
        m = metrics(df, eqc, tr)
        pos = (m12 > 0).mean() * 100
        print(f"{lev:>5.1f}{tot12:>+10.1f}{tot6:>+9.1f}{dd:>8.1f}{m['pf']:>6.2f}"
              f"{pos:>7.0f}{m12.mean():>+10.1f}{m12.max():>+8.1f}{m12.min():>+8.1f}")
        if abs(lev - 2.0) < 1e-9:
            series_for_detail = m12

    print("-" * 71)
    print("\nDetalle mes a mes (leverage 2x):")
    for ts, v in series_for_detail.items():
        bar = ("+" if v >= 0 else "-") * min(int(abs(v) / 2) + 1, 40)
        print(f"  {ts.strftime('%Y-%m')}  {v:>+7.1f}%  {bar}")


def run_wfa(df, args, is_days=540, os_days=180):
    smas = [50, 100, 200]
    n = len(df)
    windows = []
    start = 0
    while start + is_days + os_days <= n:
        windows.append((start, start + is_days, start + is_days + os_days))
        start += os_days
    print(f"WFA stop-engine: {len(windows)} ventanas (IS {is_days}d/OS {os_days}d), "
          f"3x compounding, stop directiva\n")
    print(f"{'OS range':<18}{'SMA':>5}{'OS ret%':>9}{'OS DD%':>8}{'OS PF':>7}{'stops':>7}")
    print("-" * 54)
    os_rets, os_pfs, os_dds = [], [], []
    for (a, b, c) in windows:
        sub_is = df.iloc[a:b]; sub_os = df.iloc[b:c]
        best = None
        for sn in smas:
            eqc, tr = simulate(sub_is, sn, args.leverage, args.fee, args.atr_mult,
                               args.max_acct_dd, args.trail_atr, args.trail_activate)
            m = metrics(sub_is, eqc, tr)
            if best is None or m["sharpe"] > best[1]["sharpe"]:
                best = (sn, m)
        sn = best[0]
        eqo, tro = simulate(sub_os, sn, args.leverage, args.fee, args.atr_mult,
                            args.max_acct_dd, args.trail_atr, args.trail_activate)
        om = metrics(sub_os, eqo, tro)
        os_rets.append(om["ret"]); os_pfs.append(om["pf"]); os_dds.append(om["dd"])
        print(f"{sub_os.index[0].strftime('%y-%m'):}→{sub_os.index[-1].strftime('%y-%m'):<11}"
              f"{sn:>5}{om['ret']:>+9.1f}{om['dd']:>8.1f}{om['pf']:>7.2f}{om['stops']:>7}")
    print("-" * 54)
    import statistics
    pos = sum(1 for r in os_rets if r > 0)
    print(f"\nOS positivas:     {pos}/{len(os_rets)} ({pos/len(os_rets)*100:.0f}%)")
    print(f"OS retorno total: {sum(os_rets):+.1f}%")
    print(f"OS DD promedio:   {sum(os_dds)/len(os_dds):.1f}%  (máx {max(os_dds):.1f}%)")
    print(f"OS PF mediano:    {statistics.median(os_pfs):.2f}")


if __name__ == "__main__":
    main()
