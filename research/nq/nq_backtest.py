"""
Backtest de estrategias de 1 operación por día en NQ (operando MNQ), con costos reales y validación.

Estrategias:
  ORB        Opening Range Breakout de 5 min (Zarattini y Aziz, 2023). Si la vela 9:30–9:35 ET cierra
             verde → LONG a las 9:35; roja → SHORT. Stop del otro lado de esa vela. Sale a las 16:00 ET.
  ORB-2R     Igual, con objetivo en 2R.
  ORB-ATR    Igual, con stop en 10 % del ATR diario (versión de Zarattini, Barbon y Aziz, 2024).
  MOM        Momentum de la primera media hora (Gao, Han, Li y Zhou, 2018): si NQ subió desde el
             cierre de ayer (16:00) hasta las 10:00 → LONG de 15:30 a 16:00 (y al revés). Stop 10 % ATR.
  ALEX       La estrategia del bot (zona 1D + Fibonacci 1H + diagonal 5m, bot/strategy.py), SL Fibo 0,786,
             TP en el techo del impulso, R:R ≥ 1,5. Sólo entradas de 9:30 a 15:30 ET; cierra 16:55 ET.
  ALEX-EST   ALEX con SL estructural + filtro de tendencia EMA50 diaria.
  RANDOM     Control: mismo horario y stop que ORB pero dirección al azar (se repite --random-runs veces).

Reglas comunes: máximo 1 operación por día; todo cerrado antes de las 16:59 ET (regla de Apex);
tamaño en micros = riesgo en USD / (stop en puntos × $2 + costos), máximo --max-micros.
Costos: comisión por micro ida y vuelta (--commission) + 1 tick (0,25 pt) de deslizamiento por cada
orden a mercado o stop (las salidas por objetivo son límite, sin deslizamiento).
Si en la misma vela de 1 minuto se tocan stop y objetivo, cuenta el stop.

Validación: IS = hasta --oos-start (para elegir) · OOS = desde --oos-start (para validar, no se mira
para elegir). "t" ≥ 2 ≈ evidencia razonable de que el promedio no es cero.

Uso:
  python research/nq/nq_backtest.py
  python research/nq/nq_backtest.py --risk 375 --oos-start 2023-01-01 --only ORB,ORB-ATR,RANDOM
Guarda las operaciones en data/nq/trades_<ESTRATEGIA>.csv para apex_sim.py.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import mean, pstdev

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from nq_data import DATA, NQ, daily_sessions, load_raw, prepare, resample, rth_bounds  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

POINT_USD = 2.0          # MNQ
TICK = 0.25
MIN = 60_000


@dataclass
class Trade:
    strategy: str
    session: pd.Timestamp
    side: str
    entry_ts: int
    entry: float
    stop: float
    target: float | None
    exit_ts: int = 0
    exit: float = 0.0
    reason: str = ""

    @property
    def sign(self) -> int:
        return 1 if self.side == "LONG" else -1

    @property
    def stop_pts(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def pnl_pts(self) -> float:
        return self.sign * (self.exit - self.entry)


@dataclass
class Costs:
    commission: float = 1.50     # USD por micro, ida y vuelta
    slip_ticks: float = 1.0      # por orden a mercado/stop
    max_micros: int = 60


def size_pnl(t: Trade, risk_usd: float, k: Costs) -> tuple[int, float]:
    """(micros, pnl en USD). 0 micros = el stop es demasiado ancho para ese riesgo."""
    slip = k.slip_ticks * TICK * POINT_USD
    exits_market = t.reason != "TP"
    per_micro_risk = t.stop_pts * POINT_USD + k.commission + 2 * slip
    n = min(k.max_micros, int(risk_usd // per_micro_risk)) if per_micro_risk > 0 else 0
    if n < 1:
        return 0, 0.0
    pnl = n * (t.pnl_pts * POINT_USD - k.commission - slip * (2 if exits_market else 1))
    return n, pnl


# ───────────────────────── salida con velas de 1 minuto ─────────────────────────

def run_exit(nq: NQ, t: Trade, start: int, last: int) -> Trade:
    """Recorre las velas de 1 min desde `start` hasta `last` (inclusive) y cierra en stop, objetivo o tiempo."""
    long = t.side == "LONG"
    for i in range(start, last + 1):
        if (nq.l[i] <= t.stop) if long else (nq.h[i] >= t.stop):
            # si abre más allá del stop (hueco) se ejecuta en la apertura
            px = min(nq.o[i], t.stop) if long else max(nq.o[i], t.stop)
            return replace(t, exit_ts=int(nq.ts[i]), exit=float(px), reason="SL")
        if t.target is not None and ((nq.h[i] >= t.target) if long else (nq.l[i] <= t.target)):
            return replace(t, exit_ts=int(nq.ts[i]), exit=float(t.target), reason="TP")
    return replace(t, exit_ts=int(nq.ts[last]), exit=float(nq.c[last]), reason="TIEMPO")


def find_minute(nq: NQ, lo: int, hi: int, minute: int) -> int | None:
    """Índice de la vela de las `minute` (minutos ET) entre lo y hi, o None si falta."""
    seg = nq.minute[lo:hi + 1]
    pos = np.flatnonzero(seg == minute)
    return lo + int(pos[0]) if len(pos) else None


# ───────────────────────── estrategias ─────────────────────────

def orb(nq: NQ, rth: dict, daily: pd.DataFrame, variant: str = "ORB",
        rng: random.Random | None = None) -> list[Trade]:
    out = []
    for sess, (lo, hi) in rth.items():
        i0, i_in = find_minute(nq, lo, hi, 9 * 60 + 30), find_minute(nq, lo, hi, 9 * 60 + 35)
        if i0 is None or i_in is None or i_in - i0 != 5:
            continue
        o, c = nq.o[i0], nq.c[i_in - 1]
        h, l = nq.h[i0:i_in].max(), nq.l[i0:i_in].min()
        if rng is not None:
            side = rng.choice(("LONG", "SHORT"))
        elif c > o:
            side = "LONG"
        elif c < o:
            side = "SHORT"
        else:
            continue
        entry = float(nq.o[i_in])
        if variant == "ORB-ATR":
            atr = daily["atr14_prev"].get(sess)
            if atr is None or not np.isfinite(atr):
                continue
            stop = entry - 0.1 * atr if side == "LONG" else entry + 0.1 * atr
        else:
            stop = float(l) if side == "LONG" else float(h)
        if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
            continue
        target = None
        if variant == "ORB-2R":
            target = entry + 2 * (entry - stop)
        t = Trade("RANDOM" if rng else variant, sess, side, int(nq.ts[i_in]), entry, stop, target)
        out.append(run_exit(nq, t, i_in, hi))
    return out


def momentum(nq: NQ, rth: dict, daily: pd.DataFrame) -> list[Trade]:
    out, prev_close = [], None
    for sess, (lo, hi) in rth.items():
        i10, i_in = find_minute(nq, lo, hi, 10 * 60), find_minute(nq, lo, hi, 15 * 60 + 30)
        atr = daily["atr14_prev"].get(sess)
        if prev_close is not None and i10 is not None and i_in is not None and atr is not None and np.isfinite(atr):
            r = nq.c[i10 - 1] / prev_close - 1
            if abs(r) > 1e-4:
                side = "LONG" if r > 0 else "SHORT"
                entry = float(nq.o[i_in])
                stop = entry - 0.1 * atr if side == "LONG" else entry + 0.1 * atr
                out.append(run_exit(nq, Trade("MOM", sess, side, int(nq.ts[i_in]), entry, stop, None), i_in, hi))
        prev_close = float(nq.c[hi])
    return out


def alex(nq: NQ, variant: str = "ALEX", min_rr: float = 1.5) -> list[Trade]:
    from bot.strategy import StrategyParams, SymbolStrategy, build_bars, build_daily_zones, build_hourly

    params = StrategyParams() if variant == "ALEX" else StrategyParams(sl_mode="structure", filter_trend=True)
    daily = daily_sessions(nq)
    d_candles = [{"time": int(r.time), "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                  "volume": r.volume} for r in daily.itertuples()]
    m5 = resample(nq.m1, "5min")
    bars = build_bars(m5, build_hourly(resample(nq.m1, "1h"), params), build_daily_zones(d_candles, params), params)
    engine = SymbolStrategy("NQ", params)
    out: list[Trade] = []
    taken: set = set()
    for bar in bars:
        for ev in engine.on_bar(bar):
            if ev["event"] != "entry":
                continue
            entry_ts = bar.time + 5 * MIN
            i = nq.at_or_after(entry_ts)
            if i >= len(nq.ts) or nq.ts[i] != entry_ts:
                continue
            sess = pd.Timestamp(nq.session[i])
            minute = nq.minute[i]
            if sess in taken or not (9 * 60 + 30 <= minute <= 15 * 60 + 30):
                continue
            entry, sl, tp = float(nq.o[i]), float(ev["sl"]), float(ev["tp"])
            side = ev["side"]
            risk, reward = abs(entry - sl), abs(tp - entry)
            ordered = (tp > entry > sl) if side == "LONG" else (tp < entry < sl)
            if not ordered or reward / risk < min_rr:
                continue
            # cierre obligatorio 16:55 ET de la misma sesión
            j = i
            while j + 1 < len(nq.ts) and nq.session[j + 1] == nq.session[i] and nq.minute[j + 1] < 16 * 60 + 55:
                j += 1
            taken.add(sess)
            out.append(run_exit(nq, Trade(variant, sess, side, entry_ts, entry, sl, tp), i, j))
    return out


# ───────────────────────── métricas ─────────────────────────

def evaluate(trades: list[Trade], risk: float, k: Costs) -> list[dict]:
    rows = []
    for t in trades:
        n, pnl = size_pnl(t, risk, k)
        rows.append({"session": t.session, "side": t.side, "entry": t.entry, "stop": t.stop, "exit": t.exit,
                     "stop_pts": t.stop_pts, "pnl_pts": t.pnl_pts, "reason": t.reason, "micros": n,
                     "pnl_usd": pnl, "r": pnl / risk if n else None})
    return rows


def stats(rows: list[dict]) -> dict:
    rs = [r["r"] for r in rows if r["r"] is not None]
    skipped = sum(1 for r in rows if r["r"] is None)
    if not rs:
        return {"n": 0, "skipped": skipped}
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    sd = pstdev(rs) if len(rs) > 1 else 0.0
    gw, gl = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    sessions = [r["session"] for r in rows if r["r"] is not None]
    months = max(1.0, (max(sessions) - min(sessions)).days / 30.44)
    return {"n": len(rs), "skipped": skipped, "win": sum(r > 0 for r in rs) / len(rs), "avg": mean(rs),
            "tot": sum(rs), "pf": gw / gl if gl else math.inf, "dd": dd, "per_month": len(rs) / months,
            "t": mean(rs) / (sd / math.sqrt(len(rs))) if sd and len(rs) >= 10 else None,
            "usd": sum(r["pnl_usd"] for r in rows)}


def fmt(st: dict) -> str:
    if not st.get("n"):
        return f"sin operaciones ({st.get('skipped', 0)} descartadas por stop ancho)"
    t = f"{st['t']:+.1f}" if st["t"] is not None else "—"
    text = (f"{st['n']:>5} ops · {st['per_month']:4.1f}/mes · acierto {st['win']:4.0%} · {st['avg']:+.3f} R/op "
            f"(t {t}) · PF {st['pf']:.2f} · caída máx {st['dd']:.1f} R · {st['usd']:+_.0f} USD")
    return text.replace(".", ",").replace("_", ".")      # formato argentino


def split(rows: list[dict], oos: pd.Timestamp) -> tuple[list[dict], list[dict]]:
    return [r for r in rows if r["session"] < oos], [r for r in rows if r["session"] >= oos]


def by_year(rows: list[dict]) -> str:
    years: dict[int, list[float]] = {}
    for r in rows:
        if r["r"] is not None:
            years.setdefault(r["session"].year, []).append(r["r"])
    return " · ".join(f"{y}: {sum(v):+.1f}R" for y, v in sorted(years.items()))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--risk", type=float, default=375.0, help="USD por operación (0,75 %% de 50k)")
    ap.add_argument("--commission", type=float, default=1.50, help="USD por micro ida y vuelta")
    ap.add_argument("--slip-ticks", type=float, default=1.0)
    ap.add_argument("--max-micros", type=int, default=60)
    ap.add_argument("--oos-start", default="2023-01-01")
    ap.add_argument("--random-runs", type=int, default=50)
    ap.add_argument("--only", default="", help="lista separada por comas, ej. ORB,ALEX")
    args = ap.parse_args()

    costs = Costs(args.commission, args.slip_ticks, args.max_micros)
    oos = pd.Timestamp(args.oos_start)
    print("Cargando datos...", flush=True)
    raw = load_raw()
    nq = prepare(raw)
    daily = daily_sessions(nq)
    rth = rth_bounds(nq)
    first, last = min(rth), max(rth)
    pd.DataFrame({"session": sorted(rth)}).to_csv(DATA / "calendar.csv", index=False)
    print(f"NQ 1 min: {len(nq.ts):,} velas · {len(rth)} días de RTH · {first:%d/%m/%Y} → {last:%d/%m/%Y} · "
          f"vencimientos ajustados: {raw.pipe(lambda d: d['instrument_id'].ne(d['instrument_id'].shift()).sum() - 1)}")
    print(f"Riesgo {args.risk:.0f} USD/op · comisión {args.commission:.2f} USD/micro · deslizamiento "
          f"{args.slip_ticks:g} tick · IS hasta {oos:%d/%m/%Y} · OOS desde ahí\n")

    wanted = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    runners = {
        "ORB": lambda: orb(nq, rth, daily, "ORB"),
        "ORB-2R": lambda: orb(nq, rth, daily, "ORB-2R"),
        "ORB-ATR": lambda: orb(nq, rth, daily, "ORB-ATR"),
        "MOM": lambda: momentum(nq, rth, daily),
        "ALEX": lambda: alex(nq, "ALEX"),
        "ALEX-EST": lambda: alex(nq, "ALEX-EST"),
    }
    results: dict[str, list[dict]] = {}
    for name, fn in runners.items():
        if wanted and name not in wanted:
            continue
        print(f"  corriendo {name}...", flush=True)
        rows = evaluate(fn(), args.risk, costs)
        results[name] = rows
        pd.DataFrame(rows).to_csv(DATA / f"trades_{name}.csv", index=False)

    rand_avgs: tuple[list[float], list[float]] = ([], [])
    if not wanted or "RANDOM" in wanted:
        print(f"  corriendo RANDOM ×{args.random_runs}...", flush=True)
        for k in range(args.random_runs):
            rows = evaluate(orb(nq, rth, daily, "ORB", rng=random.Random(k)), args.risk, costs)
            a, b = split(rows, oos)
            for bucket, part in zip(rand_avgs, (a, b)):
                st = stats(part)
                if st.get("n"):
                    bucket.append(st["avg"])

    print("\n" + "=" * 120)
    for name, rows in results.items():
        is_rows, oos_rows = split(rows, oos)
        print(f"\n{name}")
        print(f"  IS   {fmt(stats(is_rows))}")
        print(f"  OOS  {fmt(stats(oos_rows))}")
        print(f"  por año → {by_year(rows)}")
        for label, part, bucket in (("IS", is_rows, rand_avgs[0]), ("OOS", oos_rows, rand_avgs[1])):
            st = stats(part)
            if bucket and st.get("n"):
                beat = sum(st["avg"] > x for x in bucket) / len(bucket)
                print(f"  {label}: le gana al {beat:.0%} de las corridas al azar")
    if rand_avgs[1]:
        print(f"\nRANDOM (dirección al azar, {len(rand_avgs[1])} corridas): R/op promedio IS "
              f"{mean(rand_avgs[0]):+.3f} · OOS {mean(rand_avgs[1]):+.3f} (rango OOS {min(rand_avgs[1]):+.3f} a "
              f"{max(rand_avgs[1]):+.3f})")
    print("\nCómo leerlo: una estrategia sirve si es positiva en IS **y** en OOS, con t ≥ 2 y le gana a casi todas las"
          " corridas al azar.\nSiguiente paso: python research/nq/apex_sim.py --strategy <NOMBRE>")


if __name__ == "__main__":
    main()
