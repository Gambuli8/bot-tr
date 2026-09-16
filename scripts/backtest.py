"""
Backtest de la estrategia con el motor real del bot (bot/strategy.py).

Qué simula (conservador):
  - Riesgo FIJO por operación (--risk, default 0,50 USDT): la cantidad sale de la distancia al SL
    + comisiones. Si con ese riesgo no se llega al mínimo del contrato de BingX, la operación se descarta.
  - Entrada al cierre de la vela de 5m y salidas con 0,03 % de slippage en contra; comisiones taker.
  - Si en la misma vela se tocan SL y TP (o +1R y el break-even) → se asume lo peor.
  - Portafolio: 1 posición por par, máx. --max-open abiertas, pérdida diaria máx. --daily-loss (día argentino).

Matriz de pruebas (se activan de a una sobre la BASE):
  REF      reglas actuales del bot en vivo (SL Fibo 0,786, sin filtro de tendencia)
  BASE     SL estructural (inicio del impulso − 0,1×ATR 1H) + tendencia EMA50 diaria + TP impulso + R:R ≥ 1,5
  +IMP     impulso 1H ≥ 1,5 × ATR(14) 1H
  +VOL     vela gatillo con volumen > SMA(20)
  gestión  —: SL/TP fijos · BE: SL a break-even en +1R · PARC: cierra 50 % en +1R · BE+PARC: ambas

Anti-sobreajuste: in-sample (IS) = primeros meses, out-of-sample (OOS) = últimos --oos-months,
que no se usan para elegir. Se informa el estadístico t de la media de R en OOS.

Datos: API pública de Binance Futures (BingX sólo guarda ~45 días de 5m), cache en data/backtest/.
Pares: --pairs current (los 6 del bot) · auto (los N con mejor ATR% diario / spread de BingX,
medidos en el período IS) · o lista separada por comas (BTC-USDT,ETH-USDT,...).

Uso:
  python scripts/backtest.py --months 24 --oos-months 8 --pairs auto --n-pairs 20
  python scripts/backtest.py --months 24 --oos-months 8 --pairs current
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.bingx import BingXClient, ContractSpec  # noqa: E402
from bot.sizing import build_plan_fixed_risk  # noqa: E402
from bot.strategy import (MS_1D, StrategyParams, SymbolStrategy, atr_series, build_bars,  # noqa: E402
                          build_daily_zones, build_hourly)

CACHE = ROOT / "data" / "backtest"
TZ = ZoneInfo("America/Argentina/Buenos_Aires")
SLIPPAGE = 0.0003
MAX_LEVERAGE = 20
MIN_RR = 1.5
INTERVAL_MS = {"5m": 300_000, "1h": 3_600_000, "1d": 86_400_000}
CURRENT_PAIRS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "ZEC-USDT", "DOGE-USDT"]
BINANCE = "https://fapi.binance.com"

BASE = StrategyParams(sl_mode="structure", filter_trend=True)
ENGINES = {
    "REF": StrategyParams(),
    "BASE": BASE,
    "BASE+IMP": replace(BASE, filter_impulse=True),
    "BASE+VOL": replace(BASE, filter_volume=True),
    "BASE+IMP+VOL": replace(BASE, filter_impulse=True, filter_volume=True),
}
MGMT = {"—": (False, False), "BE": (True, False), "PARC": (False, True), "BE+PARC": (True, True)}


# ───────────────────────── datos ─────────────────────────

def _get(url, params, tries=6):
    for attempt in range(tries):
        try:
            resp = requests.get(url, params=params, timeout=20)
            if resp.status_code == 429:
                time.sleep(10)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            print(f"  reintento {url}: {exc}", flush=True)
            time.sleep(2 * (attempt + 1))
    raise SystemExit(f"Fallo definitivo pidiendo {url} {params}")


def fetch(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
    """Velas [t, o, h, l, c, v] desde Binance Futures, con cache incremental."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol}_{interval}.v2.json"
    step = INTERVAL_MS[interval]
    stored = json.loads(path.read_text()) if path.exists() else {"from": None, "rows": []}
    rows: list[list] = stored["rows"]
    covered_from = stored["from"]  # desde dónde ya se pidió (el par puede haber empezado a cotizar después)

    def download(a: int, b: int) -> list[list]:
        out, cursor = [], a
        while cursor < b:
            data = _get(f"{BINANCE}/fapi/v1/klines", {"symbol": symbol.replace("-", ""), "interval": interval,
                                                      "startTime": cursor, "endTime": b, "limit": 1500})
            if not data:
                break
            out += [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in data]
            cursor = int(data[-1][0]) + step
            time.sleep(0.12)
        return out

    changed = False
    if covered_from is None or start_ms < covered_from:
        head = download(start_ms, rows[0][0] if rows else end_ms)
        rows = head + [r for r in rows if not head or r[0] > head[-1][0]]
        covered_from = start_ms
        changed = True
    if rows and rows[-1][0] < end_ms - 2 * step:
        tail = download(rows[-1][0] + step, end_ms)
        rows += tail
        changed = bool(tail) or changed
    now_ms = int(time.time() * 1000)
    rows = [r for r in rows if r[0] + step <= now_ms]
    if changed:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"from": covered_from, "rows": rows}, separators=(",", ":")))
        tmp.replace(path)
    return [r for r in rows if start_ms <= r[0] < end_ms]


