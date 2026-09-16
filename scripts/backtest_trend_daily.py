"""
scripts/backtest_trend_daily.py
Búsqueda de edge durable en BTC con trend-following de TIMEFRAME DIARIO.
Tras refutar las familias de 1h (PA sweeps, VE breakout), probamos clásicos de TF
alto: Donchian/Turtle, SMA200, EMA cross, time-series momentum.

Sim basado en POSICIÓN (long/flat/short) — sin SL/TP fijo, sale por señal.
Costos reales (taker + slippage en cada cambio de posición). Métricas desde la
curva de equity DIARIA (Sharpe/Sortino/DD correctos). Anti-lookahead: la posición
se decide al cierre del día t y se aplica al retorno del día t+1.

Uso:
    python scripts/backtest_trend_daily.py --years 5 --leverage 1
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


# ── Estrategias: devuelven serie de posición objetivo (+1/0/-1) por día ──

def donchian(df, entry_n, exit_n, allow_short):
    c = df["close"].values
    up = df["high"].rolling(entry_n).max().shift(1).values
    dn_exit = df["low"].rolling(exit_n).min().shift(1).values
    dn_entry = df["low"].rolling(entry_n).min().shift(1).values
    up_exit = df["high"].rolling(exit_n).max().shift(1).values
    pos = np.zeros(len(df)); cur = 0
    for i in range(len(df)):
        if np.isnan(up[i]) or np.isnan(dn_exit[i]):
            pos[i] = cur; continue
        if cur <= 0 and c[i] > up[i]:
            cur = 1
        elif cur >= 0 and allow_short and c[i] < dn_entry[i]:
            cur = -1
        elif cur == 1 and c[i] < dn_exit[i]:
            cur = 0
        elif cur == -1 and c[i] > up_exit[i]:
            cur = 0
        pos[i] = cur
    return pd.Series(pos, index=df.index)


def sma_trend(df, n, allow_short):
    sma = df["close"].rolling(n).mean()
    pos = np.where(df["close"] > sma, 1.0, (-1.0 if allow_short else 0.0))
    pos = pd.Series(pos, index=df.index)
    pos[sma.isna()] = 0.0
    return pos


def ema_cross(df, fast, slow, allow_short):
    ef = df["close"].ewm(span=fast, adjust=False).mean()
    es = df["close"].ewm(span=slow, adjust=False).mean()
    pos = np.where(ef > es, 1.0, (-1.0 if allow_short else 0.0))
    pos = pd.Series(pos, index=df.index)
    pos.iloc[:slow] = 0.0
    return pos


def tsm(df, n, allow_short):
    mom = df["close"] / df["close"].shift(n) - 1
    pos = np.where(mom > 0, 1.0, (-1.0 if allow_short else 0.0))
    pos = pd.Series(pos, index=df.index)
    pos[mom.isna()] = 0.0
    return pos


def rsi_dip(df, rsi_buy=35, rsi_exit=55, trend_n=200):
    """Mean-reversion: comprar dips (RSI bajo) SOLO en uptrend (close>SMA). Sale por RSI alto o pierde el trend."""
    rsi = ta.momentum.RSIIndicator(df["close"], 14).rsi()
    sma = df["close"].rolling(trend_n).mean()
    c = df["close"].values; r = rsi.values; s = sma.values
    pos = np.zeros(len(df)); cur = 0
    for i in range(len(df)):
        if np.isnan(s[i]) or np.isnan(r[i]):
            pos[i] = cur; continue
        up = c[i] > s[i]
        if cur == 0 and up and r[i] < rsi_buy:
            cur = 1
        elif cur == 1 and (r[i] > rsi_exit or not up):
            cur = 0
        pos[i] = cur
    return pd.Series(pos, index=df.index)


def vol_target_sma(df, n=100, target_vol=0.50, cap=2.0):
    """Trend SMA pero escalando la posición por vol inversa (más en calma, menos en pánico)."""
    sma = df["close"].rolling(n).mean()
    realized = df["close"].pct_change().rolling(20).std() * math.sqrt(365)
    raw = (df["close"] > sma).astype(float)
    scale = (target_vol / realized).clip(upper=cap)
    pos = (raw * scale).fillna(0.0)
    pos[sma.isna()] = 0.0
    return pos


def weekly_sma(df, weeks, allow_short):
    """Trend en TF semanal mapeado a diario (sin lookahead: shift de la señal semanal)."""
    wk = df["close"].resample("1W").last()
    sma_w = wk.rolling(weeks).mean()
    sig_w = (wk > sma_w).astype(float)
    if allow_short:
        sig_w = np.where(wk > sma_w, 1.0, -1.0)
        sig_w = pd.Series(sig_w, index=wk.index)
    sig_w[sma_w.isna()] = 0.0
    # disponible recién al cierre de la semana → shift(1), luego reindex diario ffill
    daily = sig_w.shift(1).reindex(df.index, method="ffill").fillna(0.0)
    return daily


# ── Sim por posición con costos ──

def simulate(df, pos, leverage, fee_per_side, initial=1000.0):
    pos_eff = pos.shift(1).fillna(0.0).values   # actuar al día siguiente (no lookahead)
    ret = df["close"].pct_change().fillna(0.0).values
    eq = initial
    equity = np.empty(len(df)); equity[0] = eq
    trades = []
    seg_dir = 0; seg_start_eq = eq; seg_start_ts = df.index[0]
    for i in range(1, len(df)):
        dpos = abs(pos_eff[i] - pos_eff[i - 1])
        if dpos > 0:
            eq -= eq * dpos * leverage * fee_per_side   # costo del cambio de posición
        eq *= (1 + pos_eff[i] * ret[i] * leverage)
        equity[i] = eq
        # segmentar trades por cambio de posición efectiva
        if pos_eff[i] != pos_eff[i - 1]:
            if seg_dir != 0:
                trades.append({"exit_ts": df.index[i], "pnl_usdt": eq - seg_start_eq})
            seg_dir = pos_eff[i]; seg_start_eq = eq; seg_start_ts = df.index[i]
    if seg_dir != 0:
        trades.append({"exit_ts": df.index[-1], "pnl_usdt": eq - seg_start_eq})
    return equity, trades


def metrics(df, equity, trades, initial=1000.0):
    final = equity[-1]
    ret_pct = (final / initial - 1) * 100
    days = max((df.index[-1] - df.index[0]).days, 1)
    cagr = ((final / initial) ** (365.25 / days) - 1) * 100 if final > 0 else -100
    eq = pd.Series(equity, index=df.index)
    dret = eq.pct_change().dropna()
    sharpe = sortino = 0.0
    if dret.std() > 0:
        sharpe = dret.mean() / dret.std() * math.sqrt(365)
        dn = dret[dret < 0].std()
        if dn and dn > 0:
            sortino = dret.mean() / dn * math.sqrt(365)
    peak = eq.cummax(); dd = ((peak - eq) / peak).max() * 100
    pnls = [t["pnl_usdt"] for t in trades]
    wins = [x for x in pnls if x > 0]; losses = [x for x in pnls if x <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (99.0 if wins else 0.0)
    wr = len(wins) / len(pnls) * 100 if pnls else 0.0
    return dict(ret=ret_pct, cagr=cagr, sharpe=sharpe, sortino=sortino, dd=dd,
                pf=pf, wr=wr, n=len(pnls))


def _pos_for(name, df):
    """Reconstruye la serie de posición de una config long-only por nombre."""
    table = {
        "SMA50": lambda d: sma_trend(d, 50, False),
        "SMA100": lambda d: sma_trend(d, 100, False),
        "SMA150": lambda d: sma_trend(d, 150, False),
        "SMA200": lambda d: sma_trend(d, 200, False),
        "EMA50/200": lambda d: ema_cross(d, 50, 200, False),
        "TSM90": lambda d: tsm(d, 90, False),
        "Donchian55/20": lambda d: donchian(d, 55, 20, False),
    }
    return table[name](df)


def run_wfa(df, leverage, fee, is_days=540, os_days=180):
    grid = ["SMA50", "SMA100", "SMA150", "SMA200", "EMA50/200", "TSM90", "Donchian55/20"]
    # Pre-computar posiciones de cada config sobre todo el histórico (vectorizado).
    pos_all = {name: _pos_for(name, df) for name in grid}
    n = len(df)
    windows = []
    start = 0
    while start + is_days + os_days <= n:
        windows.append((start, start + is_days, start + is_days + os_days))
        start += os_days
    print(f"  WFA: {len(windows)} ventanas (IS {is_days}d / OS {os_days}d)\n")
    print(f"{'OS range':<20}{'best (IS)':<14}{'IS Shrp':>8}{'OS ret%':>9}{'OS Shrp':>9}{'OS PF':>7}")
    print("-" * 67)
    os_rets, os_sharpes, os_pfs = [], [], []
    for (a, b, c) in windows:
        sub_is = df.iloc[a:b]
        best = None
        for name in grid:
            pos = pos_all[name].iloc[a:b]
            eqc, tr = simulate(sub_is, pos, leverage, fee)
            m = metrics(sub_is, eqc, tr)
            if best is None or m["sharpe"] > best[2]["sharpe"]:
                best = (name, pos, m)
        name, _, is_m = best
        sub_os = df.iloc[b:c]
        pos_os = pos_all[name].iloc[b:c]
        eqo, tro = simulate(sub_os, pos_os, leverage, fee)
        om = metrics(sub_os, eqo, tro)
        os_rets.append(om["ret"]); os_sharpes.append(om["sharpe"]); os_pfs.append(om["pf"])
        rng = f"{sub_os.index[0].strftime('%y-%m')}→{sub_os.index[-1].strftime('%y-%m')}"
        print(f"{rng:<20}{name:<14}{is_m['sharpe']:>8.2f}{om['ret']:>+9.1f}{om['sharpe']:>9.2f}{om['pf']:>7.2f}")
    print("-" * 67)
    pos_w = sum(1 for r in os_rets if r > 0)
    import statistics
    print(f"\nVentanas OS:        {len(os_rets)}")
    print(f"OS positivas:       {pos_w}/{len(os_rets)} ({pos_w/len(os_rets)*100:.0f}%)")
    print(f"OS retorno total:   {sum(os_rets):+.1f}%")
    print(f"OS Sharpe mediano:  {statistics.median(os_sharpes):.2f}")
    print(f"OS PF mediano:      {statistics.median(os_pfs):.2f}")
    ok = pos_w / len(os_rets) >= 0.6 and statistics.median(os_sharpes) > 0.3 and sum(os_rets) > 0
    print(f"\nVEREDICTO WFA: {'✅ PASA' if ok else '❌ NO PASA'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--fee", type=float, default=0.0008, help="taker+slippage por lado (diario, 1 fill)")
    ap.add_argument("--wfa", action="store_true", help="correr walk-forward en vez del sweep")
    args = ap.parse_args()

    days = int(args.years * 365)
    print(f"Bajando ~{args.years}a de {args.symbol} (1D)...")
    df = fetch_history(args.symbol, "1d", days + 60)
    print(f"  {len(df)} velas diarias ({df.index[0].date()}→{df.index[-1].date()})  "
          f"leverage {args.leverage}x  fee {args.fee:.3%}/lado\n")

    if args.wfa:
        run_wfa(df, args.leverage, args.fee)
        return

    strats = [
        ("Donchian 20/10 LO",   donchian(df, 20, 10, False)),
        ("Donchian 20/10 L/S",  donchian(df, 20, 10, True)),
        ("Donchian 55/20 LO",   donchian(df, 55, 20, False)),
        ("Donchian 55/20 L/S",  donchian(df, 55, 20, True)),
        ("SMA200 LO",           sma_trend(df, 200, False)),
        ("SMA100 LO",           sma_trend(df, 100, False)),
        ("SMA50 LO",            sma_trend(df, 50, False)),
        ("EMA 20/50 LO",        ema_cross(df, 20, 50, False)),
        ("EMA 20/50 L/S",       ema_cross(df, 20, 50, True)),
        ("EMA 50/200 LO",       ema_cross(df, 50, 200, False)),
        ("TSM 90 LO",           tsm(df, 90, False)),
        ("SMA20 LO",            sma_trend(df, 20, False)),
        ("SMA30 LO",            sma_trend(df, 30, False)),
        ("SMA150 LO",           sma_trend(df, 150, False)),
        ("Donchian 100/50 LO",  donchian(df, 100, 50, False)),
        ("RSI dip (uptrend)",   rsi_dip(df)),
        ("VolTarget SMA100",    vol_target_sma(df, 100)),
        ("Weekly SMA20 LO",     weekly_sma(df, 20, False)),
        ("Weekly SMA10 LO",     weekly_sma(df, 10, False)),
        ("Buy & Hold",          pd.Series(1.0, index=df.index)),
    ]

    hdr = f"{'Estrategia':<22}{'Ret%':>9}{'CAGR%':>8}{'Sharpe':>8}{'Sortino':>9}{'MaxDD%':>8}{'PF':>6}{'WR%':>6}{'N':>5}"
    print(hdr); print("-" * len(hdr))
    rows = []
    for name, pos in strats:
        eqc, trades = simulate(df, pos, args.leverage, args.fee)
        m = metrics(df, eqc, trades)
        rows.append((name, m))
        print(f"{name:<22}{m['ret']:>+9.0f}{m['cagr']:>+8.1f}{m['sharpe']:>8.2f}"
              f"{m['sortino']:>9.2f}{m['dd']:>8.1f}{m['pf']:>6.2f}{m['wr']:>6.0f}{m['n']:>5}")
    print("-" * len(hdr))
    best = max(rows, key=lambda r: r[1]["sharpe"])
    print(f"\nMejor Sharpe: {best[0]} → Sharpe {best[1]['sharpe']:.2f}, "
          f"CAGR {best[1]['cagr']:+.1f}%, MaxDD {best[1]['dd']:.0f}%, PF {best[1]['pf']:.2f}")
    pos_sharpe = [r for r in rows if r[1]["sharpe"] > 0.8 and r[1]["pf"] > 1.3]
    print(f"Configs con Sharpe>0.8 y PF>1.3: {len(pos_sharpe)}/{len(rows)}")


if __name__ == "__main__":
    main()
