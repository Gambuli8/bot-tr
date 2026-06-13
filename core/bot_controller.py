"""
core/bot_controller.py
Estado compartido entre el listener de Telegram (thread daemon)
y la estrategia principal (thread main).
Todo acceso a estado mutable pasa por threading.Lock.
"""

import json
import threading
import time
from pathlib import Path
from typing import Optional

from logs.logger import logger

CONFIRMATION_TTL_SECONDS = 30

# Persistimos sólo flags propios del controller. El resto (capital, posición)
# vive en data/state.json a cargo del OrderManager.
STATE_FILE = Path(__file__).parent.parent / "data" / "controller_state.json"


class BotController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._is_paused: bool = False
        self._force_close_position: bool = False
        self._reconcile_requested: bool = False
        self._pending_confirmations: dict[str, float] = {}
        self._active_hours_utc: str = ""  # override en runtime via /schedule

        # Event para despertar el sleep entre ciclos del main loop.
        # Lo usa el listener cuando pide un cierre manual, así no hay que
        # esperar al próximo run_once natural.
        self._wake_event = threading.Event()

        # Referencias registradas por la estrategia para que el listener
        # pueda responder /status, /position y cerrar a precio actual.
        self.order_manager = None
        self.exchange = None
        self.telegram = None

        # Restaurar pausa persistida (si existe).
        self._load_persisted_state()

    def register_components(self, order_manager, exchange, telegram) -> None:
        with self._lock:
            self.order_manager = order_manager
            self.exchange = exchange
            self.telegram = telegram

    # ───────── pausa / resume ─────────

    def pause(self) -> bool:
        with self._lock:
            if self._is_paused:
                return False
            self._is_paused = True
            self._save_persisted_state_locked()
            return True

    def resume(self) -> bool:
        with self._lock:
            if not self._is_paused:
                return False
            self._is_paused = False
            self._save_persisted_state_locked()
            return True

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._is_paused

    # ───────── cierre forzado ─────────

    def request_force_close(self) -> None:
        with self._lock:
            self._force_close_position = True
        # Despertamos el main loop si está esperando entre ciclos.
        self._wake_event.set()

    # ───────── reconcile inmediato (user-stream watcher) ─────────

    def request_reconcile(self) -> None:
        """
        Pide un reconcile inmediato y despierta el main loop.

        Lo usa el UserStreamWatcher (WebSocket) cuando detecta que un SL/TP se
        ejecutó en el exchange: en vez de esperar el throttle de 5 min, forzamos
        que el próximo run_once reconcilie ya. El cierre local + la notificación
        de Telegram salen por el camino normal de run_once (should_close_any →
        _exchange_closed_us), que corre en el main thread → sin races.
        """
        with self._lock:
            self._reconcile_requested = True
        self._wake_event.set()

    def consume_reconcile(self) -> bool:
        """Devuelve True una sola vez si se pidió reconcile, y resetea el flag."""
        with self._lock:
            if self._reconcile_requested:
                self._reconcile_requested = False
                return True
            return False

    # ───────── sincronización con el main loop ─────────

    def wait_for_next_cycle(self, timeout: float) -> bool:
        """
        Espera hasta `timeout` segundos o hasta que alguien llame a wake().
        Devuelve True si fue despertado, False si fue por timeout natural.
        Después de retornar, limpia el flag para la próxima espera.
        """
        woken = self._wake_event.wait(timeout=timeout)
        self._wake_event.clear()
        return woken

    def wake(self) -> None:
        self._wake_event.set()

    # ───────── schedule (horas activas UTC) ─────────

    def get_active_hours(self) -> str:
        with self._lock:
            return self._active_hours_utc

    def set_active_hours(self, spec: str) -> None:
        """spec="" → siempre activo. "11-23" o "11-23,2-5"."""
        with self._lock:
            self._active_hours_utc = (spec or "").strip()
            self._save_persisted_state_locked()

    # ───────── persistencia ─────────

    def _load_persisted_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self._is_paused = bool(data.get("is_paused", False))
            self._active_hours_utc = str(data.get("active_hours_utc", "") or "")
            if self._is_paused:
                logger.info("BotController: is_paused=True restaurado desde disco")
            if self._active_hours_utc:
                logger.info(f"BotController: schedule restaurado = {self._active_hours_utc}")
        except Exception as e:
            logger.warning(f"BotController: no pude leer {STATE_FILE.name}: {e}")

    def _save_persisted_state_locked(self) -> None:
        """Asume que el lock ya está adquirido."""
        try:
            STATE_FILE.parent.mkdir(exist_ok=True)
            STATE_FILE.write_text(
                json.dumps({
                    "is_paused": self._is_paused,
                    "active_hours_utc": self._active_hours_utc,
                }, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"BotController: no pude persistir state: {e}")

    def consume_force_close(self) -> bool:
        """Devuelve True una sola vez si se pidió cierre, y resetea el flag."""
        with self._lock:
            if self._force_close_position:
                self._force_close_position = False
                return True
            return False

    @property
    def force_close_position(self) -> bool:
        with self._lock:
            return self._force_close_position

    # ───────── confirmaciones con timeout ─────────

    def request_confirmation(self, key: str) -> None:
        with self._lock:
            self._pending_confirmations[key] = time.time()

    def check_confirmation(
        self, key: str, ttl: int = CONFIRMATION_TTL_SECONDS
    ) -> bool:
        """
        Consume la confirmación 'key' si existe y no expiró.
        Si pasó el TTL o no existe, devuelve False.
        """
        with self._lock:
            ts: Optional[float] = self._pending_confirmations.pop(key, None)
            if ts is None:
                return False
            return (time.time() - ts) <= ttl
