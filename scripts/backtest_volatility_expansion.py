"""
scripts/backtest_volatility_expansion.py
Backtest del Volatility Expansion Trend Engine — VECTORIZADO, costos reales,
métricas completas. Reutiliza el Sim del backtester de PA (mismo modelo de
ejecución: taker/maker fee + slippage).

Uso:
    python scripts/backtest_volatility_expansion.py --years 3 --leverage 7 \
        --risk-pct 0.05 --taker-fee 0.0005 --maker-fee 0.0002 --slippage 0.0003 \
        --tp-atr 3.0 --sl-atr 1.5 --breakout 24
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd

from config.settings import load_settings
from core.volatility_expansion_engine import VEParams, VEDecision, compute_signals
from core.metrics import compute_metrics, format_metrics
from scripts.backtest_price_action import fetch_history, Sim


def build_params(args) -> VEParams:
    return VEParams(
        ema_fast=args.ema_fast, ema_slow=args.ema_slow,
        min_ema_sep_pct=args.min_ema_sep,
        atr_period=14,
        compression_lookback_bars=args.compression_bars,
        compression_ratio=args.compression_ratio,
        breakout_lookback=args.breakout,
        vol_period=20, vol_mult=args.vol_mult,
        sl_atr=args.sl_atr, tp_atr=args.tp_atr,
    )


def run_backtest(df_1h, df_4h, settings, p: VEParams, leverage, exec_cfg,
                 trailing_atr: float = 0.0, trailing_activate_r: float = 1.0):
    sig = compute_signals(df_1h, df_4h, p)
    sim = Sim(settings, leverage=leverage, **exec_cfg)
    warmup = max(p.compression_lookback_bars + 5, p.ema_slow + 5, p.breakout_lookback + 5)

    closes = df_1h["close"].values
    highs = df_1h["high"].values
    lows = df_1h["low"].values
    signals = sig["signal"].values
    atrs = sig["atr"].values
    idx = df_1h.index

    for i in range(warmup, len(df_1h)):
        ts = idx[i]
        bar = df_1h.iloc[i]
        # Trailing dinámico (variante opcional) ANTES de chequear SL/TP fijo.
        if trailing_atr > 0 and sim.position is not None:
            _apply_trailing(sim, float(highs[i]), float(lows[i]),
                            float(atrs[i]), trailing_atr, trailing_activate_r)
        sim.step(bar, i, ts)
        if sim.position is None and signals[i] != 0:
            atr = float(atrs[i])
            if atr <= 0:
                continue
            entry = float(closes[i])
            if signals[i] == 1:
                sl = entry - p.sl_atr * atr
                tp = entry + p.tp_atr * atr
                direction, accion = "LONG", "COMPRAR"
            else:
                sl = entry + p.sl_atr * atr
                tp = entry - p.tp_atr * atr
                direction, accion = "SHORT", "VENDER"
            dec = VEDecision(
                accion=accion, direction=direction, confianza=0.7, razon="VE",
                entry_price=entry, stop_loss_price=sl, take_profit_price=tp,
                stop_loss_pct=abs(entry - sl) / entry,
                take_profit_pct=abs(tp - entry) / entry,
            )
            sim.signal(dec, i, ts)

    if sim.position is not None:
        sim.close(float(closes[-1]), "Fin", len(df_1h) - 1, idx[-1])
    return sim


def _apply_trailing(sim, high, low, atr, trailing_atr, activate_r):
    """Trailing por ATR: una vez en +1R, el SL sigue al precio a trailing_atr×ATR.
    Solo mueve el stop a favor (nunca en contra)."""
    pos = sim.position
    entry = pos.entry_price
    risk = abs(entry - pos.stop_loss) if pos.stop_loss else atr
    if risk <= 0:
        return
    if pos.direction == "LONG":
        if (high - entry) >= activate_r * risk:
            new_sl = high - trailing_atr * atr
            if new_sl > pos.stop_loss:
                pos.stop_loss = new_sl
    else:
        if (entry - low) >= activate_r * risk:
            new_sl = low + trailing_atr * atr
            if new_sl < pos.stop_loss:
                pos.stop_loss = new_sl


def report(sim, settings, p, years, label=""):
    by_reason = {}
    for t in sim.trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    m = compute_metrics(sim.trades, settings.initial_capital)
    print("\n" + "=" * 60)
    print(f"  VOLATILITY EXPANSION {label} — BTC ~{years}a @ 1h/4h")
    print("=" * 60)
    print(f"Config: breakout={p.breakout_lookback} sl_atr={p.sl_atr} tp_atr={p.tp_atr} "
          f"compR={p.compression_ratio} volX={p.vol_mult} emaSep={p.min_ema_sep_pct}")
    print(f"Ejecución: taker={sim.taker_fee:.4%} maker={sim.maker_fee:.4%} slip={sim.slippage:.4%}")
    print("-" * 60)
    print(format_metrics(m))
    print("-" * 60)
    print("Cierres:", {k: v for k, v in sorted(by_reason.items(), key=lambda x: -x[1])})
    print("=" * 60)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--leverage", type=float, default=7.0)
    ap.add_argument("--risk-pct", type=float, default=0.05)
    ap.add_argument("--taker-fee", type=float, default=0.0005)
    ap.add_argument("--maker-fee", type=float, default=0.0002)
    ap.add_argument("--slippage", type=float, default=0.0003)
    # Params del engine
    ap.add_argument("--ema-fast", type=int, default=50)
    ap.add_argument("--ema-slow", type=int, default=200)
    ap.add_argument("--min-ema-sep", type=float, default=0.001)
    ap.add_argument("--compression-bars", type=int, default=720)
    ap.add_argument("--compression-ratio", type=float, default=0.70)
    ap.add_argument("--breakout", type=int, default=24)
    ap.add_argument("--vol-mult", type=float, default=1.5)
    ap.add_argument("--sl-atr", type=float, default=1.5)
    ap.add_argument("--tp-atr", type=float, default=3.0)
    ap.add_argument("--trailing-atr", type=float, default=0.0, help="0=off")
    ap.add_argument("--trailing-activate-r", type=float, default=1.0)
    args = ap.parse_args()

    s = load_settings()
    s.symbol = args.symbol
    s.max_risk_per_trade = args.risk_pct
    p = build_params(args)
    exec_cfg = {"taker_fee": args.taker_fee, "maker_fee": args.maker_fee,
                "slippage": args.slippage, "entry_mode": "taker"}

    days = int(args.years * 365)
    print(f"Bajando ~{args.years}a de {args.symbol} (1h y 4h)...")
    df_1h = fetch_history(args.symbol, "1h", days + 35)
    df_4h = fetch_history(args.symbol, "4h", days + 60)
    print(f"  1h: {len(df_1h)} velas | 4h: {len(df_4h)} velas "
          f"({df_1h.index[0].date()} → {df_1h.index[-1].date()})")

    sim = run_backtest(df_1h, df_4h, s, p, args.leverage, exec_cfg,
                       trailing_atr=args.trailing_atr,
                       trailing_activate_r=args.trailing_activate_r)
    report(sim, s, p, args.years,
           label=f"(trail {args.trailing_atr})" if args.trailing_atr > 0 else "")


if __name__ == "__main__":
    main()
