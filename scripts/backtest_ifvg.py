"""
scripts/backtest_ifvg.py
Captura y VALIDA el modelo estrella de Fede Esses (ICT/SMC): el Inverse Fair
Value Gap (IFVG) con barrido de liquidez. Antes de implementarlo en el bot hay
que pasarlo por el MISMO gate que reprobaron PA y VE: WFA 5a + costos reales.

Lógica mecanizada (long; short es simétrico):
  1. BARRIDO DE LIQUIDEZ sell-side: el low de una vela perfora el mínimo de las
     últimas `swing_n` velas y CIERRA por encima (mecha que caza stops y vuelve).
  2. Tras el barrido, dentro de `window` velas, se forma un FVG BAJISTA
     (3 velas: high[k] < low[k-2]) durante el retroceso.
  3. INVERSIÓN: una vela posterior CIERRA por encima del techo del FVG bajista
     (low[k-2]) → el FVG se invalida y se vuelve IFVG alcista → ENTRADA LONG.
  4. SL = mínimo del barrido − buffer·ATR.  TP = entry + RR·(entry−SL).
  5. Filtros opcionales: bias EMA (solo a favor de tendencia) y sesión horaria.

Costos reales (taker+slippage por lado, leverage). Una posición a la vez.
Anti-lookahead: todo se decide con datos hasta la vela de entrada inclusive;
la gestión SL/TP se evalúa en velas posteriores (SL primero = conservador).

Uso:
    python scripts/backtest_ifvg.py --tf 1h --years 5 --rr 2
    python scripts/backtest_ifvg.py --tf 15m --years 3 --rr 2 --bias-ema 200
    python scripts/backtest_ifvg.py --tf 1h --years 5 --wfa
"""

import argparse
import math
import statistics
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


def find_trades(df, swing_n, window, rr, sl_buf, bias_ema, sess_start, sess_end,
                fee_side, leverage):
    o = df["open"].values; h = df["high"].values
    l = df["low"].values; c = df["close"].values
    n = len(df)
    atr = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range().values
    ema = df["close"].ewm(span=bias_ema, adjust=False).mean().values if bias_ema > 0 else None
    hours = df.index.hour.values
    liq_low = pd.Series(l).rolling(swing_n).min().shift(1).values
    liq_high = pd.Series(h).rolling(swing_n).max().shift(1).values

    def in_session(i):
        if sess_start < 0:
            return True
        hh = hours[i]
        if sess_start <= sess_end:
            return sess_start <= hh < sess_end
        return hh >= sess_start or hh < sess_end

    trades = []
    n_sweep = n_fvg = n_entry = 0
    i = max(swing_n, bias_ema, 20) + 3
    while i < n:
        entered = False
        # ---- LONG: barrido sell-side ----
        if not np.isnan(liq_low[i]) and l[i] < liq_low[i] and c[i] > liq_low[i]:
            n_sweep += 1
            sweep_low = l[i]
            # buscar FVG bajista + inversión dentro de la ventana
            fvg_top = None
            for k in range(i + 2, min(i + window, n)):
                if fvg_top is None:
                    # FVG bajista formándose: high[k] < low[k-2]
                    if h[k] < l[k - 2]:
                        fvg_top = l[k - 2]; n_fvg += 1
                else:
                    # inversión: cierre por encima del techo del FVG bajista
                    if c[k] > fvg_top:
                        if bias_ema > 0 and not (c[k] > ema[k]):
                            break
                        if not in_session(k):
                            break
                        entry = c[k]
                        sl = sweep_low - sl_buf * (atr[k] if not np.isnan(atr[k]) else 0)
                        if sl >= entry:
                            break
                        tp = entry + rr * (entry - sl)
                        n_entry += 1
                        # gestionar
                        pnl = _manage(h, l, k + 1, n, entry, sl, tp, "LONG", fee_side, leverage)
                        if pnl is not None:
                            trades.append((df.index[k], "LONG", pnl))
                            i = k + 1; entered = True
                        break
        # ---- SHORT: barrido buy-side ----
        if not entered and not np.isnan(liq_high[i]) and h[i] > liq_high[i] and c[i] < liq_high[i]:
            n_sweep += 1
            sweep_high = h[i]
            fvg_bot = None
            for k in range(i + 2, min(i + window, n)):
                if fvg_bot is None:
                    # FVG alcista formándose: low[k] > high[k-2]
                    if l[k] > h[k - 2]:
                        fvg_bot = h[k - 2]; n_fvg += 1
                else:
                    if c[k] < fvg_bot:
                        if bias_ema > 0 and not (c[k] < ema[k]):
                            break
                        if not in_session(k):
                            break
                        entry = c[k]
                        sl = sweep_high + sl_buf * (atr[k] if not np.isnan(atr[k]) else 0)
                        if sl <= entry:
                            break
                        tp = entry - rr * (sl - entry)
                        n_entry += 1
                        pnl = _manage(h, l, k + 1, n, entry, sl, tp, "SHORT", fee_side, leverage)
                        if pnl is not None:
                            trades.append((df.index[k], "SHORT", pnl))
                            i = k + 1; entered = True
                        break
        if not entered:
            i += 1
    return trades, dict(sweeps=n_sweep, fvgs=n_fvg, entries=n_entry)


