"""
notifications/telegram_listener.py
Listener de comandos por Telegram basado en long polling con requests.
- NO usa asyncio.
- Corre en un thread daemon, monitoreado por un watchdog.
- Soporta whitelist multi-chat con permisos full / read-only.
- Inline keyboards para confirmaciones de /close y /stop.
- Rate limiting por ventana deslizante.
- Audit log estructurado de cada comando en data/commands.jsonl.
"""

import _thread
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from logs.logger import logger
from config.settings import Settings
from core.bot_controller import BotController


POLL_TIMEOUT_SECONDS = 25                  # long polling de Telegram
RATE_LIMIT_WINDOW_SECONDS = 60.0           # ventana de rate limiting
RATE_LIMIT_MAX_COMMANDS = 20               # máximo por ventana
WATCHDOG_INTERVAL_SECONDS = 30             # cada cuánto chequea el thread
WATCHDOG_MAX_RESTARTS = 5                  # tope de reinicios para evitar loops

COMMANDS_AUDIT_FILE = Path(__file__).parent.parent / "data" / "commands.jsonl"

# Comandos que un read-only puede ejecutar.
READONLY_COMMANDS = {
    "/help",
    "/status", "/position", "/pnl", "/trades", "/logs", "/config", "/schedule",
}


def _parse_id_list(raw) -> set[str]:
    """
    Defensivo: acepta str con CSV, list/tuple/set de ids, o None.
    Devuelve siempre set[str] de IDs válidos (enteros).

    Endurecido (2026-06-13): los chat IDs de Telegram son enteros (negativos en
    grupos). Descartamos cualquier token que no sea entero. Esto neutraliza el
    caso real visto en producción donde el .env tenía un comentario inline
    (`TELEGRAM_ADMIN_CHAT_IDS=   # CSV opcional...`) que python-dotenv dejaba
    entrar como valor y terminaba en la whitelist como un "id" basura.
    """
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        if not isinstance(raw, str):
            raw = str(raw)
        items = raw.split(",")

    out: set[str] = set()
    for item in items:
        # Cortar comentario inline y espacios.
        token = item.split("#", 1)[0].strip()
        if not token:
            continue
        try:
            int(token)  # Telegram chat IDs son enteros (pueden ser negativos)
        except ValueError:
            continue
        out.add(token)
    return out


