"""
scripts/audit_wfa_pullback.py
Walk-Forward Analysis del PullbackScalpEngine.

Espejo de audit_wfa.py (que validaba el ScalpingEngine v1), pero con grid de
parámetros pbs_* y el motor de pullback-continuación. Mismo veredicto:

  - Ventanas OS > 0 ≥ 60% y OS retorno total > 0 con PF > 1.2 → edge real.
  - OS oscilando +/− con la config "ganadora IS" → parámetros = ruido, sin edge.

⚠ ESTE ES EL FILTRO QUE MATÓ A v1. Si el pullback engine no lo pasa, NO va a
  dinero real — misma disciplina. Recomendado correr sobre SOL/USDT y AVAX/USDT
  (más ATR% que BTC = mejor ratio movimiento/fee).

Uso:
  python scripts/audit_wfa_pullback.py --symbol SOL/USDT --days 180 --grid quick
  python scripts/audit_wfa_pullback.py --symbol SOL/USDT --days 180 --grid full --leverage 7
"""

import argparse
import itertools
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import logging
logging.getLogger("core.pullback_scalp_engine").setLevel(logging.WARNING)
try:
    from logs.logger import logger as _lg
    _lg.remove()
    _lg.add(sys.stderr, level="WARNING")
except Exception:
    pass

import pandas as pd

from config.settings import load_settings
from core.pullback_scalp_engine import PullbackScalpEngine
from scripts.backtest_scalping import fetch_history, Sim


@dataclass
class WindowResult:
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    os_start: pd.Timestamp
    os_end: pd.Timestamp
    best_config: dict
    is_metric: float
    is_n: int
    is_ret: float
    os_pf: float
    os_n: int
    os_ret: float
    os_dd: float


def apply_config(settings, cfg: dict) -> None:
    for k, v in cfg.items():
        setattr(settings, k, v)


def run_subperiod(df_slice: pd.DataFrame, settings, leverage: float) -> Sim:
    engine = PullbackScalpEngine(settings)
    sim = Sim(settings, leverage=leverage)
    warm = max(settings.pbs_ema_slow + 10, 220)
    if len(df_slice) <= warm:
        return sim
    for i in range(warm, len(df_slice)):
        ts = df_slice.index[i]
        sim.step(df_slice.iloc[i], i, ts, engine=engine)
        if sim.position is None:
            dec = engine.analyze(df_slice, i)
            if dec.accion in ("COMPRAR", "VENDER"):
                sim.open(dec, i, ts)
    if sim.position is not None:
        sim.close(float(df_slice.iloc[-1]["close"]), "Fin",
                   len(df_slice) - 1, df_slice.index[-1], engine=engine)
    return sim


def sim_metrics(sim: Sim) -> dict:
    n = len(sim.trades)
    if n == 0:
        return {"n": 0, "ret": 0.0, "dd": 0.0, "pf": 0.0, "wr": 0.0, "total": 0.0}
    wins = [t for t in sim.trades if t.pnl_usdt > 0]
    losses = [t for t in sim.trades if t.pnl_usdt <= 0]
    total = sum(t.pnl_usdt for t in sim.trades)
    initial = sim.s.initial_capital
    wr = len(wins) / n * 100
    pf = (sum(t.pnl_usdt for t in wins) / abs(sum(t.pnl_usdt for t in losses))
           if losses and sum(t.pnl_usdt for t in losses) != 0 else float("inf"))
    return {"n": n, "ret": total / initial * 100, "dd": sim.max_dd,
            "pf": pf if pf != float("inf") else 99.0, "wr": wr, "total": total}


def grid_quick() -> list[dict]:
    """3 configs: defaults + 2 variantes en los knobs más sensibles."""
    return [
        {"pbs_adx_min": 18.0, "pbs_tp_rr": 1.6, "pbs_sl_atr_mult": 1.1,
         "pbs_atr_max_pct": 0.0060},
        {"pbs_adx_min": 22.0, "pbs_tp_rr": 2.0, "pbs_sl_atr_mult": 1.0,
         "pbs_atr_max_pct": 0.0080},
        {"pbs_adx_min": 15.0, "pbs_tp_rr": 1.4, "pbs_sl_atr_mult": 1.3,
         "pbs_atr_max_pct": 0.0100},
    ]


