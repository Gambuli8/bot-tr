"""
Simulador de la cuenta Apex 50K EOD (Tradovate, sin fee de activación) con las operaciones REALES de un
backtest (data/nq/trades_<ESTRATEGIA>.csv, generado por nq_backtest.py).

Reglas que simula (confirmalas en Apex antes de pagar; están todas como parámetros):
  PRUEBA  objetivo +$3.000 · drawdown $2.000 trailing al CIERRE del día (EOD) · pérdida diaria máx $1.000
          (corta el día, no te elimina) · vence a los 30 días corridos · costo US$ 119.
  PA      drawdown $2.000 EOD que deja de subir en $50.100 · retiro cuando: saldo ≥ $52.600,
          ≥ 5 días con ganancia ≥ $250 y ningún día ≥ 50 % de la ganancia · primer retiro máx $1.500.

Dos formas de medir:
  HISTÓRICO  arranca una prueba en CADA día del período y la corre con los días reales que siguieron.
  BOOTSTRAP  arma --sims secuencias al azar sacando días reales del período (con reposición).

El tamaño se recalcula para cada riesgo: micros = riesgo / (stop × $2 + costos).

Uso:
  python research/nq/apex_sim.py --strategy ORB
  python research/nq/apex_sim.py --strategy ORB --eval-risks 250,375,500 --pa-risk 250 --period oos
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from nq_backtest import POINT_USD, TICK  # noqa: E402
from nq_data import DATA  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


@dataclass
class Rules:
    target: float = 3000.0
    dd: float = 2000.0
    dll: float = 1000.0
    eval_days: int = 30            # días corridos
    eval_fee: float = 119.0
    pa_lock: float = 100.0         # el drawdown de la PA deja de subir en inicial + 100
    pa_min_balance: float = 2600.0  # ganancia mínima para pedir retiro (52.600 − 50.000)
    pa_good_day: float = 250.0
    pa_good_days: int = 5
    consistency: float = 0.5
    first_payout_max: float = 1500.0
    pa_max_days: int = 250         # días hábiles; si no se resuelve antes queda "sin resolver"


def day_pnl(trades: pd.DataFrame, calendar: list[pd.Timestamp], risk: float, commission: float,
            slip_ticks: float, max_micros: int) -> list[float]:
    """P&L en USD de cada día del calendario (0 si no hubo operación) para un riesgo dado."""
    slip = slip_ticks * TICK * POINT_USD
    by_day: dict[pd.Timestamp, float] = {}
    for t in trades.itertuples():
        per_micro = t.stop_pts * POINT_USD + commission + 2 * slip
        n = min(max_micros, int(risk // per_micro)) if per_micro > 0 else 0
        if n < 1:
            continue
        pnl = n * (t.pnl_pts * POINT_USD - commission - slip * (1 if t.reason == "TP" else 2))
        by_day[t.session] = by_day.get(t.session, 0.0) + pnl
    return [by_day.get(d, 0.0) for d in calendar]


def run_eval(days: list[float], dates: list[pd.Timestamp] | None, r: Rules) -> tuple[str, int]:
    """('pass'|'fail'|'expired', días hábiles usados)."""
    bal, thr = 0.0, -r.dd
    start = dates[0] if dates else None
    for k, pnl in enumerate(days):
        if dates is not None and (dates[k] - start).days >= r.eval_days:
            return "expired", k
        if dates is None and k >= round(r.eval_days * 5 / 7):
            return "expired", k
        bal += max(pnl, -r.dll)
        if bal <= thr:
            return "fail", k + 1
        if bal >= r.target:
            return "pass", k + 1
        thr = max(thr, bal - r.dd)
    if dates is None:
        return "expired", len(days)       # secuencia sintética: se le terminaron los días de la prueba
    return "unresolved", len(days)


def run_pa(days: list[float], r: Rules) -> tuple[str, int, float]:
    """('payout'|'fail'|'unresolved', días, monto del primer retiro)."""
    bal, thr, good, best = 0.0, -r.dd, 0, 0.0
    for k, pnl in enumerate(days[: r.pa_max_days]):
        pnl = max(pnl, -r.dll)
        bal += pnl
        if bal <= thr:
            return "fail", k + 1, 0.0
        thr = min(r.pa_lock, max(thr, bal - r.dd))
        good += pnl >= r.pa_good_day
        best = max(best, pnl)
        if bal >= r.pa_min_balance and good >= r.pa_good_days and best < r.consistency * bal:
            return "payout", k + 1, min(r.first_payout_max, bal - (r.dd + r.pa_lock))
    return "unresolved", min(len(days), r.pa_max_days), 0.0


def summarize(label: str, evals: list[tuple[str, int]], pas: list[tuple[str, int, float]], r: Rules) -> None:
    n = len(evals)
    if not n:
        print(f"  {label}: sin datos")
        return
    cnt = {k: sum(1 for e in evals if e[0] == k) for k in ("pass", "fail", "expired", "unresolved")}
    passed_days = [d for s, d in evals if s == "pass"]
    resolved_pa = [p for p in pas if p[0] != "unresolved"]
    pay = [p for p in resolved_pa if p[0] == "payout"]
    p_pass = cnt["pass"] / n
    p_pay = len(pay) / len(resolved_pa) if resolved_pa else 0.0
    avg_pay = sum(p[2] for p in pay) / len(pay) if pay else 0.0
    ev = p_pass * p_pay * avg_pay - r.eval_fee
    print(f"  {label}: pasa {p_pass:5.0%} (en {sum(passed_days) / max(1, len(passed_days)):4.1f} días hábiles) · "
          f"quema {cnt['fail'] / n:4.0%} · vence {cnt['expired'] / n:4.0%}"
          + (f" · sin resolver {cnt['unresolved'] / n:.0%}" if cnt["unresolved"] else ""))
    if pas:
        cost_pa = f"{r.eval_fee / p_pass:_.0f}" if p_pass else "∞"
        text = (f"         PA → primer retiro {p_pay:5.0%} de las PA "
                f"(en {sum(p[1] for p in pay) / max(1, len(pay)):4.0f} días, US$ {avg_pay:_.0f} promedio)"
                f" · sin resolver {sum(1 for p in pas if p[0] == 'unresolved')}\n"
                f"         valor esperado por prueba de US$ {r.eval_fee:.0f}: {ev:+_.0f} USD"
                f" · costo en pruebas por cada PA: US$ {cost_pa}")
        print(text.replace("_", "."))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", required=True, help="nombre usado en nq_backtest.py (ORB, ALEX, ...)")
    ap.add_argument("--eval-risks", default="250,375,500")
    ap.add_argument("--pa-risk", type=float, default=250.0)
    ap.add_argument("--period", choices=["oos", "is", "all"], default="oos")
    ap.add_argument("--oos-start", default="2023-01-01")
    ap.add_argument("--commission", type=float, default=1.50)
    ap.add_argument("--slip-ticks", type=float, default=1.0)
    ap.add_argument("--max-micros", type=int, default=60)
    ap.add_argument("--pa-max-micros", type=int, default=30, help="límite de la PA al inicio (confirmar en Apex)")
    ap.add_argument("--sims", type=int, default=10000)
    args = ap.parse_args()

    path = DATA / f"trades_{args.strategy.upper()}.csv"
    if not path.exists():
        raise SystemExit(f"No existe {path}. Corré primero nq_backtest.py")
    trades = pd.read_csv(path, parse_dates=["session"])
    cal_path = DATA / "calendar.csv"
    if cal_path.exists():
        calendar = list(pd.read_csv(cal_path, parse_dates=["session"])["session"])
    else:   # sin calendario: días hábiles entre la primera y la última operación
        calendar = list(pd.bdate_range(trades["session"].min(), trades["session"].max()))
    oos = pd.Timestamp(args.oos_start)
    if args.period == "oos":
        calendar = [d for d in calendar if d >= oos]
    elif args.period == "is":
        calendar = [d for d in calendar if d < oos]
    trades = trades[trades["session"].isin(set(calendar))]
    r = Rules()

    print(f"Apex 50K EOD · {args.strategy.upper()} · período {args.period.upper()} "
          f"({calendar[0]:%d/%m/%Y} → {calendar[-1]:%d/%m/%Y}, {len(calendar)} días hábiles, "
          f"{len(trades)} operaciones) · PA con riesgo US$ {args.pa_risk:.0f}\n")
    pa_days = day_pnl(trades, calendar, args.pa_risk, args.commission, args.slip_ticks, args.pa_max_micros)
    for risk in [float(x) for x in args.eval_risks.split(",")]:
        ev_days = day_pnl(trades, calendar, risk, args.commission, args.slip_ticks, args.max_micros)
        print(f"Prueba con riesgo US$ {risk:.0f} por operación")
        evals, pas = [], []
        for s in range(len(calendar)):
            res = run_eval(ev_days[s:], calendar[s:], r)
            if res[0] == "unresolved":
                continue
            evals.append(res)
            if res[0] == "pass":
                start_pa = s + res[1]
                pas.append(run_pa(pa_days[start_pa:], r) if start_pa < len(pa_days) else ("unresolved", 0, 0.0))
        summarize("HISTÓRICO", evals, pas, r)

        rng = random.Random(1)
        evals, pas = [], []
        n_eval = round(r.eval_days * 5 / 7)
        for _ in range(args.sims):
            seq = [rng.choice(ev_days) for _ in range(n_eval)]
            res = run_eval(seq, None, r)
            evals.append(res)
            if res[0] == "pass":
                pas.append(run_pa([rng.choice(pa_days) for _ in range(r.pa_max_days)], r))
        summarize("BOOTSTRAP", evals, pas, r)
        print()


if __name__ == "__main__":
    main()
