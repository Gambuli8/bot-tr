"""
Pruebas de ESTRÉS del carry (spot + short) con las mismas reglas que usa el bot (bot/carry.py).

El backtest (scripts/backtest_funding.py) dice cuánto rinde el carry en condiciones históricas normales.
Esto responde otra pregunta: **¿cuánto puedo perder en un día malo y qué lo provoca?**

Simula un par paso a paso cada 5 minutos (el ciclo del bot, CARRY_INTERVAL_S=300):
  - Capital del par: spot = C·L/(L+1), margen aislado del short = C/(L+1).
  - Liquidación del short si el máximo del perpetuo en esos 5 min toca el precio de liquidación
    (se pierde todo el margen). Si el bot está activo, al paso siguiente vende spot y rearma la cobertura.
  - Protección / recentrado como el bot: si el perpetuo se movió ±CARRY_REBALANCE_PCT/L desde el último
    ajuste, o la liquidación quedó a menos de --liq-guard/L, vuelve al reparto objetivo (con comisiones).
  - Funding cobrado (o pagado si es negativo) sobre el tamaño del short.
  - Comisiones reales (spot 0,10 %, perp 0,05 % taker) + 0,03 % de deslizamiento por pierna, multiplicado
    por el factor de spread del escenario.
  - Al final se desarma todo (salida de emergencia) con el spread del escenario.
  - Conservador: BingX liquida por precio "mark" (más suave que el último precio), acá las mechas del
    perpetuo cuentan enteras.

Escenarios sintéticos (no necesitan internet) + modo --historical que mide los peores eventos REALES
de cada par (mayor mecha en 1 h, mayor suba en 24 h, peor racha de funding) y los vuelve a pasar.

Uso:
  python scripts/stress_carry.py                         # ×1, ×2 y ×3 con la configuración del bot
  python scripts/stress_carry.py --leverages 2 --liq-guard 0.8
  python scripts/stress_carry.py --historical --pairs BTC-USDT,ETH-USDT,DOGE-USDT,XRP-USDT
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.fmt import money, pct  # noqa: E402

SPOT_FEE = 0.001
PERP_FEE = 0.0005
SLIP = 0.0003
MMR = 0.005
STEP_MIN = 5
STEPS_H = 60 // STEP_MIN
STEPS_D = 24 * STEPS_H
FUNDING_EVERY = 8 * STEPS_H


# ───────────────────────── trayectorias ─────────────────────────

@dataclass
class Bar:
    """Un paso de 5 minutos."""
    spot: float                 # precio spot al cierre del paso
    mark: float                 # precio del perpetuo al cierre
    mark_high: float            # máximo del perpetuo dentro del paso (mechas)
    funding: float | None = None  # tasa de funding que se cobra en este paso (None = no hay cobro)
    bot_up: bool = True         # False = bot o API caídos: no puede proteger ni rearmar


class Path_:
    """Constructor de trayectorias: encadena tramos y pone el funding cada 8 h."""

    def __init__(self, start: float = 100.0, funding: float = 0.0001):
        self.bars: list[Bar] = []
        self.px = start
        self.base_funding = funding

    def _push(self, spot: float, mark: float, high: float, bot_up: bool, funding: float | None) -> None:
        i = len(self.bars)
        rate = None
        if i > 0 and i % FUNDING_EVERY == 0:
            rate = self.base_funding if funding is None else funding
        self.bars.append(Bar(spot, mark, max(high, mark), rate, bot_up))

    def flat(self, steps: int, bot_up: bool = True, funding: float | None = None) -> "Path_":
        for _ in range(steps):
            self._push(self.px, self.px, self.px, bot_up, funding)
        return self

    def ramp(self, to: float, steps: int, bot_up: bool = True, funding: float | None = None) -> "Path_":
        start = self.px
        for k in range(1, steps + 1):
            prev = self.px
            self.px = start + (to - start) * k / steps
            self._push(self.px, self.px, max(prev, self.px), bot_up, funding)
        return self

    def wick(self, up_pct: float, bot_up: bool = True) -> "Path_":
        """Mecha de 5 min en el perpetuo que sube up_pct y cierra donde estaba."""
        self._push(self.px, self.px, self.px * (1 + up_pct), bot_up, None)
        return self

    def premium(self, prem: float, steps: int) -> "Path_":
        """El perpetuo cotiza prem por encima del spot durante steps (descalce de base)."""
        for _ in range(steps):
            m = self.px * (1 + prem)
            self._push(self.px, m, m, True, None)
        return self


# ───────────────────────── simulador de un par ─────────────────────────

@dataclass
class Result:
    capital: float
    final: float = 0.0
    min_equity: float = 0.0
    liquidations: int = 0
    protections: int = 0
    fees: float = 0.0
    funding: float = 0.0
    events: list[str] = field(default_factory=list)

    @property
    def pnl(self) -> float:
        return self.final - self.capital

    @property
    def worst(self) -> float:
        return self.min_equity - self.capital


class CarrySim:
    def __init__(self, capital: float, leverage: float, rebalance_pct: float = 0.3, liq_guard: float = 0.5,
                 spread_mult: float = 1.0):
        self.C, self.L = capital, leverage
        self.band = rebalance_pct / leverage
        self.guard = liq_guard / leverage
        self.slip = SLIP * spread_mult
        self.spot_qty = self.short_qty = self.entry = self.margin = self.cash = 0.0
        self.anchor = 0.0
        self.res = Result(capital)

    # ── contabilidad ──
    def equity(self, b: Bar) -> float:
        return self.cash + self.spot_qty * b.spot + self.margin + self.short_qty * (self.entry - b.mark)

    def liq_price(self) -> float:
        if self.short_qty <= 0:
            return float("inf")
        return (self.margin + self.short_qty * self.entry) / (self.short_qty * (1 + MMR))

    def _fee(self, spot_notional: float, perp_notional: float) -> float:
        cost = spot_notional * (SPOT_FEE + self.slip) + perp_notional * (PERP_FEE + self.slip)
        self.res.fees += cost
        return cost

    # ── acciones ──
    def build(self, b: Bar) -> None:
        """Arma (o rearma) el reparto objetivo con todo el patrimonio, cerrando y reabriendo el short."""
        eq = self.equity(b)
        target_qty = eq * self.L / (self.L + 1) / b.spot
        cost = self._fee(abs(target_qty - self.spot_qty) * b.spot,
                         (self.short_qty + target_qty) * b.mark if self.short_qty else target_qty * b.mark)
        eq -= cost
        self.spot_qty = self.short_qty = eq * self.L / (self.L + 1) / b.spot
        self.margin = eq - self.spot_qty * b.spot
        self.entry = b.mark
        self.cash = 0.0
        self.anchor = b.mark

    def unwind(self, b: Bar) -> None:
        eq = self.equity(b)
        eq -= self._fee(self.spot_qty * b.spot, self.short_qty * b.mark)
        self.cash, self.spot_qty, self.short_qty, self.margin = eq, 0.0, 0.0, 0.0

    def step(self, i: int, b: Bar) -> None:
        if self.short_qty > 0 and b.mark_high >= self.liq_price():
            self.res.liquidations += 1
            self.res.events.append(f"paso {i}: liquidación del short a {self.liq_price():.2f}")
            self.margin = 0.0
            self.short_qty = 0.0
            self.entry = 0.0

        if b.bot_up:
            if self.short_qty == 0 and self.spot_qty > 0:
                self.build(b)            # spot descubierto → vende lo necesario y rearma
            elif self.short_qty > 0:
                liq_dist = (self.liq_price() - b.mark) / b.mark
                moved = b.mark / self.anchor - 1
                if moved >= self.band or moved <= -self.band or liq_dist < self.guard:
                    self.build(b)
                    self.res.protections += 1

        if b.funding is not None and self.short_qty > 0:
            amount = b.funding * self.short_qty * b.mark
            self.margin += amount
            self.res.funding += amount

        self.res.min_equity = min(self.res.min_equity, self.equity(b))

    def run(self, bars: list[Bar]) -> Result:
        self.cash = self.C
        self.res.min_equity = self.C
        self.build(bars[0])
        self.res.min_equity = self.equity(bars[0])
        for i, b in enumerate(bars[1:], start=1):
            self.step(i, b)
        self.unwind(bars[-1])
        self.res.final = self.cash
        return self.res


def simulate(bars: list[Bar], leverage: float, capital: float = 50.0, rebalance_pct: float = 0.3,
             liq_guard: float = 0.5, spread_mult: float = 1.0) -> Result:
    return CarrySim(capital, leverage, rebalance_pct, liq_guard, spread_mult).run(bars)


# ───────────────────────── escenarios ─────────────────────────

@dataclass
class Scenario:
    name: str
    bars: list[Bar]
    spread_mult: float = 1.0
    note: str = ""


def scenarios(leverage: float, rebalance_pct: float = 0.3) -> list[Scenario]:
    band = rebalance_pct / leverage
    just_below = 100 * (1 + band * 0.99)       # lo más arriba que puede estar sin que el bot proteja
    out: list[Scenario] = []
    D, H = STEPS_D, STEPS_H

    out.append(Scenario("Día normal (referencia: solo comisiones)", Path_().flat(2 * D).bars))
    out.append(Scenario("Caída −20 % en un día", Path_().flat(H).ramp(80, D).flat(D).bars))
    out.append(Scenario("Caída −50 % en una semana", Path_().flat(H).ramp(50, 7 * D).flat(D).bars))
    out.append(Scenario("Suba +50 % en un día", Path_().flat(H).ramp(150, D).flat(D).bars))
    out.append(Scenario("Suba +100 % en 3 días", Path_().flat(H).ramp(200, 3 * D).flat(D).bars))
    for x in (0.2, 0.3, 0.4, 0.6):
        out.append(Scenario(f"Mecha {pct(x * 100, True, 0)} en 5 min que vuelve",
                            Path_().flat(H).wick(x).flat(D).bars))
    for x in (0.15, 0.2, 0.3):
        out.append(Scenario(f"Peor caso: sube hasta el borde de la banda y mecha {pct(x * 100, True, 0)}",
                            Path_().flat(H).ramp(just_below, 6 * H).wick(x).ramp(100, 6 * H).flat(D).bars,
                            note=f"sube a {pct(just_below - 100, True, 1)} sin activar la protección"))
    out.append(Scenario("Perpetuo +10 % sobre el spot por 6 h (base)", Path_().flat(H).premium(0.10, 6 * H).flat(D).bars))
    for hours, up in ((1, 0.30), (6, 0.50), (24, 0.60)):
        # Lo peligroso no es la suba en sí sino suba → liquidación → bajada con el spot sin cubrir.
        back = 100 * (1 + up / 3)
        out.append(Scenario(f"Bot/API caídos {hours} h: sube {pct(up * 100, True, 0)} y vuelve a "
                            f"{pct(back - 100, True, 0)}",
                            Path_().flat(H).ramp(100 * (1 + up), hours * H // 2, bot_up=False)
                            .ramp(back, hours * H - hours * H // 2, bot_up=False).flat(D).bars))
    for rate, days in ((-0.0005, 30), (-0.001, 7), (-0.003, 3)):
        out.append(Scenario(f"Funding {pct(rate * 100, True)} cada 8 h durante {days} días",
                            Path_().flat(days * D + H, funding=rate).bars))
    out.append(Scenario("Salida de emergencia con spread ×3", Path_().flat(H).bars, spread_mult=3.0))
    combo = (Path_().flat(H).ramp(80, D, funding=-0.001).ramp(80 * 1.4, H, bot_up=False, funding=-0.001)
             .ramp(95, 6 * H, funding=-0.001).flat(2 * D, funding=-0.001))
    out.append(Scenario("Día negro: −20 %, rebote +40 % en 1 h con API caída, spread ×3, funding −0,1 %",
                        combo.bars, spread_mult=3.0))
    return out


# ───────────────────────── histórico (necesita internet) ─────────────────────────

def worst_wick(candles: list[dict]) -> float:
    """Mayor suba intra-hora: máximo de la vela contra el cierre anterior."""
    best = 0.0
    for prev, c in zip(candles, candles[1:]):
        best = max(best, c["high"] / prev["close"] - 1)
    return best


def worst_rise(candles: list[dict], hours: int) -> float:
    """Mayor suba en una ventana de `hours` horas (mínimo → máximo posterior dentro de la ventana)."""
    best = 0.0
    lows: list[tuple[int, float]] = []
    for i, c in enumerate(candles):
        while lows and lows[0][0] <= i - hours:
            lows.pop(0)
        if lows:
            best = max(best, c["high"] / min(v for _, v in lows) - 1)
        lows.append((i, c["low"]))
    return best


def worst_funding(rates: list[float], window: int) -> tuple[float, float]:
    """Peor suma de funding en `window` pagos consecutivos → (suma, tasa promedio por pago)."""
    if len(rates) < window:
        return 0.0, 0.0
    s = sum(rates[:window])
    worst = s
    for i in range(window, len(rates)):
        s += rates[i] - rates[i - window]
        worst = min(worst, s)
    return worst, worst / window


def historical_scenarios(symbol: str, candles: list[dict], rates: list[float]) -> list[Scenario]:
    D, H = STEPS_D, STEPS_H
    w = worst_wick(candles)
    r24 = worst_rise(candles, 24)
    f21, avg21 = worst_funding(rates, 21)   # 7 días
    return [
        Scenario(f"{symbol}: peor mecha real en 1 h ({pct(w * 100, True, 1)}) que vuelve", Path_().flat(H).wick(w).flat(D).bars),
        Scenario(f"{symbol}: peor suba real en 24 h ({pct(r24 * 100, True, 1)})", Path_().flat(H).ramp(100 * (1 + r24), D).flat(D).bars),
        Scenario(f"{symbol}: peor suba real en 24 h con el bot caído",
                 Path_().flat(H).ramp(100 * (1 + r24), D, bot_up=False).flat(D).bars),
        Scenario(f"{symbol}: peor semana de funding real ({pct(f21 * 100, True)} en 7 días)",
                 Path_().flat(7 * D + H, funding=avg21).bars),
    ]


def load_historical(symbols: list[str], years: float) -> dict[str, tuple[list[dict], list[float]]]:
    import time as _t
    sys.path.insert(0, str(ROOT / "scripts"))
    from backtest import as_candles, fetch                 # noqa: E402
    from backtest_funding import funding_bingx             # noqa: E402

    now = int(_t.time() * 1000) // 3_600_000 * 3_600_000
    start = now - int(years * 365 * 24 * 3_600_000)
    out = {}
    for sym in symbols:
        fund = [r for t, r in funding_bingx(sym) if t >= start]
        candles = as_candles(fetch(sym, "1h", start, now))
        if candles and fund:
            out[sym] = (candles, fund)
        else:
            print(f"  {sym}: sin datos, se omite")
    return out


# ───────────────────────── salida ─────────────────────────

def table(rows: list[Scenario], leverages: list[float], capital: float, n_pairs: int, apy: float,
          rebalance_pct: float, liq_guard: float) -> None:
    yearly = capital * n_pairs * apy
    for lev in leverages:
        print(f"\n=== Short ×{lev:g} · banda ±{pct(rebalance_pct / lev * 100, False, 1)} · "
              f"protege si la liquidación está a menos de {pct(liq_guard / lev * 100, False, 0)} · "
              f"{money(capital)} por par ===")
        print(f"{'Escenario':<84} {'Resultado':>10} {'Peor punto':>11} {'Liq':>4} {'Prot':>5}  "
              f"{'Si pasa en los ' + str(n_pairs) + ' pares':>22}")
        for sc in rows:
            r = simulate(sc.bars, lev, capital, rebalance_pct, liq_guard, sc.spread_mult)
            total = r.pnl * n_pairs
            months = (-total / yearly * 12) if yearly > 0 and total < 0 else 0
            cost = f"{money(total, True)}" + (f" ({months:.1f} meses)".replace(".", ",") if months >= 0.1 else "")
            print(f"{sc.name[:84]:<84} {pct(r.pnl / capital * 100, True):>10} {pct(r.worst / capital * 100, True):>11} "
                  f"{r.liquidations:>4} {r.protections:>5}  {cost:>22}")
            if sc.note:
                print(f"{'  ↳ ' + sc.note:<84}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--leverages", default="1,2,3")
    ap.add_argument("--capital", type=float, default=50.0, help="USDT por par")
    ap.add_argument("--pairs-count", type=int, default=4, help="pares del carry (para el total)")
    ap.add_argument("--rebalance-pct", type=float, default=0.3, help="CARRY_REBALANCE_PCT del bot")
    ap.add_argument("--liq-guard", type=float, default=0.5,
                    help="protege si la liquidación queda a menos de liq_guard/L (el bot usa 0,5)")
    ap.add_argument("--apy", type=float, default=0.05,
                    help="rendimiento anual esperado, para traducir pérdidas a meses de funding")
    ap.add_argument("--historical", action="store_true", help="usa los peores eventos reales (necesita internet)")
    ap.add_argument("--pairs", default="BTC-USDT,ETH-USDT,DOGE-USDT,XRP-USDT")
    ap.add_argument("--years", type=float, default=4)
    args = ap.parse_args()

    leverages = [float(x) for x in args.leverages.split(",")]
    if not 0 < args.liq_guard < 0.9:
        # Recién armado, la liquidación queda a ~1/L: una guarda ≥ 0,9 haría que proteja en cada ciclo.
        raise SystemExit("--liq-guard tiene que estar entre 0 y 0,9")
    print("Pruebas de estrés del carry · 'Resultado' = después de desarmar todo · 'Peor punto' = la peor caída "
          "en el camino.\n'Meses' = cuántos meses de funding se come la pérdida con un rendimiento anual de "
          f"{pct(args.apy * 100, False, 0)}. Custodia: si BingX quiebra o congela fondos se pierde el 100 % (no se simula).")

    if args.historical:
        data = load_historical([p.strip().upper() for p in args.pairs.split(",") if p.strip()], args.years)
        for sym, (candles, rates) in data.items():
            print(f"\n##### {sym} · {len(candles)} velas de 1 h · {len(rates)} pagos de funding #####")
            table(historical_scenarios(sym, candles, rates), leverages, args.capital, 1, args.apy,
                  args.rebalance_pct, args.liq_guard)
        return

    for lev in leverages:
        table(scenarios(lev, args.rebalance_pct), [lev], args.capital, args.pairs_count, args.apy,
              args.rebalance_pct, args.liq_guard)


if __name__ == "__main__":
    main()
