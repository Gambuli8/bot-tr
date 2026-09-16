"""
Telegram: envío de mensajes + comandos (long polling, sin librerías extra).

Comandos (sólo admins):
  /estado            saldo, modo, pausa, posiciones abiertas y setups en análisis
  /hoy               operaciones y resultado del día
  /semana, /mes      resumen parcial del período en curso
  /pausa, /reanudar  frena / reactiva nuevas entradas (las abiertas siguen con su SL/TP)
  /cerrar BTC        cierra la posición (pide confirmación: /cerrar BTC si)
  /ayuda
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

import requests

from bot.config import Settings, normalize_symbol
from bot.narrator import esc, px, side_word, usd

if TYPE_CHECKING:
    from bot.bingx import BingXClient
    from bot.executor import Executor
    from bot.reports import Reporter
    from bot.store import Store

log = logging.getLogger(__name__)
MAX_LEN = 4000


class TelegramBot:
    def __init__(self, settings: Settings):
        self.s = settings
        self.api = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        self._offset: Optional[int] = None
        self._running = False
        # Se inyectan después (evita dependencias circulares).
        self.client: Optional["BingXClient"] = None
        self.store: Optional["Store"] = None
        self.executor: Optional["Executor"] = None
        self.reporter: Optional["Reporter"] = None

    # ───────── envío ─────────

    def send(self, text: str, chat_id: Optional[str] = None) -> None:
        if not self.s.telegram_enabled:
            log.info("[telegram deshabilitado] %s", text)
            return
        for chunk in [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [""]:
            try:
                resp = requests.post(f"{self.api}/sendMessage", json={
                    "chat_id": chat_id or self.s.telegram_chat_id, "text": chunk,
                    "parse_mode": "HTML", "disable_web_page_preview": True,
                }, timeout=10)
                if resp.status_code != 200:
                    log.warning("Telegram %s: %s", resp.status_code, resp.text[:200])
                    # Plan B: sin formato, para que el aviso llegue igual.
                    requests.post(f"{self.api}/sendMessage", json={
                        "chat_id": chat_id or self.s.telegram_chat_id, "text": chunk}, timeout=10)
            except requests.RequestException as exc:
                log.warning("Error enviando Telegram: %s", exc)

    # ───────── polling ─────────

    def start_listener(self) -> None:
        if not self.s.telegram_enabled:
            return
        self._running = True
        threading.Thread(target=self._loop, name="telegram", daemon=True).start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        try:  # descartar comandos viejos acumulados mientras el bot estaba apagado
            r = requests.get(f"{self.api}/getUpdates", params={"timeout": 0}, timeout=10).json()
            if r.get("result"):
                self._offset = r["result"][-1]["update_id"] + 1
        except (requests.RequestException, ValueError) as exc:
            log.warning("Telegram: no pude limpiar updates viejos: %s", exc)

        while self._running:
            try:
                params = {"timeout": 25, "allowed_updates": json.dumps(["message"])}
                if self._offset is not None:
                    params["offset"] = self._offset
                data = requests.get(f"{self.api}/getUpdates", params=params, timeout=30).json()
                if not data.get("ok"):
                    log.warning("Telegram getUpdates: %s", data)
                    time.sleep(5)
                    continue
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    self._handle(update)
            except (requests.RequestException, ValueError) as exc:
                log.warning("Telegram polling: %s", exc)
                time.sleep(5)
            except Exception:
                log.exception("Telegram: error procesando comando")
                time.sleep(2)

    def _handle(self, update: dict) -> None:
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        user_id = str((msg.get("from") or {}).get("id", ""))
        if not text.startswith("/"):
            return
        if chat_id not in self.s.telegram_admin_ids and user_id not in self.s.telegram_admin_ids:
            log.warning("Comando de chat no autorizado %s: %s", chat_id, text[:40])
            return
        parts = text.split()
        command = parts[0].split("@")[0].lower()
        args = parts[1:]
        handler = {
            "/estado": self._cmd_estado, "/status": self._cmd_estado,
            "/hoy": self._cmd_hoy,
            "/semana": self._cmd_semana, "/mes": self._cmd_mes,
            "/pausa": self._cmd_pausa, "/pause": self._cmd_pausa,
            "/reanudar": self._cmd_reanudar, "/resume": self._cmd_reanudar,
            "/cerrar": self._cmd_cerrar,
            "/ayuda": self._cmd_ayuda, "/start": self._cmd_ayuda, "/help": self._cmd_ayuda,
        }.get(command)
        if handler is None:
            self.send("No conozco ese comando. Probá /ayuda", chat_id)
            return
        if self.store is not None:
            self.store.log_event("command", command=command, args=args, chat_id=chat_id)
        try:
            handler(args, chat_id)
        except Exception as exc:
            log.exception("Fallo el comando %s", command)
            self.send(f"⚠️ Error ejecutando {esc(command)}: {esc(exc)}", chat_id)

    # ───────── comandos ─────────

    def _cmd_ayuda(self, args, chat_id) -> None:
        self.send(
            "🤖 <b>Comandos</b>\n"
            "/estado — saldo, operaciones abiertas y qué estoy analizando\n"
            "/hoy — resultado del día\n"
            "/semana — resumen de la semana en curso\n"
            "/mes — resumen del mes en curso\n"
            "/pausa — no abro nuevas operaciones (las abiertas siguen protegidas)\n"
            "/reanudar — vuelvo a operar\n"
            "/cerrar BTC — cierro la operación de ese par", chat_id)

    def _cmd_estado(self, args, chat_id) -> None:
        assert self.client and self.store
        lines = [f"📊 <b>Estado</b>  <i>[{self.s.mode_label}]</i>",
                 f"Nuevas entradas: {'⏸️ EN PAUSA' if self.store.paused else '▶️ activas'}"]
        try:
            bal = self.client.balance()
            lines.append(f"Saldo: {usd(bal['balance'])} · Disponible: {usd(bal['available'])} · "
                         f"PnL abierto: {usd(bal['unrealized_pnl'], signed=True)}")
        except Exception as exc:
            lines.append(f"Saldo: no disponible ({esc(exc)})")

        try:
            positions = self.client.positions()
        except Exception as exc:
            positions = []
            lines.append(f"Posiciones: no disponible ({esc(exc)})")
        lines.append("")
        if positions:
            lines.append(f"<b>Operaciones abiertas ({len(positions)}):</b>")
            for p in positions:
                trade = self.store.open_trades.get(p.symbol, {})
                lines.append(
                    f"• {esc(p.symbol.split('-')[0])} {side_word(p.side)} desde {px(p.entry_price)} → "
                    f"ahora {px(p.mark_price)} · <b>{usd(p.unrealized_pnl, signed=True)}</b>\n"
                    f"   SL {px(trade.get('stop_loss'))} · TP {px(trade.get('take_profit'))} · {p.leverage:.0f}x")
        else:
            lines.append("Sin operaciones abiertas.")

        setups = self.store.state.get("setups", {})
        lines.append("")
        if setups:
            lines.append("<b>Analizando:</b>")
            for info in setups.values():
                extra = f" (0.618 {px(info.get('fib_618'))} · 0.75 {px(info.get('fib_75'))})" if info.get("fib_618") else ""
                lines.append(f"• {esc(info['symbol'].split('-')[0])} {info['side']}: {esc(info['stage'])}{extra}")
        else:
            lines.append("Ningún setup en curso: esperando que el precio llegue a una zona diaria.")
        self.send("\n".join(lines), chat_id)

    def _cmd_hoy(self, args, chat_id) -> None:
        assert self.store
        tz = ZoneInfo(self.s.timezone)
        start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        trades = self.store.closed_trades(int(start.timestamp() * 1000))
        if not trades:
            self.send("Hoy todavía no cerré operaciones.", chat_id)
            return
        total = sum(t["pnl_usdt"] for t in trades)
        lines = [f"📅 <b>Hoy</b>: {len(trades)} operaciones · <b>{usd(total, signed=True)}</b>"]
        for t in trades:
            lines.append(f"• {esc(t['symbol'].split('-')[0])} {t['direction']} {t.get('exit_reason', '')}: "
                         f"{usd(t['pnl_usdt'], signed=True)}")
        self.send("\n".join(lines), chat_id)

    def _cmd_semana(self, args, chat_id) -> None:
        assert self.reporter
        from bot.reports import telegram_text
        period = self.reporter.current_week()
        stats, _ = self.reporter.build(period)
        self.send(telegram_text(period, stats, self.s.mode_label, None), chat_id)

    def _cmd_mes(self, args, chat_id) -> None:
        assert self.reporter
        from bot.reports import telegram_text
        period = self.reporter.current_month()
        stats, _ = self.reporter.build(period)
        self.send(telegram_text(period, stats, self.s.mode_label, None), chat_id)

    def _cmd_pausa(self, args, chat_id) -> None:
        assert self.store
        self.store.update(paused=True)
        self.send("⏸️ Pausado: no abro nuevas operaciones. Las abiertas siguen con su SL y TP.", chat_id)

    def _cmd_reanudar(self, args, chat_id) -> None:
        assert self.store
        self.store.update(paused=False)
        self.send("▶️ Reanudado: vuelvo a operar cuando aparezca un setup.", chat_id)

    def _cmd_cerrar(self, args, chat_id) -> None:
        assert self.executor
        if not args:
            self.send("Indicá el par, por ejemplo: /cerrar BTC", chat_id)
            return
        raw = args[0].upper()
        symbol = normalize_symbol(raw if "USDT" in raw else raw + "-USDT")
        if len(args) < 2 or args[1].lower() not in ("si", "sí", "yes"):
            self.send(f"¿Seguro que querés cerrar {esc(symbol)} a mercado? Confirmá con: "
                      f"/cerrar {esc(raw)} si", chat_id)
            return
        self.send(esc(self.executor.close_symbol(symbol)), chat_id)