def as_candles(rows: list[list]) -> list[dict]:
    return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in rows]


# ───────────────────────── selección objetiva de pares ─────────────────────────

def select_pairs(n: int, is_start: int, is_end: int, specs: dict[str, ContractSpec]) -> list[str]:
    """Top N por ATR% diario (en el período IS) / spread actual de BingX, entre los 40 más líquidos."""
    info = _get(f"{BINANCE}/fapi/v1/exchangeInfo", {})
    binance_ok = {s["symbol"] for s in info["symbols"]
                  if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
                  and s.get("status") == "TRADING" and int(s.get("onboardDate", 0)) <= is_start - 30 * MS_1D}
    common = [sym for sym, spec in specs.items()
              if sym.endswith("-USDT") and spec.api_open and sym.replace("-", "") in binance_ok]
    tickers = {t["symbol"]: float(t["quoteVolume"]) for t in _get(f"{BINANCE}/fapi/v1/ticker/24hr", {})}
    liquid = sorted(common, key=lambda s: tickers.get(s.replace("-", ""), 0), reverse=True)[:40]

    rows = []
    for sym in liquid:
        depth = _get("https://open-api.bingx.com/openApi/swap/v2/quote/depth", {"symbol": sym, "limit": 5})
        book = depth.get("data") or {}
        try:
            ask = min(float(a[0]) for a in book["asks"])
            bid = max(float(b[0]) for b in book["bids"])
        except (KeyError, ValueError, TypeError):
            continue
        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        daily = as_candles(fetch(sym, "1d", is_start - 20 * MS_1D, is_end))
        daily_is = [c for c in daily if c["time"] >= is_start]
        if len(daily_is) < 200 or spread_pct <= 0:
            continue
        atr = atr_series(daily)
        atr_pct = mean(a / c["close"] * 100 for a, c in zip(atr, daily) if a and c["time"] >= is_start)
        rows.append((sym, atr_pct, spread_pct, atr_pct / spread_pct, tickers.get(sym.replace("-", ""), 0)))
        time.sleep(0.1)

    rows.sort(key=lambda r: r[3], reverse=True)
    print("\nSelección de pares (ATR% diario en IS / spread BingX, entre los 40 más líquidos):")
    print(f"  {'par':<14}{'ATR%':>7}{'spread%':>10}{'ratio':>9}{'vol 24h (M)':>14}")
    for i, (sym, atr_pct, spread, ratio, vol) in enumerate(rows):
        mark = "✔" if i < n else " "
        print(f"  {mark} {sym:<12}{atr_pct:>7.2f}{spread:>10.4f}{ratio:>9.0f}{vol / 1e6:>14.0f}")
    chosen = [r[0] for r in rows[:n]]
    print(f"  De los 6 actuales entran: {', '.join(p for p in CURRENT_PAIRS if p in chosen) or 'ninguno'}")
    return chosen


# ───────────────────────── simulación de una operación ─────────────────────────

