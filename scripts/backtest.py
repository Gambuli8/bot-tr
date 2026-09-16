"""
Backtest de la estrategia (motor real de bot/strategy.py) con meses de velas.

Datos: BingX sólo guarda ~45 días de velas de 5m, así que se usa la API pública de
Binance Futures (mismos pares, precios prácticamente iguales). Se cachean en data/backtest/.

Simulación (conservadora):
  - Entrada al cierre de la vela de 5m + slippage 0,03% en contra.
  - Comisiones taker de BingX en entrada y salida (vienen en el sizing).
  - Si en la misma vela se tocan SL y TP → cuenta como SL.
  - Mismos límites que el bot: 1 posición por par, máx. 3 abiertas, pérdida diaria (día argentino).
  - Sizing real: margen fijo, apalancamiento mínimo y mínimos de contrato de BingX.

Anti-sobreajuste: las variantes se ordenan con los primeros meses (in-sample) y se
muestran en los últimos meses (out-of-sample), que NO se usan para elegir.

Uso:
  python scripts/backtest.py --months 12 --oos-months 4
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.bingx import BingXClient  # noqa: E402
from bot.sizing import build_plan  # noqa: E402
from bot.strategy import (MS_1D, StrategyParams, SymbolStrategy, build_bars,  # noqa: E402
                          build_daily_zones, build_hourly)

SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "ZEC-USDT", "DOGE-USDT"]
CACHE = Path("data/backtest")
TZ = ZoneInfo("America/Argentina/Buenos_Aires")
SLIPPAGE = 0.0003
MARGIN = 2.0
MAX_LEVERAGE = 20
MAX_OPEN = 3
DAILY_LOSS = 3.0
INTERVAL_MS = {"5m": 300_000, "1h": 3_600_000, "1d": 86_400_000}


# ───────────────────────── datos ─────────────────────────

def fetch_binance(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol}_{interval}.json"
    if path.exists():
        data = json.loads(path.read_text())
        if data and data[0]["time"] <= start_ms and data[-1]["time"] >= end_ms - 2 * INTERVAL_MS[interval]:
            return [c for c in data if start_ms <= c["time"] < end_ms]
    out, cursor = [], start_ms
    while cursor < end_ms:
        for attempt in range(5):
            try:
                resp = requests.get("https://fapi.binance.com/fapi/v1/klines", params={
                    "symbol": symbol.replace("-", ""), "interval": interval, "startTime": cursor,
                    "endTime": end_ms, "limit": 1500}, timeout=20)
                resp.raise_for_status()
                rows = resp.json()
                break
            except requests.RequestException as exc:
                print(f"  reintento {symbol} {interval}: {exc}")
                time.sleep(2 * (attempt + 1))
        else:
            raise SystemExit(f"No pude bajar {symbol} {interval}")
        if not rows:
            break
        out += [{"time": int(r[0]), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                 "close": float(r[4]), "volume": float(r[5])} for r in rows]
        cursor = int(rows[-1][0]) + INTERVAL_MS[interval]
        time.sleep(0.15)
    now_ms = int(time.time() * 1000)
    out = [c for c in out if c["time"] + INTERVAL_MS[interval] <= now_ms]
    path.write_text(json.dumps(out))
    return out


# ───────────────────────── motor → entradas ─────────────────────────

@dataclass
class Candidate:
    symbol: str
    side: str
    time: int          # cierre de la vela de entrada
    i: int             # índice de la vela en la lista de 5m
    price: float
    sl: float
    imp_tp: float      # techo del impulso


def engine_candidates(symbol, params, daily, hourly, m5, start_ms):
    zones = build_daily_zones(daily, params)
    hctx = build_hourly(hourly, params)
    bars = build_bars(m5, hctx, zones, params)
    engine = SymbolStrategy(symbol, params)
    out = []
    for i, bar in enumerate(bars):
        for ev in engine.on_bar(bar):
            if ev["event"] == "entry" and ev["time"] >= start_ms:
                out.append(Candidate(symbol, ev["side"], ev["time"], i, ev["price"], ev["sl"], ev["tp"]))
    return bars, out


def ema(values, period):
    k, out, cur = 2 / (period + 1), [], None
    for v in values:
        cur = v if cur is None else v * k + cur * (1 - k)
        out.append(cur)
    return out


# ───────────────────────── simulación ─────────────────────────

@dataclass(frozen=True)
class Variant:
    sl_mode: str
    tp: str            # impulse | 2R | 3R
    min_sl_pct: float
    min_rr: float
    trend: str         # none | ema50 | ema200

    def label(self) -> str:
        return f"SL={self.sl_mode:<9} TP={self.tp:<7} SLmin={self.min_sl_pct:.1f}% RRmin={self.min_rr:.1f} tend={self.trend}"


def outcome(bars, c: Candidate, entry: float, sl: float, tp: float):
    long = c.side == "LONG"
    for j in range(c.i + 1, len(bars)):
        b = bars[j]
        if (b.low <= sl) if long else (b.high >= sl):
            return "SL", bars[j].time + 300_000
        if (b.high >= tp) if long else (b.low <= tp):
            return "TP", bars[j].time + 300_000
    return None, None


def trend_ok(c: Candidate, trend: str, daily_ctx) -> bool:
    if trend == "none":
        return True
    times, closes, e50, e200 = daily_ctx[c.symbol]
    import bisect
    idx = bisect.bisect_right(times, c.time - MS_1D) - 1  # último día cerrado
    if idx < 0:
        return False
    ref = e50[idx] if trend == "ema50" else e200[idx]
    return closes[idx] > ref if c.side == "LONG" else closes[idx] < ref


def simulate(variant: Variant, candidates, bars_by_symbol, specs, daily_ctx, outcome_cache):
    trades = []
    for c in candidates:
        if not trend_ok(c, variant.trend, daily_ctx):
            continue
        long = c.side == "LONG"
        entry = c.price * (1 + SLIPPAGE) if long else c.price * (1 - SLIPPAGE)
        risk_px = (entry - c.sl) if long else (c.sl - entry)
        if risk_px <= 0 or risk_px / entry * 100 < variant.min_sl_pct:
            continue
        if variant.tp == "impulse":
            tp = c.imp_tp
        else:
            k = float(variant.tp[0])
            tp = entry + k * risk_px if long else entry - k * risk_px
        plan = build_plan(symbol=c.symbol, direction=c.side, entry=entry, stop_loss=c.sl, take_profit=tp,
                          spec=specs[c.symbol], margin_usdt=MARGIN, max_leverage=MAX_LEVERAGE, min_rr=variant.min_rr)
        if not plan.ok:
            continue
        key = (c.symbol, c.i, round(c.sl, 10), round(tp, 10))
        if key not in outcome_cache:
            outcome_cache[key] = outcome(bars_by_symbol[c.symbol], c, entry, c.sl, tp)
        res, exit_time = outcome_cache[key]
        if res is None:
            continue
        pnl = plan.reward_usdt if res == "TP" else -plan.risk_usdt
        trades.append({"symbol": c.symbol, "side": c.side, "time": c.time, "exit": exit_time, "res": res,
                       "pnl": pnl, "r": pnl / plan.risk_usdt, "rr": plan.rr})

    # Portafolio: una por par, máx. abiertas, límite de pérdida diaria (día argentino)
    accepted, open_trades, day_pnl = [], [], {}
    for t in sorted(trades, key=lambda x: x["time"]):
        open_trades = [o for o in open_trades if o["exit"] > t["time"]]
        for o in [a for a in accepted if a["exit"] <= t["time"] and not a.get("_booked")]:
            d = datetime.fromtimestamp(o["exit"] / 1000, TZ).date()
            day_pnl[d] = day_pnl.get(d, 0) + o["pnl"]
            o["_booked"] = True
        today = datetime.fromtimestamp(t["time"] / 1000, TZ).date()
        if any(o["symbol"] == t["symbol"] for o in open_trades) or len(open_trades) >= MAX_OPEN:
            continue
        if day_pnl.get(today, 0) <= -DAILY_LOSS:
            continue
        accepted.append(t)
        open_trades.append(t)
    return accepted


def stats(trades):
    if not trades:
        return {"n": 0, "win": 0, "avg_r": 0, "tot_r": 0, "pf": 0, "dd_r": 0, "usdt": 0}
    rs = [t["r"] for t in trades]
    gw = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(rs), "win": sum(1 for r in rs if r > 0) / len(rs) * 100, "avg_r": sum(rs) / len(rs),
            "tot_r": sum(rs), "pf": gw / gl if gl else float("inf"), "dd_r": dd,
            "usdt": sum(t["pnl"] for t in trades)}


def fmt(st):
    if not st["n"]:
        return "   sin operaciones"
    pf = "∞" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
    return (f"{st['n']:>4} ops · acierto {st['win']:>4.0f}% · {st['avg_r']:+.2f} R/op · total {st['tot_r']:+6.1f} R"
            f" · PF {pf:>4} · DD {st['dd_r']:.1f} R · {st['usdt']:+.2f} USDT")


# ───────────────────────── main ─────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--oos-months", type=int, default=4)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    now_ms = int(time.time() * 1000) // 300_000 * 300_000
    start_ms = now_ms - args.months * 30 * MS_1D
    split_ms = now_ms - args.oos_months * 30 * MS_1D
    warm_daily = start_ms - 450 * MS_1D
    warm_hourly = start_ms - 20 * MS_1D
    warm_5m = start_ms - 6 * MS_1D

    specs = BingXClient("", "", "live").contracts()
    bars_by_symbol, daily_ctx = {}, {}
    candidates_by_mode = {m: [] for m in ("fib", "atr", "structure")}

    for sym in SYMBOLS:
        t0 = time.time()
        daily = fetch_binance(sym, "1d", warm_daily, now_ms)
        hourly = fetch_binance(sym, "1h", warm_hourly, now_ms)
        m5 = fetch_binance(sym, "5m", warm_5m, now_ms)
        closes = [c["close"] for c in daily]
        daily_ctx[sym] = ([c["time"] for c in daily], closes, ema(closes, 50), ema(closes, 200))
        for mode in candidates_by_mode:
            bars, cands = engine_candidates(sym, replace(StrategyParams(), sl_mode=mode), daily, hourly, m5, start_ms)
            candidates_by_mode[mode] += cands
            bars_by_symbol[sym] = bars
        print(f"{sym}: {len(m5)} velas 5m desde {datetime.fromtimestamp(m5[0]['time'] / 1000, TZ):%d/%m/%Y} "
              f"· entradas fib/atr/estructura = "
              f"{'/'.join(str(sum(1 for c in candidates_by_mode[m] if c.symbol == sym)) for m in candidates_by_mode)}"
              f" · {time.time() - t0:.0f}s")

    variants = [Variant(*v) for v in itertools.product(
        ("fib", "atr", "structure"), ("impulse", "2R", "3R"), (0.0, 0.3, 0.6), (1.5, 2.0, 3.0),
        ("none", "ema50", "ema200"))]
    outcome_cache: dict = {}
    rows = []
    for v in variants:
        trades = simulate(v, sorted(candidates_by_mode[v.sl_mode], key=lambda c: c.time),
                          bars_by_symbol, specs, daily_ctx, outcome_cache)
        ins = [t for t in trades if t["time"] < split_ms]
        oos = [t for t in trades if t["time"] >= split_ms]
        rows.append((v, stats(ins), stats(oos), trades))

    split_txt = datetime.fromtimestamp(split_ms / 1000, TZ).strftime("%d/%m/%Y")
    base = next(r for r in rows if r[0] == Variant("fib", "impulse", 0.0, 1.5, "none"))
    print(f"\n=== REGLAS ACTUALES (SL 0,786 · TP impulso · R:R ≥ 1,5) ===")
    print(f"  in-sample (hasta {split_txt}): {fmt(base[1])}")
    print(f"  out-of-sample (desde {split_txt}): {fmt(base[2])}")

    eligible = [r for r in rows if r[1]["n"] >= 30]
    eligible.sort(key=lambda r: r[1]["avg_r"], reverse=True)
    print(f"\n=== TOP {args.top} VARIANTES elegidas por in-sample (≥30 ops) y su resultado out-of-sample ===")
    for v, ins, oos, _ in eligible[:args.top]:
        print(f"\n{v.label()}\n  IS : {fmt(ins)}\n  OOS: {fmt(oos)}")

    positive_oos = sum(1 for r in eligible[:args.top] if r[2]["n"] and r[2]["avg_r"] > 0)
    print(f"\nDe las {min(args.top, len(eligible))} mejores in-sample, {positive_oos} siguen positivas out-of-sample.")

    best = eligible[0] if eligible else None
    if best:
        print(f"\n=== Detalle por par / lado / horario (AR) de la mejor in-sample: {best[0].label()} ===")
        trades = best[3]
        for sym in SYMBOLS:
            sub = [t for t in trades if t["symbol"] == sym]
            print(f"  {sym:<10} IS {fmt(stats([t for t in sub if t['time'] < split_ms]))}")
            print(f"  {'':<10} OOS {fmt(stats([t for t in sub if t['time'] >= split_ms]))}")
        for side in ("LONG", "SHORT"):
            print(f"  {side:<10} {fmt(stats([t for t in trades if t['side'] == side]))}")
        for label, hours in (("00-08 h", range(0, 8)), ("08-16 h", range(8, 16)), ("16-24 h", range(16, 24))):
            sub = [t for t in trades if datetime.fromtimestamp(t["time"] / 1000, TZ).hour in hours]
            print(f"  {label:<10} {fmt(stats(sub))}")


if __name__ == "__main__":
    main()
