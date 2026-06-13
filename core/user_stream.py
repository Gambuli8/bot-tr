"""
core/user_stream.py
Watcher del USER DATA STREAM de Binance Futures USDT-M (WebSocket) vía ccxt.pro.

POR QUÉ EXISTE (no es para "tradear más rápido"):
El bot ya coloca SL/TP como órdenes reduceOnly REALES en el exchange (ver
execution/order_manager.py), así que la protección de riesgo ya es en tiempo
real a nivel Binance. Lo que este watcher mejora es la CONSISTENCIA DEL ESTADO
INTERNO: cuando un SL/TP se ejecuta en el exchange, el bot normalmente no se
entera hasta el próximo ciclo (hasta 15 min) o el reconcile periódico (hasta
5 min). En esa ventana el bot "cree" que sigue con la posición abierta:
  - no libera el cupo de max_concurrent_trades,
  - la alerta de cierre a Telegram llega tarde,
  - el capital interno queda desfasado,
  - con el motor scalping (5m) se pueden perder oportunidades.

Este watcher escucha los updates de órdenes y, ante un fill/cierre/cancelación,
sólo PIDE UN RECONCILE INMEDIATO (controller.request_reconcile()), que despierta
el main loop. El cierre local y la notificación de Telegram salen por el camino
normal de run_once (should_close_any → _exchange_closed_us), que corre en el
main thread. Este thread NO muta estado del OrderManager → cero hazards de
concurrencia nuevos.

DISEÑO FAIL-SAFE:
  - Corre en un thread daemon con su propio event loop asyncio.
  - Único efecto: controller.request_reconcile().
  - Si el WS se cae, reconecta con backoff exponencial; mientras tanto el
    reconcile periódico de 5 min sigue siendo la red de seguridad.
  - Es PURO ENHANCEMENT: opt-in vía env USER_STREAM_ENABLED=true. Apagado por
    default → el bot funciona idéntico a hoy.
"""

import asyncio
import threading
from typing import Optional

from logs.logger import logger
from config.settings import Settings


# Estados de orden (ccxt unificado) que implican que algo cambió en el exchange
# y conviene reconciliar el estado local.
_TRIGGER_STATUSES = {"closed", "filled", "canceled", "cancelled"}


class UserStreamWatcher:
    """
    Escucha el user data stream de Binance Futures y dispara un reconcile
    inmediato en el bot ante cualquier fill/cierre de orden.
    """

    def __init__(self, settings: Settings, controller) -> None:
        self.settings = settings
        self.controller = controller
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ───────── ciclo de vida ─────────

    def start(self) -> None:
        if self._thread is not None:
            logger.warning("UserStreamWatcher ya estaba arrancado; ignoro start()")
            return
        self._thread = threading.Thread(
            target=self._run, name="user-stream", daemon=True
        )
        self._thread.start()
        logger.info("📡 UserStreamWatcher arrancado (WebSocket user data stream)")

    def stop(self) -> None:
        self._stop.set()

    # ───────── thread + event loop ─────────

    def _run(self) -> None:
        """Target del thread: corre un event loop asyncio propio."""
        try:
            asyncio.run(self._loop())
        except Exception as e:
            logger.error(
                f"UserStreamWatcher murió: {e}. El reconcile periódico (5 min) "
                f"sigue como red de seguridad."
            )

    async def _loop(self) -> None:
        """Loop de conexión con reconexión y backoff exponencial."""
        try:
            import ccxt.pro as ccxtpro  # ccxt.pro viene incluido en ccxt 4.x
        except Exception as e:
            logger.error(
                f"No pude importar ccxt.pro ({e}). UserStreamWatcher deshabilitado; "
                f"el reconcile periódico sigue funcionando."
            )
            return

        backoff = 1.0
        max_backoff = 60.0
        symbol = self.settings.symbol

        while not self._stop.is_set():
            exchange = ccxtpro.binance({
                "apiKey": self.settings.binance_api_key,
                "secret": self.settings.binance_api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "future"},
            })
            if self.settings.binance_testnet:
                exchange.set_sandbox_mode(True)

            try:
                logger.info(f"📡 user-stream conectado | watch_orders({symbol})")
                while not self._stop.is_set():
                    orders = await exchange.watch_orders(symbol)
                    backoff = 1.0  # conexión sana → reset del backoff
                    self._handle_orders(orders)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    f"user-stream WS error: {e}; reconectando en {backoff:.0f}s"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            finally:
                try:
                    await exchange.close()
                except Exception:
                    pass

    # ───────── manejo de eventos ─────────

    def _handle_orders(self, orders: list) -> None:
        """
        Procesa una tanda de updates de órdenes. Si alguna terminó
        (filled/closed/canceled), pide un reconcile inmediato.

        Método puro y sincrónico a propósito: es el único punto testeable sin
        WebSocket real.
        """
        if not orders:
            return
        for order in orders:
            status = (order.get("status") or "").lower()
            if status in _TRIGGER_STATUSES:
                ident = order.get("clientOrderId") or order.get("id") or "?"
                logger.info(
                    f"⚡ Fill/cierre detectado por WS: orden {ident} status={status} "
                    f"→ pido reconcile inmediato"
                )
                try:
                    self.controller.request_reconcile()
                except Exception as e:
                    logger.warning(f"No pude pedir reconcile tras fill WS: {e}")
                # Un solo request por tanda alcanza: el reconcile barre todo.
                return
