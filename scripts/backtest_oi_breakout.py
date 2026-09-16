"""
scripts/backtest_oi_breakout.py
Replica la regla del "Agente de trading con IA 24/7" de Binance:
  "Escanea perpetuos UM → detecta ACUMULACIÓN DE OPEN INTEREST + PRECIO REZAGADO
   → abre LONG en la ruptura."

Traducido a regla testeable (1h, long-only):
  - acumulación OI: OI[t]/OI[t-N] - 1 > oi_thr   (entra dinero)
  - precio rezagado: retorno de precio en la misma ventana < oi_change*lag_ratio
                     (el precio NO siguió al OI todavía)
  - ruptura: close > máximo de las últimas brk velas (excluye la actual)
  - entrada LONG market; SL = sl_atr*ATR, TP = tp_atr*ATR
  - costos reales (taker+slippage por lado)

LIMITACIÓN DURA: Binance solo da ~30 días de OI por API pública. Esto NO es un
WFA — es un smell test sobre una muestra ínfima (anécdota, no evidencia). Sirve
para ver si el filtro de OI siquiera apunta en la dirección correcta.

Uso:
    python scripts/backtest_oi_breakout.py
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import ccxt
import numpy as np
import pandas as pd
import ta

UNIVERSE = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
            "BNB/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT"]


def fetch_price(ex, sym, tf, limit):
    o = ex.fetch_ohlcv(sym, timeframe=tf, limit=limit)
    df = pd.DataFrame(o, columns=["ts", "open", "high", "low", "close", "vol"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df.set_index("ts")


def fetch_oi(ex, sym, tf, limit=500):
    oi = ex.fetch_open_interest_history(sym, timeframe=tf, limit=limit)
    s = pd.Series({pd.Timestamp(r["timestamp"], unit="ms"): float(r["openInterestAmount"]) for r in oi})
    return s[~s.index.duplicated()].sort_index()


def backtest_symbol(df, oi, brk, oi_n, oi_thr, lag_ratio, sl_atr, tp_atr,
                    vol_mult, fee_side, leverage, risk_pct):
    df = df.copy()
    df["oi"] = oi.reindex(df.index, method="ffill")
    df = df.dropna(subset=["oi"])
    if len(df) < max(brk, oi_n) + 5:
        return []
    c = df["close"].values; h = df["high"].values; l = df["low"].values
    o = df["open"].values; v = df["vol"].values; oiv = df["oi"].values
    atr = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range().values
    hh = df["high"].rolling(brk).max().shift(1).values
    volavg = df["vol"].rolling(20).mean().shift(1).values
    trades = []
    in_pos = False
    entry = stop = tp = 0.0; eq_at = 1.0
    for i in range(max(brk, oi_n) + 1, len(df)):
        if in_pos:
            # salida intrabar: SL primero (conservador), luego TP
            if l[i] <= stop:
                pnl = (stop / entry - 1) * leverage - 2 * fee_side * leverage
                trades.append(pnl); in_pos = False
            elif h[i] >= tp:
                pnl = (tp / entry - 1) * leverage - 2 * fee_side * leverage
                trades.append(pnl); in_pos = False
            continue
        if np.isnan(hh[i]) or np.isnan(atr[i]) or atr[i] == 0:
            continue
        oi_change = oiv[i] / oiv[i - oi_n] - 1 if oiv[i - oi_n] > 0 else 0
        price_change = c[i] / c[i - oi_n] - 1
        acc = oi_change > oi_thr
        lagging = price_change < oi_change * lag_ratio   # precio no siguió al OI
        breakout = c[i] > hh[i]
        volok = (vol_mult <= 0) or (not np.isnan(volavg[i]) and v[i] > vol_mult * volavg[i])
        if acc and lagging and breakout and volok:
            entry = c[i]; stop = entry - sl_atr * atr[i]; tp = entry + tp_atr * atr[i]
            in_pos = True
    return trades


def summarize(all_trades, label):
    n = len(all_trades)
    if n == 0:
        print(f"{label:<28} sin trades en la muestra")
        return
    arr = np.array(all_trades)
    wins = arr[arr > 0]; losses = arr[arr <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.size and losses.sum() != 0 else (99 if wins.size else 0)
    wr = len(wins) / n * 100
    # equity compuesto simple por trade (riesgo fijo aprox: aplicamos el % de PnL sobre notional/leverage)
    eq = np.cumprod(1 + arr)
    tot = (eq[-1] - 1) * 100
    print(f"{label:<28} N={n:>3}  WR={wr:>4.0f}%  PF={pf:>5.2f}  retΣ(comp)={tot:>+7.1f}%  "
          f"avg={arr.mean()*100:>+5.2f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--brk", type=int, default=24)
    ap.add_argument("--oi-n", type=int, default=24)
    ap.add_argument("--oi-thr", type=float, default=0.03, help="OI debe subir > X en oi_n velas")
    ap.add_argument("--lag-ratio", type=float, default=0.5, help="precio_chg < oi_chg*lag_ratio")
    ap.add_argument("--sl-atr", type=float, default=1.5)
    ap.add_argument("--tp-atr", type=float, default=3.0)
    ap.add_argument("--vol-mult", type=float, default=1.5)
    ap.add_argument("--fee", type=float, default=0.0008, help="taker+slip por lado")
    ap.add_argument("--leverage", type=float, default=7.0)
    args = ap.parse_args()

    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    print(f"Smell test OI-breakout long  | tf={args.tf} brk={args.brk} oi_n={args.oi_n} "
          f"oi_thr={args.oi_thr:.0%} lag={args.lag_ratio} SL={args.sl_atr}ATR TP={args.tp_atr}ATR "
          f"vol×{args.vol_mult} lev{args.leverage}x fee{args.fee:.2%}/lado")
    print("⚠  Muestra = ~20 días de OI (límite API Binance). NO es evidencia, es anécdota.\n")

    pooled = []
    per_symbol = {}
    for sym in UNIVERSE:
        try:
            df = fetch_price(ex, sym, args.tf, 500)
            oi = fetch_oi(ex, sym, args.tf, 500)
            tr = backtest_symbol(df, oi, args.brk, args.oi_n, args.oi_thr, args.lag_ratio,
                                 args.sl_atr, args.tp_atr, args.vol_mult, args.fee,
                                 args.leverage, 0.05)
            per_symbol[sym] = tr
            pooled += tr
            span = f"{df.index[0].date()}→{df.index[-1].date()}"
            print(f"  {sym:<16} ({span}, {len(df)} velas, OI {len(oi)})")
        except Exception as e:
            print(f"  {sym:<16} ERROR {e}")
    print()
    for sym, tr in per_symbol.items():
        summarize(tr, sym)
    print("-" * 70)
    summarize(pooled, "POOL (todos los símbolos)")
    print("\nRecordatorio: con esta muestra, cualquier resultado (bueno o malo) es")
    print("estadísticamente insignificante. El veredicto real va por la tasa base")
    print("del breakout-long a 5 años (ya refutada) + la matemática del fee.")


if __name__ == "__main__":
    main()
