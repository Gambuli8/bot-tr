"""
scripts/wfa_pa_forex.py
Walk-Forward Analysis del PriceActionEngineFX sobre EUR/USD, con costos reales.
Mismo espíritu que scripts/audit_wfa_pa.py (cripto): ventanas rolling IS/OS, grid
search en IS por PF NETO, se aplica la mejor config al OS siguiente (sin overlap),
y se evalúa el agregado OS. Una estrategia sólo "pasa" si sobrevive el OS.

Dos modos:
  --mode rolling : ventanas IS/OS rolling dentro de un año (default 2019).
  --mode holdout : tunea en --train-year (grid por PF neto del año completo) y
                   testea esa config bloqueada en --test-year (out-of-sample puro).

Costos: SIEMPRE realistas (spread efectivo ≥0.8pip + 0.6pip comisión + swap),
salvo que se pase --no-cost (sólo para diagnóstico).

Uso:
    python scripts/wfa_pa_forex.py --mode rolling --year 2019 --trig 1h --htf 4h
    python scripts/wfa_pa_forex.py --mode holdout --train-year 2019 --test-year 2022
"""

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd

from scripts.fx_data import load_5m, resample
from scripts.backtest_pa_forex import run_backtest, metrics


class S:
    """Settings liviano para engine + sim."""
    def __init__(self, capital=10000.0, risk=0.01):
        self.initial_capital = capital
        self.max_risk_per_trade = risk


def apply_cfg(s: S, cfg: dict):
    s.pafx_vol_mult = cfg["vm"]
    s.pafx_atr_sl_mult = cfg["atr"]
    s.pafx_tp_rr = cfg["rr"]
    s.pafx_fractal_n = cfg["fr"]


def grid() -> list[dict]:
    return [
        {"vm": 1.5, "atr": 1.5, "rr": 2.5, "fr": 3},   # default (cripto-validado)
        {"vm": 1.2, "atr": 1.2, "rr": 2.0, "fr": 2},   # agresiva (más señales)
        {"vm": 2.0, "atr": 2.0, "rr": 3.0, "fr": 4},   # selectiva
        {"vm": 1.5, "atr": 2.0, "rr": 2.0, "fr": 3},   # SL ancho, RR bajo
    ]


def sim_kwargs(no_cost: bool, swap_pips: float = 0.3) -> dict:
    if no_cost:
        return dict(min_spread_pips=0.0, commission_pips_rt=0.0, swap_pips_per_night=0.0)
    return dict(min_spread_pips=0.8, commission_pips_rt=0.6, swap_pips_per_night=swap_pips)


def run_window(df_trig, df_htf, cfg, no_cost, capital, risk):
    s = S(capital, risk)
    apply_cfg(s, cfg)
    sim = run_backtest(df_trig, df_htf, s, sim_kwargs(no_cost))
    days = (df_trig.index[-1] - df_trig.index[0]).total_seconds() / 86400.0
    return metrics(sim, days)


def fmt_cfg(c: dict) -> str:
    return f"vm{c['vm']}/atr{c['atr']}/rr{c['rr']}/fr{c['fr']}"


# ─────────────────────────────────────────
#  Modo rolling
# ─────────────────────────────────────────

def mode_rolling(args):
    df5 = load_5m(args.pair, args.year)
    df_trig = resample(df5, args.trig)
    df_htf = resample(df5, args.htf)
    print(f"[{args.pair} {args.year}] trig {args.trig}={len(df_trig)} velas, "
          f"htf {args.htf}={len(df_htf)} velas", file=sys.stderr)

    # Ventanas por días calendario
    t0 = df_trig.index[0]
    is_td = pd.Timedelta(days=args.is_days)
    os_td = pd.Timedelta(days=args.os_days)
    step_td = pd.Timedelta(days=args.step_days)

    windows = []
    start = t0
    end = df_trig.index[-1]
    while start + is_td + os_td <= end:
        windows.append((start, start + is_td, start + is_td + os_td))
        start += step_td
    print(f"Ventanas WFA: {len(windows)} (IS={args.is_days}d OS={args.os_days}d step={args.step_days}d)")

    g = grid()
    rows = []
    for wi, (a, b, c) in enumerate(windows, 1):
        df_is_t = df_trig.loc[a:b]
        df_os_t = df_trig.loc[b:c]
        # HTF se pasa completo; analyze() lo recorta con .loc[:ts]
        print(f"\nVentana {wi}/{len(windows)}: IS {a.date()}→{b.date()}  OS {b.date()}→{c.date()}")
        is_results = []
        for cfg in g:
            m = run_window(df_is_t, df_htf, cfg, args.no_cost, args.capital, args.risk)
            is_results.append((cfg, m))
            print(f"   IS {fmt_cfg(cfg):<22} n={m.get('n',0):>3} "
                  f"ret={m.get('ret',0):>+6.2f}% PFnet={m.get('pf_net',0):>4.2f}")
        eligible = [(cfg, m) for cfg, m in is_results if m.get("n", 0) >= args.min_is_trades]
        if not eligible:
            print("   ⚠️  ninguna config con n>=min en IS. Skip OS.")
            continue
        best_cfg, best_is = max(eligible, key=lambda x: x[1]["pf_net"])
        m_os = run_window(df_os_t, df_htf, best_cfg, args.no_cost, args.capital, args.risk)
        print(f"   → BEST IS {fmt_cfg(best_cfg)} (PFnet {best_is['pf_net']:.2f}) "
              f"⇒ OS: ret={m_os.get('ret',0):+.2f}% PFnet={m_os.get('pf_net',0):.2f} "
              f"n={m_os.get('n',0)} DD={m_os.get('dd',0):.1f}%")
        rows.append((best_cfg, best_is, m_os))

    summarize_os([r[2] for r in rows], args)


