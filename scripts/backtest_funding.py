"""
Backtest de CAPTURA DE FUNDING delta-neutral en BingX.

Idea: comprar la moneda en SPOT y abrir un SHORT del mismo tamaño en el perpetuo. Lo que el precio hace
en un lado se compensa en el otro; lo que queda es el funding que cobra el short cuando es positivo
(y paga cuando es negativo).

Qué simula (conservador):
  - Funding REAL de BingX (historial completo que exponga la API) y, para estresar con más años,
    el funding de Binance Futures (desde --binance-years atrás).
  - Precios horarios (Binance Futures, prácticamente iguales a BingX) para rebalanceo y liquidación.
  - Comisiones reales de la cuenta: spot 0,10 % · perp 0,05 % taker (o 0,02 % maker con --maker) +
    0,03 % de deslizamiento por pierna en cada operación.
  - Capital C por par: spot = C·L/(L+1), margen del short = C/(L+1)  (L = apalancamiento del short).
  - Rebalanceo cuando el precio se mueve ±--rebalance-pct/L desde el último ajuste: se vende/compra spot
    para volver al reparto objetivo (con comisiones). Si el máximo horario toca la liquidación del short
    antes de rebalancear, se registra la liquidación: se pierde el margen y se vuelve a cubrir.
  - Modos: always (siempre dentro) · switch (sale si el funding promedio de 3 días es negativo y vuelve a
    entrar cuando supera +0,003 %/8h).

Uso:
  python scripts/backtest_funding.py                       # BingX, pares por defecto
  python scripts/backtest_funding.py --source binance --years 4
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import _get, as_candles, fetch  # noqa: E402  (velas horarias de Binance con cache)

CACHE = ROOT / "data" / "backtest"
TZ = ZoneInfo("America/Argentina/Buenos_Aires")
MS_H = 3_600_000
MS_D = 24 * MS_H
SPOT_FEE = 0.001
PERP_TAKER = 0.0005
PERP_MAKER = 0.0002
SLIP = 0.0003
MMR = 0.005
DEFAULT_PAIRS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "DOGE-USDT", "ZEC-USDT",
                 "BNB-USDT", "LINK-USDT", "AAVE-USDT", "TRX-USDT", "XLM-USDT", "BCH-USDT"]


# ───────────────────────── datos de funding ─────────────────────────

def funding_bingx(symbol: str) -> list[tuple[int, float]]:
    path = CACHE / f"funding_bingx_{symbol}.json"
    if path.exists() and time.time() - path.stat().st_mtime < 6 * 3600:
        return [tuple(x) for x in json.loads(path.read_text())]
    out: dict[int, float] = {}
    end = None
    for _ in range(20):
        params = {"symbol": symbol, "limit": 1000}
        if end:
            params["endTime"] = end
        data = _get("https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate", params).get("data") or []
        new = [(int(x["fundingTime"]), float(x["fundingRate"])) for x in data if int(x["fundingTime"]) not in out]
        if not new:
            break
        out.update(new)
        end = min(t for t, _ in new) - 1
        time.sleep(0.2)
    rows = sorted(out.items())
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    return rows


def funding_binance(symbol: str, start_ms: int) -> list[tuple[int, float]]:
    path = CACHE / f"funding_binance_{symbol}.json"
    rows: list = json.loads(path.read_text()) if path.exists() else []
    if not rows or rows[0][0] > start_ms + 8 * MS_H or rows[-1][0] < time.time() * 1000 - 9 * MS_H:
        out, cursor = {}, start_ms
        while True:
            data = _get("https://fapi.binance.com/fapi/v1/fundingRate",
                        {"symbol": symbol.replace("-", ""), "startTime": cursor, "limit": 1000})
            if not data:
                break
            for x in data:
                out[int(x["fundingTime"])] = float(x["fundingRate"])
            nxt = int(data[-1]["fundingTime"]) + 1
            if nxt <= cursor or len(data) < 1000:
                break
            cursor = nxt
            time.sleep(0.2)
        rows = sorted(out.items())
        path.write_text(json.dumps(rows))
    return [tuple(x) for x in rows if x[0] >= start_ms]


def spot_symbols() -> set[str]:
    data = _get("https://open-api.bingx.com/openApi/spot/v1/common/symbols", {}).get("data") or {}
    return {s["symbol"] for s in data.get("symbols", []) if s.get("status") == 1}


# ───────────────────────── simulación de un par ─────────────────────────

@dataclass
class Book:
    capital: float
    leverage: float
    perp_fee: float
    rebalance_pct: float
    cash: float = 0.0
    qty: float = 0.0            # monedas en spot = monedas en short
    margin: float = 0.0         # USDT en la cuenta de futuros (incluye funding cobrado)
    perp_entry: float = 0.0
    last_rebalance_px: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    rebalances: int = 0
    liquidations: int = 0
    trades: int = 0

    def __post_init__(self):
        self.cash = self.capital

    @property
    def in_position(self) -> bool:
        return self.qty > 0

    def equity(self, px: float) -> float:
        return self.cash + self.qty * px + self.margin + self.qty * (self.perp_entry - px)

    def _cost(self, notional: float) -> float:
        return notional * (SPOT_FEE + self.perp_fee + 2 * SLIP)

    def enter(self, px: float) -> None:
        total = self.cash
        spot = total * self.leverage / (self.leverage + 1)
        cost = self._cost(spot)
        self.qty = (spot - cost) / px
        self.margin = total - spot
        self.perp_entry = px
        self.cash = 0.0
        self.fees += cost
        self.last_rebalance_px = px
        self.trades += 1

    def exit(self, px: float) -> None:
        eq = self.equity(px)
        cost = self._cost(self.qty * px)
        self.cash = eq - cost
        self.fees += cost
        self.qty = self.margin = 0.0
        self.trades += 1

    def rebalance(self, px: float) -> None:
        eq = self.equity(px)
        target_spot = eq * self.leverage / (self.leverage + 1)
        new_qty = target_spot / px
        traded = abs(new_qty - self.qty) * px
        cost = self._cost(traded)
        self.qty = new_qty - cost / px
        self.margin = eq - target_spot
        self.perp_entry = px
        self.fees += cost
        self.rebalances += 1
        self.last_rebalance_px = px

    def check_liquidation(self, high: float, low: float) -> bool:
        """Liquidación del short (aislado) si el máximo horario alcanza el precio de liquidación."""
        if not self.in_position:
            return False
        liq = (self.margin + self.qty * self.perp_entry) / (self.qty * (1 + MMR))
        if high >= liq:
            # Se pierde el margen del short; el spot queda y se vuelve a cubrir al precio de liquidación.
            spot_value = self.qty * liq             # vende el spot para rearmar
            self.fees += spot_value * SPOT_FEE
            self.cash += spot_value * (1 - SPOT_FEE)
            self.qty = 0.0
            self.margin = 0.0
            self.liquidations += 1
            return True
        return False


def simulate_pair(prices: list[dict], funding: list[tuple[int, float]], mode: str, leverage: float,
                  perp_fee: float, rebalance_pct: float, start_ms: int, end_ms: int,
                  capital: float = 100.0) -> tuple[list[tuple[int, float]], Book]:
    """Devuelve la curva de equity diaria [(ms, equity)] y el libro con contadores."""
    book = Book(capital, leverage, perp_fee, rebalance_pct)
    f_idx = 0
    recent: list[float] = []
    curve: list[tuple[int, float]] = []
    last_day = None
    started = False
    for c in prices:
        t = c["time"]
        if t < start_ms or t >= end_ms:
            continue
        px = c["close"]
        if not started:
            if mode in ("always", "switch"):
                book.enter(c["open"])
            started = True

        if book.in_position:
            if book.check_liquidation(c["high"], c["low"]):
                book.enter(px)
            elif abs(px / book.last_rebalance_px - 1) >= rebalance_pct / leverage:
                book.rebalance(px)

        while f_idx < len(funding) and funding[f_idx][0] < t + MS_H:
            ft, rate = funding[f_idx]
            f_idx += 1
            if ft < start_ms:
                continue
            if book.in_position:
                amount = rate * book.qty * px
                book.margin += amount
                book.funding += amount
            recent.append(rate)
            recent = recent[-9:]  # 3 días de pagos cada 8 h
            if mode == "switch" and len(recent) >= 9:
                avg = mean(recent)
                if book.in_position and avg < 0:
                    book.exit(px)
                elif not book.in_position and avg > 0.00003:
                    book.enter(px)

        day = t // MS_D
        if day != last_day:
            curve.append((t, book.equity(px)))
            last_day = day
    return curve, book


# ───────────────────────── métricas ─────────────────────────

def metrics(curve: list[tuple[int, float]]) -> dict:
    if len(curve) < 30:
        return {}
    start, end = curve[0][1], curve[-1][1]
    years = (curve[-1][0] - curve[0][0]) / (365 * MS_D)
    peak, dd = start, 0.0
    for _, e in curve:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak)
    months: dict[str, list[float]] = {}
    for t, e in curve:
        months.setdefault(datetime.fromtimestamp(t / 1000, TZ).strftime("%Y-%m"), []).append(e)
    keys = sorted(months)
    rets, prev = [], start
    for k in keys:
        rets.append(months[k][-1] / prev - 1)
        prev = months[k][-1]
    years_ret, prev_y = {}, start
    by_year: dict[str, float] = {}
    for t, e in curve:
        by_year[datetime.fromtimestamp(t / 1000, TZ).strftime("%Y")] = e
    for y in sorted(by_year):
        years_ret[y] = by_year[y] / prev_y - 1
        prev_y = by_year[y]
    return {"by_year": years_ret, "total": end / start - 1, "apy": (end / start) ** (1 / years) - 1 if years > 0 and end > 0 else -1,
            "dd": dd, "months_pos": sum(1 for r in rets if r > 0) / len(rets), "worst_month": min(rets),
            "best_month": max(rets), "n_months": len(rets), "years": years}


def fmt(m: dict) -> str:
    if not m:
        return "sin datos suficientes"
    return (f"anual {m['apy'] * 100:+6.2f}% · total {m['total'] * 100:+7.2f}% · caída máx {m['dd'] * 100:5.2f}% · "
            f"meses + {m['months_pos'] * 100:3.0f}% · peor mes {m['worst_month'] * 100:+5.2f}% · "
            f"{m['n_months']} meses")


def combine(curves: list[list[tuple[int, float]]]) -> list[tuple[int, float]]:
    days = sorted(set.intersection(*[{t // MS_D for t, _ in c} for c in curves])) if curves else []
    maps = [{t // MS_D: e for t, e in c} for c in curves]
    return [(d * MS_D, sum(m[d] for m in maps)) for d in days]


# ───────────────────────── main ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["bingx", "binance"], default="bingx")
    ap.add_argument("--years", type=float, default=4, help="años hacia atrás (para binance)")
    ap.add_argument("--pairs", default=",".join(DEFAULT_PAIRS))
    ap.add_argument("--maker", action="store_true", help="short con órdenes limit (0,02 %)")
    ap.add_argument("--rebalance-pct", type=float, default=0.5)
    args = ap.parse_args()

    now_ms = int(time.time() * 1000) // MS_H * MS_H
    spot = spot_symbols()
    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    missing = [p for p in pairs if p not in spot]
    pairs = [p for p in pairs if p in spot]
    if missing:
        print(f"Sin mercado spot en BingX (se excluyen): {', '.join(missing)}")

    data = {}
    for sym in pairs:
        fund = funding_bingx(sym) if args.source == "bingx" else funding_binance(sym, now_ms - int(args.years * 365 * MS_D))
        if len(fund) < 90:
            print(f"  {sym}: funding insuficiente ({len(fund)})")
            continue
        start = max(fund[0][0], now_ms - int(args.years * 365 * MS_D))
        prices = as_candles(fetch(sym, "1h", start - 2 * MS_D, now_ms))
        if not prices:
            continue
        start = max(start, prices[0]["time"])
        rates = [r for t, r in fund if t >= start]
        per_day = len(rates) / max(1, (fund[-1][0] - start) / MS_D)
        data[sym] = (prices, fund, start)
        print(f"  {sym:<10} funding {len(rates):>5} pagos desde "
              f"{datetime.fromtimestamp(start / 1000, TZ):%d/%m/%Y} · promedio anualizado "
              f"{mean(rates) * per_day * 365 * 100:+5.1f}% · pagos positivos {sum(r > 0 for r in rates) / len(rates) * 100:3.0f}%")

    if not data:
        raise SystemExit("Sin datos")
    common_start = max(v[2] for v in data.values())
    perp_fee = PERP_MAKER if args.maker else PERP_TAKER
    print(f"\nFuente de funding: {args.source.upper()} · período común {datetime.fromtimestamp(common_start / 1000, TZ):%d/%m/%Y}"
          f" → {datetime.fromtimestamp(now_ms / 1000, TZ):%d/%m/%Y} · short {'maker' if args.maker else 'taker'} · "
          f"rebalanceo ±{args.rebalance_pct / 1:.0%}/L")

    summary = {}
    for mode in ("always", "switch"):
        for lev in (1.0, 2.0, 3.0):
            curves, books = [], []
            print(f"\n=== modo {mode} · short ×{lev:g} (capital en spot {lev / (lev + 1):.0%}) ===")
            for sym, (prices, fund, _) in data.items():
                curve, book = simulate_pair(prices, fund, mode, lev, perp_fee, args.rebalance_pct, common_start, now_ms)
                curves.append(curve)
                books.append(book)
                m = metrics(curve)
                print(f"  {sym:<10} {fmt(m)} · funding {book.funding:+6.2f} · comisiones {book.fees:5.2f} · "
                      f"rebal {book.rebalances} · liq {book.liquidations} · entradas/salidas {book.trades}")
            basket = combine(curves)
            mb = metrics(basket)
            summary[(mode, lev)] = mb
            print(f"  CANASTA    {fmt(mb)} · liquidaciones {sum(b.liquidations for b in books)}")
            print("             por año → " + " · ".join(f"{y}: {r * 100:+.2f}%" for y, r in mb.get("by_year", {}).items()))

    print("\nResumen canasta (misma ponderación entre pares):")
    for (mode, lev), m in summary.items():
        years = " · ".join(f"{y}: {r * 100:+.2f}%" for y, r in m.get("by_year", {}).items())
        print(f"  {mode:<7} ×{lev:g}: {fmt(m)}\n             por año → {years}")


if __name__ == "__main__":
    main()
