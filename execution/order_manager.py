"""
execution/order_manager.py
Gestión completa del ciclo de vida de órdenes.
- Stop-loss dinámico con ATR × 2.0
- Take-profit con ratio R/R mínimo 1:2
- TRAILING STOP automático cuando profit > 2%
- Daily drawdown limit
- Soporta posiciones LONG (compra al alza) y SHORT (venta al baja)
  En paper trading (sin exchange real) los SHORTs son simulados.
"""

import json
import time
import uuid
from datetime import datetime, date
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Literal
from logs.logger import logger
from config.settings import Settings
from core.indicators import MarketSnapshot
from core.claude_agent import TradeDecision


STATE_FILE = Path(__file__).parent.parent / "data" / "state.json"
JOURNAL_FILE = Path(__file__).parent.parent / "data" / "trade_journal.jsonl"


@dataclass
class Position:
    """
    Posición abierta. Soporta LONG (compra al alza) y SHORT (venta al baja).
    Convenciones del trailing stop:
      - LONG : highest_price_seen = máximo desde la apertura, stop sólo SUBE.
      - SHORT: highest_price_seen = mínimo desde la apertura, stop sólo BAJA.
    """
    order_id: str
    client_order_id: str
    direction: Literal["LONG", "SHORT"]
    entry_price: float
    amount_btc: float
    amount_usdt: float
    stop_loss: float
    original_stop_loss: float
    take_profit: float
    entry_time: str
    entry_reason: str
    claude_confidence: float
    trailing_active: bool = False
    highest_price_seen: float = 0.0
    # TP escalado: TP1 parcial + breakeven. initial_amount_btc guarda el tamaño
    # original; amount_btc se reduce tras el parcial. realized_pnl_usdt acumula
    # lo cobrado en TP1.
    take_profit_1: float = 0.0
    initial_amount_btc: float = 0.0
    tp1_done: bool = False
    realized_pnl_usdt: float = 0.0


@dataclass
class BotState:
    capital: float
    capital_initial: float
    capital_peak: float
    daily_capital_start: float
    last_reset_date: str
    open_position: Optional[dict]
    total_trades: int
    winning_trades: int
    consecutive_failures: int
    is_stopped: bool
    last_close_at: float = 0.0     # epoch del último cierre (para cooldown)


