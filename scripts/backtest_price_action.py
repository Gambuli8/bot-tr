"""
scripts/backtest_price_action.py
Backtest puro del PriceActionEngine sobre TF 1h con contexto 4h.
- Itera cada vela 1h cerrada, llama analyze(df_1h_so_far, df_4h_so_far).
- Si dispara señal, simula la posición con SL/TP absolutos.
- Comisión: settings.commission_pct_per_side por lado.
- Riesgo: max_risk_per_trade del capital (no Kelly por ahora).

Uso:
    python scripts/backtest_price_action.py --days 120
    python scripts/backtest_price_action.py --days 120 --vol-mult 1.3 --rr 3.0
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
    def __init__(self, settings, leverage: float = 1.0, liquidate_at_pct: float = 0.95):
        self.s = settings
        self.capital = settings.initial_capital
        self.leverage = leverage              # 1.0 = Spot, >1 = Futures con apalancamiento
        # Cuando con leverage el unrealized loss alcanza este % del margen, te liquidan.
        # Binance ~99% por defecto; uso 95% conservador para mantenernos lejos del bord.
        self.liquidate_at_pct = liquidate_at_pct
        self.position: Optional[SimPos] = None
        self.trades: list[TradeRec] = []
        self.peak = self.capital
        self.max_dd = 0.0

    def _position_size_usdt(self, sl_pct: float) -> float:
        # max_risk_per_trade ahora se interpreta como % del capital nominal (no del notional).
        max_risk = self.capital * self.s.max_risk_per_trade
        position = max_risk / sl_pct if sl_pct > 0 else max_risk * 10
        # Capital "tradable" amplificado por leverage
        tradable_cap = self.capital * (1 - self.s.trade_reserve_pct) * self.leverage
        return min(position, tradable_cap)

    def open(self, decision: PriceActionDecision, idx: int, ts):
        sl_pct = decision.stop_loss_pct
        size = self._position_size_usdt(sl_pct)
        if size <= 0:
            return
        entry = decision.entry_price
        amt = size / entry
        # Con leverage, sólo bloqueamos el margen (size / leverage), no toda la position.
        margin = size / self.leverage
        # Sanity: el margen no puede superar el capital disponible
        if margin > self.capital * (1 - self.s.trade_reserve_pct):
            margin = self.capital * (1 - self.s.trade_reserve_pct)
            size = margin * self.leverage
            amt = size / entry
        self.position = SimPos(
            direction=decision.direction,
            entry_price=entry,
            amount_btc=amt,
            amount_usdt=size,         # notional (la "posición" real)
            stop_loss=decision.stop_loss_price,
            take_profit=decision.take_profit_price,
            entry_idx=idx,
            entry_ts=ts,
        )
        # Sólo bloqueamos margen del capital nominal
        self.capital -= margin
        # Guardamos el margen en la posición para devolverlo al cerrar
        self.position.margin = margin

    def close(self, exit_price: float, reason: str, idx: int, ts):
        if self.position is None:
            return
        p = self.position
        if p.direction == "LONG":
            pnl_gross = (exit_price - p.entry_price) * p.amount_btc
        else:
            pnl_gross = (p.entry_price - exit_price) * p.amount_btc
        entry_value = p.amount_usdt   # notional, no margin
        exit_value = p.amount_btc * exit_price
        fee = (entry_value + exit_value) * self.s.commission_pct_per_side
        pnl = pnl_gross - fee
        # Devolvemos el margen + el PnL (ganado o perdido)
        self.capital += p.margin + pnl
        # Liquidación: si el PnL en cierto momento supera el margen, sería liquidación.
        # Acá lo detectamos ex-post (no afecta este trade, sino el max_dd).
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

    def step(self, bar: pd.Series, idx: int, ts) -> None:
        """Procesa SL/TP/liquidación de la posición abierta sobre la vela actual."""
        if self.position is None:
            return
        p = self.position
        high = float(bar["high"])
        low = float(bar["low"])

        # Liquidation price: el precio al que la pérdida unrealized == margen.
        # PnL = (current - entry) * amt_btc (LONG); == -margin ⇒ liquidación.
        # Con leverage 5x y position 5x del margen, una caída del 20% liquida.
        if self.leverage > 1.0:
            if p.direction == "LONG":
                liq_price = p.entry_price - (p.margin * self.liquidate_at_pct) / p.amount_btc
                if low <= liq_price:
                    # Liquidación intra-vela ANTES del SL
                    self.close(liq_price, "LIQUIDACIÓN", idx, ts)
                    return
            else:
                liq_price = p.entry_price + (p.margin * self.liquidate_at_pct) / p.amount_btc
                if high >= liq_price:
                    self.close(liq_price, "LIQUIDACIÓN", idx, ts)
                    return

        if p.direction == "LONG":
            if low <= p.stop_loss:
                self.close(p.stop_loss, "Stop-loss", idx, ts)
            elif high >= p.take_profit:
                self.close(p.take_profit, "Take-profit", idx, ts)
        else:
            if high >= p.stop_loss:
                self.close(p.stop_loss, "Stop-loss", idx, ts)
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
        print(f"Capital final: ${final:,.2f}")
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
    print(f"Capital inicial:    ${settings.initial_capital:>10,.2f}")
    print(f"Capital final:      ${final:>10,.2f}  ({ret_pct:+.2f}%)")
    print(f"Max drawdown:       {sim.max_dd:>10.2f}%")
    print()
    longs = [t for t in sim.trades if t.direction == "LONG"]
    shorts = [t for t in sim.trades if t.direction == "SHORT"]
    print(f"Operaciones:        {n:>10}  ({len(longs)} LONG, {len(shorts)} SHORT)")
    print(f"Operaciones/semana: {n / max(days/7, 1):>10.2f}")
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
    print()
    if n > 0:
        print("Últimas 5 operaciones:")
        for t in sim.trades[-5:]:
            emoji = "🟢" if t.pnl_usdt > 0 else "🔴"
            print(f"  {emoji} {t.direction} {t.entry_ts.date()} | "
                  f"${t.entry_price:,.2f} → ${t.exit_price:,.2f} "
                  f"| {t.pnl_usdt:+.2f} | {t.exit_reason}")
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
    p.add_argument("--commission", type=float, default=None, help="Override commission_pct_per_side (ej. 0.0004 = Futures taker)")
    p.add_argument("--reserve-pct", type=float, default=None, help="Override trade_reserve_pct")
    p.add_argument("--trigger", type=str, default=None, choices=["sweep", "choch"],
                   help="Gatillo del PA engine: sweep (default) | choch (webinar filtrado)")
    p.add_argument("--choch-vol", action="store_true",
                   help="Si --trigger choch: exigir confirmación de volumen en el quiebre")
    args = p.parse_args()

    s = load_settings()
    if args.symbol:
        s.symbol = args.symbol
    # Inyectar overrides al settings (los lee el engine via getattr)
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
    if args.trigger is not None:
        setattr(s, "pa_trigger_mode", args.trigger)
    if args.choch_vol:
        setattr(s, "pa_choch_require_vol", True)

    print(f"Bajando {args.days} días de {s.symbol} (1h y 4h)...")
    df_1h = fetch_history(s.symbol, "1h", args.days + 5)
    df_4h = fetch_history(s.symbol, "4h", args.days + 30)
    print(f"  1h: {len(df_1h)} velas | 4h: {len(df_4h)} velas")

    engine = PriceActionEngine(s)
    sim = Sim(s, leverage=args.leverage)
    print(f"  trigger={getattr(s, 'pa_trigger_mode', 'sweep')}  "
          f"risk_pct={s.max_risk_per_trade:.2%}  leverage={args.leverage}x  "
          f"fee={s.commission_pct_per_side:.4%}  reserve={s.trade_reserve_pct:.0%}")

    # Iterar cada vela 1h. Por vela:
    #   1. Procesar SL/TP de la posición abierta sobre la vela actual.
    #   2. Si no hay posición, evaluar señal con df_1h hasta esa vela + df_4h equivalente.
    warmup = 60
    for i in range(warmup, len(df_1h)):
        ts = df_1h.index[i]
        # Sim de SL/TP usando la vela actual
        sim.step(df_1h.iloc[i], i, ts)
        # Si no hay posición, intentar abrir con info histórica hasta el cierre de la vela anterior
        # (modelo realista: decidimos al cierre de la vela y la próxima vela ya puede tocar SL/TP)
        if sim.position is None:
            df_1h_so_far = df_1h.iloc[: i + 1]
            df_4h_so_far = df_4h.loc[: ts]
            if len(df_4h_so_far) < 30:
                continue
            decision = engine.analyze(df_1h_so_far, df_4h_so_far)
            if decision.accion in ("COMPRAR", "VENDER"):
                # Abrir en el cierre de la vela actual (entry_price ya viene del engine)
                sim.open(decision, i, ts)

    if sim.position is not None:
        last_close = float(df_1h.iloc[-1]["close"])
        sim.close(last_close, "Fin del backtest", len(df_1h) - 1, df_1h.index[-1])

    report(sim, s, args.days)


if __name__ == "__main__":
    main()
