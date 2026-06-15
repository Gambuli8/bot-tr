"""
scripts/sweep_volatility_expansion.py
Sweep adversarial del Volatility Expansion Engine: baja la data UNA vez y prueba
muchas variantes para (a) darle a la estrategia su mejor chance y (b) medir
robustez/sensibilidad. Imprime una tabla con métricas completas por config.

Uso:
    python scripts/sweep_volatility_expansion.py --years 3 --leverage 7 --risk-pct 0.05
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config.settings import load_settings
from core.volatility_expansion_engine import VEParams
from core.metrics import compute_metrics
from scripts.backtest_price_action import fetch_history, Sim
from scripts.backtest_volatility_expansion import run_backtest


def configs():
    """(label, VEParams kwargs, trailing_atr)."""
    base = dict(ema_fast=50, ema_slow=200, min_ema_sep_pct=0.001, atr_period=14,
                compression_lookback_bars=720, compression_ratio=0.70,
                breakout_lookback=24, vol_period=20, vol_mult=1.5, sl_atr=1.5, tp_atr=3.0)

    def v(**over):
        d = dict(base); d.update(over); return d

    return [
        ("base bk24 tp3",        v(),                              0.0),
        ("tp2",                  v(tp_atr=2.0),                    0.0),
        ("tp4",                  v(tp_atr=4.0),                    0.0),
        ("tp5",                  v(tp_atr=5.0),                    0.0),
        ("bk12",                 v(breakout_lookback=12),          0.0),
        ("bk48",                 v(breakout_lookback=48),          0.0),
        ("bk96 (turtle)",        v(breakout_lookback=96),          0.0),
        ("sin compresion",       v(compression_ratio=99.0),        0.0),
        ("regime fuerte",        v(min_ema_sep_pct=0.005),         0.0),
        ("vol 2.0",              v(vol_mult=2.0),                  0.0),
        ("trailing 1atr (tp5)",  v(tp_atr=5.0),                    1.0),
        ("trailing 2atr (tp5)",  v(tp_atr=5.0),                    2.0),
        ("bk48 tp5 trail2",      v(breakout_lookback=48, tp_atr=5.0), 2.0),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--leverage", type=float, default=7.0)
    ap.add_argument("--risk-pct", type=float, default=0.05)
    ap.add_argument("--taker-fee", type=float, default=0.0005)
    ap.add_argument("--maker-fee", type=float, default=0.0002)
    ap.add_argument("--slippage", type=float, default=0.0003)
    args = ap.parse_args()

    s = load_settings()
    s.symbol = args.symbol
    s.max_risk_per_trade = args.risk_pct
    exec_cfg = {"taker_fee": args.taker_fee, "maker_fee": args.maker_fee,
                "slippage": args.slippage, "entry_mode": "taker"}

    days = int(args.years * 365)
    print(f"Bajando ~{args.years}a de {args.symbol}...")
    df_1h = fetch_history(args.symbol, "1h", days + 35)
    df_4h = fetch_history(args.symbol, "4h", days + 60)
    print(f"  1h: {len(df_1h)} | 4h: {len(df_4h)} ({df_1h.index[0].date()}→{df_1h.index[-1].date()})\n")

    rows = []
    for label, kw, trail in configs():
        p = VEParams(**kw)
        sim = run_backtest(df_1h, df_4h, s, p, args.leverage, exec_cfg,
                           trailing_atr=trail, trailing_activate_r=1.0)
        m = compute_metrics(sim.trades, s.initial_capital)
        rows.append((label, m))

    hdr = f"{'Config':<22}{'Ret%':>9}{'CAGR%':>8}{'PF':>6}{'WR%':>6}{'DD%':>7}{'Sharpe':>8}{'N':>5}"
    print(hdr)
    print("-" * len(hdr))
    for label, m in rows:
        print(f"{label:<22}{m['return_pct']:>+9.1f}{m['cagr_pct']:>+8.1f}{m['profit_factor']:>6.2f}"
              f"{m['win_rate_pct']:>6.1f}{m['max_drawdown_pct']:>7.1f}{m['sharpe']:>8.2f}{m['trades']:>5}")
    print("-" * len(hdr))
    best = max(rows, key=lambda r: r[1]["profit_factor"])
    print(f"\nMejor PF: {best[0]} → PF {best[1]['profit_factor']:.2f}, "
          f"Ret {best[1]['return_pct']:+.1f}%, Sharpe {best[1]['sharpe']:.2f}")
    pos = [r for r in rows if r[1]["return_pct"] > 0 and r[1]["profit_factor"] > 1.0]
    print(f"Configs con PF>1 y Ret>0: {len(pos)}/{len(rows)}")


if __name__ == "__main__":
    main()