def simulate_trade(bars, i: int, side: str, entry: float, sl: float, tp: float, qty: float, taker: float,
                   move_be: bool, partial: bool):
    """Devuelve (hora_de_salida, pnl_usdt, motivo) o None si quedó abierta al final de los datos."""
    long = side == "LONG"
    sign = 1 if long else -1
    risk_px = abs(entry - sl)
    one_r = entry + sign * risk_px
    be = entry * (1 + sign * 2 * taker)
    cur_sl, remaining, pnl = sl, 1.0, -qty * entry * taker          # comisión de entrada
    moved = took_partial = False

    def close(px_trigger: float, part: float):
        px = px_trigger * (1 - sign * SLIPPAGE)
        return sign * (px - entry) * qty * part - qty * part * px * taker

    for j in range(i + 1, len(bars)):
        b = bars[j]
        exit_time = b.time + 300_000
        if (b.low <= cur_sl) if long else (b.high >= cur_sl):
            return exit_time, pnl + close(cur_sl, remaining), ("BE" if moved else "SL")
        if (b.high >= tp) if long else (b.low <= tp):
            return exit_time, pnl + close(tp, remaining), "TP"
        if (b.high >= one_r) if long else (b.low <= one_r):
            if partial and not took_partial:
                pnl += close(one_r, 0.5)
                remaining = 0.5
                took_partial = True
            if move_be and not moved:
                cur_sl, moved = be, True
                if (b.low <= cur_sl) if long else (b.high >= cur_sl):   # misma vela: se asume lo peor
                    return exit_time, pnl + close(cur_sl, remaining), "BE"
    return None


# ───────────────────────── trabajo por par (proceso aparte) ─────────────────────────

def run_symbol(symbol: str, spec: dict, start_ms: int, end_ms: int, risk: float) -> dict:
    spec_obj = ContractSpec(**spec)
    daily = as_candles(fetch(symbol, "1d", start_ms - 450 * MS_1D, end_ms))
    hourly = as_candles(fetch(symbol, "1h", start_ms - 30 * MS_1D, end_ms))
    m5 = as_candles(fetch(symbol, "5m", start_ms - 7 * MS_1D, end_ms))
    if len(m5) < 1000:
        return {"symbol": symbol, "trades": [], "rejects": {}, "entries": {}, "bars": len(m5)}
    zones = build_daily_zones(daily, BASE)
    bars = build_bars(m5, build_hourly(hourly, BASE), zones, BASE)

    trades, rejects, entries = [], {}, {}
    for eng_name, params in ENGINES.items():
        engine = SymbolStrategy(symbol, params)
        n_entries = 0
        for i, bar in enumerate(bars):
            for ev in engine.on_bar(bar):
                if ev["event"] != "entry" or ev["time"] < start_ms:
                    continue
                n_entries += 1
                long = ev["side"] == "LONG"
                entry = ev["price"] * (1 + SLIPPAGE if long else 1 - SLIPPAGE)
                plan = build_plan_fixed_risk(symbol=symbol, direction=ev["side"], entry=entry,
                                             stop_loss=ev["sl"], take_profit=ev["tp"], spec=spec_obj,
                                             risk_usdt=risk, max_leverage=MAX_LEVERAGE, min_rr=MIN_RR)
                if not plan.ok:
                    key = ("R:R" if plan.reason.startswith("R:R") else
                           "mínimo de contrato" if "no llego al mínimo" in plan.reason else "otro")
                    rejects[(eng_name, key)] = rejects.get((eng_name, key), 0) + 1
                    continue
                modes = ["—"] if eng_name == "REF" else list(MGMT)
                for mgmt in modes:
                    move_be, partial = MGMT[mgmt]
                    res = simulate_trade(bars, i, ev["side"], entry, ev["sl"], ev["tp"], plan.qty,
                                         spec_obj.taker_fee, move_be, partial)
                    if res is None:
                        continue
                    exit_time, pnl, why = res
                    trades.append({"engine": eng_name, "mgmt": mgmt, "symbol": symbol, "side": ev["side"],
                                   "time": ev["time"], "exit": exit_time, "pnl": pnl, "r": pnl / plan.risk_usdt,
                                   "why": why, "margin": plan.margin_used, "sl_pct": plan.sl_distance_pct})
        entries[eng_name] = n_entries
    return {"symbol": symbol, "trades": trades, "rejects": {f"{k[0]}|{k[1]}": v for k, v in rejects.items()},
            "entries": entries, "bars": len(m5)}


