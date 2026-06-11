"""
scripts/sweep_pairs_forex.py
Sweep multi-par del PriceActionEngineFX con CONFIG FIJA (default heredada de
cripto, SIN optimización por par — evita el overfit que tumbó el WFA en TAREA 1).
Costos retail reales. Mide, por par, la CONSISTENCIA del edge a través de años y
de ventanas walk-forward de 45d. Rankea para armar portafolio balanceado.

pip_size por par (JPY = 0.01). Cost model en "pips" (conteo) → consistente entre pares.

Uso:
    python scripts/sweep_pairs_forex.py --pairs EURUSD GBPUSD USDJPY ... \
        --years 2017 2018 --trig 1h --htf 4h
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

PIP_MAP = {  # pip size por par
    "USDJPY": 0.01, "EURJPY": 0.01, "GBPJPY": 0.01,
}
def pip_of(pair: str) -> float:
    return PIP_MAP.get(pair.upper(), 0.0001)

# Config FIJA = default validada en cripto. NO se optimiza por par.
FIXED = dict(pafx_vol_mult=1.5, pafx_atr_sl_mult=1.5, pafx_tp_rr=2.5,
             pafx_fractal_n=3, pafx_sl_min_pips=8.0, pafx_sl_max_pips=60.0)


class S:
    def __init__(self, capital, risk, pip):
        self.initial_capital = capital
        self.max_risk_per_trade = risk
        for k, v in FIXED.items():
            setattr(self, k, v)
        self.pafx_pip_size = pip


def cost_kwargs(pip, no_cost, swap=0.3):
    if no_cost:
        return dict(min_spread_pips=0.0, commission_pips_rt=0.0,
                    swap_pips_per_night=0.0, pip_size=pip)
    return dict(min_spread_pips=0.8, commission_pips_rt=0.6,
                swap_pips_per_night=swap, pip_size=pip)


def windows_45d(df_trig):
    t0, end = df_trig.index[0], df_trig.index[-1]
    step = pd.Timedelta(days=45)
    out, a = [], t0
    while a + step <= end:
        out.append((a, a + step)); a += step
    return out


def run_pair_year(pair, year, trig, htf, capital, risk, no_cost):
    pip = pip_of(pair)
    df5 = load_5m(pair, year)
    dt = resample(df5, trig)
    dh = resample(df5, htf)
    s = S(capital, risk, pip)
    ck = cost_kwargs(pip, no_cost)
    # Año completo
    sim = run_backtest(dt, dh, s, ck)
    days = (dt.index[-1] - dt.index[0]).total_seconds() / 86400.0
    m = metrics(sim, days)
    # Walk-forward 45d (consistencia)
    win_rets = []
    for (x, y) in windows_45d(dt):
        s2 = S(capital, risk, pip)
        sw = run_backtest(dt.loc[x:y], dh, s2, ck)
        d2 = (y - x).total_seconds() / 86400.0
        win_rets.append(metrics(sw, d2).get("ret", 0.0))
    m["win_pos"] = sum(1 for r in win_rets if r > 0)
    m["win_total"] = len(win_rets)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", nargs="+", required=True)
    ap.add_argument("--years", nargs="+", type=int, required=True)
    ap.add_argument("--trig", default="1h")
    ap.add_argument("--htf", default="4h")
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument("--no-cost", action="store_true")
    args = ap.parse_args()

    print(f"\nSWEEP {args.trig}/{args.htf} | config FIJA | "
          f"{'SIN COSTOS' if args.no_cost else 'costos retail'} | risk {args.risk:.1%}")
    print(f"Pares: {args.pairs}  Años: {args.years}\n")

    agg = {}
    for pair in args.pairs:
        print(f"━━━ {pair} ━━━")
        rets, pfs, dds, ns, wpos, wtot, yr_pos = [], [], [], [], 0, 0, 0
        for yr in args.years:
            try:
                m = run_pair_year(pair, yr, args.trig, args.htf,
                                  args.capital, args.risk, args.no_cost)
            except Exception as e:
                print(f"   {yr}: ERROR {e}")
                continue
            if m.get("n", 0) == 0:
                print(f"   {yr}: sin trades")
                continue
            rets.append(m["ret"]); pfs.append(m["pf_net"]); dds.append(m["dd"])
            ns.append(m["n"]); wpos += m["win_pos"]; wtot += m["win_total"]
            if m["ret"] > 0:
                yr_pos += 1
            print(f"   {yr}: ret={m['ret']:>+7.2f}%  PFnet={m['pf_net']:>4.2f}  "
                  f"WR={m['wr']:>4.0f}%  DD={m['dd']:>4.1f}%  n={m['n']:>3}  "
                  f"WF45d={m['win_pos']}/{m['win_total']}")
        if rets:
            pf_robust = [p for p in pfs if p != float('inf')]
            agg[pair] = {
                "avg_ret": sum(rets) / len(rets),
                "sum_ret": sum(rets),
                "yr_pos": yr_pos, "yr_tot": len(rets),
                "pf_med": statistics.median(pf_robust) if pf_robust else 0,
                "avg_dd": sum(dds) / len(dds), "max_dd": max(dds),
                "avg_n": sum(ns) / len(ns),
                "wf_pos": wpos, "wf_tot": wtot,
            }
        print()

    # Ranking
    print("=" * 92)
    print(f"  RANKING — {args.trig}/{args.htf} (config fija, costos reales) — "
          f"ordenado por % ventanas WF positivas")
    print("=" * 92)
    print(f"{'Par':<8}{'ret medio/año':>14}{'años+':>8}{'WF 45d +':>12}"
          f"{'PF med':>8}{'DD medio':>10}{'DD max':>9}{'n/año':>8}")
    print("─" * 92)
    ranked = sorted(agg.items(),
                    key=lambda kv: (kv[1]["wf_pos"] / max(kv[1]["wf_tot"], 1),
                                    kv[1]["avg_ret"]), reverse=True)
    for pair, a in ranked:
        wf_pct = a["wf_pos"] / max(a["wf_tot"], 1) * 100
        yr_str = f"{a['yr_pos']}/{a['yr_tot']}"
        wf_str = f"{a['wf_pos']}/{a['wf_tot']} ({wf_pct:.0f}%)"
        print(f"{pair:<8}{a['avg_ret']:>+13.2f}%{yr_str:>8}{wf_str:>12}"
              f"{a['pf_med']:>8.2f}{a['avg_dd']:>9.1f}%{a['max_dd']:>8.1f}%"
              f"{a['avg_n']:>8.0f}")
    print("─" * 92)


if __name__ == "__main__":
    main()
