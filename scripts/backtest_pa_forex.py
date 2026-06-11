"""
scripts/backtest_pa_forex.py
Backtest del PriceActionEngineFX (liquidity sweep A FAVOR de la estructura HTF)
sobre EUR/USD, con datos reales de Dukascopy (scripts/fx_data.py) y modelo de
costos retail realista.

Modelo de costos (por el enunciado de la TAREA 1):
  - Entramos/salimos sobre precio MID. El cruce bid/ask se cobra como:
      spread_efectivo = max(spread_real_de_la_vela_de_entrada, 0.8 pip)
      comisión        = 0.6 pip round-trip
    costo_total_RT (en precio) = (spread_efectivo + comisión) × pip
  - Swap overnight: swap_pips por noche (rollover UTC) que la posición sigue abierta.
    Conservador: siempre débito (en retail solemos pagar más de lo que cobramos).
  - pip EUR/USD = 0.0001.

Sizing: riesgo fijo por trade = capital × max_risk. units(EUR) = riesgo$ / SL_distancia.
Pérdida en SL ≈ riesgo$ exacto (antes de costos). PnL en USD = (exit-entry)×units.

Sin look-ahead: la señal se evalúa al CIERRE de la vela de gatillo y la posición
se abre a ese cierre; SL/TP recién se evalúan desde la vela siguiente.

Uso:
    python scripts/backtest_pa_forex.py --year 2019 --trig 1h --htf 4h
    python scripts/backtest_pa_forex.py --year 2022 --trig 5m --htf 1h --rr 2.0
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd

from core.price_action_engine_fx import PriceActionEngineFX, FxDecision
from scripts.fx_data import load_5m, resample

PIP = 0.0001


@dataclass
class Pos:
    direction: str
    entry_price: float
    units: float          # EUR (base)
    stop_loss: float
    take_profit: float
    entry_idx: int
    entry_ts: pd.Timestamp
    entry_spread: float   # spread real (precio) de la vela de entrada


@dataclass
class Trade:
    direction: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    pnl_gross: float
    pnl_net: float
    cost: float
    bars_held: int
    nights: int
    exit_reason: str


class FxSim:
    def __init__(self, settings, *, min_spread_pips=0.8, commission_pips_rt=0.6,
                 swap_pips_per_night=0.3, max_leverage=30.0, pip_size=PIP):
        self.s = settings
        self.capital = settings.initial_capital
        self.risk = settings.max_risk_per_trade
        self.pip = pip_size
        self.min_spread = min_spread_pips * pip_size
        self.commission = commission_pips_rt * pip_size
        self.swap = swap_pips_per_night * pip_size
        self.max_leverage = max_leverage
        self.pos: Optional[Pos] = None
        self.trades: list[Trade] = []
        self.peak = self.capital
        self.max_dd = 0.0
        self.equity_curve: list[float] = [self.capital]

    def open(self, d: FxDecision, idx: int, ts, entry_spread: float):
        sl_dist = abs(d.entry_price - d.stop_loss_price)
        if sl_dist <= 0:
            return
        risk_dollars = self.capital * self.risk
        units = risk_dollars / sl_dist
        # Cap por apalancamiento (notional = units × precio)
        max_units = self.capital * self.max_leverage / d.entry_price
        if units > max_units:
            units = max_units
        self.pos = Pos(
            direction=d.direction, entry_price=d.entry_price, units=units,
            stop_loss=d.stop_loss_price, take_profit=d.take_profit_price,
            entry_idx=idx, entry_ts=ts, entry_spread=entry_spread,
        )

    def _close(self, exit_price: float, reason: str, idx: int, ts):
        p = self.pos
        if p.direction == "LONG":
            pnl_gross = (exit_price - p.entry_price) * p.units
        else:
            pnl_gross = (p.entry_price - exit_price) * p.units
        # Costo de transacción: spread efectivo + comisión, sobre las units.
        eff_spread = max(p.entry_spread, self.min_spread)
        trans_cost = (eff_spread + self.commission) * p.units
        # Swap: nº de rollovers UTC (cambios de día) mientras la posición vivió.
        nights = max(0, (ts.normalize() - p.entry_ts.normalize()).days)
        swap_cost = self.swap * p.units * nights
        cost = trans_cost + swap_cost
        pnl_net = pnl_gross - cost
        self.capital += pnl_net
        self.equity_curve.append(self.capital)
        if self.capital > self.peak:
            self.peak = self.capital
        dd = (self.peak - self.capital) / self.peak * 100
        if dd > self.max_dd:
            self.max_dd = dd
        self.trades.append(Trade(
            direction=p.direction, entry_ts=p.entry_ts, exit_ts=ts,
            entry_price=p.entry_price, exit_price=exit_price,
            pnl_gross=pnl_gross, pnl_net=pnl_net, cost=cost,
            bars_held=idx - p.entry_idx, nights=nights, exit_reason=reason,
        ))
        self.pos = None

    def step(self, bar: pd.Series, idx: int, ts):
        """Evalúa SL/TP sobre la vela actual (intra-vela con high/low del MID)."""
        if self.pos is None:
            return
        p = self.pos
        high = float(bar["high"])
        low = float(bar["low"])
        if p.direction == "LONG":
            # Conservador: si la vela toca SL y TP, asumimos SL primero.
            if low <= p.stop_loss:
                self._close(p.stop_loss, "Stop-loss", idx, ts)
            elif high >= p.take_profit:
                self._close(p.take_profit, "Take-profit", idx, ts)
        else:
            if high >= p.stop_loss:
                self._close(p.stop_loss, "Stop-loss", idx, ts)
            elif low <= p.take_profit:
                self._close(p.take_profit, "Take-profit", idx, ts)


def run_backtest(df_trig: pd.DataFrame, df_htf: pd.DataFrame, settings,
                 sim_kwargs: dict, warmup: int = 60) -> FxSim:
    engine = PriceActionEngineFX(settings)
    sim = FxSim(settings, **sim_kwargs)
    for i in range(warmup, len(df_trig)):
        ts = df_trig.index[i]
        sim.step(df_trig.iloc[i], i, ts)
        if sim.pos is None:
            df_trig_sf = df_trig.iloc[: i + 1]
            df_htf_sf = df_htf.loc[:ts]
            if len(df_htf_sf) < 30:
                continue
            d = engine.analyze(df_trig_sf, df_htf_sf)
            if d.accion in ("COMPRAR", "VENDER"):
                entry_spread = float(df_trig.iloc[i]["spread"])
                sim.open(d, i, ts, entry_spread)
    if sim.pos is not None:
        sim._close(float(df_trig.iloc[-1]["close"]), "Fin del backtest",
                   len(df_trig) - 1, df_trig.index[-1])
    return sim


def metrics(sim: FxSim, days: float) -> dict:
    n = len(sim.trades)
    if n == 0:
        return {"n": 0}
    wins = [t for t in sim.trades if t.pnl_net > 0]
    losses = [t for t in sim.trades if t.pnl_net <= 0]
    gross_win = sum(t.pnl_gross for t in sim.trades if t.pnl_gross > 0)
    gross_loss = sum(t.pnl_gross for t in sim.trades if t.pnl_gross <= 0)
    net_win = sum(t.pnl_net for t in wins)
    net_loss = sum(t.pnl_net for t in losses)
    pf_gross = gross_win / abs(gross_loss) if gross_loss != 0 else float("inf")
    pf_net = net_win / abs(net_loss) if net_loss != 0 else float("inf")
    total_net = sum(t.pnl_net for t in sim.trades)
    total_cost = sum(t.cost for t in sim.trades)
    return {
        "n": n,
        "wr": len(wins) / n * 100,
        "pf_gross": pf_gross,
        "pf_net": pf_net,
        "ret": total_net / sim.s.initial_capital * 100,
        "final": sim.s.initial_capital + total_net,
        "dd": sim.max_dd,
        "cost": total_cost,
        "cost_pct": total_cost / sim.s.initial_capital * 100,
        "trades_per_month": n / max(days / 30.0, 1e-9),
        "longs": sum(1 for t in sim.trades if t.direction == "LONG"),
        "shorts": sum(1 for t in sim.trades if t.direction == "SHORT"),
    }


def report(sim: FxSim, m: dict, label: str):
    print("\n" + "=" * 64)
    print(f"  BACKTEST PA-FOREX — {label}")
    print("=" * 64)
    if m.get("n", 0) == 0:
        print("Sin operaciones.")
        print("=" * 64)
        return
    print(f"Capital:            ${sim.s.initial_capital:,.2f} → ${m['final']:,.2f}  ({m['ret']:+.2f}%)")
    print(f"Max drawdown:       {m['dd']:.2f}%")
    print(f"Operaciones:        {m['n']}  ({m['longs']} LONG, {m['shorts']} SHORT)")
    print(f"Trades/mes:         {m['trades_per_month']:.1f}")
    print(f"Win rate:           {m['wr']:.1f}%")
    print(f"PF BRUTO:           {m['pf_gross']:.2f}")
    print(f"PF NETO:            {m['pf_net']:.2f}")
    print(f"Costos totales:     ${m['cost']:,.2f}  ({m['cost_pct']:.1f}% del capital)")
    by_reason: dict = {}
    for t in sim.trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    print("Cierres:            " + " / ".join(f"{k} {v}" for k, v in
                                              sorted(by_reason.items(), key=lambda x: -x[1])))
    print("=" * 64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="EURUSD")
    ap.add_argument("--year", type=int, default=2019)
    ap.add_argument("--trig", default="1h", help="TF de gatillo (5m/15m/1h)")
    ap.add_argument("--htf", default="4h", help="TF de contexto (1h/4h)")
    ap.add_argument("--risk", type=float, default=0.01, help="riesgo por trade (0.01=1%)")
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument("--vol-mult", type=float, default=1.5)
    ap.add_argument("--atr-mult", type=float, default=1.5)
    ap.add_argument("--rr", type=float, default=2.5)
    ap.add_argument("--fractal", type=int, default=3)
    ap.add_argument("--sl-min-pips", type=float, default=8.0)
    ap.add_argument("--sl-max-pips", type=float, default=60.0)
    ap.add_argument("--swap-pips", type=float, default=0.3)
    ap.add_argument("--no-cost", action="store_true", help="reporta bruto (sin costos)")
    args = ap.parse_args()

    class S:  # settings liviano para el engine + sim
        pass
    s = S()
    s.initial_capital = args.capital
    s.max_risk_per_trade = args.risk
    s.pafx_vol_mult = args.vol_mult
    s.pafx_atr_sl_mult = args.atr_mult
    s.pafx_tp_rr = args.rr
    s.pafx_fractal_n = args.fractal
    s.pafx_sl_min_pips = args.sl_min_pips
    s.pafx_sl_max_pips = args.sl_max_pips

    print(f"Cargando {args.pair} {args.year} (5m)...", file=sys.stderr)
    df5 = load_5m(args.pair, args.year)
    df_trig = resample(df5, args.trig)
    df_htf = resample(df5, args.htf)
    days = (df5.index[-1] - df5.index[0]).total_seconds() / 86400.0
    print(f"  trig {args.trig}: {len(df_trig)} velas | htf {args.htf}: {len(df_htf)} velas | "
          f"{days:.0f} días", file=sys.stderr)

    sim_kwargs = dict(
        min_spread_pips=0.0 if args.no_cost else 0.8,
        commission_pips_rt=0.0 if args.no_cost else 0.6,
        swap_pips_per_night=0.0 if args.no_cost else args.swap_pips,
    )
    sim = run_backtest(df_trig, df_htf, s, sim_kwargs)
    m = metrics(sim, days)
    cost_tag = "SIN COSTOS (bruto)" if args.no_cost else "con costos retail"
    label = (f"{args.pair} {args.year} | {args.trig}/{args.htf} | "
             f"RR {args.rr} vm {args.vol_mult} atr {args.atr_mult} fr {args.fractal} | "
             f"risk {args.risk:.1%} | {cost_tag}")
    report(sim, m, label)


if __name__ == "__main__":
    main()
