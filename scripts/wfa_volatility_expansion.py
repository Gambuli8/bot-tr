"""
scripts/wfa_volatility_expansion.py
Walk-Forward Analysis del Volatility Expansion Engine.

Ventanas rodantes: IS=6 meses (elige la mejor config por PF), OS=3 meses (la
aplica out-of-sample). Agrega OS positivas / retorno OS / PF mediano OS.
Mismo criterio que el WFA de PA → comparable.

Señales VECTORIZADAS pre-computadas por config; el Sim corre por rango de barras.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import logging
logging.getLogger("core.volatility_expansion_engine").setLevel(logging.WARNING)

import pandas as pd

from config.settings import load_settings
from core.volatility_expansion_engine import VEParams, VEDecision, compute_signals
from core.metrics import compute_metrics
from scripts.backtest_price_action import fetch_history, Sim


GRID = [  # (breakout_lookback, tp_atr)
    (24, 3.0), (24, 4.0), (48, 3.0), (48, 4.0), (48, 5.0),
    (96, 3.0), (96, 4.0), (96, 5.0),
]


def run_range(df_1h, sig, sl_atr, tp_atr, lo, hi, settings, leverage, exec_cfg) -> Sim:
    sim = Sim(settings, leverage=leverage, **exec_cfg)
    closes = df_1h["close"].values
    signals = sig["signal"].values
    atrs = sig["atr"].values
    idx = df_1h.index
    for i in range(lo, hi):
        sim.step(df_1h.iloc[i], i, idx[i])
        if sim.position is None and signals[i] != 0:
            atr = float(atrs[i])
            if atr <= 0:
                continue
            entry = float(closes[i])
            if signals[i] == 1:
                sl, tp, d, a = entry - sl_atr * atr, entry + tp_atr * atr, "LONG", "COMPRAR"
            else:
                sl, tp, d, a = entry + sl_atr * atr, entry - tp_atr * atr, "SHORT", "VENDER"
            sim.signal(VEDecision(a, d, 0.7, "VE", entry, sl, tp,
                                   abs(entry - sl) / entry, abs(tp - entry) / entry), i, idx[i])
    if sim.position is not None:
        sim.close(float(closes[hi - 1]), "Fin", hi - 1, idx[hi - 1])
    return sim


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--leverage", type=float, default=7.0)
    ap.add_argument("--risk-pct", type=float, default=0.05)
    ap.add_argument("--is-months", type=int, default=6)
    ap.add_argument("--os-months", type=int, default=3)
    args = ap.parse_args()

    s = load_settings()
    s.symbol = args.symbol
    s.max_risk_per_trade = args.risk_pct
    exec_cfg = {"taker_fee": 0.0005, "maker_fee": 0.0002, "slippage": 0.0003,
                "entry_mode": "taker"}

    days = int(args.years * 365)
    print(f"Bajando ~{args.years}a de {args.symbol}...")
    df_1h = fetch_history(args.symbol, "1h", days + 35)
    df_4h = fetch_history(args.symbol, "4h", days + 60)
    print(f"  1h: {len(df_1h)} ({df_1h.index[0].date()}→{df_1h.index[-1].date()})")

    # Pre-computar señales por breakout (lo único que cambia las señales en el grid).
    bks = sorted(set(b for b, _ in GRID))
    sigs = {}
    for b in bks:
        sigs[b] = compute_signals(df_1h, df_4h, VEParams(breakout_lookback=b))
    print(f"  señales pre-computadas para breakout={bks}")

    bpd = 24
    is_bars = args.is_months * 30 * bpd
    os_bars = args.os_months * 30 * bpd
    step = os_bars
    windows = []
    start = 0
    while start + is_bars + os_bars <= len(df_1h):
        windows.append((start, start + is_bars, start + is_bars + os_bars))
        start += step
    print(f"  ventanas WFA: {len(windows)} (IS {args.is_months}m / OS {args.os_months}m)\n")

    os_rets, os_pfs, os_dds = [], [], []
    print(f"{'OS range':<20}{'best cfg':<16}{'IS PF':>7}{'OS ret%':>9}{'OS PF':>7}{'OS n':>6}")
    print("-" * 65)
    for (a, b, c) in windows:
        # IS: elegir mejor config por PF (con n>=5).
        best = None
        for (bk, tp) in GRID:
            sim_is = run_range(df_1h, sigs[bk], 1.5, tp, a, b, s, args.leverage, exec_cfg)
            m = compute_metrics(sim_is.trades, s.initial_capital)
            if m["trades"] >= 5:
                if best is None or m["profit_factor"] > best[1]["profit_factor"]:
                    best = ((bk, tp), m)
        if best is None:
            continue
        (bk, tp), is_m = best
        sim_os = run_range(df_1h, sigs[bk], 1.5, tp, b, c, s, args.leverage, exec_cfg)
        os_m = compute_metrics(sim_os.trades, s.initial_capital)
        os_rets.append(os_m["return_pct"]); os_pfs.append((os_m["profit_factor"], os_m["trades"]))
        os_dds.append(os_m["max_drawdown_pct"])
        rng = f"{df_1h.index[b].strftime('%y-%m')}→{df_1h.index[c-1].strftime('%y-%m')}"
        print(f"{rng:<20}bk{bk}/tp{tp:<10.0f}{is_m['profit_factor']:>7.2f}"
              f"{os_m['return_pct']:>+9.1f}{os_m['profit_factor']:>7.2f}{os_m['trades']:>6}")
    print("-" * 65)

    if not os_rets:
        print("Sin ventanas válidas."); return
    pos = sum(1 for r in os_rets if r > 0)
    total = sum(os_rets)
    pf_robust = sorted(pf for pf, n in os_pfs if n >= 3)
    pf_med = pf_robust[len(pf_robust)//2] if pf_robust else 0.0
    print(f"\nVentanas OS:        {len(os_rets)}")
    print(f"OS positivas:       {pos}/{len(os_rets)} ({pos/len(os_rets)*100:.0f}%)")
    print(f"OS retorno total:   {total:+.1f}%")
    print(f"OS PF mediano(n>=3):{pf_med:.2f}")
    print(f"OS DD promedio:     {sum(os_dds)/len(os_dds):.1f}%")
    ok = pf_med >= 1.15 and pos/len(os_rets) >= 0.55 and total >= 10
    print(f"\nVEREDICTO WFA: {'✅ PASA' if ok else '❌ NO PASA'} "
          f"(criterio: PF mediano>=1.15, OS pos>=55%, OS total>=+10%)")


if __name__ == "__main__":
    main()
