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
from dataclasses import dataclass, asdict, field
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
    # IDs de órdenes condicionales en el exchange (si las pusimos):
    sl_exchange_order_id: str = ""
    tp_exchange_order_id: str = ""
    # TP escalado:
    tp1_price: float = 0.0
    tp1_partial_pct: float = 0.0
    tp1_filled: bool = False
    tp1_exchange_order_id: str = ""
    original_amount_btc: float = 0.0


@dataclass
class BotState:
    capital: float
    capital_initial: float
    capital_peak: float
    daily_capital_start: float
    last_reset_date: str
    # Legacy: posición única (compat con state.json viejo).
    open_position: Optional[dict] = None
    # Nuevo: lista de posiciones abiertas en simultáneo (hasta MAX_CONCURRENT_TRADES).
    open_positions: list = field(default_factory=list)
    total_trades: int = 0
    winning_trades: int = 0
    consecutive_failures: int = 0
    is_stopped: bool = False
    last_close_at: float = 0.0     # epoch del último cierre (para cooldown)

    def __post_init__(self):
        # Migración silenciosa: si veníamos del schema viejo (open_position single)
        # lo movemos a la lista.
        if self.open_position is not None and not self.open_positions:
            self.open_positions = [self.open_position]
            self.open_position = None


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
        max_concurrent = getattr(self.settings, "max_concurrent_trades", 1)
        if len(self.state.open_positions) >= max_concurrent:
            logger.info(
                f"Cap de posiciones concurrentes alcanzado "
                f"({len(self.state.open_positions)}/{max_concurrent})"
            )
            return False
        # No abrir 2 posiciones en la MISMA dirección — no agrega edge.
        wanted_dir = self._decision_direction(decision)
        for p in self.state.open_positions:
            if p.get("direction") == wanted_dir:
                logger.info(f"Ya hay una posición {wanted_dir} abierta, salteo")
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

    def should_close_any(
        self, snapshot: MarketSnapshot, decision: TradeDecision,
    ) -> list[tuple[str, str]]:
        """
        Itera todas las posiciones abiertas y devuelve [(client_order_id, motivo)]
        para las que deben cerrarse en este ciclo.
        Multi-trade: cada posición se evalúa por separado.
        """
        to_close: list[tuple[str, str]] = []
        positions_snapshot = list(self.state.open_positions)
        for pos_dict in positions_snapshot:
            self.state.open_position = pos_dict  # contexto temporal para should_close
            should, reason = self.should_close(snapshot, decision)
            if should:
                to_close.append((pos_dict.get("client_order_id", ""), reason))
        # Restaurar el "primary"
        self.state.open_position = (
            self.state.open_positions[0] if self.state.open_positions else None
        )
        return to_close

    def should_close(
        self, snapshot: MarketSnapshot, decision: TradeDecision
    ) -> tuple[bool, str]:
        """
        Evalúa si cerrar la posición indicada por self.state.open_position
        (set previamente por should_close_any en multi-trade). Mantiene la
        compatibilidad con código viejo que esperaba "la posición".
        """
        if self.state.open_position is None:
            return False, ""

        # ───── Reconciliar con el exchange ─────
        # Si el SL o TP que pusimos en el exchange ya cerró la posición,
        # detectamos ahora y devolvemos True para que el strategy llame
        # close_position (que actualiza estado local).
        if self.exchange is not None:
            try:
                exchange_close = self._exchange_closed_us()
                if exchange_close:
                    return True, exchange_close
            except Exception as e:
                logger.warning(f"No pude reconciliar con exchange: {e}")

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
            tp1_price = price * (1 + effective_sl_pct * self.settings.tp1_rr_multiple)
        else:  # SHORT
            stop_loss_price = price * (1 + effective_sl_pct)
            take_profit_price = price * (1 - decision.take_profit_pct)
            tp1_price = price * (1 - effective_sl_pct * self.settings.tp1_rr_multiple)

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
            tp1_price=round(tp1_price, 2),
            tp1_partial_pct=(
                self.settings.tp1_partial_pct if self.settings.tp_scaling_enabled else 0.0
            ),
            original_amount_btc=round(amount_btc, 8),
        )

        if self.exchange is not None:
            # Pre-flight: validar filtros antes de gastar capital o intentar la orden.
            side = "buy" if direction == "LONG" else "sell"
            check_btc = position.amount_btc
            check_usdt = trade_size_usdt if side == "buy" else (check_btc * price)
            ok, reason = self.exchange.validate_order_filters(
                amount_btc=float(check_btc), amount_usdt=float(check_usdt),
            )
            if not ok:
                logger.warning(
                    f"⚠️ Orden NO enviada (filtros del exchange): {reason}. "
                    f"Posición saltada."
                )
                return None

            try:
                order = self.exchange.place_market_order(
                    side=side, amount_usdt=trade_size_usdt,
                    client_order_id=client_order_id,
                )
                position.order_id = order.get("id", "UNKNOWN")
            except ValueError as e:
                # Filtros rechazaron post-precisión (caso edge)
                logger.warning(f"⚠️ {e}")
                return None
            except Exception as e:
                # En Spot, SHORT (sell sin BTC) cae acá con InsufficientFunds.
                logger.error(f"Error al ejecutar orden de entrada: {e}")
                return None

            # ───── Colocar SL y TP como órdenes reales en el exchange ─────
            # Si fallan, hacemos rollback cerrando la entrada inmediatamente
            # para no quedar con posición desprotegida.
            close_side = "sell" if direction == "LONG" else "buy"
            try:
                sl_order = self.exchange.place_stop_loss_market(
                    side=close_side,
                    amount_btc=float(position.amount_btc),
                    stop_price=float(position.stop_loss),
                    client_order_id=f"{client_order_id}_sl",
                )
                position.sl_exchange_order_id = str(sl_order.get("id", ""))
            except Exception as e:
                logger.critical(
                    f"❌ FALLÓ poner SL en exchange ({e}). Cierro la posición "
                    f"inmediatamente a mercado para no quedar desprotegidos."
                )
                try:
                    self.exchange.place_market_order(
                        side=close_side,
                        amount_usdt=float(position.amount_btc),  # qty en sell
                        client_order_id=f"{client_order_id}_rollback",
                    )
                except Exception as e2:
                    logger.critical(
                        f"❌❌ TAMBIÉN falló el rollback ({e2}). "
                        f"REVISÁ MANUALMENTE EN BINANCE."
                    )
                return None

            # Si TP escalado activo: ponemos TP1 (parcial) en exchange como
            # protección de profit. El TP final se chequea localmente para
            # poder activarlo recién después de que TP1 mueva el SL a breakeven.
            # Si no, ponemos TP completo (legacy).
            try:
                if self.settings.tp_scaling_enabled and position.tp1_partial_pct > 0:
                    tp1_amt = float(position.amount_btc) * position.tp1_partial_pct
                    tp1_order = self.exchange.place_take_profit_limit(
                        side=close_side,
                        amount_btc=tp1_amt,
                        limit_price=float(position.tp1_price),
                        client_order_id=f"{client_order_id}_tp1",
                    )
                    position.tp1_exchange_order_id = str(tp1_order.get("id", ""))
                    logger.info(
                        f"TP1 parcial colocado: {tp1_amt:.6f} BTC @ ${position.tp1_price:,.2f} "
                        f"({position.tp1_partial_pct:.0%} de la posición)"
                    )
                else:
                    tp_order = self.exchange.place_take_profit_limit(
                        side=close_side,
                        amount_btc=float(position.amount_btc),
                        limit_price=float(position.take_profit),
                        client_order_id=f"{client_order_id}_tp",
                    )
                    position.tp_exchange_order_id = str(tp_order.get("id", ""))
            except Exception as e:
                logger.warning(
                    f"⚠️ No pude poner TP en exchange ({e}). El SL sí está."
                    f" Se va a chequear con la lógica local del bot en cada ciclo."
                )

        self.state.capital -= trade_size_usdt
        self.state.open_positions.append(asdict(position))
        # Compat: open_position siempre apunta a la primera posición abierta
        self.state.open_position = (
            self.state.open_positions[0] if self.state.open_positions else None
        )
        self._save_state()

        emoji = "🟢" if direction == "LONG" else "🔻"
        verb = "COMPRA LONG" if direction == "LONG" else "VENTA SHORT"
        logger.info(
            f"{emoji} {verb} | ${price:,.2f} | {amount_btc:.6f} BTC | "
            f"SL: ${stop_loss_price:,.2f} ({effective_sl_pct:.2%}) | "
            f"TP: ${take_profit_price:,.2f} ({decision.take_profit_pct:.2%})"
        )
        return position

    def close_position(self, snapshot: MarketSnapshot, reason: str) -> dict:
        if self.state.open_position is None:
            return {}

        pos = self._get_open_position()

        # Si el cierre fue por reconciliación (SL/TP ya disparados en exchange),
        # NO necesitamos mandar otra orden ni cancelar nada. Pero si el cierre
        # viene por señal opuesta o /close manual, sí: cancelar protectoras y
        # cerrar a mercado en el exchange.
        from_exchange_reconcile = "ejecutado en exchange" in reason
        if not from_exchange_reconcile and self.exchange is not None:
            close_side = "sell" if pos.direction == "LONG" else "buy"
            # 1) Cancelar las órdenes protectoras
            self._cancel_protective_orders(pos)
            # 2) Cerrar a mercado
            try:
                self.exchange.place_market_order(
                    side=close_side,
                    amount_usdt=float(pos.amount_btc),  # cantidad para sell
                    client_order_id=f"{pos.client_order_id}_close",
                )
            except Exception as e:
                logger.error(
                    f"Error cerrando posición a mercado en exchange: {e}. "
                    f"Continúo igual con el cierre local."
                )

        current_price = snapshot.price

        if pos.direction == "LONG":
            pnl_usdt = (current_price - pos.entry_price) * pos.amount_btc
            pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100
        else:  # SHORT
            pnl_usdt = (pos.entry_price - current_price) * pos.amount_btc
            pnl_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100

        exit_usdt = pos.amount_usdt + pnl_usdt

        self.state.capital += exit_usdt
        self.state.total_trades += 1
        if pnl_usdt > 0:
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
            "amount_btc": pos.amount_btc,
            "amount_usdt": pos.amount_usdt,
            "pnl": round(pnl_usdt, 2),
            "pnl_pct": round(pnl_pct, 4),
            "exit_reason": reason,
            "trailing_was_active": pos.trailing_active,
            "highest_price_seen": pos.highest_price_seen,
            "original_stop_loss": pos.original_stop_loss,
            "final_stop_loss": pos.stop_loss,
            "claude_confidence": pos.claude_confidence,
            "entry_reason": pos.entry_reason,
        }
        self._write_journal(trade_record)
        # Sacar la posición cerrada de la lista (match por client_order_id)
        self.state.open_positions = [
            p for p in self.state.open_positions
            if p.get("client_order_id") != pos.client_order_id
        ]
        # Compat: open_position apunta a la primera restante (o None)
        self.state.open_position = (
            self.state.open_positions[0] if self.state.open_positions else None
        )
        self.state.last_close_at = time.time()
        self._check_daily_drawdown()
        self._save_state()

        win = pnl_usdt > 0
        emoji = "🟢" if win else "🔴"
        verb = "CIERRE LONG" if pos.direction == "LONG" else "CIERRE SHORT"
        logger.info(
            f"{emoji} {verb} | ${current_price:,.2f} | P&L: {pnl_pct:+.2f}% "
            f"(${pnl_usdt:+.2f}) | {reason}"
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

    def _exchange_closed_us(self) -> Optional[str]:
        """
        Chequea si las órdenes condicionales (SL o TP) que pusimos en el
        exchange ya se ejecutaron. Si sí, devuelve un string con el motivo.
        Si no, None.

        TP1 (parcial) se trata aparte: si se ejecutó, ajustamos la posición
        in-place y NO devolvemos motivo (la posición sigue abierta con el
        tamaño restante).
        """
        pos = self._get_open_position()
        if pos is None:
            return None

        # ─── TP1 parcial: si se ejecutó, ajustar posición ───
        if pos.tp1_exchange_order_id and not pos.tp1_filled:
            try:
                tp1 = self.exchange.get_order(pos.tp1_exchange_order_id)
                if (tp1.get("status") or "").lower() in ("closed", "filled"):
                    self._handle_tp1_filled(pos, tp1)
                    # Posición sigue abierta (con el resto), no cerramos.
            except Exception as e:
                logger.warning(f"No pude consultar TP1 {pos.tp1_exchange_order_id}: {e}")

        # ─── SL o TP final: cerraron la posición ───
        for kind, oid in (
            ("Stop-loss (exchange)", pos.sl_exchange_order_id),
            ("Take-profit (exchange)", pos.tp_exchange_order_id),
        ):
            if not oid:
                continue
            try:
                order = self.exchange.get_order(oid)
            except Exception as e:
                logger.warning(f"No pude consultar orden {oid}: {e}")
                continue
            status = (order.get("status") or "").lower()
            if status in ("closed", "filled"):
                fill_price = float(order.get("average") or order.get("price") or 0)
                logger.info(
                    f"🔗 Reconciliación: {kind} se ejecutó en el exchange "
                    f"@ ${fill_price:,.2f} (orden {oid})"
                )
                return f"{kind} ejecutado en exchange a ${fill_price:,.2f}"
        return None

    def _handle_tp1_filled(self, pos: Position, tp1_order: dict) -> None:
        """
        TP1 (parcial) se ejecutó. Hay que:
        1) Reducir la cantidad de la posición a la fracción restante.
        2) Cancelar el SL viejo (era por full size).
        3) Poner SL nuevo en breakeven por la cantidad restante.
        4) Marcar tp1_filled=True.
        5) Acreditar la ganancia parcial al capital.
        """
        fill_price = float(tp1_order.get("average") or tp1_order.get("price") or pos.tp1_price)
        partial_btc = pos.original_amount_btc * pos.tp1_partial_pct
        partial_usdt_entry = partial_btc * pos.entry_price

        # PnL del parcial
        if pos.direction == "LONG":
            partial_pnl = (fill_price - pos.entry_price) * partial_btc
        else:
            partial_pnl = (pos.entry_price - fill_price) * partial_btc

        # Acreditar al capital
        self.state.capital += partial_usdt_entry + partial_pnl

        # Reducir posición
        remaining_btc = pos.original_amount_btc - partial_btc
        remaining_usdt = pos.amount_usdt * (1 - pos.tp1_partial_pct)

        # Cancelar SL viejo y poner SL nuevo en breakeven
        breakeven_price = pos.entry_price * (
            (1 + self.settings.breakeven_buffer_pct) if pos.direction == "LONG"
            else (1 - self.settings.breakeven_buffer_pct)
        )
        close_side = "sell" if pos.direction == "LONG" else "buy"
        new_sl_oid = ""
        try:
            if pos.sl_exchange_order_id:
                self.exchange.cancel_order(pos.sl_exchange_order_id)
            new_sl_order = self.exchange.place_stop_loss_market(
                side=close_side,
                amount_btc=remaining_btc,
                stop_price=breakeven_price,
                client_order_id=f"{pos.client_order_id}_sl_be",
            )
            new_sl_oid = str(new_sl_order.get("id", ""))
            logger.info(
                f"🔄 SL movido a breakeven @ ${breakeven_price:,.2f} "
                f"para {remaining_btc:.6f} BTC restantes"
            )
        except Exception as e:
            logger.critical(
                f"❌ No pude mover SL a breakeven tras TP1 ({e}). "
                f"REVISÁ MANUALMENTE: TP1 ya se ejecutó pero el SL puede "
                f"estar mal dimensionado."
            )

        # Persistir cambios
        pos.tp1_filled = True
        pos.amount_btc = round(remaining_btc, 8)
        pos.amount_usdt = round(remaining_usdt, 2)
        pos.stop_loss = round(breakeven_price, 2)
        if new_sl_oid:
            pos.sl_exchange_order_id = new_sl_oid
        self.state.open_position = asdict(pos)
        self._save_state()

        # Loggear como evento de venta parcial (estilo trade_journal)
        partial_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "exit_price": fill_price,
            "amount_btc": partial_btc,
            "amount_usdt": partial_usdt_entry,
            "pnl": round(partial_pnl, 2),
            "pnl_pct": round(partial_pnl / partial_usdt_entry * 100, 4) if partial_usdt_entry else 0,
            "exit_reason": "TP1 parcial (escalado)",
            "entry_reason": pos.entry_reason,
            "partial": True,
        }
        self._write_journal(partial_record)
        logger.info(
            f"🎯 TP1 parcial cerrado: {pos.tp1_partial_pct:.0%} de la posición "
            f"@ ${fill_price:,.2f} | PnL parcial: ${partial_pnl:+.2f}"
        )

    def reconcile_with_exchange(self) -> dict:
        """
        Reconcilia el state.json local con el estado real del exchange.

        Casos manejados:
        1) Tenemos posición local + las órdenes SL/TP están en el exchange OK → nada.
        2) Tenemos posición local + alguna orden protectora desapareció (cancelada
           o ejecutada): si fue ejecutada, lo va a detectar el siguiente ciclo.
           Si fue cancelada externamente, alertamos.
        3) NO tenemos posición local pero hay órdenes "bot_*" vivas en el exchange:
           son huérfanas (probable crash entre place_order y _save_state). Cancelar.
        4) Tenemos posición local pero NO hay órdenes en el exchange: la entrada
           se ejecutó, las protectoras nunca llegaron. POSICIÓN DESPROTEGIDA.
           Cerrar a mercado inmediatamente.

        Devuelve un dict con acciones tomadas y issues encontrados.
        """
        result = {
            "skipped": False,
            "open_orders_total": 0,
            "our_orders": 0,
            "orphan_canceled": 0,
            "actions": [],
            "issues": [],
        }
        if self.exchange is None:
            result["skipped"] = True
            return result

        # Traer órdenes abiertas del par
        try:
            open_orders = self.exchange.get_open_orders()
        except Exception as e:
            result["issues"].append(f"No pude consultar open orders: {e}")
            return result

        result["open_orders_total"] = len(open_orders)
        our_orders = [
            o for o in open_orders
            if (o.get("clientOrderId") or "").startswith("bot_")
        ]
        result["our_orders"] = len(our_orders)

        has_local_pos = self.state.open_position is not None

        if has_local_pos:
            pos = self._get_open_position()
            expected_oids = {
                pos.sl_exchange_order_id,
                pos.tp_exchange_order_id,
                pos.tp1_exchange_order_id,
            }
            expected_oids.discard("")
            # Mapear ids de las órdenes vivas en el exchange
            live_oids = {str(o.get("id", "")) for o in open_orders}

            # ¿Estamos completamente desprotegidos? (ninguna orden esperada está viva)
            if expected_oids and not (expected_oids & live_oids):
                # Doble check: consultar cada una para distinguir entre "ejecutada"
                # (la cubre _exchange_closed_us en should_close) y "cancelada externamente".
                any_executed = False
                for oid in expected_oids:
                    try:
                        o = self.exchange.get_order(oid)
                        status = (o.get("status") or "").lower()
                        if status in ("closed", "filled"):
                            any_executed = True
                            break
                    except Exception:
                        pass
                if not any_executed:
                    # Sin órdenes vivas ni ejecutadas: cerrar a mercado para no
                    # quedar a la deriva.
                    msg = "POSICIÓN DESPROTEGIDA: sin SL/TP en exchange. Cerrando a mercado."
                    logger.critical(f"❌ {msg}")
                    result["issues"].append(msg)
                    try:
                        close_side = "sell" if pos.direction == "LONG" else "buy"
                        self.exchange.place_market_order(
                            side=close_side,
                            amount_usdt=float(pos.amount_btc),
                            client_order_id=f"{pos.client_order_id}_reconcile_close",
                        )
                        result["actions"].append("posición cerrada a mercado (sin protecciones)")
                    except Exception as e:
                        result["issues"].append(f"Falló el cierre forzado: {e}")

            # Detectar órdenes externas: las nuestras vivas pero no esperadas
            for o in our_orders:
                if str(o.get("id", "")) not in expected_oids:
                    result["issues"].append(
                        f"Orden propia inesperada viva: {o.get('id')} "
                        f"(client_id={o.get('clientOrderId')})"
                    )

        else:
            # Sin posición local: cualquier orden "bot_*" viva es huérfana
            for o in our_orders:
                oid = str(o.get("id", ""))
                client_id = o.get("clientOrderId", "")
                try:
                    self.exchange.cancel_order(oid)
                    result["orphan_canceled"] += 1
                    result["actions"].append(f"cancelada huérfana {client_id}")
                    logger.warning(
                        f"🧹 Reconciliación: cancelada orden huérfana {oid} "
                        f"({client_id}) — sin posición local"
                    )
                except Exception as e:
                    result["issues"].append(f"No pude cancelar huérfana {oid}: {e}")

        return result

    def _cancel_protective_orders(self, pos: Position) -> None:
        """Cancela SL, TP y TP1 en el exchange (si están vivos). Ignora errores."""
        if self.exchange is None:
            return
        for oid in (
            pos.sl_exchange_order_id,
            pos.tp_exchange_order_id,
            pos.tp1_exchange_order_id,
        ):
            if not oid:
                continue
            try:
                self.exchange.cancel_order(oid)
                logger.info(f"Cancelada orden protectora {oid}")
            except Exception as e:
                logger.debug(f"No pude cancelar {oid}: {e}")

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