# ─────────────────────────────────────────
#  Modo holdout
# ─────────────────────────────────────────

def mode_holdout(args):
    # Train: grid sobre el año completo de train, elegir best por PF neto.
    df5_tr = load_5m(args.pair, args.train_year)
    tr_trig = resample(df5_tr, args.trig)
    tr_htf = resample(df5_tr, args.htf)
    print(f"[TRAIN {args.train_year}] grid por PF neto sobre año completo:")
    g = grid()
    train_results = []
    for cfg in g:
        m = run_window(tr_trig, tr_htf, cfg, args.no_cost, args.capital, args.risk)
        train_results.append((cfg, m))
        print(f"   {fmt_cfg(cfg):<22} n={m.get('n',0):>3} ret={m.get('ret',0):>+6.2f}% "
              f"PFnet={m.get('pf_net',0):>4.2f} WR={m.get('wr',0):.0f}%")
    eligible = [(cfg, m) for cfg, m in train_results if m.get("n", 0) >= args.min_is_trades]
    best_cfg, best_m = max(eligible, key=lambda x: x[1]["pf_net"])
    print(f"\n→ Config elegida (lock): {fmt_cfg(best_cfg)} | "
          f"train ret {best_m['ret']:+.2f}% PFnet {best_m['pf_net']:.2f}")

    # Test: aplicar config bloqueada al test-year (OOS puro).
    df5_te = load_5m(args.pair, args.test_year)
    te_trig = resample(df5_te, args.trig)
    te_htf = resample(df5_te, args.htf)
    m_te = run_window(te_trig, te_htf, best_cfg, args.no_cost, args.capital, args.risk)
    print(f"\n[TEST {args.test_year}] OUT-OF-SAMPLE con config bloqueada:")
    print(f"   n={m_te.get('n',0)} ret={m_te.get('ret',0):+.2f}% "
          f"PFnet={m_te.get('pf_net',0):.2f} PFbruto={m_te.get('pf_gross',0):.2f} "
          f"WR={m_te.get('wr',0):.0f}% DD={m_te.get('dd',0):.1f}% "
          f"trades/mes={m_te.get('trades_per_month',0):.1f}")
    verdict = "✅ OOS positivo" if m_te.get("ret", 0) > 0 and m_te.get("pf_net", 0) >= 1.1 \
        else "❌ OOS no convence"
    print(f"\nVEREDICTO HOLDOUT: {verdict}")


def summarize_os(os_metrics: list[dict], args):
    print("\n" + "=" * 70)
    print(f"  WFA RESUMEN — {args.pair} {args.year} | {args.trig}/{args.htf} | "
          f"{'SIN COSTOS' if args.no_cost else 'costos retail'}")
    print("=" * 70)
    valid = [m for m in os_metrics if m.get("n", 0) > 0]
    if not valid:
        print("Sin OS válidos.")
        return
    rets = [m["ret"] for m in valid]
    pos = sum(1 for r in rets if r > 0)
    total = sum(rets)
    pf_robust = [m["pf_net"] for m in valid if m.get("n", 0) >= 3 and m["pf_net"] != float("inf")]
    pf_med = statistics.median(pf_robust) if pf_robust else 0.0
    n_total = sum(m["n"] for m in valid)
    print(f"Ventanas OS válidas:   {len(valid)}")
    print(f"OS positivas:          {pos}/{len(valid)} ({pos/len(valid)*100:.0f}%)")
    print(f"OS retorno total:      {total:+.2f}%")
    print(f"OS retorno medio:      {total/len(valid):+.2f}% / ventana")
    print(f"OS PF neto mediano:    {pf_med:.2f} (n>=3: {len(pf_robust)} ventanas)")
    print(f"OS trades totales:     {n_total}")
    pos_ratio = pos / len(valid)
    if pf_med >= 1.15 and pos_ratio >= 0.55 and total >= 10:
        v = "✅ EDGE ROBUSTO — PF mediano>=1.15, mayoría OS positivas, ret>=+10%."
    elif pf_med >= 1.05 and total > 0:
        v = "🟡 EDGE MARGINAL — positivo pero al borde."
    else:
        v = "❌ NO PASA — edge insuficiente."
    print(f"\nVEREDICTO: {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["rolling", "holdout"], default="rolling")
    ap.add_argument("--pair", default="EURUSD")
    ap.add_argument("--year", type=int, default=2019)
    ap.add_argument("--train-year", type=int, default=2019)
    ap.add_argument("--test-year", type=int, default=2022)
    ap.add_argument("--trig", default="1h")
    ap.add_argument("--htf", default="4h")
    ap.add_argument("--is-days", type=int, default=90)
    ap.add_argument("--os-days", type=int, default=45)
    ap.add_argument("--step-days", type=int, default=45)
    ap.add_argument("--min-is-trades", type=int, default=4)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument("--no-cost", action="store_true")
    args = ap.parse_args()

    if args.mode == "rolling":
        mode_rolling(args)
    else:
        mode_holdout(args)


if __name__ == "__main__":
    main()
