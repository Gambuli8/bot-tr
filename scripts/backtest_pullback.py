"""
scripts/backtest_pullback.py
Backtest del PullbackScalpEngine (trend-pullback, maker-first, fee-gated).

Modela fees diferenciadas (entrada/TP = maker, SL/liquidación = taker) y
liquidación por leverage, igual que backtest_scalping.py.

⚠️  Un backtest single-period NO es validación suficiente. Si los números son
    buenos acá, el paso obligatorio es WFA antes de cualquier dinero real:
        python scripts/audit_wfa.py ...   (adaptar al engine pullback)

Uso:
  python scripts/backtest_pullback.py --symbol SOL/USDT --days 90
  python scripts/backtest_pullback.py --symbol SOL/USDT --days 90 --leverage 7 --risk-pct 0.02
  python scripts/backtest_pullback.py --symbol AVAX/USDT --days 90 --skip-hours 6-11
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
import pandas as pd

from config.settings import load_settings
from core.pullback_scalp_engine import PullbackScalpEngine
# Reutilizamos el simulador del backtest de scalping (fees maker/taker +
# liquidación + position sizing por riesgo ya están modelados ahí).
from scripts.backtest_scalping import Sim, fetch_history, report


def main():
    p = argparse.ArgumentParser(description="Backtest Pullback Scalp Engine")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--timeframe", type=str, default="5m")
    p.add_argument("--symbol", type=str, default="SOL/USDT",
                   help="Default SOL/USDT (más ATR% que BTC = mejor ratio movimiento/fee)")
    p.add_argument("--leverage", type=float, default=7.0)
    p.add_argument("--risk-pct", type=float, default=0.02, help="Riesgo por trade. Default 2%")
    p.add_argument("--fee", type=float, default=0.0002,
                   help="Maker fee por lado (entrada + TP). Default 0.02% Futures USDT-M")
    p.add_argument("--taker-fee", type=float, default=0.0005,
                   help="Taker fee (SL + liquidación). Default 0.05%")
    p.add_argument("--skip-hours", type=str, default=None,
                   help="Horas UTC a saltar, ej '6-11'. Validar con el breakdown del WFA.")
    # Overrides rápidos de los knobs más sensibles del engine.
    p.add_argument("--adx-min", type=float, default=None)
    p.add_argument("--tp-rr", type=float, default=None)
    p.add_argument("--sl-atr", type=float, default=None)
    p.add_argument("--atr-max", type=float, default=None)
    args = p.parse_args()

    s = load_settings()
    s.symbol = args.symbol
    s.max_risk_per_trade = args.risk_pct
    s.commission_pct_per_side = args.fee
    setattr(s, "commission_taker_pct", args.taker_fee)
    if args.skip_hours is not None:
        setattr(s, "scalp_skip_hours_utc", args.skip_hours)
    if args.adx_min is not None:
        setattr(s, "pbs_adx_min", args.adx_min)
    if args.tp_rr is not None:
        setattr(s, "pbs_tp_rr", args.tp_rr)
    if args.sl_atr is not None:
        setattr(s, "pbs_sl_atr_mult", args.sl_atr)
    if args.atr_max is not None:
        setattr(s, "pbs_atr_max_pct", args.atr_max)

    print(f"Bajando {args.days} días de {s.symbol} @ {args.timeframe}...")
    df = fetch_history(s.symbol, args.timeframe, args.days)
    print(f"  {len(df)} velas")
    print(f"  risk={s.max_risk_per_trade:.2%}  leverage={args.leverage}x  "
          f"maker={s.commission_pct_per_side:.4%}  taker={args.taker_fee:.4%}")

    engine = PullbackScalpEngine(s)
    sim = Sim(s, leverage=args.leverage)

    warm = max(s.pbs_ema_slow + 10, 220)
    for i in range(warm, len(df)):
        ts = df.index[i]
        sim.step(df.iloc[i], i, ts, engine=engine)
        if sim.position is None:
            dec = engine.analyze(df, i)
            if dec.accion in ("COMPRAR", "VENDER"):
                sim.open(dec, i, ts)

    if sim.position is not None:
        sim.close(float(df.iloc[-1]["close"]), "Fin", len(df) - 1, df.index[-1], engine=engine)

    report(sim, s, args.days, args.timeframe)
    print("\n⚠️  Backtest single-period. NO es validación. Próximo paso: WFA.")


if __name__ == "__main__":
    main()
