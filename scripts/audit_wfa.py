"""
scripts/audit_wfa.py
Auditoría #4 — Walk-Forward Analysis (WFA).

PROBLEMA QUE RESUELVE:
El breakdown (audit E) y los filtros (audit F) se derivaron del MISMO período
que ahora estamos midiendo. Eso es overfitting por construcción: cualquier
patrón ruidoso del dataset se va a "validar" trivialmente sobre sí mismo.

QUÉ HACE EL WFA:
1. Divide el período total en ventanas IS (In-Sample) y OS (Out-of-Sample),
   tipo:  [---40 días IS---][--20 días OS--]
                                   [---40 días IS---][--20 días OS--]
                                                            ...
2. En cada ventana IS, hace grid search sobre un set de parámetros
   (squeeze_pct, vol_spike, tp_atr_mult, sl_atr_mult, cooldown) y elige
   la mejor config IS.
3. Aplica ESA config en el OS sin tocar nada.
4. Suma los resultados OS de todas las ventanas — esa es la performance
   honesta esperada en producción.

VEREDICTO:
- Si la suma de OS > 0 con PF > 1.2 y DD aceptable → edge real.
- Si OS oscila violentamente entre +/− con la config que "ganaba" IS, no hay
  edge — los parámetros eran ruido.

⚠ Performance: este script es lento. Cada ventana IS prueba N configs × 40
días de backtest. Si N=20 configs y hay 3 ventanas, son 60 backtests. En
máquinas modestas puede tardar 20-30 minutos. Para iteraciones rápidas
usar --grid quick (3 configs en vez de ~20).
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
logging.getLogger("core.scalping_engine").setLevel(logging.WARNING)
try:
    from logs.logger import logger as _lg
    _lg.remove()
    _lg.add(sys.stderr, level="WARNING")
except Exception:
    pass

import pandas as pd

from config.settings import load_settings
from core.scalping_engine import ScalpingEngine
from scripts.backtest_scalping import fetch_history, Sim


@dataclass
class WindowResult:
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    os_start: pd.Timestamp
    os_end: pd.Timestamp
    best_config: dict
    is_metric: float      # mejor PF (o métrica elegida) en IS
    is_n: int
    is_ret: float
    os_pf: float
    os_n: int
    os_ret: float
    os_dd: float


def apply_config(settings, cfg: dict) -> None:
    """Pisa los settings con los parámetros del grid."""
    for k, v in cfg.items():
        setattr(settings, k, v)


def run_subperiod(df_slice: pd.DataFrame, settings, leverage: float) -> Sim:
    engine = ScalpingEngine(settings)
    sim = Sim(settings, leverage=leverage)
    warm = 110
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
    return {
        "n": n,
        "ret": total / initial * 100,
        "dd": sim.max_dd,
        "pf": pf if pf != float("inf") else 99.0,
        "wr": wr,
        "total": total,
    }


def grid_quick() -> list[dict]:
    """3 configs principales: defaults + 2 variantes."""
    return [
        {"scalp_squeeze_pct": 20.0, "scalp_vol_spike": 2.0,
         "scalp_tp_atr_mult": 1.5, "scalp_sl_atr_mult": 1.0,
         "scalp_cooldown_bars": 3},
        {"scalp_squeeze_pct": 15.0, "scalp_vol_spike": 2.5,
         "scalp_tp_atr_mult": 2.0, "scalp_sl_atr_mult": 1.0,
         "scalp_cooldown_bars": 5},
        {"scalp_squeeze_pct": 25.0, "scalp_vol_spike": 1.5,
         "scalp_tp_atr_mult": 1.5, "scalp_sl_atr_mult": 1.2,
         "scalp_cooldown_bars": 3},
    ]


def grid_full() -> list[dict]:
    """~24 configs (cartesiano reducido)."""
    grid = list(itertools.product(
        [15.0, 20.0, 25.0],     # squeeze_pct
        [1.5, 2.0, 2.5],        # vol_spike
        [1.5, 2.0],             # tp_atr_mult
        [1.0],                  # sl_atr_mult
        [3, 5],                 # cooldown_bars
    ))
    return [
        {"scalp_squeeze_pct": sq, "scalp_vol_spike": vs,
         "scalp_tp_atr_mult": tp, "scalp_sl_atr_mult": sl,
         "scalp_cooldown_bars": cd}
        for sq, vs, tp, sl, cd in grid
    ]


def select_best(results: list[tuple[dict, dict]], metric: str = "pf",
                 min_n: int = 8) -> Optional[tuple[dict, dict]]:
    """Elige la config con mejor 'metric' de los resultados IS.
    Requisitos: n >= min_n para no quedarse con configs que ni operaron."""
    eligible = [(c, m) for c, m in results if m["n"] >= min_n]
    if not eligible:
        return None
    return max(eligible, key=lambda x: x[1][metric])


def main():
    ap = argparse.ArgumentParser(description="Walk-Forward Analysis del ScalpingEngine")
    ap.add_argument("--days", type=int, default=180,
                     help="Total de días a descargar (default 180)")
    ap.add_argument("--is-days", type=int, default=40, dest="is_days",
                     help="Días en el In-Sample (default 40)")
    ap.add_argument("--os-days", type=int, default=20, dest="os_days",
                     help="Días en el Out-of-Sample (default 20)")
    ap.add_argument("--step-days", type=int, default=20, dest="step_days",
                     help="Pasos entre ventanas (default 20 — rolling no overlap OS)")
    ap.add_argument("--timeframe", type=str, default="5m")
    ap.add_argument("--leverage", type=float, default=10.0)
    ap.add_argument("--risk-pct", type=float, default=0.08)
    ap.add_argument("--maker-fee", type=float, default=0.0002)
    ap.add_argument("--taker-fee", type=float, default=0.0005)
    ap.add_argument("--grid", choices=["quick", "full"], default="quick")
    ap.add_argument("--metric", choices=["pf", "ret", "total"], default="pf",
                     help="Métrica para seleccionar mejor config IS")
    args = ap.parse_args()

    s = load_settings()
    s.max_risk_per_trade = args.risk_pct
    s.commission_pct_per_side = args.maker_fee
    setattr(s, "commission_taker_pct", args.taker_fee)

    print(f"Bajando {args.days} días de {s.symbol} @ {args.timeframe}...")
    df = fetch_history(s.symbol, args.timeframe, args.days)
    print(f"  {len(df)} velas")

    grid = grid_quick() if args.grid == "quick" else grid_full()
    print(f"\nGrid size: {len(grid)} configs")

    # Generar ventanas
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
        print(f"IS: {is_start_ts.date()} → {is_end_ts.date()}  "
               f"({len(df_is)} velas)")
        print(f"OS: {os_start_ts.date()} → {os_end_ts.date()}  "
               f"({len(df_os)} velas)")

        # 1) Grid sobre IS
        print(f"   Optimizando {len(grid)} configs en IS...")
        is_results = []
        for c_idx, cfg in enumerate(grid, 1):
            apply_config(s, cfg)
            sim = run_subperiod(df_is, s, args.leverage)
            m = sim_metrics(sim)
            is_results.append((cfg, m))
            # Una línea por config (compacto)
            cfg_str = f"sq={cfg['scalp_squeeze_pct']:.0f} vs={cfg['scalp_vol_spike']:.1f} " \
                       f"tp={cfg['scalp_tp_atr_mult']:.1f} cd={cfg['scalp_cooldown_bars']}"
            print(f"      [{c_idx:>2}/{len(grid)}] {cfg_str:<40}  "
                   f"n={m['n']:>3}  ret={m['ret']:>+6.2f}%  PF={m['pf']:>4.2f}")

        # 2) Elegir mejor config IS
        best = select_best(is_results, metric=args.metric, min_n=5)
        if best is None:
            print("   ⚠️  ninguna config superó el mínimo n=5 en IS. Skip OS.")
            continue
        best_cfg, best_is = best
        print(f"   → MEJOR IS: ret={best_is['ret']:+.2f}%  PF={best_is['pf']:.2f}  "
               f"n={best_is['n']}")

        # 3) Aplicar best_cfg en OS
        apply_config(s, best_cfg)
        sim_os = run_subperiod(df_os, s, args.leverage)
        m_os = sim_metrics(sim_os)
        print(f"   ⇒ OS: ret={m_os['ret']:+.2f}%  PF={m_os['pf']:.2f}  "
               f"n={m_os['n']}  DD={m_os['dd']:.2f}%\n")

        all_results.append(WindowResult(
            is_start=is_start_ts, is_end=is_end_ts,
            os_start=os_start_ts, os_end=os_end_ts,
            best_config=best_cfg,
            is_metric=best_is[args.metric],
            is_n=best_is["n"], is_ret=best_is["ret"],
            os_pf=m_os["pf"], os_n=m_os["n"],
            os_ret=m_os["ret"], os_dd=m_os["dd"],
        ))

    # ─── Reporte agregado ───
    print("\n" + "=" * 75)
    print("  RESUMEN WFA")
    print("=" * 75)
    if not all_results:
        print("Sin resultados.")
        return

    print(f"{'IS→OS':<22}{'best_cfg':<30}{'IS ret%':>10}{'OS ret%':>10}{'OS PF':>8}{'OS n':>6}")
    print("─" * 86)
    for r in all_results:
        cfg = r.best_config
        cstr = f"sq={cfg['scalp_squeeze_pct']:.0f}/vs={cfg['scalp_vol_spike']:.1f}/tp={cfg['scalp_tp_atr_mult']:.1f}/cd={cfg['scalp_cooldown_bars']}"
        rng = f"{r.os_start.strftime('%m-%d')}→{r.os_end.strftime('%m-%d')}"
        print(f"{rng:<22}{cstr:<30}{r.is_ret:>+9.2f}%{r.os_ret:>+9.2f}%{r.os_pf:>8.2f}{r.os_n:>6}")
    print("─" * 86)

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
        print("\n✅ EDGE SOBREVIVE WFA — parámetros óptimos IS generalizan a OS.")
    elif os_total > 0:
        print("\n🟡 EDGE MARGINAL — positivo en suma pero inestable entre ventanas.")
    else:
        print("\n❌ EDGE NO SOBREVIVE WFA — la optimización IS no se traduce a OS.")
        print("   Curve fitting confirmado. La estrategia necesita un cambio estructural.")


if __name__ == "__main__":
    main()
