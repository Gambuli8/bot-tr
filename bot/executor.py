"""
Ejecutor: recibe una señal ya validada y decide/abre la operación en BingX.

Orden de controles para una entrada (cualquiera que falle → no se entra y se avisa):
  1. Señal duplicada (TradingView puede reenviar) → se ignora en silencio.
  2. Bot en pausa, par no habilitado, señal vieja.
  3. Ya hay posición en ese par / tope de posiciones abiertas / límite de pérdida diaria.
  4. Precio en vivo: slippage contra el precio de la señal y SL no cruzado.
  5. Sizing (margen fijo, apalancamiento mínimo, liquidación vs SL, R:R).
  6. Orden a mercado con SL/TP adjuntos → verificación de que la posición y el SL existen.
     Si el SL no quedó puesto y no se puede poner → se cierra la posición (nunca desnuda).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from bot.bingx import BingXClient, BingXError, Position
from bot.config import Settings
from bot.fmt import money, pct, price
from bot.narrator import Narrator
from bot.signals import Signal
from bot.sizing import build_plan, build_plan_fixed_risk
from bot.store import Store

log = logging.getLogger(__name__)


def fmt_qty(qty: float) -> str:
    """0.0001 → '0.0001', 26.0 → '26' (sin notación científica)."""
    return format(Decimal(str(qty)).normalize(), "f")


STOP_TYPES = {"STOP_MARKET", "STOP"}
TP_TYPES = {"TAKE_PROFIT_MARKET", "TAKE_PROFIT"}


class Executor:
    def __init__(self, settings: Settings, client: BingXClient, store: Store,
                 narrator: Narrator, notify: Callable[[str], None]):
        self.s = settings
        self.client = client
        self.store = store
        self.narrator = narrator
        self.notify = notify
        self._entry_lock = threading.Lock()

    # ───────── entrada pública ─────────

    def handle(self, sig: Signal) -> dict:
        key = f"{sig.id}:{sig.event}"
        if not self.store.mark_seen(key):
            log.info("Señal duplicada ignorada: %s", key)
            return {"status": "duplicate"}

        self.store.log_event("signal", **sig.model_dump(exclude={"secret"}))

        if sig.symbol not in self.s.symbols:
            reason = f"{sig.symbol} no está en la lista de pares habilitados"
            self.store.log_event("rejected", id=sig.id, reason=reason)
            return {"status": "rejected", "reason": reason}

        if sig.event != "entry":
            self._track_setup(sig)
            self.notify(self.narrator.setup_event(sig))
            return {"status": "narrated"}

        with self._entry_lock:
            return self._handle_entry(sig)

    # ───────── etapas de análisis ─────────

    @staticmethod
    def setup_key(sig: Signal) -> str:
        """El indicador sigue un único setup por par y dirección: la etapa nueva reemplaza a la anterior."""
        return f"{sig.symbol}:{sig.side}"

    def _track_setup(self, sig: Signal) -> None:
        if sig.event == "cancel":
            self.store.set_setup(self.setup_key(sig), None)
            return
        stage = {"zone": "en zona diaria, esperando cambio 1H",
                 "choch": "cambio 1H, esperando retroceso al 0,618",
                 "fib": "en 0,618, esperando gatillo 5m"}[sig.event]
        self.store.set_setup(self.setup_key(sig), {
            "id": sig.id, "symbol": sig.symbol, "side": sig.side, "stage": stage, "price": sig.price,
            "fib_618": sig.fib_618, "fib_75": sig.fib_75, "fib_sl": sig.fib_sl,
            "zone_low": sig.zone_low, "zone_high": sig.zone_high, "updated": time.time()})

    # ───────── entrada ─────────

    def _reject(self, sig: Signal, reason: str) -> dict:
        log.info("Entrada rechazada %s: %s", sig.id, reason)
        self.store.log_event("rejected", id=sig.id, symbol=sig.symbol, reason=reason)
        self.notify(self.narrator.entry_rejected(sig, reason))
        return {"status": "rejected", "reason": reason}

    def todays_realized_pnl(self) -> float:
        tz = ZoneInfo(self.s.timezone)
        start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return sum(t.get("pnl_usdt", 0.0) for t in self.store.closed_trades(int(start.timestamp() * 1000)))

    def _handle_entry(self, sig: Signal) -> dict:
        if self.store.paused:
            return self._reject(sig, "el bot está en pausa (/reanudar para activarlo)")

        if sig.time and self.s.max_signal_age_s > 0:
            age = time.time() - sig.time / 1000
            if age > self.s.max_signal_age_s:
                return self._reject(sig, f"la señal llegó tarde ({age:.0f} s de atraso, máximo {self.s.max_signal_age_s} s)")

        try:
            positions = self.client.positions()
        except BingXError as exc:
            return self._reject(sig, f"no pude consultar posiciones en BingX: {exc}")

        if any(p.symbol == sig.symbol for p in positions) or sig.symbol in self.store.open_trades:
            return self._reject(sig, f"ya hay una operación abierta en {sig.base_asset}")
        if len(positions) >= self.s.max_open_positions:
            return self._reject(sig, f"ya hay {len(positions)} operaciones abiertas (máximo {self.s.max_open_positions})")

        day_pnl = self.todays_realized_pnl()
        if day_pnl <= -abs(self.s.daily_loss_limit_usdt):
            return self._reject(sig, f"se alcanzó el límite de pérdida del día (hoy {money(day_pnl, True)}, límite {money(-abs(self.s.daily_loss_limit_usdt), True)})")

        try:
            spec = self.client.contract(sig.symbol)
            live = self.client.price(sig.symbol)
        except BingXError as exc:
            return self._reject(sig, f"no pude leer el mercado: {exc}")
        if not spec.api_open:
            return self._reject(sig, f"{sig.symbol} no admite órdenes por API ahora mismo")

        slippage = abs(live - sig.price) / sig.price * 100
        if slippage > self.s.max_slippage_pct:
            return self._reject(sig, f"el precio se movió {pct(slippage)} desde la señal "
                                     f"({price(sig.price)} → {price(live)}); máximo {pct(self.s.max_slippage_pct)}")

        common = dict(symbol=sig.symbol, direction=sig.side, entry=live, stop_loss=float(sig.sl),
                      take_profit=float(sig.tp), spec=spec, max_leverage=self.s.max_leverage, min_rr=self.s.min_rr)
        if self.s.sizing_mode == "risk":
            plan = build_plan_fixed_risk(risk_usdt=self.s.risk_per_trade_usdt, **common)
        else:
            plan = build_plan(margin_usdt=self.s.margin_per_trade_usdt, **common)
        if not plan.ok:
            return self._reject(sig, plan.reason)

        client_id = sig.client_order_id()
        try:
            self.client.set_margin_type(sig.symbol, self.s.margin_type)
            self.client.set_leverage(sig.symbol, plan.leverage, "BOTH")
            order = self.client.place_market_with_brackets(
                sig.symbol, plan.direction, plan.qty_str, plan.sl_str, plan.tp_str, client_id)
        except BingXError as exc:
            # Un timeout puede haber abierto igual la orden: se verifica por clientOrderID.
            order = self.client.order_by_client_id(sig.symbol, client_id) if exc.code == "network" else None
            if not order:
                self.store.log_event("entry_error", id=sig.id, symbol=sig.symbol, error=str(exc))
                self.notify(self.narrator.entry_failed(sig, str(exc)))
                return {"status": "error", "error": str(exc)}

        position = self._wait_position(sig.symbol)
        if position is None:
            msg = "BingX aceptó la orden pero no veo la posición abierta. Revisar manualmente."
            self.store.log_event("entry_error", id=sig.id, symbol=sig.symbol, error=msg, order=order)
            self.notify(self.narrator.entry_failed(sig, msg))
            return {"status": "error", "error": msg}

        self.ensure_protection(sig.symbol, position, plan.sl_str, plan.tp_str)

        record = {
            "id": sig.id, "symbol": sig.symbol, "direction": plan.direction,
            "opened_at": int(time.time() * 1000), "entry_price": position.entry_price or live,
            "signal_price": sig.price, "qty": position.qty, "leverage": plan.leverage,
            "margin_used": plan.margin_used, "notional": plan.notional,
            "stop_loss": plan.stop_loss, "take_profit": plan.take_profit,
            "sl_str": plan.sl_str, "tp_str": plan.tp_str,
            "risk_usdt": plan.risk_usdt, "reward_usdt": plan.reward_usdt, "rr": plan.rr,
            "order_id": str(order.get("orderId", "")) if isinstance(order, dict) else "",
            "mode": self.s.mode, "fib_618": sig.fib_618, "fib_75": sig.fib_75,
        }
        self.store.open_trade(sig.symbol, record)
        self.store.set_setup(self.setup_key(sig), None)
        self.store.log_event("opened", **record)
        self.notify(self.narrator.entry_opened(sig, plan, record["entry_price"]))
        return {"status": "opened", "trade": record}

    def _wait_position(self, symbol: str, timeout_s: float = 8.0) -> Optional[Position]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                for p in self.client.positions(symbol):
                    if p.symbol == symbol:
                        return p
            except BingXError as exc:
                log.warning("Esperando posición %s: %s", symbol, exc)
            time.sleep(1.0)
        return None

    # ───────── protección (también la usa el monitor) ─────────

    def ensure_protection(self, symbol: str, position: Position, sl_str: str, tp_str: str) -> bool:
        """Garantiza SL (y TP) en el exchange. Si el SL no se puede poner, cierra la posición."""
        try:
            orders = self.client.open_orders(symbol)
        except BingXError as exc:
            log.warning("No pude leer órdenes abiertas de %s: %s", symbol, exc)
            return True  # no se asume lo peor por un error de lectura; el monitor reintenta

        types = {str(o.get("type", "")).upper() for o in orders}
        qty = fmt_qty(position.qty)

        if not types & STOP_TYPES:
            try:
                self.client.place_protective_order(symbol, position.side, "SL", qty, sl_str)
                self.notify(self.narrator.alert(f"{symbol}: el Stop Loss no estaba puesto; lo coloqué en {sl_str}."))
                self.store.log_event("sl_replaced", symbol=symbol, stop=sl_str)
            except BingXError as exc:
                log.error("No pude poner SL en %s: %s → cierro", symbol, exc)
                try:
                    self.client.close_position_market(symbol, position.side, qty)
                    self.notify(self.narrator.alert(
                        f"{symbol}: no pude poner el Stop Loss ({exc}). Cerré la posición por seguridad.", critical=True))
                except BingXError as exc2:
                    self.notify(self.narrator.alert(
                        f"{symbol}: SIN STOP LOSS y no pude cerrar ({exc2}). ¡Cerrala a mano en BingX!", critical=True))
                self.store.log_event("unprotected", symbol=symbol, error=str(exc))
                return False

        if not types & TP_TYPES:
            try:
                self.client.place_protective_order(symbol, position.side, "TP", qty, tp_str)
                self.store.log_event("tp_replaced", symbol=symbol, take_profit=tp_str)
            except BingXError as exc:
                log.warning("No pude poner TP en %s: %s", symbol, exc)
        return True

    # ───────── cierre manual (/cerrar) ─────────

    def close_symbol(self, symbol: str) -> str:
        positions = [p for p in self.client.positions(symbol) if p.symbol == symbol]
        if not positions:
            return f"No hay posición abierta en {symbol}."
        pos = positions[0]
        trade = self.store.open_trades.get(symbol)
        if trade is not None:
            trade["manual_close"] = True
            self.store.save()
        self.client.close_position_market(symbol, pos.side, fmt_qty(pos.qty))
        try:
            self.client.cancel_all_orders(symbol)
        except BingXError as exc:
            log.warning("No pude cancelar órdenes de %s: %s", symbol, exc)
        self.store.log_event("manual_close", symbol=symbol)
        return f"Orden de cierre enviada para {symbol}. Te aviso el resultado en unos segundos."