class TelegramListener:
    def __init__(self, settings: Settings, controller: BotController) -> None:
        self.settings = settings
        self.controller = controller
        self.token = settings.telegram_bot_token
        self.api_base = f"https://api.telegram.org/bot{self.token}"

        # Whitelist + permisos
        primary = str(settings.telegram_chat_id)
        self._admins: set[str] = _parse_id_list(
            settings.telegram_admin_chat_ids
        ) | {primary}
        self._readonly: set[str] = _parse_id_list(
            settings.telegram_readonly_chat_ids
        ) - self._admins
        # chat_id principal a donde mandar notificaciones espontáneas
        self.primary_chat_id = primary

        # Estado del poller
        self._offset: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._running: bool = False
        self._restart_count: int = 0

        # Rate limiting: timestamps de los últimos N comandos por chat_id
        self._cmd_timestamps: dict[str, list[float]] = {}
        self._rate_lock = threading.Lock()

    # ───────── ciclo de vida ─────────

    def start(self) -> None:
        if self._running:
            logger.warning("TelegramListener ya estaba corriendo")
            return
        self._running = True
        self._spawn_poller()
        self._spawn_watchdog()
        logger.info(
            f"TelegramListener arrancado | admins={sorted(self._admins)} "
            f"| readonly={sorted(self._readonly) or '∅'}"
        )

    def stop(self) -> None:
        """Apaga el listener limpiamente. Idempotente."""
        if not self._running:
            return
        logger.info("TelegramListener: stop solicitado")
        self._running = False
        # Si el polling está bloqueado, no hay forma limpia de cancelar el
        # request en curso, pero el daemon morirá con el proceso. El flag
        # _running detiene el bucle en la próxima iteración.

    def _spawn_poller(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="TgListener"
        )
        self._thread.start()

    def _spawn_watchdog(self) -> None:
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="TgListenerWatchdog"
        )
        self._watchdog.start()

    def _watchdog_loop(self) -> None:
        while self._running:
            time.sleep(WATCHDOG_INTERVAL_SECONDS)
            if not self._running:
                return
            t = self._thread
            if t is not None and t.is_alive():
                continue
            if self._restart_count >= WATCHDOG_MAX_RESTARTS:
                logger.critical(
                    f"Watchdog: límite de reinicios ({WATCHDOG_MAX_RESTARTS}) "
                    f"alcanzado, no reintento más"
                )
                self._notify_admin(
                    "🚨 <b>Listener Telegram caído</b>\n"
                    "Se alcanzó el límite de reinicios automáticos."
                )
                return
            self._restart_count += 1
            logger.warning(
                f"Watchdog: poller murió, reinicio #{self._restart_count}"
            )
            self._notify_admin(
                f"⚠️ Listener Telegram reiniciado (intento {self._restart_count})"
            )
            self._spawn_poller()

    def _run_loop(self) -> None:
        try:
            self._drain_old_updates()
        except Exception as e:
            logger.warning(f"Listener: no pude limpiar updates viejos: {e}")

        while self._running:
            try:
                for update in self._get_updates():
                    self._handle_update(update)
            except requests.exceptions.RequestException as e:
                logger.warning(f"Listener Telegram: error red ({e}). Reintento en 5s.")
                time.sleep(5)
            except Exception as e:
                logger.error(
                    f"Listener Telegram: error inesperado: {e}", exc_info=True
                )
                time.sleep(5)

    def _drain_old_updates(self) -> None:
        r = requests.get(
            f"{self.api_base}/getUpdates",
            params={"timeout": 0},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("result", [])
        if results:
            self._offset = results[-1]["update_id"] + 1

    def _get_updates(self) -> list[dict]:
        params: dict = {
            "timeout": POLL_TIMEOUT_SECONDS,
            # callback_query es lo que mandan los inline keyboards
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if self._offset is not None:
            params["offset"] = self._offset
        r = requests.get(
            f"{self.api_base}/getUpdates",
            params=params,
            timeout=POLL_TIMEOUT_SECONDS + 5,
        )
        r.raise_for_status()
        results = r.json().get("result", [])
        if results:
            self._offset = results[-1]["update_id"] + 1
        return results

    # ───────── dispatch ─────────

    def _handle_update(self, update: dict) -> None:
        # 1) Botones inline
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return

        # 2) Mensajes de texto / comandos
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat", {})
        text = (msg.get("text") or "").strip()
        chat_id = str(chat.get("id", ""))

        if not text.startswith("/"):
            return

        permission = self._permission_for(chat_id)
        if permission is None:
            logger.warning(
                f"Comando ignorado de chat no autorizado: {chat_id} -> {text[:40]}"
            )
            return

        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]

        # Rate limit por chat
        if not self._allow_rate(chat_id):
            self._send(
                "⚠️ Demasiados comandos. Esperá unos segundos.",
                chat_id=chat_id,
            )
            self._audit(command, args, chat_id, "rate_limited")
            return

        # Permisos
        if permission == "readonly" and command not in READONLY_COMMANDS:
            self._send(
                f"🔒 Tu chat tiene permisos read-only. "
                f"<code>{command}</code> no está habilitado.",
                chat_id=chat_id,
            )
            self._audit(command, args, chat_id, "forbidden")
            return

        logger.info(
            f"Comando Telegram: {command} | args={args} | chat={chat_id} | perm={permission}"
        )
        self._ack(command, chat_id)

        try:
            self._process_command(command, args, chat_id)
            self._audit(command, args, chat_id, "ok")
        except Exception as e:
            logger.error(f"Error procesando {command}: {e}", exc_info=True)
            self._send(
                f"❌ Error procesando {command}: {str(e)[:200]}",
                chat_id=chat_id,
            )
            self._audit(command, args, chat_id, f"error:{type(e).__name__}")

    def _process_command(
        self, command: str, args: list[str], chat_id: str
    ) -> None:
        handlers = {
            "/help": self._cmd_help,
            "/start": self._cmd_resume,    # /start = encender (alias de /resume)
            "/on": self._cmd_resume,       # idem
            "/off": self._cmd_pause,       # /off = apagar (alias de /pause)
            "/status": self._cmd_status,
            "/position": self._cmd_position,
            "/pnl": self._cmd_pnl,
            "/trades": self._cmd_trades,
            "/logs": self._cmd_logs,
            "/config": self._cmd_config,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/close": self._cmd_close,
            "/close_confirm": self._cmd_close_confirm,
            "/stop": self._cmd_stop,
            "/stop_confirm": self._cmd_stop_confirm,
            "/schedule": self._cmd_schedule,
        }
        handler = handlers.get(command)
        if handler is None:
            self._send(
                f"Comando desconocido: <code>{command}</code>. Usá /help.",
                chat_id=chat_id,
            )
            return
        handler(args, chat_id)

    def _handle_callback(self, cq: dict) -> None:
        cq_id = cq.get("id", "")
        data = cq.get("data", "")
        chat = (cq.get("message") or {}).get("chat", {})
        chat_id = str(chat.get("id", ""))
        permission = self._permission_for(chat_id)

        # Respondemos siempre al callback (Telegram lo exige)
        try:
            requests.post(
                f"{self.api_base}/answerCallbackQuery",
                json={"callback_query_id": cq_id},
                timeout=5,
            )
        except Exception:
            pass

        if permission is None:
            logger.warning(f"Callback ignorado de chat no autorizado: {chat_id}")
            return
        if permission == "readonly":
            self._send(
                "🔒 Acción no permitida para read-only.", chat_id=chat_id
            )
            return

        if data == "close:confirm":
            self.controller.request_force_close()
            self._send(
                "🔴 Cierre confirmado (botón). Ejecutando ahora mismo.",
                chat_id=chat_id,
            )
            self._audit("/close_confirm", ["inline"], chat_id, "ok")
        elif data == "close:cancel":
            self._send("✖️ Cierre cancelado.", chat_id=chat_id)
        elif data == "stop:confirm":
            self._send(
                "🛑 Deteniendo el bot (botón)...", chat_id=chat_id
            )
            logger.warning(
                "Stop solicitado por inline button. Interrumpiendo main thread."
            )
            self._audit("/stop_confirm", ["inline"], chat_id, "ok")
            _thread.interrupt_main()
        elif data == "stop:cancel":
            self._send("✖️ Stop cancelado.", chat_id=chat_id)

    # ───────── handlers ─────────

    def _cmd_help(self, args: list[str], chat_id: str) -> None:
        text = (
            "<b>🤖 Comandos disponibles</b>\n\n"
            "<b>Información (mirar)</b>\n"
            "/status — capital, ganancia/pérdida, win rate\n"
            "/position — operación abierta con P&amp;L en vivo\n"
            "/pnl — P&amp;L 24h / 7d / total\n"
            "/trades [N] — últimas N operaciones (default 5)\n"
            "/logs [N] — últimas N líneas del log\n"
            "/config — settings activos\n\n"
            "<b>Encender / apagar</b>\n"
            "/on (o /start) — empieza a buscar oportunidades\n"
            "/off (o /pause) — para de abrir, mantiene la abierta\n"
            "/schedule [HH-HH] — horario activo en UTC (ej: 12-2 = 12 UTC a 2 UTC)\n\n"
            "<b>Cerrar manualmente</b>\n"
            "/close → /close_confirm (válido 30s)\n\n"
            "<b>Apagar el proceso</b>\n"
            "/stop → /stop_confirm (válido 30s)"
        )
        self._send(text, chat_id=chat_id)

    def _cmd_status(self, args: list[str], chat_id: str) -> None:
        om = self.controller.order_manager
        if om is None:
            self._send("Todavía estoy arrancando, dame un toque.", chat_id=chat_id)
            return
        stats = om.get_stats()
        is_paused = self.controller.is_paused
        ret = stats["total_return_pct"]

        # Estado en una línea
        if is_paused:
            estado = "⏸ <b>Pausado</b> — no abro operaciones nuevas"
        elif stats["is_stopped"]:
            estado = "🛑 <b>Frenado por caída del día</b> — vuelve mañana"
        else:
            estado = "▶️ <b>Andando</b> — buscando oportunidades"

        # Resumen plata
        if ret > 0:
            ret_line = f"📈 Ganamos un <b>{ret:+.2f}%</b> arriba del capital inicial"
        elif ret < 0:
            ret_line = f"📉 Vamos perdiendo <b>{ret:.2f}%</b> del capital inicial"
        else:
            ret_line = f"➖ Estamos parejos (sin ganancia ni pérdida todavía)"

        pos_line = "💼 Operación abierta: <b>sí</b>" if stats["open_position"] else "💼 Operación abierta: <b>no</b>"

        text = (
            f"{estado}\n\n"
            f"💵 Plata ahora: <b>${stats['capital']:,.2f}</b>\n"
            f"{ret_line}\n"
            f"🎯 Acertamos en <b>{stats['win_rate_pct']:.0f}%</b> de las operaciones (de {stats['total_trades']} totales)\n"
            f"📉 Mayor bajón: <b>{stats['max_drawdown_pct']:.2f}%</b>\n"
            f"{pos_line}\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text, chat_id=chat_id)

    def _cmd_position(self, args: list[str], chat_id: str) -> None:
        om = self.controller.order_manager
        if om is None or om.state.open_position is None:
            self._send("Por ahora no tengo nada abierto. Estoy mirando el mercado.", chat_id=chat_id)
            return

        pos = om.state.open_position
        direction = pos.get("direction", "LONG")
        entry = float(pos["entry_price"])
        amt_btc = float(pos["amount_btc"])
        sl = float(pos["stop_loss"])
        tp = float(pos["take_profit"])
        invertido = entry * amt_btc
        entry_time = pos.get("entry_time", "")

        try:
            t0 = datetime.fromisoformat(entry_time.replace("Z", ""))
            mins = int((datetime.utcnow() - t0).total_seconds() // 60)
            hh, mm = divmod(mins, 60)
            elapsed = f"{hh}h {mm}m" if hh else f"{mm}m"
        except Exception:
            elapsed = "n/d"

        # P&L actual
        pnl_usdt = pnl_pct = None
        current = None
        try:
            if self.controller.exchange is not None:
                df = self.controller.exchange.get_ohlcv()
                current = float(df["close"].iloc[-1])
                if direction == "LONG":
                    pnl_usdt = (current - entry) * amt_btc
                    pnl_pct = (current - entry) / entry * 100
                else:
                    pnl_usdt = (entry - current) * amt_btc
                    pnl_pct = (entry - current) / entry * 100
        except Exception as e:
            logger.warning(f"No pude obtener precio actual para /position: {e}")

        # Línea de estado
        if pnl_usdt is None:
            estado = "<i>(no pude leer el precio actual)</i>"
        elif pnl_usdt > 0:
            estado = f"🎉 <b>Vamos ganando +${pnl_usdt:,.2f}</b> ({pnl_pct:+.2f}%)"
        elif pnl_usdt < 0:
            estado = f"😬 <b>Vamos perdiendo -${abs(pnl_usdt):,.2f}</b> ({pnl_pct:+.2f}%)"
        else:
            estado = f"➖ Justito por ahí, sin ganar ni perder"

        # Cuánto si toca TP/SL
        if direction == "SHORT":
            ganancia_tp = (entry - tp) * amt_btc
            perdida_sl = (sl - entry) * amt_btc
            dir_humano = "🔻 Aposté a que <b>baja</b>"
            txt_tp = f"si BAJA a <b>${tp:,.2f}</b>"
            txt_sl = f"si SUBE a <b>${sl:,.2f}</b>"
        else:
            ganancia_tp = (tp - entry) * amt_btc
            perdida_sl = (entry - sl) * amt_btc
            dir_humano = "🟢 Aposté a que <b>sube</b>"
            txt_tp = f"si SUBE a <b>${tp:,.2f}</b>"
            txt_sl = f"si BAJA a <b>${sl:,.2f}</b>"

        trailing_line = ""
        if pos.get("trailing_active"):
            trailing_line = "🎯 <i>Trailing activado: el stop sigue al precio para asegurar ganancia.</i>\n"

        precio_actual_line = (
            f"💵 Precio ahora: <b>${current:,.2f}</b>\n" if current is not None else ""
        )

        text = (
            f"{dir_humano}\n\n"
            f"📍 Compré a: <b>${entry:,.2f}</b>\n"
            f"💸 Invertí: <b>${invertido:,.2f}</b> ({amt_btc:.6f} BTC)\n"
            f"{precio_actual_line}"
            f"{estado}\n\n"
            f"🎯 Ganamos +${ganancia_tp:,.2f} {txt_tp}\n"
            f"🛑 Perdemos -${perdida_sl:,.2f} {txt_sl}\n"
            f"⏳ Lleva abierta: <b>{elapsed}</b>\n"
            f"{trailing_line}\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text, chat_id=chat_id)

    def _cmd_pnl(self, args: list[str], chat_id: str) -> None:
        trades = self._load_journal()
        if not trades:
            self._send("Sin operaciones cerradas todavía.", chat_id=chat_id)
            return

        now = datetime.utcnow()
        bucket_24h = 0.0
        bucket_7d = 0.0
        total = 0.0
        wins_24h = losses_24h = 0
        for t in trades:
            pnl = float(t.get("pnl", 0))
            total += pnl
            try:
                ts = datetime.fromisoformat(
                    t.get("exit_time", t.get("timestamp", "")).replace("Z", "")
                )
                delta = (now - ts).total_seconds()
                if delta <= 24 * 3600:
                    bucket_24h += pnl
                    if pnl > 0:
                        wins_24h += 1
                    else:
                        losses_24h += 1
                if delta <= 7 * 24 * 3600:
                    bucket_7d += pnl
            except Exception:
                pass

        def sign(x: float) -> str:
            return f"+${x:,.2f}" if x >= 0 else f"-${abs(x):,.2f}"

        text = (
            f"<b>💵 P&amp;L</b>\n\n"
            f"📅 24h: <b>{sign(bucket_24h)}</b> (W:{wins_24h} L:{losses_24h})\n"
            f"📆 7d:  <b>{sign(bucket_7d)}</b>\n"
            f"💰 Total: <b>{sign(total)}</b>\n"
            f"📋 Trades cerrados: <b>{len(trades)}</b>\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M UTC')}"
        )
        self._send(text, chat_id=chat_id)

    def _cmd_trades(self, args: list[str], chat_id: str) -> None:
        n = self._parse_int_arg(args, default=5, lo=1, hi=20)
        trades = self._load_journal()[-n:]
        if not trades:
            self._send("Sin operaciones registradas.", chat_id=chat_id)
            return
        lines = [f"<b>📋 ÚLTIMOS {len(trades)} TRADES</b>\n"]
        for t in trades:
            pnl = float(t.get("pnl", 0))
            pnl_pct = float(t.get("pnl_pct", 0))
            emoji = "🟢" if pnl > 0 else "🔴"
            ts = t.get("exit_time", "")[:16].replace("T", " ")
            lines.append(
                f"{emoji} {ts} | {pnl:+.2f} USDT ({pnl_pct:+.2f}%) "
                f"| {str(t.get('exit_reason', ''))[:40]}"
            )
        self._send("\n".join(lines), chat_id=chat_id)

    def _cmd_logs(self, args: list[str], chat_id: str) -> None:
        n = self._parse_int_arg(args, default=20, lo=1, hi=100)
        log_file = Path(__file__).parent.parent / "data" / "bot.log"
        if not log_file.exists():
            self._send("No hay log todavía.", chat_id=chat_id)
            return
        try:
            with log_file.open("r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-n:]
        except Exception as e:
            self._send(f"Error leyendo log: {e}", chat_id=chat_id)
            return
        body = "".join(tail).strip()
        if len(body) > 3500:
            body = body[-3500:]
        self._send(
            f"<b>📜 Últimas {len(tail)} líneas</b>\n<pre>{self._escape(body)}</pre>",
            chat_id=chat_id,
        )

    def _cmd_config(self, args: list[str], chat_id: str) -> None:
        s = self.settings
        text = (
            f"<b>⚙️ CONFIG ACTIVO</b>\n\n"
            f"Par: <b>{s.symbol}</b> @ <b>{s.timeframe}</b>\n"
            f"Testnet: <b>{s.binance_testnet}</b>\n"
            f"Capital inicial: <b>${s.initial_capital:,.2f}</b>\n"
            f"Riesgo/trade: <b>{s.max_risk_per_trade:.2%}</b>\n"
            f"Drawdown diario: <b>{s.daily_drawdown_limit:.2%}</b>\n"
            f"Confianza mín. Claude: <b>{s.min_claude_confidence:.0%}</b>\n"
            f"Trailing: <b>{s.trailing_stop_enabled}</b> | "
            f"activa en <b>{s.trailing_activation_pct:.2%}</b> | "
            f"distancia <b>{s.trailing_distance_pct:.2%}</b>"
        )
        self._send(text, chat_id=chat_id)

    def _cmd_pause(self, args: list[str], chat_id: str) -> None:
        if not self.controller.pause():
            self._send("El bot ya estaba pausado.", chat_id=chat_id)
            return
        self._send(
            "⏸ <b>Bot PAUSADO</b>. No se abrirán nuevas posiciones. "
            "SL/TP siguen activos.",
            chat_id=chat_id,
        )
        notifier = self.controller.telegram
        if notifier is not None:
            try:
                notifier.notify_paused()
            except Exception as e:
                logger.warning(f"notify_paused falló: {e}")

    def _cmd_resume(self, args: list[str], chat_id: str) -> None:
        if not self.controller.resume():
            self._send("El bot ya estaba activo.", chat_id=chat_id)
            return
        self._send("▶️ <b>Bot REACTIVADO</b>.", chat_id=chat_id)
        notifier = self.controller.telegram
        if notifier is not None:
            try:
                notifier.notify_resumed()
            except Exception as e:
                logger.warning(f"notify_resumed falló: {e}")

    def _cmd_close(self, args: list[str], chat_id: str) -> None:
        om = self.controller.order_manager
        if om is None or om.state.open_position is None:
            self._send("No hay posición abierta para cerrar.", chat_id=chat_id)
            return
        self.controller.request_confirmation("close")
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Confirmar cierre", "callback_data": "close:confirm"},
                {"text": "✖️ Cancelar", "callback_data": "close:cancel"},
            ]]
        }
        self._send(
            "⚠️ Vas a <b>cerrar la posición a mercado</b>.\n"
            "Tocá un botón o mandá <code>/close_confirm</code> en 30s.",
            chat_id=chat_id,
            reply_markup=keyboard,
        )

    def _cmd_close_confirm(self, args: list[str], chat_id: str) -> None:
        if not self.controller.check_confirmation("close"):
            self._send(
                "❌ No hay <code>/close</code> pendiente o pasó el timeout (30s). "
                "Mandá <code>/close</code> primero.",
                chat_id=chat_id,
            )
            return
        self.controller.request_force_close()
        self._send(
            "🔴 Cierre confirmado. Ejecutando ahora mismo (sin esperar al próximo ciclo).",
            chat_id=chat_id,
        )

    def _cmd_stop(self, args: list[str], chat_id: str) -> None:
        self.controller.request_confirmation("stop")
        keyboard = {
            "inline_keyboard": [[
                {"text": "🛑 Confirmar stop", "callback_data": "stop:confirm"},
                {"text": "✖️ Cancelar", "callback_data": "stop:cancel"},
            ]]
        }
        self._send(
            "⚠️ Vas a <b>detener el bot</b>.\n"
            "Tocá un botón o mandá <code>/stop_confirm</code> en 30s.",
            chat_id=chat_id,
            reply_markup=keyboard,
        )

    def _cmd_schedule(self, args: list[str], chat_id: str) -> None:
        """
        /schedule           → ver el horario actual
        /schedule 11-23     → activar de 11 UTC a 23 UTC
        /schedule 22-3,9-12 → multi-rango (acepta cruzar medianoche)
        /schedule off       → desactivar (volver a 24/7)
        /schedule 24/7      → idem
        """
        from datetime import datetime as _dt
        current = self.controller.get_active_hours()
        if not args:
            now_utc = _dt.utcnow().strftime("%H:%M")
            if not current:
                txt = (
                    f"📅 <b>Horario activo: 24/7</b> (siempre operando)\n\n"
                    f"Ahora son las <b>{now_utc} UTC</b>.\n\n"
                    f"Para activar horario:\n"
                    f"<code>/schedule 12-2</code> → 12 UTC a 2 UTC del día siguiente\n"
                    f"<code>/schedule off</code> → 24/7"
                )
            else:
                txt = (
                    f"📅 <b>Horario activo: {current} UTC</b>\n\n"
                    f"Ahora son las <b>{now_utc} UTC</b>.\n"
                    f"<i>Recordá: AR = UTC-3</i>\n\n"
                    f"Cambiarlo: <code>/schedule HH-HH</code> o <code>/schedule off</code>"
                )
            self._send(txt, chat_id=chat_id)
            return

        arg = args[0].lower().strip()
        if arg in ("off", "24/7", "always", "all"):
            self.controller.set_active_hours("")
            self._send("📅 <b>Schedule desactivado</b> — opero 24/7.", chat_id=chat_id)
            return

        # Validar formato
        valid = True
        for r in arg.split(","):
            if "-" not in r:
                valid = False
                break
            try:
                a, b = r.split("-", 1)
                a, b = int(a), int(b)
                if not (0 <= a <= 23 and 0 <= b <= 23):
                    valid = False
                    break
            except ValueError:
                valid = False
                break
        if not valid:
            self._send(
                "❌ Formato inválido. Usá <code>HH-HH</code> en UTC.\n"
                "Ejemplos: <code>/schedule 12-2</code>, <code>/schedule 8-22</code>, "
                "<code>/schedule off</code>.",
                chat_id=chat_id,
            )
            return

        self.controller.set_active_hours(arg)
        self._send(
            f"📅 <b>Schedule activado: {arg} UTC</b>\n\n"
            f"<i>AR = UTC-3, ej. 12 UTC = 9 AM AR.</i>\n"
            f"Fuera de ese horario duermo y no consumo recursos.\n"
            f"Adentro: opero normal.",
            chat_id=chat_id,
        )

    def _cmd_stop_confirm(self, args: list[str], chat_id: str) -> None:
        if not self.controller.check_confirmation("stop"):
            self._send(
                "❌ No hay <code>/stop</code> pendiente o pasó el timeout (30s). "
                "Mandá <code>/stop</code> primero.",
                chat_id=chat_id,
            )
            return
        self._send("🛑 Deteniendo el bot...", chat_id=chat_id)
        logger.warning("Stop solicitado por Telegram. Interrumpiendo main thread.")
        _thread.interrupt_main()

    # ───────── auth / rate limit / audit ─────────

    def _permission_for(self, chat_id: str) -> Optional[str]:
        """Devuelve 'admin', 'readonly' o None."""
        if chat_id in self._admins:
            return "admin"
        if chat_id in self._readonly:
            return "readonly"
        return None

    def _allow_rate(self, chat_id: str) -> bool:
        now = time.time()
        with self._rate_lock:
            times = self._cmd_timestamps.setdefault(chat_id, [])
            cutoff = now - RATE_LIMIT_WINDOW_SECONDS
            times[:] = [t for t in times if t > cutoff]
            if len(times) >= RATE_LIMIT_MAX_COMMANDS:
                return False
            times.append(now)
            return True

    def _audit(
        self, command: str, args: list[str], chat_id: str, result: str
    ) -> None:
        try:
            COMMANDS_AUDIT_FILE.parent.mkdir(exist_ok=True)
            record = {
                "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "command": command,
                "args": args,
                "chat_id": chat_id,
                "result": result,
            }
            with COMMANDS_AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"Audit log falló: {e}")

    # ───────── helpers ─────────

    def _load_journal(self) -> list[dict]:
        journal = Path(__file__).parent.parent / "data" / "trade_journal.jsonl"
        if not journal.exists():
            return []
        try:
            lines = journal.read_text(encoding="utf-8").strip().splitlines()
            return [json.loads(l) for l in lines if l.strip()]
        except Exception as e:
            logger.warning(f"No pude leer journal: {e}")
            return []

    def _parse_int_arg(
        self, args: list[str], default: int, lo: int, hi: int
    ) -> int:
        if not args:
            return default
        try:
            n = int(args[0])
            return max(lo, min(hi, n))
        except ValueError:
            return default

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # ───────── envío ─────────

    def _ack(self, command: str, chat_id: str) -> None:
        notifier = self.controller.telegram
        if notifier is not None and chat_id == self.primary_chat_id:
            try:
                notifier.notify_command_received(command)
                return
            except Exception as e:
                logger.warning(f"notify_command_received falló, uso fallback: {e}")
        self._send(f"✅ Recibido: <code>{command}</code>", chat_id=chat_id)

    def _notify_admin(self, text: str) -> None:
        """Envía al chat principal (no a la whitelist entera)."""
        self._send(text, chat_id=self.primary_chat_id)

    def _send(
        self,
        text: str,
        chat_id: Optional[str] = None,
        reply_markup: Optional[dict] = None,
    ) -> None:
        target = chat_id or self.primary_chat_id
        payload: dict = {
            "chat_id": target,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            r = requests.post(
                f"{self.api_base}/sendMessage",
                json=payload,
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning(
                    f"Listener: error send {r.status_code}: {r.text[:200]}"
                )
        except Exception as e:
            logger.warning(f"Listener: error enviando Telegram: {e}")