def grid_full() -> list[dict]:
    """Cartesiano reducido (~36 configs) sobre adx/tp/sl/atr_max."""
    grid = list(itertools.product(
        [15.0, 18.0, 22.0],      # adx_min
        [1.4, 1.6, 2.0],         # tp_rr
        [1.0, 1.3],              # sl_atr_mult
        [0.0060, 0.0100],        # atr_max_pct
    ))
    return [
        {"pbs_adx_min": adx, "pbs_tp_rr": tp, "pbs_sl_atr_mult": sl,
         "pbs_atr_max_pct": amax}
        for adx, tp, sl, amax in grid
    ]


def select_best(results, metric: str = "pf", min_n: int = 8):
    eligible = [(c, m) for c, m in results if m["n"] >= min_n]
    if not eligible:
        return None
    return max(eligible, key=lambda x: x[1][metric])


def _cfg_str(cfg: dict) -> str:
    return (f"adx={cfg['pbs_adx_min']:.0f}/tp={cfg['pbs_tp_rr']:.1f}/"
            f"sl={cfg['pbs_sl_atr_mult']:.1f}/amax={cfg['pbs_atr_max_pct']:.4f}")


def main():
    ap = argparse.ArgumentParser(description="Walk-Forward Analysis del PullbackScalpEngine")
    ap.add_argument("--symbol", type=str, default="SOL/USDT")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--is-days", type=int, default=40, dest="is_days")
    ap.add_argument("--os-days", type=int, default=20, dest="os_days")
    ap.add_argument("--step-days", type=int, default=20, dest="step_days")
    ap.add_argument("--timeframe", type=str, default="5m")
    ap.add_argument("--leverage", type=float, default=7.0)
    ap.add_argument("--risk-pct", type=float, default=0.02)
    ap.add_argument("--maker-fee", type=float, default=0.0002)
    ap.add_argument("--taker-fee", type=float, default=0.0005)
    ap.add_argument("--grid", choices=["quick", "full"], default="quick")
    ap.add_argument("--metric", choices=["pf", "ret", "total"], default="pf")
    args = ap.parse_args()

    s = load_settings()
    s.symbol = args.symbol
    s.max_risk_per_trade = args.risk_pct
    s.commission_pct_per_side = args.maker_fee
    setattr(s, "commission_taker_pct", args.taker_fee)

    print(f"Bajando {args.days} días de {s.symbol} @ {args.timeframe}...")
    df = fetch_history(s.symbol, args.timeframe, args.days)
    print(f"  {len(df)} velas")

    grid = grid_quick() if args.grid == "quick" else grid_full()
    print(f"\nGrid size: {len(grid)} configs")

    mpb = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}[args.timeframe]
    bars_per_day = (24 * 60) // mpb
    is_bars = args.is_days * bars_per_day
    os_bars = args.os_days * bars_per_day
    step_bars = args.step_days * bars_per_day

    windows = []
    start = 0
    while start + is_bars + os_bars <= len(df):
        windows.append((start, start + is_bars, start + is_bars + os_bars))
        start += step_bars
    print(f"Ventanas WFA: {len(windows)}\n")

    if not windows:
        print("ERROR: no entran ventanas en el período. Subí --days o bajá --is/--os.")
        return

    all_results: list[WindowResult] = []

    for w_idx, (is_a, is_b, os_b) in enumerate(windows, 1):
        df_is = df.iloc[is_a:is_b]
        df_os = df.iloc[is_b:os_b]
        is_start_ts, is_end_ts = df_is.index[0], df_is.index[-1]
        os_start_ts, os_end_ts = df_os.index[0], df_os.index[-1]

        print(f"─── Ventana {w_idx}/{len(windows)} ───")
        print(f"IS: {is_start_ts.date()} → {is_end_ts.date()}  ({len(df_is)} velas)")
        print(f"OS: {os_start_ts.date()} → {os_end_ts.date()}  ({len(df_os)} velas)")

        print(f"   Optimizando {len(grid)} configs en IS...")
        is_results = []
        for c_idx, cfg in enumerate(grid, 1):
            apply_config(s, cfg)
            sim = run_subperiod(df_is, s, args.leverage)
            m = sim_metrics(sim)
            is_results.append((cfg, m))
            print(f"      [{c_idx:>2}/{len(grid)}] {_cfg_str(cfg):<40}  "
                   f"n={m['n']:>3}  ret={m['ret']:>+6.2f}%  PF={m['pf']:>4.2f}")

        best = select_best(is_results, metric=args.metric, min_n=5)
        if best is None:
            print("   ⚠️  ninguna config superó el mínimo n=5 en IS. Skip OS.")
            continue
        best_cfg, best_is = best
        print(f"   → MEJOR IS: ret={best_is['ret']:+.2f}%  PF={best_is['pf']:.2f}  n={best_is['n']}")

        apply_config(s, best_cfg)
        sim_os = run_subperiod(df_os, s, args.leverage)
        m_os = sim_metrics(sim_os)
        print(f"   ⇒ OS: ret={m_os['ret']:+.2f}%  PF={m_os['pf']:.2f}  "
               f"n={m_os['n']}  DD={m_os['dd']:.2f}%\n")

        all_results.append(WindowResult(
            is_start=is_start_ts, is_end=is_end_ts,
            os_start=os_start_ts, os_end=os_end_ts,
            best_config=best_cfg, is_metric=best_is[args.metric],
            is_n=best_is["n"], is_ret=best_is["ret"],
            os_pf=m_os["pf"], os_n=m_os["n"],
            os_ret=m_os["ret"], os_dd=m_os["dd"],
        ))

    print("\n" + "=" * 75)
    print("  RESUMEN WFA — PullbackScalpEngine")
    print("=" * 75)
    if not all_results:
        print("Sin resultados.")
        return

    print(f"{'OS range':<16}{'best_cfg':<40}{'IS ret%':>9}{'OS ret%':>9}{'OS PF':>7}{'OS n':>6}")
    print("─" * 87)
    for r in all_results:
        rng = f"{r.os_start.strftime('%m-%d')}→{r.os_end.strftime('%m-%d')}"
        print(f"{rng:<16}{_cfg_str(r.best_config):<40}{r.is_ret:>+8.2f}%"
               f"{r.os_ret:>+8.2f}%{r.os_pf:>7.2f}{r.os_n:>6}")
    print("─" * 87)

    os_rets = [r.os_ret for r in all_results]
    os_total = sum(os_rets)
    os_pos = sum(1 for r in os_rets if r > 0)
    avg_os = os_total / len(os_rets)
    print(f"\nTotal ventanas:    {len(all_results)}")
    print(f"Ventanas OS > 0:   {os_pos}/{len(all_results)}  ({os_pos/len(all_results)*100:.0f}%)")
    print(f"OS retorno total:  {os_total:+.2f}%")
    print(f"OS retorno medio:  {avg_os:+.2f}% por ventana")
    print(f"OS retorno peor:   {min(os_rets):+.2f}%")
    print(f"OS retorno mejor:  {max(os_rets):+.2f}%")

    if os_total > 0 and os_pos / len(all_results) >= 0.6:
        print("\n✅ EDGE SOBREVIVE WFA — los parámetros óptimos IS generalizan a OS.")
        print("   Siguiente paso: paper trading en testnet antes de real.")
    elif os_total > 0:
        print("\n🟡 EDGE MARGINAL — positivo en suma pero inestable entre ventanas.")
        print("   NO deployar aún. Probar otro símbolo o revisar el régimen.")
    else:
        print("\n❌ EDGE NO SOBREVIVE WFA — la optimización IS no se traduce a OS.")
        print("   Mismo destino que v1: no va a real. Cambio estructural necesario.")


if __name__ == "__main__":
    main()
