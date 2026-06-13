"""
scripts/backtest_price_action.py
Backtest del PriceActionEngine sobre TF 1h con contexto 4h.
- Itera cada vela 1h cerrada, llama analyze(df_1h_so_far, df_4h_so_far).
- Si dispara señal, simula la posición con SL/TP absolutos.

MODELO DE EJECUCIÓN (2026-06-13): fees diferenciadas maker/taker + slippage +
entradas maker con fill realista. Los DEFAULTS reproducen el baseline anterior
(entry-mode=taker, slippage=0, maker_fee=taker_fee=commission), así que el comando
de validación de siempre da el mismo resultado.

Uso:
    python scripts/backtest_price_action.py --days 180 --leverage 7 --risk-pct 0.05 --commission 0.0005
    # Comparar ejecución realista taker vs maker:
    python scripts/backtest_price_action.py --days 180 --leverage 7 --risk-pct 0.05 \
        --taker-fee 0.0005 --maker-fee 0.0002 --slippage 0.0003 --entry-mode taker
    python scripts/backtest_price_action.py ... --entry-mode maker --maker-timeout 2
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
from core.price_action_engine import PriceActionEngine, PriceActionDecision


# ─────────────────────────────────────────
#  Modelo de posición simulada
# ─────────────────────────────────────────

@dataclass
class SimPos:
    direction: str          # "LONG" | "SHORT"
    entry_price: float
    amount_btc: float
    amount_usdt: float      # notional (posición × leverage)
    stop_loss: float
    take_profit: float
    entry_idx: int
    entry_ts: pd.Timestamp
    margin: float = 0.0     # capital bloqueado (notional / leverage)
    entry_taker: bool = True   # cómo entró (afecta el fee de entrada)


@dataclass
class PendingEntry:
    """Orden maker (post-only) esperando fill: limit al cierre de la señal."""
    decision: PriceActionDecision
    limit_price: float
    place_idx: int
    place_ts: pd.Timestamp


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


# ─────────────────────────────────────────
#  Fetch histórico
# ─────────────────────────────────────────

def fetch_history(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    minutes_per_bar = {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                       "1h": 60, "4h": 240, "1d": 1440}[timeframe]
    total_bars = (days * 24 * 60) // minutes_per_bar
    end_ms = ex.milliseconds()
    start_ms = end_ms - (total_bars * minutes_per_bar * 60_000)

    all_rows: list = []
    chunk_limit = 1000
    cursor = start_ms
    while cursor < end_ms:
        rows = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=chunk_limit)
        if not rows:
            break
        all_rows.extend(rows)
        cursor = rows[-1][0] + minutes_per_bar * 60_000
        if len(rows) < chunk_limit:
            break
    df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


# ─────────────────────────────────────────
#  Simulador
# ─────────────────────────────────────────

class Sim:
    def __init__(self, settings, leverage: float = 1.0, liquidate_at_pct: float = 0.95,
                 taker_fee: Optional[float] = None, maker_fee: Optional[float] = None,
                 slippage: float = 0.0, entry_mode: str = "taker",
                 maker_timeout_bars: int = 2):
        self.s = settings
        self.capital = settings.initial_capital
        self.leverage = leverage              # 1.0 = Spot, >1 = Futures con apalancamiento
        self.liquidate_at_pct = liquidate_at_pct
        # Fees: si no se especifican, usan commission_pct_per_side (baseline).
        base_fee = settings.commission_pct_per_side
        self.taker_fee = base_fee if taker_fee is None else taker_fee
        self.maker_fee = base_fee if maker_fee is None else maker_fee
        self.slippage = slippage
        self.entry_mode = entry_mode          # "taker" (market) | "maker" (post-only limit)
        self.maker_timeout_bars = maker_timeout_bars

        self.position: Optional[SimPos] = None
        self.pending: Optional[PendingEntry] = None
        self.missed = 0                        # entradas maker que no se llenaron
        self.trades: list[TradeRec] = []
        self.peak = self.capital
        self.max_dd = 0.0

    def _position_size_usdt(self, sl_pct: float) -> float:
        max_risk = self.capital * self.s.max_risk_per_trade
        position = max_risk / sl_pct if sl_pct > 0 else max_risk * 10
        tradable_cap = self.capital * (1 - self.s.trade_reserve_pct) * self.leverage
        return min(position, tradable_cap)

    def _open_at(self, decision: PriceActionDecision, entry_price: float,
                 idx: int, ts, entry_taker: bool):
        sl_pct = decision.stop_loss_pct
        size = self._position_size_usdt(sl_pct)
        if size <= 0:
            return
        amt = size / entry_price
        margin = size / self.leverage
        if margin > self.capital * (1 - self.s.trade_reserve_pct):
            margin = self.capital * (1 - self.s.trade_reserve_pct)
            size = margin * self.leverage
            amt = size / entry_price
        self.position = SimPos(
            direction=decision.direction,
            entry_price=entry_price,
            amount_btc=amt,
            amount_usdt=size,
            stop_loss=decision.stop_loss_price,
            take_profit=decision.take_profit_price,
            entry_idx=idx,
            entry_ts=ts,
            margin=margin,
            entry_taker=entry_taker,
        )
        self.capital -= margin

    def signal(self, decision: PriceActionDecision, idx: int, ts):
        """Procesa una señal del engine según el modo de entrada."""
        if self.entry_mode == "maker":
            # Post-only: dejamos un limit al precio de cierre; se llena en velas
            # siguientes solo si el precio lo toca (si no, se descarta → missed).
            self.pending = PendingEntry(
                decision=decision, limit_price=decision.entry_price,
                place_idx=idx, place_ts=ts,
            )
        else:
            # Taker: entrada a mercado YA, con slippage en contra.
            entry = decision.entry_price
            if decision.direction == "LONG":
                entry *= (1 + self.slippage)
            else:
                entry *= (1 - self.slippage)
            self._open_at(decision, entry, idx, ts, entry_taker=True)

    def _try_fill_pending(self, bar: pd.Series, idx: int, ts):
        """Intenta llenar la orden maker pendiente sobre la vela actual."""
        if self.pending is None:
            return
        pe = self.pending
        # Timeout: si pasaron demasiadas velas sin tocar el limit, se cancela.
        if idx - pe.place_idx > self.maker_timeout_bars:
            self.missed += 1
            self.pending = None
            return
        high = float(bar["high"])
        low = float(bar["low"])
        limit = pe.limit_price
        filled = (low <= limit) if pe.decision.direction == "LONG" else (high >= limit)
        if filled:
            # Maker: entra exactamente al limit, sin slippage.
            self._open_at(pe.decision, limit, idx, ts, entry_taker=False)
            self.pending = None

    def close(self, exit_price: float, reason: str, idx: int, ts):
        if self.position is None:
            return
        p = self.position
        if p.direction == "LONG":
            pnl_gross = (exit_price - p.entry_price) * p.amount_btc
        else:
            pnl_gross = (p.entry_price - exit_price) * p.amount_btc

        entry_value = p.amount_usdt
        exit_value = p.amount_btc * exit_price
        # Fee por lado según cómo fue cada pata:
        #   entrada: taker o maker según entry_taker.
        #   salida: maker si fue Take-profit (limit), taker en SL/liq/fin.
        entry_fee_pct = self.taker_fee if p.entry_taker else self.maker_fee
        exit_fee_pct = self.maker_fee if reason.startswith("Take-profit") else self.taker_fee
        fee = entry_value * entry_fee_pct + exit_value * exit_fee_pct
        pnl = pnl_gross - fee

        self.capital += p.margin + pnl
        if self.capital > self.peak:
            self.peak = self.capital
        dd = (self.peak - self.capital) / self.peak * 100
        if dd > self.max_dd:
            self.max_dd = dd
        self.trades.append(TradeRec(
            direction=p.direction, entry_ts=p.entry_ts, exit_ts=ts,
            entry_price=p.entry_price, exit_price=exit_price,
            pnl_usdt=pnl, pnl_gross=pnl_gross, fee_paid=fee,
            bars_held=idx - p.entry_idx, exit_reason=reason,
        ))
        self.position = None

    def step(self, bar: pd.Series, idx: int, ts) -> None:
        """Procesa SL/TP/liquidación de la posición abierta sobre la vela actual."""
        if self.position is None:
            return
        p = self.position
        high = float(bar["high"])
        low = float(bar["low"])
        slip = self.slippage

        if self.leverage > 1.0:
            if p.direction == "LONG":
                liq_price = p.entry_price - (p.margin * self.liquidate_at_pct) / p.amount_btc
                if low <= liq_price:
                    self.close(liq_price, "LIQUIDACIÓN", idx, ts)
                    return
            else:
                liq_price = p.entry_price + (p.margin * self.liquidate_at_pct) / p.amount_btc
                if high >= liq_price:
                    self.close(liq_price, "LIQUIDACIÓN", idx, ts)
                    return

        if p.direction == "LONG":
            if low <= p.stop_loss:
                # SL es STOP_MARKET (taker) → slippage en contra.
                self.close(p.stop_loss * (1 - slip), "Stop-loss", idx, ts)
            elif high >= p.take_profit:
                # TP es LIMIT (maker) → sin slippage.
                self.close(p.take_profit, "Take-profit", idx, ts)
        else:
            if high >= p.stop_loss:
                self.close(p.stop_loss * (1 + slip), "Stop-loss", idx, ts)
            elif low <= p.take_profit:
                self.close(p.take_profit, "Take-profit", idx, ts)


# ─────────────────────────────────────────
#  Reporte
# ─────────────────────────────────────────

def report(sim: Sim, settings, days: int):
    n = len(sim.trades)
    final = settings.initial_capital + sum(t.pnl_usdt for t in sim.trades)
    ret_pct = (final - settings.initial_capital) / settings.initial_capital * 100
    if n == 0:
        print("\n=== BACKTEST PA — sin operaciones ===")
        print(f"Capital final: ${final:,.2f}  ·  Entradas maker no llenadas: {sim.missed}")
        return
    wins = [t for t in sim.trades if t.pnl_usdt > 0]
    losses = [t for t in sim.trades if t.pnl_usdt <= 0]
    wr = len(wins) / n * 100
    avg_w = sum(t.pnl_usdt for t in wins) / len(wins) if wins else 0
    avg_l = sum(t.pnl_usdt for t in losses) / len(losses) if losses else 0
    pf = (sum(t.pnl_gross for t in wins) /
          abs(sum(t.pnl_gross for t in losses))
          if losses and sum(t.pnl_gross for t in losses) != 0 else float("inf"))
    pf_net = (sum(t.pnl_usdt for t in wins) /
              abs(sum(t.pnl_usdt for t in losses))
              if losses and sum(t.pnl_usdt for t in losses) != 0 else float("inf"))
    total_fees = sum(t.fee_paid for t in sim.trades)
    gross_wins = sum(t.pnl_gross for t in sim.trades if t.pnl_gross > 0)
    gross_losses = sum(t.pnl_gross for t in sim.trades if t.pnl_gross <= 0)

    by_reason = {}
    for t in sim.trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1

    print("\n" + "=" * 60)
    print(f"  BACKTEST PRICE ACTION — {days} días @ TF 1h (4h structure)")
    print("=" * 60)
    print(f"Ejecución:          entry={sim.entry_mode} taker={sim.taker_fee:.4%} "
          f"maker={sim.maker_fee:.4%} slip={sim.slippage:.4%}")
    print(f"Capital inicial:    ${settings.initial_capital:>10,.2f}")
    print(f"Capital final:      ${final:>10,.2f}  ({ret_pct:+.2f}%)")
    print(f"Max drawdown:       {sim.max_dd:>10.2f}%")
    print()
    longs = [t for t in sim.trades if t.direction == "LONG"]
    shorts = [t for t in sim.trades if t.direction == "SHORT"]
    print(f"Operaciones:        {n:>10}  ({len(longs)} LONG, {len(shorts)} SHORT)")
    if sim.entry_mode == "maker":
        print(f"Entradas no llenadas (maker): {sim.missed}")
    print(f"Acertamos:          {wr:>10.1f}%  ({len(wins)} ganadoras, {len(losses)} perdedoras)")
    print()
    print("--- BRUTO ---")
    print(f"Ganancia bruta:     ${gross_wins:>+10,.2f}")
    print(f"Pérdida bruta:      ${gross_losses:>+10,.2f}")
    print(f"Profit Factor BRUTO:{pf:>10.2f}")
    print()
    print("--- NETO (con fees) ---")
    print(f"Total fees:         ${total_fees:>+10,.2f}")
    print(f"% capital en fees:  {total_fees / settings.initial_capital * 100:>10.2f}%")
    print(f"PnL neto:           ${final - settings.initial_capital:>+10,.2f}")
    print(f"Profit Factor NETO: {pf_net:>10.2f}")
    print(f"Ganancia promedio:  ${avg_w:>+10,.2f}")
    print(f"Pérdida promedio:   ${avg_l:>+10,.2f}")
    print()
    print("Cómo cerraron:")
    for r, c in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"  • {r}: {c}")
    print("=" * 60)


# ─────────────────────────────────────────
#  Main
# ─────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Backtest Price Action")
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--symbol", type=str, default=None)
    p.add_argument("--vol-mult", type=float, default=None, help="Override pa_vol_mult")
    p.add_argument("--atr-mult", type=float, default=None, help="Override pa_atr_sl_mult")
    p.add_argument("--rr", type=float, default=None, help="Override pa_tp_rr")
    p.add_argument("--fractal", type=int, default=None, help="Override pa_fractal_n")
    p.add_argument("--risk-pct", type=float, default=None, help="Override max_risk_per_trade (ej. 0.07 = 7%)")
    p.add_argument("--leverage", type=float, default=1.0, help="Apalancamiento (1=Spot, 5=Futures 5x)")
    p.add_argument("--commission", type=float, default=None, help="Fee base por lado si no se dan taker/maker (ej. 0.0005)")
    p.add_argument("--reserve-pct", type=float, default=None, help="Override trade_reserve_pct")
    # Modelo de ejecución realista (opt-in; defaults = baseline)
    p.add_argument("--entry-mode", choices=["taker", "maker"], default="taker",
                   help="taker=market inmediato | maker=post-only limit (puede no llenarse)")
    p.add_argument("--taker-fee", type=float, default=None, help="Fee taker por lado (market/stop)")
    p.add_argument("--maker-fee", type=float, default=None, help="Fee maker por lado (limit/TP)")
    p.add_argument("--slippage", type=float, default=0.0, help="Slippage en órdenes taker (ej. 0.0003)")
    p.add_argument("--maker-timeout", type=int, default=2, help="Velas que espera el limit maker antes de cancelar")
    args = p.parse_args()

    s = load_settings()
    if args.symbol:
        s.symbol = args.symbol
    if args.vol_mult is not None:
        setattr(s, "pa_vol_mult", args.vol_mult)
    if args.atr_mult is not None:
        setattr(s, "pa_atr_sl_mult", args.atr_mult)
    if args.rr is not None:
        setattr(s, "pa_tp_rr", args.rr)
    if args.fractal is not None:
        setattr(s, "pa_fractal_n", args.fractal)
    if args.risk_pct is not None:
        s.max_risk_per_trade = args.risk_pct
    if args.commission is not None:
        s.commission_pct_per_side = args.commission
    if args.reserve_pct is not None:
        s.trade_reserve_pct = args.reserve_pct

    print(f"Bajando {args.days} días de {s.symbol} (1h y 4h)...")
    df_1h = fetch_history(s.symbol, "1h", args.days + 5)
    df_4h = fetch_history(s.symbol, "4h", args.days + 30)
    print(f"  1h: {len(df_1h)} velas | 4h: {len(df_4h)} velas")

    engine = PriceActionEngine(s)
    sim = Sim(s, leverage=args.leverage, taker_fee=args.taker_fee,
              maker_fee=args.maker_fee, slippage=args.slippage,
              entry_mode=args.entry_mode, maker_timeout_bars=args.maker_timeout)
    print(f"  risk_pct={s.max_risk_per_trade:.2%}  leverage={args.leverage}x  "
          f"entry={sim.entry_mode}  taker={sim.taker_fee:.4%}  maker={sim.maker_fee:.4%}  "
          f"slip={sim.slippage:.4%}  reserve={s.trade_reserve_pct:.0%}")

    warmup = 60
    for i in range(warmup, len(df_1h)):
        ts = df_1h.index[i]
        # 1) SL/TP/liq de la posición abierta.
        sim.step(df_1h.iloc[i], i, ts)
        # 2) Intentar llenar una orden maker pendiente (si hay y no hay posición).
        if sim.position is None:
            sim._try_fill_pending(df_1h.iloc[i], i, ts)
        # 3) Si no hay posición ni pendiente, evaluar nueva señal.
        if sim.position is None and sim.pending is None:
            df_1h_so_far = df_1h.iloc[: i + 1]
            df_4h_so_far = df_4h.loc[: ts]
            if len(df_4h_so_far) < 30:
                continue
            decision = engine.analyze(df_1h_so_far, df_4h_so_far)
            if decision.accion in ("COMPRAR", "VENDER"):
                sim.signal(decision, i, ts)

    if sim.position is not None:
        last_close = float(df_1h.iloc[-1]["close"])
        sim.close(last_close, "Fin del backtest", len(df_1h) - 1, df_1h.index[-1])

    report(sim, s, args.days)


if __name__ == "__main__":
    main()
