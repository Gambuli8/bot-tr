"""
strategies/main_strategy.py
Orquestador principal del ciclo de trading.
Soporta MODO TESTING (sin Claude, reglas técnicas).
"""

import time
from datetime import datetime, date
from typing import Optional
from logs.logger import logger
from config.settings import Settings
from core.exchange import ExchangeClient
from core.indicators import IndicatorEngine
from core.claude_agent import ClaudeAgent
from core.technical_engine import TechnicalEngine
from execution.order_manager import OrderManager
from notifications.telegram import TelegramNotifier


class MainStrategy:
    def __init__(self, settings: Settings, controller=None):
        self.settings = settings
        self.controller = controller
        self.exchange = ExchangeClient(settings)
        self.indicator_engine = IndicatorEngine(settings)

        # Elegir motor de decisión según modo
        if settings.testing_mode:
            self.agent = TechnicalEngine(settings)
            logger.warning("⚠️  MODO TESTING ACTIVO — operando sin Claude")
        else:
            self.agent = ClaudeAgent(settings)
            logger.info("Modo producción — usando Claude API")

        self.order_manager = OrderManager(settings, exchange_client=None)
        self.telegram = TelegramNotifier(settings)
        self._last_report_date = None

        # Registrar componentes en el controller para que el listener pueda
        # responder /status, /position, /pause, /close, etc.
        if self.controller is not None:
            try:
                self.controller.register_components(
                    order_manager=self.order_manager,
                    exchange=self.exchange,
                    telegram=self.telegram,
                )
            except Exception as e:
                logger.warning(f"No pude registrar componentes en el controller: {e}")

        # Panorama periódico (cada 30 min). El diagnóstico de ciclo legacy queda
        # apagado salvo para señales fuertes — el listener recibe sólo aperturas,
        # cierres y este panorama.
        self._last_panorama_at: float = 0.0
        self._panorama_interval_seconds: int = 30 * 60

        # Schedule: tracking del último estado para detectar transiciones.
        # Si el controller no tiene schedule seteado, fallback al settings.
        if self.controller is not None and not self.controller.get_active_hours():
            if settings.active_hours_utc:
                self.controller.set_active_hours(settings.active_hours_utc)
        self._was_schedule_paused: Optional[bool] = None

        logger.info("MainStrategy inicializada")

    def run_once(self) -> dict:
        """
        Un ciclo del SCANNER LOOP: recorre los símbolos de settings.symbols de
        forma SECUENCIAL (no asyncio) para no saturar la API ni arriesgar bans.
        ccxt ya serializa con enableRateLimit. Cada símbolo se evalúa y opera de
        forma independiente; el candado de exposición global vive en el OrderManager.
        """
        cycle_result = {
            "timestamp": datetime.utcnow().isoformat(),
            "action": "ESPERAR", "reason": "", "error": None,
        }

        try:
            # Schedule: detectar transición y notificar.
            self._check_schedule_transition()

            # 0. ¿Pedido(s) de cierre manual via Telegram? (puede targetear símbolos)
            if self.controller is not None:
                close_targets = self.controller.consume_force_close()
                if close_targets:
                    self._handle_force_close(close_targets, cycle_result)
                    return cycle_result

            # SLEEP PROFUNDO: pausados (manual o schedule) y SIN posiciones abiertas.
            schedule_paused = self._is_schedule_paused()
            manual_paused = (
                self.controller is not None and self.controller.is_paused
            )
            if (manual_paused or schedule_paused) and self.order_manager.count_open() == 0:
                why = "pausado por /off" if manual_paused else "fuera de horario activo"
                cycle_result["action"] = "ESPERAR"
                cycle_result["reason"] = f"Dormido ({why}), sin posiciones abiertas"
                logger.debug(f"Sleep profundo: {why}")
                return cycle_result

            if hasattr(self.agent, 'is_safe_mode') and self.agent.is_safe_mode:
                self.telegram.notify_critical(
                    "Circuit breaker de Claude activado. Bot en modo SAFE."
                )

            # ── SCANNER: símbolo por símbolo, secuencial ──
            per_symbol: list[dict] = []
            actions: list[str] = []
            for symbol in self.settings.symbols:
                info = self._process_symbol(symbol, manual_paused, schedule_paused)
                if info is None:
                    continue
                per_symbol.append(info)
                if info["action"] != "ESPERAR":
                    actions.append(f"{symbol}:{info['action']}")

            # Panorama de PORTAFOLIO cada 30 min (un solo mensaje con las N monedas).
            now_ts = time.time()
            if (now_ts - self._last_panorama_at) >= self._panorama_interval_seconds:
                try:
                    stats = self.order_manager.get_stats()
                    self.telegram.notify_multi_panorama(stats, per_symbol)
                    self._last_panorama_at = now_ts
                except Exception as e:
                    logger.warning(f"No pude mandar panorama: {e}")

            stats = self.order_manager.get_stats()
            cycle_result["action"] = "OPERANDO" if actions else "ESPERAR"
            cycle_result["reason"] = ", ".join(actions) if actions else "sin señales accionables"
            logger.info(
                f"Ciclo completado | {len(self.settings.symbols)} símbolos | "
                f"{cycle_result['reason']} | Equity: ${stats['capital']:,.2f} | "
                f"Abiertas: {stats['open_positions_count']}/{stats['max_concurrent_trades']} | "
                f"Trades: {stats['total_trades']} | WR: {stats['win_rate_pct']:.1f}%"
            )

            self._maybe_send_daily_report(stats)

        except Exception as e:
            logger.error(f"Error inesperado en ciclo: {e}", exc_info=True)
            cycle_result["error"] = str(e)
            self.telegram.notify_warning(f"Error en ciclo: {str(e)[:200]}")

        return cycle_result

    def _process_symbol(
        self, symbol: str, manual_paused: bool, schedule_paused: bool,
    ) -> Optional[dict]:
        """
        Evalúa y opera UN símbolo: data → decisión → gestión (TP1/cierre) → entrada.
        La entrada queda sujeta al candado global dentro de order_manager.should_open.
        Devuelve un resumen para el panorama, o None si falló la lectura de datos.
        """
        try:
            df = self.exchange.get_ohlcv(symbol)
            snapshot = self.indicator_engine.calculate(df)
            # Aseguramos que el snapshot sepa de qué símbolo es (IndicatorEngine
            # usa settings.symbol como default).
            try:
                snapshot.symbol = symbol
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[{symbol}] No pude leer/computar datos: {e}")
            return None

        mtf = None
        if self.settings.require_mtf_confluence:
            try:
                mtf = self.exchange.get_mtf_context(symbol)
            except Exception as e:
                logger.warning(f"[{symbol}] No pude traer MTF context: {e}")

        trade_history = self._load_recent_trades(symbol)
        try:
            decision = self.agent.analyze(snapshot, trade_history, mtf=mtf)
        except TypeError:
            decision = self.agent.analyze(snapshot, trade_history)

        action = "ESPERAR"
        reason = decision.razon

        # TP escalado: parcial en TP1 + breakeven, antes de evaluar el cierre.
        try:
            partial = self.order_manager.maybe_take_partial_tp1(snapshot, symbol)
            if partial:
                self.telegram.notify_partial_tp(
                    price=partial["price"], portion_btc=partial["portion_btc"],
                    pnl_usdt=partial["pnl_usdt"], new_stop=partial["new_stop"],
                    direction=partial["direction"], symbol=symbol,
                )
        except Exception as e:
            logger.warning(f"[{symbol}] No pude procesar TP1 parcial: {e}")

        # ¿Cerrar?
        should_close, close_reason = self.order_manager.should_close(snapshot, decision, symbol)
        if should_close:
            pos_before = self.order_manager.state.open_positions.get(symbol, {})
            direction_closed = pos_before.get("direction", "LONG")
            trade = self.order_manager.close_position(snapshot, close_reason, symbol)
            self.telegram.notify_sell(
                price=snapshot.price, pnl_usdt=trade.get("pnl", 0),
                pnl_pct=trade.get("pnl_pct", 0), reason=close_reason,
                direction=direction_closed, symbol=symbol,
            )
            action, reason = f"CERRAR_{direction_closed}", close_reason

        elif manual_paused:
            reason = "bot pausado (/off)"
        elif schedule_paused:
            reason = "fuera del horario activo"

        # ¿Abrir? (sujeto al candado global)
        elif self.order_manager.should_open(decision, snapshot, symbol):
            position = self.order_manager.open_position(snapshot, decision, symbol)
            if position:
                self.telegram.notify_buy(
                    price=position.entry_price, amount_btc=position.amount_btc,
                    stop_loss=position.stop_loss, take_profit=position.take_profit,
                    reason=decision.razon, confidence=position.claude_confidence,
                    direction=position.direction, symbol=symbol,
                )
                action, reason = f"ABRIR_{position.direction}", decision.razon

        return {
            "symbol": symbol, "price": snapshot.price, "action": action,
            "reason": reason, "decision": decision.accion,
            "has_position": self.order_manager.has_position(symbol),
        }

    def _handle_force_close(self, targets: list[str], cycle_result: dict) -> None:
        """Cierra a mercado los símbolos pedidos por Telegram ('*' = todos)."""
        syms: list[str] = []
        for t in targets:
            if t == "*":
                syms.extend(self.order_manager.open_symbols())
            else:
                syms.append(t)
        closed = []
        for sym in dict.fromkeys(syms):   # de-dup preservando orden
            if not self.order_manager.has_position(sym):
                continue
            try:
                df = self.exchange.get_ohlcv(sym)
                snapshot = self.indicator_engine.calculate(df)
                reason = "Cierre manual via Telegram"
                direction_closed = self.order_manager.state.open_positions[sym].get(
                    "direction", "LONG"
                )
                trade = self.order_manager.close_position(snapshot, reason, sym)
                self.telegram.notify_sell(
                    price=snapshot.price, pnl_usdt=trade.get("pnl", 0),
                    pnl_pct=trade.get("pnl_pct", 0), reason=reason,
                    direction=direction_closed, symbol=sym,
                )
                closed.append(sym)
            except Exception as e:
                logger.error(f"[{sym}] Error en cierre manual: {e}")
        if closed:
            cycle_result["action"] = "CERRAR_MANUAL"
            cycle_result["reason"] = f"cerrados: {', '.join(closed)}"
            logger.info(f"Ciclo abreviado: cierre manual ejecutado ({', '.join(closed)})")
        else:
            logger.info("Force close pedido pero no había posiciones que cerrar")

    def run_forever(self, interval_seconds: int = 900) -> None:
        # En modo testing, override el intervalo
        if self.settings.testing_mode:
            interval_seconds = self.settings.testing_loop_seconds
            mode = f"TESTING (sin Claude, cada {interval_seconds}s)"
        else:
            mode = f"PAPER TRADING (Claude, cada {interval_seconds}s)"

        logger.info(f"🚀 Bot arrancado | {mode}")
        self.telegram.notify_bot_started(mode)

        try:
            while True:
                start = time.time()
                self.run_once()
                elapsed = time.time() - start
                sleep_time = max(0, interval_seconds - elapsed)
                logger.debug(f"Próximo ciclo en {sleep_time:.0f}s")
                # Si hay controller, dormimos sobre un Event para que
                # /close_confirm pueda despertar el loop antes del timeout.
                if self.controller is not None:
                    if self.controller.wait_for_next_cycle(sleep_time):
                        logger.info("Loop despertado por evento externo")
                else:
                    time.sleep(sleep_time)
        except KeyboardInterrupt:
            logger.info("Bot detenido por el usuario (Ctrl+C)")
            self.telegram.notify_bot_stopped("Interrupción manual")
        except Exception as e:
            logger.critical(f"Error fatal en loop: {e}", exc_info=True)
            self.telegram.notify_critical(f"Error fatal — bot detenido:\n{str(e)[:300]}")
            raise

    def _is_schedule_paused(self) -> bool:
        """
        Devuelve True si la hora UTC actual está fuera del rango activo.
        El spec viene del controller (puede haberlo cambiado /schedule).
        Formato: "" (siempre activo), "11-23", "11-23,2-5".
        Soporta ventana que cruza medianoche (ej. "22-3" = 22:00 → 03:00).
        """
        spec = ""
        if self.controller is not None:
            spec = self.controller.get_active_hours()
        if not spec:
            return False
        hour_utc = datetime.utcnow().hour
        for raw in spec.split(","):
            r = raw.strip()
            if "-" not in r:
                continue
            try:
                a_str, b_str = r.split("-", 1)
                a, b = int(a_str), int(b_str)
            except ValueError:
                continue
            if a == b:
                continue
            if a < b:
                if a <= hour_utc < b:
                    return False
            else:  # wraps midnight, ej. 22-3
                if hour_utc >= a or hour_utc < b:
                    return False
        return True

    def _check_schedule_transition(self) -> None:
        """Notifica cuando entramos/salimos de la ventana activa."""
        now_paused = self._is_schedule_paused()
        if self._was_schedule_paused is None:
            self._was_schedule_paused = now_paused
            return
        if now_paused == self._was_schedule_paused:
            return
        spec = self.controller.get_active_hours() if self.controller else ""
        if now_paused:
            self.telegram.notify_warning(
                f"⏰ Estoy fuera del horario activo ({spec} UTC). "
                f"Me voy a dormir hasta que vuelva a empezar."
            )
        else:
            self.telegram.notify_warning(
                f"⏰ Empezó mi horario activo ({spec} UTC). "
                f"Vuelvo a buscar oportunidades."
            )
        self._was_schedule_paused = now_paused

    def _load_recent_trades(self, symbol: Optional[str] = None) -> list[dict]:
        from pathlib import Path
        journal_path = Path(__file__).parent.parent / "data" / "trade_journal.jsonl"
        if not journal_path.exists():
            return []
        try:
            import json
            lines = journal_path.read_text().strip().split("\n")
            trades = [json.loads(line) for line in lines if line.strip()]
            if symbol is not None:
                # Trades viejos sin "symbol" se asumen del símbolo primario.
                trades = [
                    t for t in trades
                    if t.get("symbol", self.settings.symbol) == symbol
                ]
            return trades[-10:]
        except Exception as e:
            logger.warning(f"No se pudo cargar journal: {e}")
            return []

    def _maybe_send_daily_report(self, stats: dict) -> None:
        today = str(date.today())
        if self._last_report_date != today:
            hour = datetime.utcnow().hour
            if hour == 0:
                self.telegram.notify_daily_report(stats)
                self._last_report_date = today