class OrderManager:
    def __init__(self, settings: Settings, exchange_client=None):
        self.settings = settings
        self.exchange = exchange_client
        STATE_FILE.parent.mkdir(exist_ok=True)
        JOURNAL_FILE.parent.mkdir(exist_ok=True)
        self.state = self._load_state()
        logger.info(
            f"OrderManager inicializado | Capital: ${self.state.capital:,.2f} | "
            f"Posición abierta: {'SÍ' if self.state.open_position else 'NO'}"
        )

    # ─────────────────────────────────────────
    #  LÓGICA DE DECISIÓN
    # ─────────────────────────────────────────

    def _decision_direction(self, decision: TradeDecision) -> Literal["LONG", "SHORT"]:
        """LONG si accion=COMPRAR, SHORT si accion=VENDER. Respeta decision.direction si está."""
        explicit = getattr(decision, "direction", None)
        if explicit in ("LONG", "SHORT"):
            return explicit
        return "LONG" if decision.accion == "COMPRAR" else "SHORT"

    def should_open(
        self, decision: TradeDecision, snapshot: MarketSnapshot
    ) -> bool:
        """Decide si abrir una nueva posición (LONG o SHORT)."""
        if self.state.is_stopped:
            logger.warning("🛑 Bot detenido por drawdown diario")
            return False
        if self.state.open_position is not None:
            return False
        if decision.accion not in ("COMPRAR", "VENDER"):
            return False
        # Cooldown: tras cerrar una posición, esperamos N velas antes de reabrir.
        # Esto evita el ruido de oscilar entre señales opuestas en velas adyacentes.
        if self.settings.cooldown_bars > 0 and self.state.last_close_at > 0:
            tf_min = {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                      "1h": 60, "4h": 240, "1d": 1440}.get(self.settings.timeframe, 15)
            need_secs = self.settings.cooldown_bars * tf_min * 60
            since_close = time.time() - self.state.last_close_at
            if since_close < need_secs:
                logger.info(
                    f"Cooldown activo: faltan {int(need_secs - since_close)}s "
                    f"({self.settings.cooldown_bars} velas)"
                )
                return False
        if decision.confianza < self.settings.min_claude_confidence:
            logger.info(
                f"Confianza insuficiente: {decision.confianza:.0%} < "
                f"{self.settings.min_claude_confidence:.0%}"
            )
            return False
        if decision.take_profit_pct < decision.stop_loss_pct * self.settings.min_risk_reward:
            logger.warning(
                f"Ratio R/R insuficiente: TP={decision.take_profit_pct:.2%} / "
                f"SL={decision.stop_loss_pct:.2%}"
            )
            return False
        tradeable_capital = self.state.capital * (1 - self.settings.trade_reserve_pct)
        trade_size = self._calculate_position_size(snapshot, decision)
        if trade_size > tradeable_capital:
            logger.warning(
                f"Capital insuficiente: {trade_size:.2f} > {tradeable_capital:.2f}"
            )
            return False
        return True

    # Retrocompatibilidad con código que aún llame a should_buy
    def should_buy(self, decision: TradeDecision, snapshot: MarketSnapshot) -> bool:
        return self.should_open(decision, snapshot)

    def should_close(
        self, snapshot: MarketSnapshot, decision: TradeDecision
    ) -> tuple[bool, str]:
        """
        Evalúa si cerrar la posición abierta. Antes de revisar, actualiza el
        trailing stop en el sentido correcto.
        """
        if self.state.open_position is None:
            return False, ""

        self._update_trailing_stop(snapshot.price)

        pos = self._get_open_position()
        current_price = snapshot.price

        if pos.direction == "LONG":
            # SL para LONG: precio baja al stop
            if current_price <= pos.stop_loss:
                reason = (
                    f"Trailing stop activado: ${current_price:,.2f} <= ${pos.stop_loss:,.2f}"
                    if pos.trailing_active
                    else f"Stop-loss alcanzado: ${current_price:,.2f} <= ${pos.stop_loss:,.2f}"
                )
                return True, reason
            # TP fijo solo si trailing no se activó
            if not pos.trailing_active and current_price >= pos.take_profit:
                return True, f"Take-profit alcanzado: ${current_price:,.2f} >= ${pos.take_profit:,.2f}"
            # Señal contraria explícita (Claude/engine quiere vender un LONG)
            if (decision.accion == "VENDER" and
                    decision.confianza >= self.settings.min_claude_confidence):
                return True, f"Señal VENDER: {decision.razon}"

        else:  # SHORT
            # SL para SHORT: precio sube al stop
            if current_price >= pos.stop_loss:
                reason = (
                    f"Trailing stop activado: ${current_price:,.2f} >= ${pos.stop_loss:,.2f}"
                    if pos.trailing_active
                    else f"Stop-loss alcanzado: ${current_price:,.2f} >= ${pos.stop_loss:,.2f}"
                )
                return True, reason
            # TP para SHORT: precio bajó hasta el take-profit
            if not pos.trailing_active and current_price <= pos.take_profit:
                return True, f"Take-profit alcanzado: ${current_price:,.2f} <= ${pos.take_profit:,.2f}"
            # Señal contraria: COMPRAR para cerrar un SHORT
            if (decision.accion == "COMPRAR" and
                    decision.confianza >= self.settings.min_claude_confidence):
                return True, f"Señal COMPRAR: {decision.razon}"

        return False, ""

    # Retrocompat
    def should_sell(
        self, snapshot: MarketSnapshot, decision: TradeDecision
    ) -> tuple[bool, str]:
        return self.should_close(snapshot, decision)

    # ─────────────────────────────────────────
    #  TRAILING STOP
    # ─────────────────────────────────────────

    def _update_trailing_stop(self, current_price: float) -> None:
        """
        LONG: activa trailing cuando profit > activation_pct, después rastrea
              el máximo y el stop = highest * (1 - distance_pct). El stop sólo SUBE.
        SHORT: activa trailing cuando profit > activation_pct (precio bajó),
               rastrea el mínimo y el stop = lowest * (1 + distance_pct). El stop sólo BAJA.
        """
        if not self.settings.trailing_stop_enabled:
            return
        if self.state.open_position is None:
            return

        pos_dict = self.state.open_position
        entry_price = pos_dict["entry_price"]
        direction = pos_dict.get("direction", "LONG")

        if direction == "LONG":
            # Rastrear máximo
            if current_price > pos_dict.get("highest_price_seen", 0):
                pos_dict["highest_price_seen"] = current_price
            profit_pct = (current_price - entry_price) / entry_price

            if not pos_dict.get("trailing_active", False):
                if profit_pct >= self.settings.trailing_activation_pct:
                    pos_dict["trailing_active"] = True
                    logger.info(
                        f"🎯 Trailing LONG ACTIVADO | profit: {profit_pct:.2%} | "
                        f"precio: ${current_price:,.2f}"
                    )

            if pos_dict.get("trailing_active", False):
                highest = pos_dict["highest_price_seen"]
                new_stop = highest * (1 - self.settings.trailing_distance_pct)
                if new_stop > pos_dict["stop_loss"]:
                    old_stop = pos_dict["stop_loss"]
                    pos_dict["stop_loss"] = round(new_stop, 2)
                    self._save_state()
                    logger.info(
                        f"📈 Trailing LONG actualizado: ${old_stop:,.2f} → "
                        f"${new_stop:,.2f} (high: ${highest:,.2f})"
                    )

        else:  # SHORT
            # Rastrear mínimo. Si nunca lo seteamos, lo inicializamos al entry.
            best = pos_dict.get("highest_price_seen", entry_price)
            if best <= 0 or current_price < best:
                pos_dict["highest_price_seen"] = current_price
            profit_pct = (entry_price - current_price) / entry_price

            if not pos_dict.get("trailing_active", False):
                if profit_pct >= self.settings.trailing_activation_pct:
                    pos_dict["trailing_active"] = True
                    logger.info(
                        f"🎯 Trailing SHORT ACTIVADO | profit: {profit_pct:.2%} | "
                        f"precio: ${current_price:,.2f}"
                    )

            if pos_dict.get("trailing_active", False):
                lowest = pos_dict["highest_price_seen"]  # reusamos el campo
                new_stop = lowest * (1 + self.settings.trailing_distance_pct)
                # Para SHORT, el stop sólo BAJA
                if new_stop < pos_dict["stop_loss"]:
                    old_stop = pos_dict["stop_loss"]
                    pos_dict["stop_loss"] = round(new_stop, 2)
                    self._save_state()
                    logger.info(
                        f"📉 Trailing SHORT actualizado: ${old_stop:,.2f} → "
                        f"${new_stop:,.2f} (low: ${lowest:,.2f})"
                    )

    # ─────────────────────────────────────────
    #  EJECUCIÓN
    # ─────────────────────────────────────────

    def open_position(
        self, snapshot: MarketSnapshot, decision: TradeDecision
    ) -> Optional[Position]:
        price = snapshot.price
        direction = self._decision_direction(decision)
        trade_size_usdt = self._calculate_position_size(snapshot, decision)

        atr_stop_pct = snapshot.atr_pct * self.settings.atr_sl_multiplier / 100
        effective_sl_pct = max(decision.stop_loss_pct, atr_stop_pct)

        if direction == "LONG":
            stop_loss_price = price * (1 - effective_sl_pct)
            take_profit_price = price * (1 + decision.take_profit_pct)
        else:  # SHORT
            stop_loss_price = price * (1 + effective_sl_pct)
            take_profit_price = price * (1 - decision.take_profit_pct)

        # TP1 escalado: a tp1_r_multiple × la distancia del SL efectivo.
        take_profit_1 = 0.0
        if self.settings.scaled_tp_enabled:
            tp1_dist = effective_sl_pct * self.settings.tp1_r_multiple
            if direction == "LONG":
                take_profit_1 = price * (1 + tp1_dist)
            else:
                take_profit_1 = price * (1 - tp1_dist)

        client_order_id = f"bot_{uuid.uuid4().hex[:12]}"
        amount_btc = trade_size_usdt / price

        position = Position(
            order_id="PAPER",
            client_order_id=client_order_id,
            direction=direction,
            entry_price=price,
            amount_btc=round(amount_btc, 8),
            amount_usdt=round(trade_size_usdt, 2),
            stop_loss=round(stop_loss_price, 2),
            original_stop_loss=round(stop_loss_price, 2),
            take_profit=round(take_profit_price, 2),
            entry_time=datetime.utcnow().isoformat(),
            entry_reason=decision.razon,
            claude_confidence=decision.confianza,
            trailing_active=False,
            highest_price_seen=price,
            take_profit_1=round(take_profit_1, 2),
            initial_amount_btc=round(amount_btc, 8),
        )

        if self.exchange is not None:
            try:
                side = "buy" if direction == "LONG" else "sell"
                order = self.exchange.place_market_order(
                    side=side, amount_usdt=trade_size_usdt,
                    client_order_id=client_order_id,
                )
                position.order_id = order.get("id", "UNKNOWN")
            except Exception as e:
                logger.error(f"Error al ejecutar orden: {e}")
                return None

        self.state.capital -= trade_size_usdt
        self.state.open_position = asdict(position)
        self._save_state()

        emoji = "🟢" if direction == "LONG" else "🔻"
        verb = "COMPRA LONG" if direction == "LONG" else "VENTA SHORT"
        logger.info(
            f"{emoji} {verb} | ${price:,.2f} | {amount_btc:.6f} BTC | "
            f"SL: ${stop_loss_price:,.2f} ({effective_sl_pct:.2%}) | "
            f"TP: ${take_profit_price:,.2f} ({decision.take_profit_pct:.2%})"
        )
        return position

    def maybe_take_partial_tp1(self, snapshot: MarketSnapshot) -> Optional[dict]:
        """
        Si el TP escalado está activo y el precio tocó TP1, cierra una fracción
        (tp1_size_pct del tamaño original), contabiliza la ganancia parcial y
        mueve el SL a breakeven. Devuelve un dict con el evento (para notificar)
        o None si no hubo parcial. El remanente sigue corriendo al TP completo.
        """
        if not self.settings.scaled_tp_enabled:
            return None
        if self.state.open_position is None:
            return None
        pos = self.state.open_position
        if pos.get("tp1_done"):
            return None
        tp1 = pos.get("take_profit_1", 0.0)
        if not tp1:
            return None

        price = snapshot.price
        direction = pos.get("direction", "LONG")
        hit = price >= tp1 if direction == "LONG" else price <= tp1
        if not hit:
            return None

        entry = pos["entry_price"]
        initial_btc = pos.get("initial_amount_btc") or pos.get("amount_btc", 0.0)
        portion_btc = round(min(initial_btc * self.settings.tp1_size_pct, pos["amount_btc"]), 8)
        if portion_btc <= 0:
            return None
        portion_usdt = portion_btc * entry
        if direction == "LONG":
            pnl = (price - entry) * portion_btc
        else:
            pnl = (entry - price) * portion_btc

        # Exchange real: cerrar el parcial a mercado en sentido contrario.
        if self.exchange is not None:
            try:
                side = "sell" if direction == "LONG" else "buy"
                self.exchange.place_market_order(
                    side=side, amount_usdt=portion_usdt,
                    client_order_id=f"{pos['client_order_id']}_tp1",
                )
            except Exception as e:
                logger.error(f"Error al ejecutar TP1 parcial: {e}")
                return None

        self.state.capital += portion_usdt + pnl
        pos["amount_btc"] = round(pos["amount_btc"] - portion_btc, 8)
        pos["amount_usdt"] = round(pos["amount_usdt"] - portion_usdt, 2)
        pos["realized_pnl_usdt"] = round(pos.get("realized_pnl_usdt", 0.0) + pnl, 2)
        pos["tp1_done"] = True

        if self.settings.breakeven_after_tp1:
            off = self.settings.breakeven_offset_pct
            if direction == "LONG":
                be = round(entry * (1 + off), 2)
                if be > pos["stop_loss"]:
                    pos["stop_loss"] = be
            else:
                be = round(entry * (1 - off), 2)
                if be < pos["stop_loss"]:
                    pos["stop_loss"] = be

        if self.state.capital > self.state.capital_peak:
            self.state.capital_peak = self.state.capital
        self._save_state()

        logger.info(
            f"🎯 TP1 PARCIAL | cerré {portion_btc:.6f} BTC @ ${price:,.2f} | "
            f"+${pnl:,.2f} | SL → breakeven ${pos['stop_loss']:,.2f}"
        )
        return {
            "price": price,
            "portion_btc": portion_btc,
            "pnl_usdt": round(pnl, 2),
            "new_stop": pos["stop_loss"],
            "direction": direction,
        }

    def close_position(self, snapshot: MarketSnapshot, reason: str) -> dict:
        if self.state.open_position is None:
            return {}

        pos = self._get_open_position()
        current_price = snapshot.price

        if pos.direction == "LONG":
            pnl_usdt = (current_price - pos.entry_price) * pos.amount_btc
        else:  # SHORT
            pnl_usdt = (pos.entry_price - current_price) * pos.amount_btc

        exit_usdt = pos.amount_usdt + pnl_usdt
        # PnL total del trade = remanente + lo ya cobrado en TP1. El % se mide
        # sobre el notional original para que sea comparable entre trades.
        total_pnl = pnl_usdt + pos.realized_pnl_usdt
        base_usdt = (pos.initial_amount_btc or pos.amount_btc) * pos.entry_price
        pnl_pct = (total_pnl / base_usdt) * 100 if base_usdt else 0.0

        self.state.capital += exit_usdt
        self.state.total_trades += 1
        if total_pnl > 0:
            self.state.winning_trades += 1
        if self.state.capital > self.state.capital_peak:
            self.state.capital_peak = self.state.capital

        trade_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "exit_price": current_price,
            "entry_time": pos.entry_time,
            "exit_time": datetime.utcnow().isoformat(),
            "amount_btc": pos.initial_amount_btc or pos.amount_btc,
            "amount_usdt": pos.amount_usdt,
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "realized_tp1_pnl": round(pos.realized_pnl_usdt, 2),
            "took_tp1": pos.tp1_done,
            "exit_reason": reason,
            "trailing_was_active": pos.trailing_active,
            "highest_price_seen": pos.highest_price_seen,
            "original_stop_loss": pos.original_stop_loss,
            "final_stop_loss": pos.stop_loss,
            "claude_confidence": pos.claude_confidence,
            "entry_reason": pos.entry_reason,
        }
        self._write_journal(trade_record)
        self.state.open_position = None
        self.state.last_close_at = time.time()
        self._check_daily_drawdown()
        self._save_state()

        win = total_pnl > 0
        emoji = "🟢" if win else "🔴"
        verb = "CIERRE LONG" if pos.direction == "LONG" else "CIERRE SHORT"
        tp1_note = f" (incluye +${pos.realized_pnl_usdt:.2f} de TP1)" if pos.tp1_done else ""
        logger.info(
            f"{emoji} {verb} | ${current_price:,.2f} | P&L: {pnl_pct:+.2f}% "
            f"(${total_pnl:+.2f}){tp1_note} | {reason}"
        )
        return trade_record

    # ─────────────────────────────────────────
    #  HELPERS
    # ─────────────────────────────────────────

    def _calculate_position_size(
        self, snapshot: MarketSnapshot, decision: TradeDecision
    ) -> float:
        """
        Dimensionamiento: arriesgar exactamente max_risk_per_trade del capital.
        Position size = (capital * risk%) / stop_loss%
        """
        risk_pct = self._effective_risk_pct()
        max_risk_usdt = self.state.capital * risk_pct
        atr_stop_pct = snapshot.atr_pct * self.settings.atr_sl_multiplier / 100
        effective_sl_pct = max(decision.stop_loss_pct, atr_stop_pct)
        if effective_sl_pct > 0:
            position_size = max_risk_usdt / effective_sl_pct
        else:
            position_size = max_risk_usdt * 10
        max_usdt = self.state.capital * (1 - self.settings.trade_reserve_pct)
        return min(position_size, max_usdt)

    def _effective_risk_pct(self) -> float:
        """
        Si Kelly está activado y hay suficientes trades, ajusta el riesgo según
        el edge histórico (win_rate, avg_win/avg_loss). Si no, usa el fijo del
        settings (max_risk_per_trade).
        """
        base = self.settings.max_risk_per_trade
        if not self.settings.use_kelly_sizing:
            return base
        n = self.state.total_trades
        if n < self.settings.kelly_min_trades:
            return base
        # Calcular avg_win y avg_loss leyendo el journal
        try:
            with open(JOURNAL_FILE) as f:
                trades = [json.loads(l) for l in f if l.strip()]
        except Exception:
            return base
        wins = [t["pnl_pct"] / 100 for t in trades if t.get("pnl", 0) > 0]
        losses = [abs(t["pnl_pct"] / 100) for t in trades if t.get("pnl", 0) <= 0]
        if not wins or not losses:
            return base
        win_rate = len(wins) / (len(wins) + len(losses))
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        if avg_loss <= 0:
            return base
        b = avg_win / avg_loss
        # Kelly = win_rate - (1 - win_rate) / b
        full_kelly = win_rate - (1 - win_rate) / b
        if full_kelly <= 0:
            # Sin edge esperado: usar el mínimo
            return self.settings.kelly_min_risk_pct
        fractional = full_kelly * self.settings.kelly_fraction
        return max(
            self.settings.kelly_min_risk_pct,
            min(self.settings.kelly_max_risk_pct, fractional),
        )

    def _check_daily_drawdown(self) -> None:
        today = str(date.today())
        if self.state.last_reset_date != today:
            self.state.daily_capital_start = self.state.capital
            self.state.last_reset_date = today
            self.state.is_stopped = False
            logger.info(f"Nuevo día | Capital inicial: ${self.state.capital:,.2f}")
        daily_dd = (
            (self.state.daily_capital_start - self.state.capital)
            / self.state.daily_capital_start
        )
        if daily_dd >= self.settings.daily_drawdown_limit:
            self.state.is_stopped = True
            logger.critical(
                f"🚨 DAILY DRAWDOWN: {daily_dd:.1%}. Bot detenido hasta mañana."
            )

    def _get_open_position(self) -> Optional[Position]:
        if self.state.open_position is None:
            return None
        return Position(**self.state.open_position)

    def get_stats(self) -> dict:
        win_rate = (
            self.state.winning_trades / self.state.total_trades * 100
            if self.state.total_trades > 0 else 0
        )
        total_return = (
            (self.state.capital - self.state.capital_initial)
            / self.state.capital_initial * 100
        )
        max_drawdown = (
            (self.state.capital_peak - self.state.capital) / self.state.capital_peak * 100
            if self.state.capital_peak > 0 else 0
        )
        return {
            "capital": round(self.state.capital, 2),
            "total_return_pct": round(total_return, 3),
            "total_trades": self.state.total_trades,
            "win_rate_pct": round(win_rate, 1),
            "max_drawdown_pct": round(max_drawdown, 3),
            "is_stopped": self.state.is_stopped,
            "open_position": self.state.open_position is not None,
        }

    # ─────────────────────────────────────────
    #  PERSISTENCIA
    # ─────────────────────────────────────────

    def _load_state(self) -> BotState:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                return BotState(**data)
            except Exception as e:
                logger.warning(f"No se pudo cargar state.json: {e}")

        initial = BotState(
            capital=self.settings.initial_capital,
            capital_initial=self.settings.initial_capital,
            capital_peak=self.settings.initial_capital,
            daily_capital_start=self.settings.initial_capital,
            last_reset_date=str(date.today()),
            open_position=None,
            total_trades=0, winning_trades=0,
            consecutive_failures=0, is_stopped=False,
        )
        self._save_state(initial)
        return initial

    def _save_state(self, state: Optional[BotState] = None) -> None:
        state = state or self.state
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(state), f, indent=2, default=str)

    def _write_journal(self, record: dict) -> None:
        with open(JOURNAL_FILE, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
