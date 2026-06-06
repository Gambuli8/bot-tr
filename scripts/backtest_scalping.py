"""
scripts/backtest_scalping.py
Backtest del ScalpingEngine (BB squeeze + expansion + volume).

Por defecto:
  - TF base: 5m
  - Leverage: 10x (Futures USDT-M)
  - Risk por trade: 8% del capital base
  - Fee: 0.03% por lado (mezcla maker/taker realista)

Uso:
  python scripts/backtest_scalping.py --days 60
  python scripts/backtest_scalping.py --days 60 --timeframe 15m --risk-pct 0.10
  python scripts/backtest_scalping.py --days 60 --leverage 15 --fee 0.0002
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

import ccxt
import pandas as pd

from config.settings import load_settings
from core.scalping_engine import ScalpingEngine, ScalpDecision


@dataclass
class SimPos:
    direction: str
    entry_price: float
    amount_btc: float
    amount_usdt: float           # notional
    stop_loss: float
    take_profit: float
    entry_idx: int
    entry_ts: pd.Timestamp
    margin: float = 0.0


@dataclass
class TradeRec:
    direction: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    pnl_usdt: float
    pnl_gross: float
    fee_paid: float
    bars_held: int
    exit_reason: str


def fetch_history(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    mpb = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}[timeframe]
    total = (days * 24 * 60) // mpb
    end_ms = ex.milliseconds()
    start_ms = end_ms - (total * mpb * 60_000)
    rows: list = []
    cursor = start_ms
    while cursor < end_ms:
        chunk = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
        if not chunk:
            break
        rows.extend(chunk)
        cursor = chunk[-1][0] + mpb * 60_000
        if len(chunk) < 1000:
            break
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


class Sim:
    def __init__(self, settings, leverage: float = 10.0, liquidate_at_pct: float = 0.95):
        self.s = settings
        self.capital = settings.initial_capital
        self.leverage = leverage
        self.liquidate_at_pct = liquidate_at_pct
        self.position: Optional[SimPos] = None
        self.trades: list[TradeRec] = []
        self.peak = self.capital
        self.max_dd = 0.0
        self.liquidations = 0

    def _position_size_usdt(self, sl_pct: float) -> float:
        max_risk = self.capital * self.s.max_risk_per_trade
        position = max_risk / sl_pct if sl_pct > 0 else max_risk * 10
        tradable_cap = self.capital * (1 - self.s.trade_reserve_pct) * self.leverage
        return min(position, tradable_cap)

    def open(self, decision: ScalpDecision, idx: int, ts):
        size = self._position_size_usdt(decision.stop_loss_pct)
        if size <= 0:
            return
        entry = decision.entry_price
        amt = size / entry
        margin = size / self.leverage
        if margin > self.capital * (1 - self.s.trade_reserve_pct):
            margin = self.capital * (1 - self.s.trade_reserve_pct)
            size = margin * self.leverage
            amt = size / entry
        self.position = SimPos(
            direction=decision.direction,
            entry_price=entry,
            amount_btc=amt,
            amount_usdt=size,
            stop_loss=decision.stop_loss_price,
            take_profit=decision.take_profit_price,
            entry_idx=idx,
            entry_ts=ts,
            margin=margin,
        )
        self.capital -= margin

    def close(self, exit_price: float, reason: str, idx: int, ts, engine=None):
        if self.position is None:
            return
        p = self.position
        if p.direction == "LONG":
            pnl_gross = (exit_price - p.entry_price) * p.amount_btc
        else:
            pnl_gross = (p.entry_price - exit_price) * p.amount_btc
        entry_value = p.amount_usdt
        exit_value = p.amount_btc * exit_price
        # Fees diferenciadas: entrada y TP son Limit Post-Only → maker.
        # SL y liquidación son Market (taker) por seguridad patrimonial.
        maker_fee = self.s.commission_pct_per_side
        taker_fee = getattr(self.s, "commission_taker_pct", maker_fee)
        if reason in ("Stop-loss", "LIQUIDACIÓN"):
            exit_fee = exit_value * taker_fee
        else:
            exit_fee = exit_value * maker_fee
        entry_fee = entry_value * maker_fee
        fee = entry_fee + exit_fee
        pnl = pnl_gross - fee
        self.capital += p.margin + pnl
        if reason == "LIQUIDACIÓN":
            self.liquidations += 1
        if self.capital > self.peak:
            self.peak = self.capital
        dd = (self.peak - self.capital) / self.peak * 100
        if dd > self.max_dd:
            self.max_dd = dd
        self.trades.append(TradeRec(
            direction=p.direction,
            entry_ts=p.entry_ts,
            exit_ts=ts,
            entry_price=p.entry_price,
            exit_price=exit_price,
            pnl_usdt=pnl,
            pnl_gross=pnl_gross,
            fee_paid=fee,
            bars_held=idx - p.entry_idx,
            exit_reason=reason,
        ))
        self.position = None
        if engine is not None:
            engine.mark_close(idx)

    def step(self, bar: pd.Series, idx: int, ts, engine=None) -> None:
        if self.position is None:
            return
        p = self.position
        high = float(bar["high"])
        low = float(bar["low"])

        if self.leverage > 1.0:
            if p.direction == "LONG":
                liq = p.entry_price - (p.margin * self.liquidate_at_pct) / p.amount_btc
                if low <= liq:
                    self.close(liq, "LIQUIDACIÓN", idx, ts, engine=engine)
                    return
            else:
                liq = p.entry_price + (p.margin * self.liquidate_at_pct) / p.amount_btc
                if high >= liq:
                    self.close(liq, "LIQUIDACIÓN", idx, ts, engine=engine)
                    return

        if p.direction == "LONG":
            if low <= p.stop_loss:
                self.close(p.stop_loss, "Stop-loss", idx, ts, engine=engine)
            elif high >= p.take_profit:
                self.close(p.take_profit, "Take-profit", idx, ts, engine=engine)
        else:
            if high >= p.stop_loss:
                self.close(p.stop_loss, "Stop-loss", idx, ts, engine=engine)
            elif low <= p.take_profit:
                self.close(p.take_profit, "Take-profit", idx, ts, engine=engine)


def report(sim: Sim, settings, days: int, timeframe: str):
    n = len(sim.trades)
    final = settings.initial_capital + sum(t.pnl_usdt for t in sim.trades)
    ret_pct = (final - settings.initial_capital) / settings.initial_capital * 100
    if n == 0:
        print("\n=== sin operaciones ===")
        print(f"Capital final: ${final:,.2f}")
        return
    wins = [t for t in sim.trades if t.pnl_usdt > 0]
    losses = [t for t in sim.trades if t.pnl_usdt <= 0]
    wr = len(wins) / n * 100
    avg_w = sum(t.pnl_usdt for t in wins) / len(wins) if wins else 0
    avg_l = sum(t.pnl_usdt for t in losses) / len(losses) if losses else 0
    pf_bruto = (sum(t.pnl_gross for t in wins) /
                abs(sum(t.pnl_gross for t in losses))
                if losses and sum(t.pnl_gross for t in losses) != 0 else float("inf"))
    pf_neto = (sum(t.pnl_usdt for t in wins) /
               abs(sum(t.pnl_usdt for t in losses))
               if losses and sum(t.pnl_usdt for t in losses) != 0 else float("inf"))
    total_fees = sum(t.fee_paid for t in sim.trades)
    gross_wins = sum(t.pnl_gross for t in sim.trades if t.pnl_gross > 0)
    gross_losses = sum(t.pnl_gross for t in sim.trades if t.pnl_gross <= 0)

    by_reason = {}
    for t in sim.trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    longs = sum(1 for t in sim.trades if t.direction == "LONG")
    shorts = n - longs

    print("\n" + "=" * 60)
    print(f"  BACKTEST SCALPING — {days} días @ TF {timeframe}")
    print("=" * 60)
    print(f"Capital inicial:    ${settings.initial_capital:>10,.2f}")
    print(f"Capital final:      ${final:>10,.2f}  ({ret_pct:+.2f}%)")
    print(f"Max drawdown:       {sim.max_dd:>10.2f}%")
    print(f"Liquidaciones:      {sim.liquidations:>10}")
    print()
    print(f"Operaciones:        {n:>10}  ({longs} LONG, {shorts} SHORT)")
    print(f"Operaciones/día:    {n / max(days, 1):>10.2f}")
    print(f"Acertamos:          {wr:>10.1f}%  ({len(wins)} ganadoras, {len(losses)} perdedoras)")
    print()
    print("--- BRUTO ---")
    print(f"Ganancia bruta:     ${gross_wins:>+10,.2f}")
    print(f"Pérdida bruta:      ${gross_losses:>+10,.2f}")
    print(f"Profit Factor BRUTO:{pf_bruto:>10.2f}")
    print()
    print("--- NETO (con fees) ---")
    print(f"Total fees:         ${total_fees:>+10,.2f}")
    print(f"% capital en fees:  {total_fees / settings.initial_capital * 100:>10.2f}%")
    print(f"PnL neto:           ${final - settings.initial_capital:>+10,.2f}")
    print(f"Profit Factor NETO: {pf_neto:>10.2f}")
    print(f"Ganancia promedio:  ${avg_w:>+10,.2f}")
    print(f"Pérdida promedio:   ${avg_l:>+10,.2f}")
    print()
    print("Cómo cerraron:")
    for r, c in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"  • {r}: {c}")
    print("=" * 60)


def main():
    p = argparse.ArgumentParser(description="Backtest Scalping")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--timeframe", type=str, default="5m")
    p.add_argument("--symbol", type=str, default=None)
    p.add_argument("--leverage", type=float, default=10.0)
    p.add_argument("--risk-pct", type=float, default=0.08, help="Default 8% (rango 8-10%)")
    p.add_argument("--fee", type=float, default=0.0002,
                   help="Maker fee por lado. Default 0.02% (Futures USDT-M)")
    p.add_argument("--taker-fee", type=float, default=0.0005,
                   help="Taker fee. Aplicado al SL/Liquidación. Default 0.05%")
    p.add_argument("--vol-spike", type=float, default=None)
    p.add_argument("--squeeze-pct", type=float, default=None)
    p.add_argument("--tp-atr", type=float, default=None)
    p.add_argument("--sl-atr", type=float, default=None)
    args = p.parse_args()

    s = load_settings()
    if args.symbol:
        s.symbol = args.symbol
    s.max_risk_per_trade = args.risk_pct
    s.commission_pct_per_side = args.fee
    setattr(s, "commission_taker_pct", args.taker_fee)
    if args.vol_spike is not None:
        setattr(s, "scalp_vol_spike", args.vol_spike)
    if args.squeeze_pct is not None:
        setattr(s, "scalp_squeeze_pct", args.squeeze_pct)
    if args.tp_atr is not None:
        setattr(s, "scalp_tp_atr_mult", args.tp_atr)
    if args.sl_atr is not None:
        setattr(s, "scalp_sl_atr_mult", args.sl_atr)

    print(f"Bajando {args.days} días de {s.symbol} @ {args.timeframe}...")
    df = fetch_history(s.symbol, args.timeframe, args.days)
    print(f"  {len(df)} velas")
    print(f"  risk={s.max_risk_per_trade:.2%}  leverage={args.leverage}x  fee={s.commission_pct_per_side:.4%}")

    engine = ScalpingEngine(s)
    sim = Sim(s, leverage=args.leverage)

    warm = 110
    for i in range(warm, len(df)):
        ts = df.index[i]
        # Procesar SL/TP/liquidación de la posición abierta
        sim.step(df.iloc[i], i, ts, engine=engine)
        # Buscar nueva señal si no hay posición
        if sim.position is None:
            dec = engine.analyze(df, i)
            if dec.accion in ("COMPRAR", "VENDER"):
                sim.open(dec, i, ts)

    if sim.position is not None:
        sim.close(float(df.iloc[-1]["close"]), "Fin", len(df) - 1, df.index[-1], engine=engine)

    report(sim, s, args.days, args.timeframe)


if __name__ == "__main__":
    main()