def _manage(h, l, start, n, entry, sl, tp, direction, fee_side, leverage):
    """Devuelve PnL fraccional (sobre equity, ya con leverage y fees) o None si no cerró."""
    for j in range(start, n):
        if direction == "LONG":
            if l[j] <= sl:
                return (sl / entry - 1) * leverage - 2 * fee_side * leverage
            if h[j] >= tp:
                return (tp / entry - 1) * leverage - 2 * fee_side * leverage
        else:
            if h[j] >= sl:
                return (entry / sl - 1) * leverage - 2 * fee_side * leverage
            if l[j] <= tp:
                return (entry / tp - 1) * leverage - 2 * fee_side * leverage
    return None  # quedó abierta al final → descartar


def metrics(trades):
    pnls = np.array([t[2] for t in trades]) if trades else np.array([])
    n = len(pnls)
    if n == 0:
        return dict(n=0, ret=0, cagr=0, sharpe=0, sortino=0, dd=0, pf=0, wr=0, expectancy=0)
    eq = np.cumprod(1 + pnls)
    ret = (eq[-1] - 1) * 100
    wins = pnls[pnls > 0]; losses = pnls[pnls <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.size and losses.sum() != 0 else (99 if wins.size else 0)
    wr = len(wins) / n * 100
    peak = np.maximum.accumulate(eq); dd = ((peak - eq) / peak).max() * 100
    # Sharpe por-trade (no anualizado de forma estricta; comparativo)
    sharpe = pnls.mean() / pnls.std() * math.sqrt(n) if pnls.std() > 0 else 0
    dn = pnls[pnls < 0].std()
    sortino = pnls.mean() / dn * math.sqrt(n) if dn and dn > 0 else 0
    return dict(n=n, ret=ret, sharpe=sharpe, sortino=sortino, dd=dd, pf=pf, wr=wr,
                expectancy=pnls.mean() * 100)


def run_once(df, args, label=""):
    trades, diag = find_trades(df, args.swing_n, args.window, args.rr, args.sl_buf,
                               args.bias_ema, args.sess_start, args.sess_end,
                               args.fee, args.leverage)
    m = metrics(trades)
    print(f"{label}sweeps={diag['sweeps']} fvgs={diag['fvgs']} entries={diag['entries']} "
          f"cerrados={m['n']}")
    print(f"  Ret(comp)={m['ret']:+.0f}%  PF={m['pf']:.2f}  WR={m['wr']:.0f}%  "
          f"MaxDD={m['dd']:.0f}%  Sharpe(tr)={m['sharpe']:.2f}  Exp={m['expectancy']:+.2f}%/trade  N={m['n']}")
    return trades, m


def run_wfa(df, args):
    # ventanas IS/OS por nº de velas equivalentes a 540/180 días
    bars_per_day = {"1m": 1440, "5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}[args.tf]
    is_n = 540 * bars_per_day; os_n = 180 * bars_per_day
    n = len(df)
    grid_rr = [1.5, 2.0, 2.5, 3.0]
    windows = []
    s = 0
    while s + is_n + os_n <= n:
        windows.append((s, s + is_n, s + is_n + os_n)); s += os_n
    if not windows:
        print("  No alcanzan los datos para una ventana IS+OS completa."); return
    print(f"  WFA: {len(windows)} ventanas (IS 540d / OS 180d), optimizando RR en {grid_rr}\n")
    print(f"{'OS range':<18}{'RR*':>5}{'OS ret%':>9}{'OS PF':>7}{'OS WR':>7}{'N':>5}")
    print("-" * 51)
    os_rets, os_pfs = [], []
    for (a, b, cc) in windows:
        sub_is = df.iloc[a:b]
        best = None
        for rr in grid_rr:
            args.rr = rr
            tr, _ = find_trades(sub_is, args.swing_n, args.window, rr, args.sl_buf,
                                args.bias_ema, args.sess_start, args.sess_end, args.fee, args.leverage)
            mm = metrics(tr)
            if mm["n"] >= 5 and (best is None or mm["ret"] > best[1]["ret"]):
                best = (rr, mm)
        if best is None:
            best = (2.0, {"ret": 0})
        rr = best[0]; args.rr = rr
        sub_os = df.iloc[b:cc]
        tro, _ = find_trades(sub_os, args.swing_n, args.window, rr, args.sl_buf,
                             args.bias_ema, args.sess_start, args.sess_end, args.fee, args.leverage)
        om = metrics(tro)
        os_rets.append(om["ret"]); os_pfs.append(om["pf"])
        rng = f"{sub_os.index[0].strftime('%y-%m')}->{sub_os.index[-1].strftime('%y-%m')}"
        print(f"{rng:<18}{rr:>5.1f}{om['ret']:>+9.1f}{om['pf']:>7.2f}{om['wr']:>7.0f}{om['n']:>5}")
    print("-" * 51)
    pos = sum(1 for r in os_rets if r > 0)
    print(f"\nOS positivas:      {pos}/{len(os_rets)} ({pos/len(os_rets)*100:.0f}%)")
    print(f"OS retorno total:  {sum(os_rets):+.1f}%")
    print(f"OS PF mediano:     {statistics.median(os_pfs):.2f}")
    ok = pos / len(os_rets) >= 0.6 and statistics.median(os_pfs) > 1.0 and sum(os_rets) > 0
    print(f"\nVEREDICTO WFA: {'✅ PASA' if ok else '❌ NO PASA'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--swing-n", type=int, default=20, help="lookback del pool de liquidez")
    ap.add_argument("--window", type=int, default=12, help="velas tras el barrido para formar IFVG")
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--sl-buf", type=float, default=0.25, help="buffer del SL en ATR bajo el barrido")
    ap.add_argument("--bias-ema", type=int, default=0, help="EMA de bias (0=off, ej 200)")
    ap.add_argument("--sess-start", type=int, default=-1, help="hora UTC inicio sesión (-1=24h)")
    ap.add_argument("--sess-end", type=int, default=-1, help="hora UTC fin sesión")
    ap.add_argument("--fee", type=float, default=0.0008, help="taker+slippage por lado")
    ap.add_argument("--leverage", type=float, default=7.0)
    ap.add_argument("--wfa", action="store_true")
    args = ap.parse_args()

    days = int(args.years * 365)
    print(f"Bajando ~{args.years}a de {args.symbol} ({args.tf})...")
    df = fetch_history(args.symbol, args.tf, days)
    print(f"  {len(df)} velas ({df.index[0].date()}->{df.index[-1].date()})  "
          f"lev {args.leverage}x  fee {args.fee:.3%}/lado  swing{args.swing_n} win{args.window} "
          f"SLbuf{args.sl_buf}ATR bias{args.bias_ema or 'off'}\n")

    if args.wfa:
        run_wfa(df, args)
        return

    run_once(df, args, label="FULL-SAMPLE  ")


if __name__ == "__main__":
    main()
