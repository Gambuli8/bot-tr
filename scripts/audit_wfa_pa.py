"""
scripts/audit_wfa_pa.py
WFA del PriceActionEngine, parametrizable por símbolo y timeframe.

Diseñado para correrse en paralelo: 1 invocación por combinación (símbolo, TF).
El script descarga main + higher de Binance, corre 7 ventanas IS=40d/OS=20d,
hace grid search por PF y reporta tabla con OS metrics.

Mapeo de higher_tf:
  - 15m → 1h
  - 30m → 4h
  - 1h  → 4h

Uso:
    python scripts/audit_wfa_pa.py --symbol BTC/USDT --timeframe 1h --days 180
    python scripts/audit_wfa_pa.py --symbol ETH/USDT --timeframe 15m --days 180
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
logging.getLogger("core.price_action_engine").setLevel(logging.WARNING)
try:
    from logs.logger import logger as _lg
    _lg.remove()
    _lg.add(sys.stderr, level="WARNING")
except Exception:
    pass

import pandas as pd

from config.settings import load_settings
from core.price_action_engine import PriceActionEngine
from scripts.backtest_price_action import fetch_history, Sim


HIGHER_TF_MAP = {
    "15m": "1h",
    "30m": "4h",
    "1h": "4h",
}


@dataclass
class WindowResult:
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    os_start: pd.Timestamp
    os_end: pd.Timestamp
    best_config: dict
    is_pf: float
    is_n: int
    is_ret: float
    os_pf: float
    os_n: int
    os_ret: float
    os_dd: float


def apply_config(settings, cfg: dict) -> None:
    for k, v in cfg.items():
        setattr(settings, k, v)


def run_subperiod_pa(df_main: pd.DataFrame, df_higher: pd.DataFrame,
                      settings, leverage: float) -> Sim:
    engine = PriceActionEngine(settings)
    sim = Sim(settings, leverage=leverage)
    warmup = 60
    if len(df_main) <= warmup:
        return sim
    for i in range(warmup, len(df_main)):
        ts = df_main.index[i]
        sim.step(df_main.iloc[i], i, ts)
        if sim.position is None:
            df_main_sf = df_main.iloc[: i + 1]
            df_higher_sf = df_higher.loc[: ts]
            if len(df_higher_sf) < 30:
                continue
            dec = engine.analyze(df_main_sf, df_higher_sf)
            if dec.accion in ("COMPRAR", "VENDER"):
                sim.open(dec, i, ts)
    if sim.position is not None:
        sim.close(float(df_main.iloc[-1]["close"]), "Fin",
                   len(df_main) - 1, df_main.index[-1])
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


def grid_pa() -> list[dict]:
    """3 configs: default + más agresiva (más señales) + más selectiva."""
    return [
        # Default — validado en backtests previos
        {"pa_vol_mult": 1.5, "pa_atr_sl_mult": 1.5, "pa_tp_rr": 2.5, "pa_fractal_n": 3},
        # Más agresiva: menos filtros, más señales (necesario para 15m/30m)
        {"pa_vol_mult": 1.2, "pa_atr_sl_mult": 1.2, "pa_tp_rr": 2.0, "pa_fractal_n": 2},
        # Más selectiva: filtros más altos
        {"pa_vol_mult": 2.0, "pa_atr_sl_mult": 2.0, "pa_tp_rr": 3.0, "pa_fractal_n": 4},
    ]


def select_best(results: list[tuple[dict, dict]],
                 metric: str = "pf", min_n: int = 4) -> Optional[tuple[dict, dict]]:
    eligible = [(c, m) for c, m in results if m["n"] >= min_n]
    if not eligible:
        return None
    return max(eligible, key=lambda x: x[1][metric])


def bars_per_day(tf: str) -> int:
    mpb = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}[tf]
    return (24 * 60) // mpb


def main():
    ap = argparse.ArgumentParser(description="WFA del PriceActionEngine")
    ap.add_argument("--symbol", type=str, required=True)
    ap.add_argument("--timeframe", type=str, required=True,
                     choices=["15m", "30m", "1h"])
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--is-days", type=int, default=40)
    ap.add_argument("--os-days", type=int, default=20)
    ap.add_argument("--step-days", type=int, default=20)
    ap.add_argument("--leverage", type=float, default=5.0,
                     help="Default 5x (PA usa más leverage que scalping)")
    ap.add_argument("--risk-pct", type=float, default=0.08)
    ap.add_argument("--commission", type=float, default=0.0005,
                     help="Mezcla maker/taker para PA (TP es limit, SL es market)")
    args = ap.parse_args()

    higher_tf = HIGHER_TF_MAP[args.timeframe]

    s = load_settings()
    s.symbol = args.symbol
    s.max_risk_per_trade = args.risk_pct
    s.commission_pct_per_side = args.commission

    print(f"[{args.symbol} @ {args.timeframe}/{higher_tf}] Bajando {args.days}d...")
    df_main = fetch_history(args.symbol, args.timeframe, args.days + 5)
    df_higher = fetch_history(args.symbol, higher_tf, args.days + 30)
    print(f"  main: {len(df_main)} velas | higher: {len(df_higher)} velas")

    grid = grid_pa()
    print(f"  Grid size: {len(grid)} configs | leverage {args.leverage}x | "
          f"risk {args.risk_pct:.0%} | fee {args.commission:.4%}")

    bpd = bars_per_day(args.timeframe)
    is_bars = args.is_days * bpd
    os_bars = args.os_days * bpd
    step_bars = args.step_days * bpd

    windows = []
    start = 0
    while start + is_bars + os_bars <= len(df_main):
        windows.append((start, start + is_bars, start + is_bars + os_bars))
        start += step_bars
    print(f"  Ventanas WFA: {len(windows)}")

    if not windows:
        print(f"ERROR: no entran ventanas en el período.")
        return

    all_results: list[WindowResult] = []

    for w_idx, (is_a, is_b, os_b) in enumerate(windows, 1):
        df_is = df_main.iloc[is_a:is_b]
        df_os = df_main.iloc[is_b:os_b]
        is_start_ts, is_end_ts = df_is.index[0], df_is.index[-1]
        os_start_ts, os_end_ts = df_os.index[0], df_os.index[-1]

        print(f"\n[{args.symbol} @ {args.timeframe}] Ventana {w_idx}/{len(windows)}: "
               f"IS {is_start_ts.date()}→{is_end_ts.date()}  "
               f"OS {os_start_ts.date()}→{os_end_ts.date()}")

        # IS sweep
        is_results = []
        for c_idx, cfg in enumerate(grid, 1):
            apply_config(s, cfg)
            sim = run_subperiod_pa(df_is, df_higher, s, args.leverage)
            m = sim_metrics(sim)
            is_results.append((cfg, m))
            cstr = f"vm={cfg['pa_vol_mult']:.1f}/asl={cfg['pa_atr_sl_mult']:.1f}/rr={cfg['pa_tp_rr']:.1f}/fr={cfg['pa_fractal_n']}"
            print(f"   IS [{c_idx}/{len(grid)}] {cstr:<35}  n={m['n']:>3}  "
                   f"ret={m['ret']:>+6.2f}%  PF={m['pf']:>4.2f}")

        best = select_best(is_results, metric="pf", min_n=4)
        if best is None:
            print(f"   ⚠️  ninguna config llegó a n>=4 en IS. Skip OS.")
            continue
        best_cfg, best_is = best
        print(f"   → BEST IS: ret={best_is['ret']:+.2f}%  PF={best_is['pf']:.2f}  n={best_is['n']}")

        apply_config(s, best_cfg)
        sim_os = run_subperiod_pa(df_os, df_higher, s, args.leverage)
        m_os = sim_metrics(sim_os)
        print(f"   ⇒ OS: ret={m_os['ret']:+.2f}%  PF={m_os['pf']:.2f}  "
               f"n={m_os['n']}  DD={m_os['dd']:.2f}%")

        all_results.append(WindowResult(
            is_start=is_start_ts, is_end=is_end_ts,
            os_start=os_start_ts, os_end=os_end_ts,
            best_config=best_cfg,
            is_pf=best_is["pf"], is_n=best_is["n"], is_ret=best_is["ret"],
            os_pf=m_os["pf"], os_n=m_os["n"],
            os_ret=m_os["ret"], os_dd=m_os["dd"],
        ))

    # ─── Reporte final ───
    print("\n" + "=" * 90)
    print(f"  WFA RESUMEN — {args.symbol} @ {args.timeframe} / {higher_tf}")
    print("=" * 90)
    if not all_results:
        print("Sin resultados.")
        return

    print(f"{'OS range':<22}{'best_cfg':<35}{'IS ret%':>9}{'OS ret%':>10}{'OS PF':>7}{'OS n':>6}")
    print("─" * 89)
    for r in all_results:
        cfg = r.best_config
        cstr = f"vm={cfg['pa_vol_mult']:.1f}/asl={cfg['pa_atr_sl_mult']:.1f}/rr={cfg['pa_tp_rr']:.1f}/fr={cfg['pa_fractal_n']}"
        rng = f"{r.os_start.strftime('%m-%d')}→{r.os_end.strftime('%m-%d')}"
        print(f"{rng:<22}{cstr:<35}{r.is_ret:>+8.2f}%{r.os_ret:>+9.2f}%{r.os_pf:>7.2f}{r.os_n:>6}")
    print("─" * 89)

    os_rets = [r.os_ret for r in all_results]
    os_ns = [r.os_n for r in all_results]
    os_total_ret = sum(os_rets)
    os_pos = sum(1 for r in os_rets if r > 0)
    total_os_trades = sum(os_ns)
    trades_per_day = total_os_trades / (len(all_results) * args.os_days)

    # PF robusto: SOLO ventanas con n>=3 trades (excluir muestras chicas que
    # producen PF=inf cuando no hay losses). Adicionalmente reportamos la
    # MEDIANA del PF (resistente a outliers) en vez de la media.
    pf_robust = [r.os_pf for r in all_results if r.os_n >= 3]
    if pf_robust:
        pf_robust_sorted = sorted(pf_robust)
        mid = len(pf_robust_sorted) // 2
        if len(pf_robust_sorted) % 2 == 1:
            pf_median = pf_robust_sorted[mid]
        else:
            pf_median = (pf_robust_sorted[mid - 1] + pf_robust_sorted[mid]) / 2
    else:
        pf_median = 0.0

    print(f"\nVentanas:                 {len(all_results)}")
    print(f"OS positivas:             {os_pos}/{len(all_results)}  ({os_pos/len(all_results)*100:.0f}%)")
    print(f"OS retorno total:         {os_total_ret:+.2f}%")
    print(f"OS retorno medio:         {os_total_ret/len(all_results):+.2f}% / ventana")
    print(f"OS PF mediano (n>=3):     {pf_median:.2f}  (sobre {len(pf_robust)} ventanas robustas)")
    print(f"OS trades totales:        {total_os_trades}")
    print(f"OS trades/día:            {trades_per_day:.2f}")

    # Veredicto: AHORA pide tanto edge robusto (PF mediano + WR de ventanas)
    # COMO ganancia neta positiva en el agregado. LTC, LINK y similares ya no
    # se cuelan por tener PF medio inflado pero retorno total negativo/marginal.
    pos_ratio = os_pos / len(all_results)
    if pf_median >= 1.15 and pos_ratio >= 0.55 and os_total_ret >= 10:
        verdict = "✅ EDGE ROBUSTO — PF mediano >= 1.15, mayoría de ventanas positivas, ret >= +10%."
    elif pf_median >= 1.05 and os_total_ret > 0:
        verdict = "🟡 EDGE MARGINAL — positivo pero al borde."
    else:
        verdict = "❌ NO PASA — edge insuficiente para producción."
    print(f"\nVEREDICTO: {verdict}")


if __name__ == "__main__":
    main()
