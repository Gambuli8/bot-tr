"""
scripts/backtest_donchian.py
Backtest del DonchianEngine (breakout Donchian 15m + volumen + ADX>=20)
sobre OHLCV histórico de Binance Futures MAINNET (misma fuente que el bot).

Modelo de ejecución ESTRICTO (anti falsos positivos):
- Fees taker (default 0.05% por lado) en TODA ejecución a mercado
  (entrada, SL, TP y cierre por señal opuesta).
- Slippage adverso (default 2 bps por fill) sobre el precio de ejecución.
- Las señales se evalúan al CLOSE de la vela i pero se ejecutan al OPEN
  de la vela i+1 (nunca al mismo close que las generó).
- Cierre por señal opuesta: el cierre del trade sale a mercado en la vela
  de la señal; la apertura inversa queda PENDIENTE y se ejecuta recién en
  la siguiente iteración del loop (separación estricta de concerns, igual
  que el bot en vivo).
- Intrabar: si SL y TP caen en la misma vela, se asume el peor caso (SL).

Uso:
    python scripts/backtest_donchian.py                     # 90 días BTC/USDT 15m
    python scripts/backtest_donchian.py --days 180
    python scripts/backtest_donchian.py --period 20 --adx-min 20 --sl-mode tighter
    python scripts/backtest_donchian.py --fee-bps 5 --slippage-bps 2
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass

import ccxt
import pandas as pd

from config.settings import load_settings, Settings
from core.donchian_engine import DonchianEngine, DonchianDecision


def settings_or_defaults() -> Settings:
    """
    El backtest sólo usa datos PÚBLICOS (OHLCV) — no necesita API keys.
    Si no hay .env completo, armamos un Settings con secretos dummy.
    """
    try:
        return load_settings()
    except Exception:
        return Settings(
            binance_api_key="backtest_only",
            binance_api_secret="backtest_only",
            anthropic_api_key="backtest_only",
            telegram_bot_token="backtest_only",
            telegram_chat_id="0",
        )


# ───────────────────────── modelos ─────────────────────────

class EngineLike(Protocol):
    """Cualquier motor con analyze(df, idx) → decisión estilo DonchianDecision."""
    def analyze(self, df: pd.DataFrame, current_idx: int) -> DonchianDecision: ...


@dataclass(frozen=True)
class ExecutionModel:
    """Fricción de mercado aplicada a cada fill a mercado (taker)."""
    taker_fee_pct: float = 0.0005      # 0.05% Binance Futures USDT-M taker
    slippage_pct: float = 0.0002       # 2 bps de penalización adversa por fill

    def fill_price(self, ideal: float, side: str) -> float:
        """Precio ejecutado: siempre PEOR que el ideal. side: 'buy'|'sell'."""
        if side == "buy":
            return ideal * (1 + self.slippage_pct)
        return ideal * (1 - self.slippage_pct)

    def fee(self, notional_usdt: float) -> float:
        return abs(notional_usdt) * self.taker_fee_pct


@dataclass
class OpenPosition:
    direction: str                 # "LONG" | "SHORT"
    entry_price: float             # precio YA con slippage
    amount_base: float             # BTC
    margin_usdt: float             # capital reservado (notional)
    stop_loss: float
    take_profit: float
    entry_idx: int
    entry_fee: float
    entry_slip_cost: float
    entry_reason: str


@dataclass
class ClosedTrade:
    direction: str
    entry_price: float
    exit_price: float
    amount_base: float
    pnl_gross: float               # sin fees (pero con slippage en los precios)
    fees: float                    # entrada + salida
    pnl_net: float
    pnl_pct: float                 # neto, sobre el margen
    slippage_cost: float           # costo total de slippage vs precios ideales
    bars_held: int
    entry_reason: str
    exit_reason: str


@dataclass
class PendingEntry:
    """Señal generada al close de una vela, a ejecutar al open de la siguiente."""
    direction: str
    sl_pct: float
    tp_pct: float
    reason: str
    signal_idx: int


@dataclass
class BacktestResult:
    trades: list[ClosedTrade]
    equity_curve: list[tuple[pd.Timestamp, float]]
    initial_capital: float
    final_equity: float
    max_drawdown_pct: float

    # ── métricas institucionales ──

    @property
    def net_return_pct(self) -> float:
        return (self.final_equity - self.initial_capital) / self.initial_capital * 100

    @property
    def win_rate_pct(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl_net > 0)
        return wins / len(self.trades) * 100

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pnl_net for t in self.trades if t.pnl_net > 0)
        gross_loss = abs(sum(t.pnl_net for t in self.trades if t.pnl_net <= 0))
        if gross_loss == 0:
            return float("inf") if gross_win > 0 else 0.0
        return gross_win / gross_loss

    @property
    def total_fees(self) -> float:
        return sum(t.fees for t in self.trades)

    @property
    def total_slippage(self) -> float:
        return sum(t.slippage_cost for t in self.trades)


# ───────────────────────── simulador ─────────────────────────

class DonchianBacktester:
    """
    Loop histórico vela a vela. Orden estricto por iteración:
      1) Ejecutar la entrada PENDIENTE (señal de la vela anterior) al open.
      2) Evaluar salidas intrabar de la posición viva (SL primero, peor caso).
      3) Evaluar la señal al close:
         - sin posición → queda pendiente para la próxima vela;
         - posición contraria → cierre a mercado YA, apertura inversa pendiente.
      4) Marcar equity (mark-to-market al close).
    """

    def __init__(
        self,
        settings: Settings,
        engine: EngineLike,
        exec_model: ExecutionModel,
        min_confidence: Optional[float] = None,
    ):
        self.s = settings
        self.engine = engine
        self.x = exec_model
        self.min_confidence = (
            settings.min_claude_confidence if min_confidence is None else min_confidence
        )
        self.capital: float = settings.initial_capital
        self.position: Optional[OpenPosition] = None
        self.pending: Optional[PendingEntry] = None
        self.trades: list[ClosedTrade] = []
        self.equity_curve: list[tuple[pd.Timestamp, float]] = []
        self._peak: float = self.capital
        self._max_dd: float = 0.0

    # ── sizing ──

    def _position_size_usdt(self, sl_pct: float) -> float:
        """Riesgo fijo: (capital × max_risk_per_trade) / SL%, capeado por la reserva."""
        max_risk = self.capital * self.s.max_risk_per_trade
        size = max_risk / sl_pct if sl_pct > 0 else 0.0
        cap = self.capital * (1 - self.s.trade_reserve_pct)
        return max(0.0, min(size, cap))

    # ── ejecución ──

    def _execute_entry(self, pend: PendingEntry, open_price: float, idx: int) -> None:
        size_usdt = self._position_size_usdt(pend.sl_pct)
        if size_usdt <= 0:
            return
        side = "buy" if pend.direction == "LONG" else "sell"
        fill = self.x.fill_price(open_price, side)
        amount = size_usdt / fill
        fee = self.x.fee(size_usdt)
        slip_cost = abs(fill - open_price) * amount

        if pend.direction == "LONG":
            sl = fill * (1 - pend.sl_pct)
            tp = fill * (1 + pend.tp_pct)
        else:
            sl = fill * (1 + pend.sl_pct)
            tp = fill * (1 - pend.tp_pct)

        self.capital -= size_usdt + fee
        self.position = OpenPosition(
            direction=pend.direction,
            entry_price=fill,
            amount_base=amount,
            margin_usdt=size_usdt,
            stop_loss=sl,
            take_profit=tp,
            entry_idx=idx,
            entry_fee=fee,
            entry_slip_cost=slip_cost,
            entry_reason=pend.reason,
        )

    def _execute_close(self, ideal_price: float, reason: str, idx: int) -> None:
        p = self.position
        if p is None:
            return
        side = "sell" if p.direction == "LONG" else "buy"
        fill = self.x.fill_price(ideal_price, side)
        exit_notional = p.amount_base * fill
        exit_fee = self.x.fee(exit_notional)
        exit_slip = abs(fill - ideal_price) * p.amount_base

        if p.direction == "LONG":
            pnl_gross = (fill - p.entry_price) * p.amount_base
        else:
            pnl_gross = (p.entry_price - fill) * p.amount_base

        fees = p.entry_fee + exit_fee
        pnl_net = pnl_gross - fees
        self.capital += p.margin_usdt + pnl_gross - exit_fee  # entry_fee ya se descontó al abrir

        self.trades.append(ClosedTrade(
            direction=p.direction,
            entry_price=p.entry_price,
            exit_price=fill,
            amount_base=p.amount_base,
            pnl_gross=pnl_gross,
            fees=fees,
            pnl_net=pnl_net,
            pnl_pct=(pnl_net / p.margin_usdt * 100) if p.margin_usdt else 0.0,
            slippage_cost=p.entry_slip_cost + exit_slip,
            bars_held=idx - p.entry_idx,
            entry_reason=p.entry_reason,
            exit_reason=reason,
        ))
        self.position = None

    def _check_intrabar_exits(self, bar: pd.Series, idx: int) -> None:
        """SL/TP con high/low de la vela. Si ambos tocan, gana el SL (peor caso)."""
        p = self.position
        if p is None:
            return
        high, low = float(bar["high"]), float(bar["low"])
        if p.direction == "LONG":
            if low <= p.stop_loss:
                self._execute_close(p.stop_loss, "Stop-loss", idx)
            elif high >= p.take_profit:
                self._execute_close(p.take_profit, "Take-profit", idx)
        else:
            if high >= p.stop_loss:
                self._execute_close(p.stop_loss, "Stop-loss", idx)
            elif low <= p.take_profit:
                self._execute_close(p.take_profit, "Take-profit", idx)

    # ── loop principal ──

    def run(self, df: pd.DataFrame, warmup: int) -> BacktestResult:
        for i in range(warmup, len(df)):
            bar = df.iloc[i]
            ts = df.index[i]

            # 1) Entrada pendiente de la vela anterior → se ejecuta al open de ESTA.
            if self.pending is not None and self.position is None:
                self._execute_entry(self.pending, float(bar["open"]), i)
                self.pending = None

            # 2) Salidas intrabar (SL prioritario).
            self._check_intrabar_exits(bar, i)

            # 3) Señal al close de la vela i.
            decision = self.engine.analyze(df, i)
            is_signal = (
                decision.accion in ("COMPRAR", "VENDER")
                and decision.confianza >= self.min_confidence
            )
            if is_signal:
                if self.position is None:
                    if self.pending is None:
                        self.pending = PendingEntry(
                            direction=decision.direction,
                            sl_pct=decision.stop_loss_pct,
                            tp_pct=decision.take_profit_pct,
                            reason=decision.razon,
                            signal_idx=i,
                        )
                elif self.position.direction != decision.direction:
                    # Señal opuesta fuerte: PRIMERO cierre total a mercado.
                    # La apertura inversa queda para la PRÓXIMA iteración.
                    self._execute_close(
                        float(bar["close"]),
                        f"Señal opuesta ({decision.direction})",
                        i,
                    )
                    self.pending = PendingEntry(
                        direction=decision.direction,
                        sl_pct=decision.stop_loss_pct,
                        tp_pct=decision.take_profit_pct,
                        reason=decision.razon,
                        signal_idx=i,
                    )
                # Señal en la misma dirección con posición abierta: se ignora.

            # 4) Equity mark-to-market al close.
            close = float(bar["close"])
            if self.position is not None:
                p = self.position
                upnl = (
                    (close - p.entry_price) * p.amount_base
                    if p.direction == "LONG"
                    else (p.entry_price - close) * p.amount_base
                )
                equity = self.capital + p.margin_usdt + upnl
            else:
                equity = self.capital
            self.equity_curve.append((ts, equity))
            if equity > self._peak:
                self._peak = equity
            dd = (self._peak - equity) / self._peak * 100 if self._peak > 0 else 0.0
            if dd > self._max_dd:
                self._max_dd = dd

        # Cierre forzado al final del histórico (mark-out honesto).
        if self.position is not None:
            self._execute_close(
                float(df.iloc[-1]["close"]), "Fin del backtest", len(df) - 1
            )

        final_equity = self.equity_curve[-1][1] if self.equity_curve else self.capital
        # El cierre forzado puede haber movido el capital después del último mark.
        if self.equity_curve and not self.position:
            final_equity = self.capital
        return BacktestResult(
            trades=self.trades,
            equity_curve=self.equity_curve,
            initial_capital=self.s.initial_capital,
            final_equity=final_equity,
            max_drawdown_pct=self._max_dd,
        )


# ───────────────────────── datos ─────────────────────────

def fetch_history(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """Velas de Binance Futures USDT-M MAINNET (misma fuente de datos que el bot en vivo)."""
    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    minutes_per_bar = {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                       "1h": 60, "4h": 240, "1d": 1440}[timeframe]
    total_bars = (days * 24 * 60) // minutes_per_bar
    end_ms = ex.milliseconds()
    cursor = end_ms - total_bars * minutes_per_bar * 60_000

    rows: list[list] = []
    while cursor < end_ms:
        chunk = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
        if not chunk:
            break
        rows.extend(chunk)
        cursor = chunk[-1][0] + minutes_per_bar * 60_000
        if len(chunk) < 1000:
            break

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    df = df.dropna()
    return df[df["close"] > 0]


# ───────────────────────── reporte ─────────────────────────

def report(res: BacktestResult, exec_model: ExecutionModel, settings: Settings) -> None:
    print("\n" + "=" * 64)
    print("  BACKTEST DONCHIAN — resultados netos (fees + slippage)")
    print("=" * 64)
    print(f"Fricción aplicada:  taker {exec_model.taker_fee_pct:.4%}/lado | "
          f"slippage {exec_model.slippage_pct:.4%}/fill")
    print(f"Capital inicial:    ${res.initial_capital:,.2f}")
    print(f"Capital final:      ${res.final_equity:,.2f}")
    print(f"Retorno neto:       {res.net_return_pct:+.2f}%")
    print(f"Max drawdown:       {res.max_drawdown_pct:.2f}%")

    n = len(res.trades)
    if n == 0:
        print("\nSin operaciones — nada que reportar.")
        print("=" * 64)
        return

    wins = [t for t in res.trades if t.pnl_net > 0]
    losses = [t for t in res.trades if t.pnl_net <= 0]
    longs = [t for t in res.trades if t.direction == "LONG"]
    shorts = [t for t in res.trades if t.direction == "SHORT"]
    avg_win = sum(t.pnl_net for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl_net for t in losses) / len(losses) if losses else 0.0
    expectancy = sum(t.pnl_net for t in res.trades) / n
    pf = res.profit_factor

    print()
    print(f"Operaciones:        {n}  ({len(longs)} LONG / {len(shorts)} SHORT)")
    print(f"Win rate:           {res.win_rate_pct:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"Profit factor:      {'∞' if pf == float('inf') else f'{pf:.2f}'}")
    print(f"Ganancia promedio:  ${avg_win:+.2f}")
    print(f"Pérdida promedio:   ${avg_loss:+.2f}")
    print(f"Expectancy/trade:   ${expectancy:+.2f}")
    print(f"Fees totales:       ${res.total_fees:,.2f}")
    print(f"Slippage total:     ${res.total_slippage:,.2f}")

    by_reason: dict[str, int] = {}
    for t in res.trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    print("\nCierres por motivo:")
    for r, c in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"  • {r}: {c}")

    print("\nÚltimas 5 operaciones:")
    for t in res.trades[-5:]:
        mark = "🟢" if t.pnl_net > 0 else "🔴"
        print(f"  {mark} {t.direction} ${t.entry_price:,.2f} → ${t.exit_price:,.2f} | "
              f"{t.pnl_net:+.2f} USDT ({t.pnl_pct:+.2f}%) | fees ${t.fees:.2f} | "
              f"{t.exit_reason} | {t.bars_held} velas")
    print("=" * 64)


# ───────────────────────── CLI ─────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest DonchianEngine (fees + slippage reales)")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbol", type=str, default=None)
    parser.add_argument("--timeframe", type=str, default="15m")
    parser.add_argument("--period", type=int, default=None, help="N del canal Donchian (default 20)")
    parser.add_argument("--adx-min", type=float, default=None, help="Umbral ADX (default 20)")
    parser.add_argument("--vol-min", type=float, default=None, help="Ratio mínimo de volumen (default 1.0)")
    parser.add_argument("--sl-mode", type=str, default=None, choices=["channel", "atr", "tighter"])
    parser.add_argument("--sl-atr", type=float, default=None, help="Multiplicador ATR del SL (default 1.5)")
    parser.add_argument("--tp-atr", type=float, default=None, help="Multiplicador ATR del TP (default 3.0)")
    parser.add_argument("--fee-bps", type=float, default=5.0, help="Taker fee por lado en bps (default 5 = 0.05%%)")
    parser.add_argument("--slippage-bps", type=float, default=2.0, help="Slippage por fill en bps (default 2)")
    args = parser.parse_args()

    settings = settings_or_defaults()
    settings.timeframe = args.timeframe
    if args.symbol:
        settings.symbol = args.symbol
    if args.period is not None:
        settings.dc_period = args.period
    if args.adx_min is not None:
        settings.dc_adx_min = args.adx_min
    if args.vol_min is not None:
        settings.dc_vol_ratio_min = args.vol_min
    if args.sl_mode is not None:
        settings.dc_sl_mode = args.sl_mode
    if args.sl_atr is not None:
        settings.dc_sl_atr_mult = args.sl_atr
    if args.tp_atr is not None:
        settings.dc_tp_atr_mult = args.tp_atr

    exec_model = ExecutionModel(
        taker_fee_pct=args.fee_bps / 10_000,
        slippage_pct=args.slippage_bps / 10_000,
    )
    engine = DonchianEngine(settings)

    print(f"Bajando {args.days} días de {settings.symbol} @ {settings.timeframe} "
          f"(Binance Futures mainnet)...")
    df = fetch_history(settings.symbol, settings.timeframe, args.days)
    print(f"  → {len(df)} velas ({df.index[0]} → {df.index[-1]})")

    print("Simulando...")
    bt = DonchianBacktester(settings, engine, exec_model)
    result = bt.run(df, warmup=engine.warmup_bars())
    report(result, exec_model, settings)


if __name__ == "__main__":
    main()