# ───────────────────────── portafolio y métricas ─────────────────────────

def portfolio(trades: list[dict], max_open: int, daily_loss: float) -> tuple[list[dict], int, float]:
    accepted, open_now, day_pnl, skipped, max_margin = [], [], {}, 0, 0.0
    for t in sorted(trades, key=lambda x: x["time"]):
        for o in [o for o in open_now if o["exit"] <= t["time"]]:
            d = datetime.fromtimestamp(o["exit"] / 1000, TZ).date()
            day_pnl[d] = day_pnl.get(d, 0.0) + o["pnl"]
        open_now = [o for o in open_now if o["exit"] > t["time"]]
        today = datetime.fromtimestamp(t["time"] / 1000, TZ).date()
        if any(o["symbol"] == t["symbol"] for o in open_now) or len(open_now) >= max_open \
                or day_pnl.get(today, 0.0) <= -daily_loss:
            skipped += 1
            continue
        accepted.append(t)
        open_now.append(t)
        max_margin = max(max_margin, sum(o["margin"] for o in open_now))
    return accepted, skipped, max_margin


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    gw, gl = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    sd = pstdev(rs) if len(rs) > 1 else 0.0
    return {"n": len(rs), "win": sum(1 for t in trades if t["pnl"] > 0) / len(rs) * 100, "avg": mean(rs),
            "tot": sum(rs), "pf": gw / gl if gl else math.inf, "dd": dd, "usdt": sum(t["pnl"] for t in trades),
            "t": mean(rs) / (sd / math.sqrt(len(rs))) if sd and len(rs) >= 10 else None}


def line(st: dict) -> str:
    if not st["n"]:
        return "sin operaciones"
    pf = "∞" if st["pf"] == math.inf else f"{st['pf']:.2f}"
    return (f"{st['n']:>4} ops · acierto {st['win']:>3.0f}% · {st['avg']:+.3f} R/op · total {st['tot']:+6.1f} R · "
            f"PF {pf:>4} · DD {st['dd']:>4.1f} R · {st['usdt']:+7.2f} USDT · t={'—' if st['t'] is None else format(st['t'], '+.2f')}")


