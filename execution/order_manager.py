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
    symbol: str = ""


@dataclass
class BotState:
    capital: float
    capital_initial: float
    capital_peak: float
    daily_capital_start: float
    last_reset_date: str
    # Multi-symbol: posiciones abiertas indexadas por símbolo ("ETH/USDT": {...}).
    # El capital es un POOL COMPARTIDO global; cada posición descuenta su notional.
    open_positions: dict
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
            f"Posiciones abiertas: {self.count_open()} "
            f"({', '.join(self.open_symbols()) or 'ninguna'})"
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

    # ───────── inventario de posiciones (multi-symbol) ─────────

    def count_open(self) -> int:
        return len(self.state.open_positions)

    def open_symbols(self) -> list[str]:
        return list(self.state.open_positions.keys())

    def has_position(self, symbol: str) -> bool:
        return symbol in self.state.open_positions

    def _committed_notional(self) -> float:
        """Suma de los notionals comprometidos en posiciones abiertas."""
        return sum(
            float(p.get("amount_usdt", 0.0))
            for p in self.state.open_positions.values()
        )

    def _equity_basis(self) -> float:
        """
        Base de equity para sizing/reserva: capital libre + notional comprometido.
        (No incluye PnL no realizado, para que el sizing sea estable.) Con esto
        cada trade arriesga el mismo % del PORTAFOLIO, sin importar cuántos haya.
        """
        return self.state.capital + self._committed_notional()

    def should_open(
        self, decision: TradeDecision, snapshot: MarketSnapshot,
        symbol: Optional[str] = None,
    ) -> bool:
        """
        Decide si abrir una nueva posición (LONG o SHORT) en `symbol`.
        Incluye el CANDADO DE EXPOSICIÓN GLOBAL: no abrir si ya hay
        max_concurrent_trades posiciones abiertas en el portafolio.
        """
        symbol = symbol or self.settings.symbol
        if self.state.is_stopped:
            logger.warning("🛑 Bot detenido por drawdown diario")
            return False
        # Una sola posición por símbolo.
        if self.has_position(symbol):
            return False
        if decision.accion not in ("COMPRAR", "VENDER"):
            return False

        # ── CANDADO GLOBAL: techo de posiciones concurrentes en el portafolio ──
        if self.count_open() >= self.settings.max_concurrent_trades:
            logger.info(
                f"🔒 Candado global: {self.count_open()}/"
                f"{self.settings.max_concurrent_trades} trades abiertos "
                f"({', '.join(self.open_symbols())}). Ignoro señal en {symbol}."
            )
            return False

        # Cooldown: tras cerrar una posición, esperamos N velas antes de reabrir.
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
        # Capital del POOL COMPARTIDO: que la nueva no rompa la reserva del 30%.
        trade_size = self._calculate_position_size(snapshot, decision)
        if trade_size <= 0:
            logger.warning(
                f"Sin capital disponible en el pool para {symbol} "
                f"(comprometido ${self._committed_notional():,.2f} / "
                f"equity ${self._equity_basis():,.2f})"
            )
            return False
        return True

    # Retrocompatibilidad con código que aún llame a should_buy
    def should_buy(
        self, decision: TradeDecision, snapshot: MarketSnapshot,
        symbol: Optional[str] = None,
    ) -> bool:
        return self.should_open(decision, snapshot, symbol)

    def should_close(
        self, snapshot: MarketSnapshot, decision: TradeDecision,
        symbol: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Evalúa si cerrar la posición abierta en `symbol`. Antes de revisar,
        actualiza el trailing stop en el sentido correcto.
        """
        symbol = symbol or self.settings.symbol
        if not self.has_position(symbol):
            return False, ""

        self._update_trailing_stop(snapshot.price, symbol)

        pos = self.get_open_position(symbol)
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
        self, snapshot: MarketSnapshot, decision: TradeDecision,
        symbol: Optional[str] = None,
    ) -> tuple[bool, str]:
        return self.should_close(snapshot, decision, symbol)

    # ─────────────────────────────────────────
    #  TRAILING STOP
    # ─────────────────────────────────────────

    def _update_trailing_stop(self, current_price: float, symbol: Optional[str] = None) -> None:
        """
        LONG: activa trailing cuando profit > activation_pct, después rastrea
              el máximo y el stop = highest * (1 - distance_pct). El stop sólo SUBE.
        SHORT: activa trailing cuando profit > activation_pct (precio bajó),
               rastrea el mínimo y el stop = lowest * (1 + distance_pct). El stop sólo BAJA.
        """
        symbol = symbol or self.settings.symbol
        if not self.settings.trailing_stop_enabled:
            return
        if not self.has_position(symbol):
            return

        pos_dict = self.state.open_positions[symbol]
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
        self, snapshot: MarketSnapshot, decision: TradeDecision,
        symbol: Optional[str] = None,
    ) -> Optional[Position]:
        symbol = symbol or getattr(snapshot, "symbol", None) or self.settings.symbol
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
            symbol=symbol,
        )

        if self.exchange is not None:
            try:
                side = "buy" if direction == "LONG" else "sell"
                order = self.exchange.place_market_order(
                    side=side, amount_usdt=trade_size_usdt,
                    client_order_id=client_order_id, symbol=symbol,
                )
                position.order_id = order.get("id", "UNKNOWN")
            except Exception as e:
                logger.error(f"Error al ejecutar orden en {symbol}: {e}")
                return None

        self.state.capital -= trade_size_usdt
        self.state.open_positions[symbol] = asdict(position)
        self._save_state()

        emoji = "🟢" if direction == "LONG" else "🔻"
        verb = "COMPRA LONG" if direction == "LONG" else "VENTA SHORT"
        logger.info(
            f"{emoji} {verb} {symbol} | ${price:,.2f} | {amount_btc:.6f} | "
            f"SL: ${stop_loss_price:,.2f} ({effective_sl_pct:.2%}) | "
            f"TP: ${take_profit_price:,.2f} ({decision.take_profit_pct:.2%}) | "
            f"abiertas: {self.count_open()}/{self.settings.max_concurrent_trades}"
        )
        return position

    def maybe_take_partial_tp1(
        self, snapshot: MarketSnapshot, symbol: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Si el TP escalado está activo y el precio tocó TP1, cierra una fracción
        (tp1_size_pct del tamaño original), contabiliza la ganancia parcial y
        mueve el SL a breakeven. Devuelve un dict con el evento (para notificar)
        o None si no hubo parcial. El remanente sigue corriendo al TP completo.
        """
        symbol = symbol or self.settings.symbol
        if not self.settings.scaled_tp_enabled:
            return None
        if not self.has_position(symbol):
            return None
        pos = self.state.open_positions[symbol]
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
                    client_order_id=f"{pos['client_order_id']}_tp1", symbol=symbol,
                )
            except Exception as e:
                logger.error(f"Error al ejecutar TP1 parcial en {symbol}: {e}")
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
            f"🎯 TP1 PARCIAL {symbol} | cerré {portion_btc:.6f} @ ${price:,.2f} | "
            f"+${pnl:,.2f} | SL → breakeven ${pos['stop_loss']:,.2f}"
        )
        return {
            "symbol": symbol,
            "price": price,
            "portion_btc": portion_btc,
            "pnl_usdt": round(pnl, 2),
            "new_stop": pos["stop_loss"],
            "direction": direction,
        }

    def close_position(
        self, snapshot: MarketSnapshot, reason: str, symbol: Optional[str] = None,
    ) -> dict:
        symbol = symbol or self.settings.symbol
        if not self.has_position(symbol):
            return {}

        pos = self.get_open_position(symbol)
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
            "symbol": pos.symbol or symbol,
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
        self.state.open_positions.pop(symbol, None)
        self.state.last_close_at = time.time()
        self._check_daily_drawdown()
        self._save_state()

        win = total_pnl > 0
        emoji = "🟢" if win else "🔴"
        verb = "CIERRE LONG" if pos.direction == "LONG" else "CIERRE SHORT"
        tp1_note = f" (incluye +${pos.realized_pnl_usdt:.2f} de TP1)" if pos.tp1_done else ""
        logger.info(
            f"{emoji} {verb} {symbol} | ${current_price:,.2f} | P&L: {pnl_pct:+.2f}% "
            f"(${total_pnl:+.2f}){tp1_note} | {reason} | "
            f"abiertas: {self.count_open()}/{self.settings.max_concurrent_trades}"
        )
        return trade_record

    # ─────────────────────────────────────────
    #  RECONCILIACIÓN CON EL EXCHANGE (roadmap #4)
    # ─────────────────────────────────────────

    def reconcile(
        self, exchange_positions: dict, *, source: str = "startup",
        auto_heal: bool = True,
    ) -> dict:
        """
        Compara el estado local (open_positions) contra las posiciones REALES del
        exchange y resuelve las discrepancias (patrón RESUME / FRESH / orphan,
        inspirado en GRVTBot). `exchange_positions` = {symbol: signed_size}
        (positivo = LONG, negativo = SHORT; ausente/0 = plano en el exchange).

        Casos:
          - RESUME   : local abierto y el exchange coincide en signo → OK, seguimos.
          - DRIFT    : local abierto pero el exchange está plano → la posición se
                       cerró por afuera (SL/TP/liquidación/cierre manual mientras el
                       bot estaba caído). auto_heal → la sacamos del estado local.
          - MISMATCH : local y exchange en direcciones opuestas → grave. auto_heal
                       → confiamos en el exchange y soltamos la local.
          - ORPHAN   : el exchange tiene posición que el bot NO trackea → NUNCA la
                       adoptamos automáticamente (no sabemos su SL/TP). Avisamos.

        Devuelve un reporte. NO hace de fail-closed por sí mismo: el caller decide
        (en el startup, si el fetch del snapshot falla, debe abortar el arranque).
        """
        report = {"source": source, "resumed": [], "drifts": [],
                  "mismatches": [], "orphans": [], "ok": True}

        # 1) Revisar cada posición local contra el exchange.
        for symbol in self.open_symbols():
            pos = self.state.open_positions[symbol]
            direction = pos.get("direction", "LONG")
            ex_size = exchange_positions.get(symbol, 0.0)
            ex_dir = "LONG" if ex_size > 0 else ("SHORT" if ex_size < 0 else "FLAT")

            if ex_dir == "FLAT":
                report["drifts"].append(symbol)
                report["ok"] = False
                logger.warning(
                    f"🔧 RECONCILE [{source}] DRIFT: local cree {direction} en "
                    f"{symbol} pero el exchange está PLANO. "
                    f"{'Soltando posición local.' if auto_heal else 'Sin auto-heal.'}"
                )
                if auto_heal:
                    self._reconcile_drop(symbol, f"reconciliación [{source}]: ausente en exchange")
            elif ex_dir != direction:
                report["mismatches"].append(symbol)
                report["ok"] = False
                logger.critical(
                    f"🚨 RECONCILE [{source}] MISMATCH: local {direction} pero "
                    f"exchange {ex_dir} en {symbol}. "
                    f"{'Soltando local, confiar en exchange.' if auto_heal else 'Sin auto-heal.'}"
                )
                if auto_heal:
                    self._reconcile_drop(symbol, f"reconciliación [{source}]: dirección opuesta en exchange")
            else:
                report["resumed"].append(symbol)
                logger.info(f"✅ RECONCILE [{source}] RESUME: {symbol} {direction} coincide con el exchange.")

        # 2) Posiciones en el exchange que el bot no trackea → orphans.
        for symbol, size in exchange_positions.items():
            if size == 0:
                continue
            if not self.has_position(symbol):
                report["orphans"].append(symbol)
                report["ok"] = False
                logger.critical(
                    f"🚨 RECONCILE [{source}] ORPHAN: el exchange tiene "
                    f"{'LONG' if size > 0 else 'SHORT'} {abs(size)} en {symbol} que "
                    f"el bot NO trackea. NO la administro automáticamente — revisá manualmente."
                )

        if report["ok"]:
            logger.info(f"✅ RECONCILE [{source}]: estado local y exchange consistentes.")
        return report

    def _reconcile_drop(self, symbol: str, reason: str) -> None:
        """
        Saca una posición fantasma del estado local (dejó de existir en el
        exchange). Devuelve el notional al pool y registra en el journal con
        PnL marcado como desconocido (necesita revisión manual del operador).
        """
        pos = self.state.open_positions.pop(symbol, None)
        if pos is None:
            return
        # Best-effort: devolvemos el notional comprometido al pool. El PnL real
        # no se puede reconstruir sin los fills; queda flagueado para revisión.
        self.state.capital += float(pos.get("amount_usdt", 0.0))
        record = {
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": symbol,
            "direction": pos.get("direction", "LONG"),
            "entry_price": pos.get("entry_price"),
            "exit_price": pos.get("entry_price"),
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "exit_reason": reason,
            "reconciled": True,
            "pnl_unknown": True,
            "entry_reason": pos.get("entry_reason", ""),
        }
        self._write_journal(record)
        self._save_state()
        logger.warning(
            f"🔧 {symbol}: posición local soltada por reconciliación. "
            f"PnL real desconocido (revisar journal) | {reason}"
        )

    # ─────────────────────────────────────────
    #  HELPERS
    # ─────────────────────────────────────────

    def _calculate_position_size(
        self, snapshot: MarketSnapshot, decision: TradeDecision
    ) -> float:
        """
        Dimensionamiento sobre el POOL COMPARTIDO. Arriesga max_risk_per_trade
        del equity total del portafolio (capital libre + notional comprometido),
        así cada trade arriesga el mismo % sin importar cuántos haya abiertos.
        El tamaño se topea por lo que queda disponible dentro de la reserva 30%:
          disponible = equity*(1-reserve) - notional_ya_comprometido
        Si no queda lugar, devuelve 0 (la apertura se rechaza aguas arriba).
        """
        risk_pct = self._effective_risk_pct()
        basis = self._equity_basis()
        max_risk_usdt = basis * risk_pct
        atr_stop_pct = snapshot.atr_pct * self.settings.atr_sl_multiplier / 100
        effective_sl_pct = max(decision.stop_loss_pct, atr_stop_pct)
        if effective_sl_pct > 0:
            position_size = max_risk_usdt / effective_sl_pct
        else:
            position_size = max_risk_usdt * 10
        tradeable_total = basis * (1 - self.settings.trade_reserve_pct)
        # Cupo por trade: repartimos el capital tradeable entre los slots del
        # candado, así CABEN max_concurrent_trades posiciones dentro de la reserva
        # (sin esto, la primera se come todo el budget y el candado de 2 es inútil).
        slots = max(1, self.settings.max_concurrent_trades)
        per_trade_cap = tradeable_total / slots
        available = tradeable_total - self._committed_notional()
        return max(0.0, min(position_size, per_trade_cap, available))

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
        # Medimos sobre el EQUITY del portafolio (cash + notional comprometido),
        # no sobre el cash libre: con posiciones abiertas el capital descontado
        # no es una pérdida. Si no, cerrar 1 de N daría un falso drawdown.
        today = str(date.today())
        equity = self._equity_basis()
        if self.state.last_reset_date != today:
            self.state.daily_capital_start = equity
            self.state.last_reset_date = today
            self.state.is_stopped = False
            logger.info(f"Nuevo día | Equity inicial: ${equity:,.2f}")
        daily_dd = (
            (self.state.daily_capital_start - equity)
            / self.state.daily_capital_start
        )
        if daily_dd >= self.settings.daily_drawdown_limit:
            self.state.is_stopped = True
            logger.critical(
                f"🚨 DAILY DRAWDOWN: {daily_dd:.1%}. Bot detenido hasta mañana."
            )

    def get_open_position(self, symbol: Optional[str] = None) -> Optional[Position]:
        symbol = symbol or self.settings.symbol
        d = self.state.open_positions.get(symbol)
        return Position(**d) if d else None

    def get_stats(self) -> dict:
        win_rate = (
            self.state.winning_trades / self.state.total_trades * 100
            if self.state.total_trades > 0 else 0
        )
        # Reportamos sobre el equity del portafolio (cash libre + notional
        # comprometido), no sobre el cash a secas: con posiciones abiertas el
        # capital descontado no es una pérdida. (No incluye PnL no realizado.)
        equity = self._equity_basis()
        total_return = (
            (equity - self.state.capital_initial)
            / self.state.capital_initial * 100
        )
        max_drawdown = (
            max(0.0, (self.state.capital_peak - equity) / self.state.capital_peak * 100)
            if self.state.capital_peak > 0 else 0
        )
        return {
            "capital": round(equity, 2),
            "cash_free": round(self.state.capital, 2),
            "committed": round(self._committed_notional(), 2),
            "total_return_pct": round(total_return, 3),
            "total_trades": self.state.total_trades,
            "win_rate_pct": round(win_rate, 1),
            "max_drawdown_pct": round(max_drawdown, 3),
            "is_stopped": self.state.is_stopped,
            "open_position": self.count_open() > 0,
            "open_positions_count": self.count_open(),
            "open_symbols": self.open_symbols(),
            "max_concurrent_trades": self.settings.max_concurrent_trades,
        }

    # ─────────────────────────────────────────
    #  PERSISTENCIA
    # ─────────────────────────────────────────

    def _load_state(self) -> BotState:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                data = self._migrate_state(data)
                return BotState(**data)
            except Exception as e:
                logger.warning(f"No se pudo cargar state.json: {e}")

        initial = BotState(
            capital=self.settings.initial_capital,
            capital_initial=self.settings.initial_capital,
            capital_peak=self.settings.initial_capital,
            daily_capital_start=self.settings.initial_capital,
            last_reset_date=str(date.today()),
            open_positions={},
            total_trades=0, winning_trades=0,
            consecutive_failures=0, is_stopped=False,
        )
        self._save_state(initial)
        return initial

    @staticmethod
    def _migrate_state(data: dict) -> dict:
        """
        Migra el state.json viejo (single-symbol, clave `open_position`) al nuevo
        formato multi-symbol (`open_positions` indexado por símbolo). Idempotente.
        """
        if "open_positions" in data:
            data.pop("open_position", None)
            return data
        legacy = data.pop("open_position", None)
        positions: dict = {}
        if legacy:
            sym = legacy.get("symbol") or legacy.get("pair") or "BTC/USDT"
            legacy["symbol"] = sym
            positions[sym] = legacy
            logger.info(f"Migrando state.json a multi-symbol | posición legacy → {sym}")
        data["open_positions"] = positions
        return data

    def _save_state(self, state: Optional[BotState] = None) -> None:
        state = state or self.state
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(state), f, indent=2, default=str)

    def _write_journal(self, record: dict) -> None:
        with open(JOURNAL_FILE, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