# ───────────────────────── main ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--oos-months", type=int, default=8)
    ap.add_argument("--pairs", default="auto", help="auto | current | BTC-USDT,ETH-USDT,...")
    ap.add_argument("--n-pairs", type=int, default=20)
    ap.add_argument("--risk", type=float, default=0.5, help="USDT arriesgados por operación")
    ap.add_argument("--max-open", type=int, default=3)
    ap.add_argument("--daily-loss", type=float, default=3.0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    now_ms = int(time.time() * 1000) // 300_000 * 300_000
    start_ms = now_ms - args.months * 30 * MS_1D
    split_ms = now_ms - args.oos_months * 30 * MS_1D
    specs = BingXClient("", "", "live").contracts()

    if args.pairs == "auto":
        pairs = select_pairs(args.n_pairs, start_ms, split_ms, specs)
    elif args.pairs == "current":
        pairs = CURRENT_PAIRS
    else:
        pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]

    print(f"\nPeríodo: {datetime.fromtimestamp(start_ms / 1000, TZ):%d/%m/%Y} → "
          f"{datetime.fromtimestamp(now_ms / 1000, TZ):%d/%m/%Y} · OOS desde "
          f"{datetime.fromtimestamp(split_ms / 1000, TZ):%d/%m/%Y} · riesgo {args.risk} USDT/op · "
          f"máx. {args.max_open} abiertas · {len(pairs)} pares", flush=True)

    # Descarga secuencial (respeta límites de la API) y después procesamiento en paralelo
    for sym in pairs:
        t0 = time.time()
        fetch(sym, "1d", start_ms - 450 * MS_1D, now_ms)
        fetch(sym, "1h", start_ms - 30 * MS_1D, now_ms)
        rows = fetch(sym, "5m", start_ms - 7 * MS_1D, now_ms)
        print(f"  datos {sym:<14} {len(rows):>7} velas 5m · {time.time() - t0:4.0f}s", flush=True)

    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_symbol, sym, asdict(specs[sym]), start_ms, now_ms, args.risk): sym
                   for sym in pairs if sym in specs}
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            print(f"  motor {res['symbol']:<14} entradas {res['entries']}", flush=True)

    all_trades = [t for r in results for t in r["trades"]]
    rejects: dict[str, int] = {}
    for r in results:
        for k, v in r["rejects"].items():
            rejects[k] = rejects.get(k, 0) + v

    variants = [("REF", "—")] + [(e, m) for e in ENGINES if e != "REF" for m in MGMT]
    table = []
    print(f"\n{'variante':<24} {'período':<4} resultado")
    for eng, mgmt in variants:
        subset = [t for t in all_trades if t["engine"] == eng and t["mgmt"] == mgmt]
        accepted, skipped, max_margin = portfolio(subset, args.max_open, args.daily_loss)
        ins = stats([t for t in accepted if t["time"] < split_ms])
        oos = stats([t for t in accepted if t["time"] >= split_ms])
        table.append((eng, mgmt, ins, oos, accepted, skipped, max_margin))
        name = f"{eng} · {mgmt}"
        print(f"{name:<24} IS   {line(ins)}")
        print(f"{'':<24} OOS  {line(oos)}   (salteadas por límites: {skipped} · margen máx. simultáneo "
              f"{max_margin:.2f} USDT)")

    print("\nDescartadas antes de operar (todas las entradas del período):")
    for k, v in sorted(rejects.items()):
        print(f"  {k}: {v}")

    candidates = [row for row in table if row[0] != "REF" and row[2]["n"] >= 50 and row[2]["avg"] > 0]
    both = [row for row in table if row[2]["n"] and row[3]["n"] and row[2]["avg"] > 0 and row[3]["avg"] > 0]
    both_names = ", ".join(f"{e} · {m}" for e, m, *_ in both) or "NINGUNA"
    print(f"\nVariantes positivas en IS y en OOS a la vez: {both_names}")
    if not candidates:
        print("Ninguna variante con ≥ 50 operaciones es positiva in-sample: no hay nada que validar fuera de muestra.")
    else:
        best = max(candidates, key=lambda row: row[2]["avg"])
        eng, mgmt, ins, oos, accepted, *_ = best
        print(f"\n=== Elegida por IS (máx. R/op con ≥ 50 ops): {eng} · {mgmt} ===")
        print(f"  IS : {line(ins)}")
        print(f"  OOS: {line(oos)}")
        verdict = ("POSITIVA y con t ≥ 2 (evidencia razonable)" if oos["n"] and oos["avg"] > 0 and (oos["t"] or 0) >= 2 else
                   "positiva pero NO significativa (t < 2)" if oos["n"] and oos["avg"] > 0 else
                   "NEGATIVA fuera de muestra")
        print(f"  Veredicto OOS: {verdict}")
        oos_trades = [t for t in accepted if t["time"] >= split_ms]
        print("  OOS por par:")
        for sym in sorted({t['symbol'] for t in oos_trades}):
            print(f"    {sym:<14} {line(stats([t for t in oos_trades if t['symbol'] == sym]))}")
        for side in ("LONG", "SHORT"):
            print(f"  OOS {side:<5} {line(stats([t for t in oos_trades if t['side'] == side]))}")
        why = {}
        for t in oos_trades:
            why[t["why"]] = why.get(t["why"], 0) + 1
        print(f"  OOS salidas: {why}")

    out = CACHE / "results.json"
    out.write_text(json.dumps({"args": vars(args), "pairs": pairs, "split_ms": split_ms,
                               "table": [{"engine": e, "mgmt": m, "is": i, "oos": o, "skipped": s, "max_margin": mm}
                                         for e, m, i, o, _, s, mm in table]}, default=str, indent=1))
    print(f"\nResultados guardados en {out}")


if __name__ == "__main__":
    main()
