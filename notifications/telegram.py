"""
notifications/telegram.py
Notificaciones en tiempo real via Telegram.
Versión con manejo robusto de event loops (httpx síncrono).
"""

from datetime import datetime
import requests
from logs.logger import logger
from config.settings import Settings


class TelegramNotifier:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        # Símbolo base para los mensajes (ej. "BTC/USDT" → "BTC"). Permite que
        # cuando hay varios bots multi-asset, cada uno reporte con su propio
        # ticker en vez del "BTC" hardcoded.
        self.base_asset = (settings.symbol.split("/")[0] if settings.symbol else "BTC")
        logger.info("TelegramNotifier inicializado")

    def _send(self, text: str) -> None:
        """Envío síncrono con requests — sin asyncio."""
        try:
            response = requests.post(
                self.api_url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            if response.status_code != 200:
                logger.warning(f"Telegram error {response.status_code}: {response.text[:200]}")
        except Exception as e:
            logger.warning(f"Error enviando Telegram: {e}")

    # ─────────────────────────────────────────
    #  DIAGNÓSTICO DE CICLO
    # ─────────────────────────────────────────

    def notify_cycle_diagnostic(self, snapshot, decision) -> None:
        # Pasada cada ciclo. Resumen corto para no inundar el chat.
        action_emoji = {
            "COMPRAR": "🟢",
            "VENDER": "🔻",
            "ESPERAR": "💤",
        }.get(decision.accion, "💤")

        trend_emoji = {"BULL": "📈 subiendo", "BEAR": "📉 bajando", "LATERAL": "➡️ lateral"}.get(
            snapshot.trend, snapshot.trend
        )

        if decision.accion == "ESPERAR":
            titulo = f"{action_emoji} Por ahora me quedo quieto"
            extra = f"<i>Lo que veo:</i> {decision.razon[:160]}"
        elif decision.accion == "COMPRAR":
            titulo = f"{action_emoji} Veo oportunidad de COMPRAR (subir)"
            extra = f"<i>Motivo:</i> {decision.razon[:160]}"
        else:
            titulo = f"{action_emoji} Veo oportunidad de VENDER (bajar)"
            extra = f"<i>Motivo:</i> {decision.razon[:160]}"

        text = (
            f"{titulo}\n\n"
            f"💵 {self.base_asset}: <b>${snapshot.price:,.2f}</b>  "
            f"(1h: {snapshot.price_change_1h:+.2f}% | 24h: {snapshot.price_change_24h:+.2f}%)\n"
            f"{trend_emoji}  •  confianza {decision.confianza:.0%}\n\n"
            f"{extra}\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    # ─────────────────────────────────────────
    #  OTRAS NOTIFICACIONES
    # ─────────────────────────────────────────

    def notify_buy(
        self, price, amount_btc, stop_loss, take_profit,
        reason, confidence, direction: str = "LONG",
    ):
        notional = price * amount_btc
        lev = getattr(self.settings, "leverage", 1)
        margin = getattr(self.settings, "margin_mode", "")
        sl_pct = abs(stop_loss - price) / price * 100
        tp_pct = abs(take_profit - price) / price * 100
        if direction == "SHORT":
            emoji = "🔻"
            sl_amt = (stop_loss - price) * amount_btc      # pérdida si toca SL
            tp_amt = (price - take_profit) * amount_btc     # ganancia si toca TP
        else:
            emoji = "🟢"
            sl_amt = (price - stop_loss) * amount_btc
            tp_amt = (take_profit - price) * amount_btc

        text = (
            f"{emoji} <b>{direction} ABIERTO — {self.base_asset}/USDT</b>\n\n"
            f"<pre>\n"
            f"Entrada      ${price:,.2f}\n"
            f"Tamaño       ${notional:,.2f} ({amount_btc:.6f} {self.base_asset}) · {lev}x {margin}\n"
            f"Stop-loss    ${stop_loss:,.2f}  -{sl_pct:.2f}%  (-${sl_amt:,.2f})\n"
            f"Take-profit  ${take_profit:,.2f}  +{tp_pct:.2f}%  (+${tp_amt:,.2f})\n"
            f"</pre>\n"
            f"📝 {reason}\n"
            f"🧠 Confianza: {confidence:.0%}  ·  🕐 {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    def notify_sell(
        self, price, pnl_usdt, pnl_pct, reason, direction: str = "LONG",
    ):
        win = pnl_usdt > 0
        emoji = "✅" if win else "🔴"
        label = "ganancia" if win else "pérdida"

        text = (
            f"{emoji} <b>CIERRE {direction} — {self.base_asset}/USDT</b> ({label})\n\n"
            f"<pre>\n"
            f"Resultado    {pnl_usdt:+,.2f} USDT ({pnl_pct:+.2f}%)\n"
            f"Salida       ${price:,.2f}\n"
            f"</pre>\n"
            f"📝 {reason}\n"
            f"🕐 {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    def notify_trailing_activated(self, current_price, profit_pct):
        text = (
            f"🎯 <b>¡Activé el trailing stop!</b>\n\n"
            f"Ya vamos ganando <b>+{profit_pct:.2%}</b> a ${current_price:,.2f}.\n"
            f"<i>De acá en adelante el stop sube con el precio para asegurar la ganancia.</i>\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    def notify_risk_alert(self, message, severity: str = "warning"):
        head = (
            "🚨 <b>ALERTA DE RIESGO</b>" if severity == "critical"
            else "⚠️ <b>Atención — riesgo</b>"
        )
        text = f"{head}\n\n{message}\n\n🕐 {datetime.utcnow().strftime('%H:%M')} UTC"
        self._send(text)

    def notify_warning(self, message):
        text = f"⚠️ <b>Atención</b>\n\n{message}\n\n⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        self._send(text)

    def notify_critical(self, message):
        text = f"🚨 <b>Algo grave pasó</b>\n\n{message}\n\n⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        self._send(text)

    def notify_daily_report(self, stats):
        ret = stats.get("total_return_pct", 0)
        n = stats.get("total_trades", 0)
        wr_s = f"{stats.get('win_rate_pct', 0):.0f}%" if n > 0 else "—"
        text = (
            f"🗓 <b>Reporte diario — {self.base_asset}/USDT</b>\n\n"
            f"<pre>\n"
            f"Capital     ${stats.get('capital', 0):,.2f}  ({ret:+.1f}%)\n"
            f"Trades      {n}   WR {wr_s}\n"
            f"DD máximo   {stats.get('max_drawdown_pct', 0):.1f}%\n"
            f"Posición    {'sí' if stats.get('open_position') else 'no'}\n"
            f"</pre>\n"
            f"🕐 {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    def notify_bot_started(self, mode="PAPER TRADING"):
        text = (
            f"🤖 <b>¡Arranqué!</b>\n\n"
            f"Voy a operar <b>{self.base_asset}</b> en modo: <b>{mode}</b>\n"
            f"<i>Te aviso cuando vea oportunidad o abra/cierre algo.</i>\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    def notify_bot_stopped(self, reason):
        text = (
            f"🛑 <b>Me apagué</b>\n\n"
            f"Razón: {reason}\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    # ─────────────────────────────────────────
    #  CONTROL REMOTO POR TELEGRAM
    # ─────────────────────────────────────────

    def notify_paused(self):
        text = (
            f"⏸ <b>Pause</b>\n\n"
            f"Listo, no abro nuevas operaciones.\n"
            f"<i>Si hay una abierta, el stop-loss y take-profit siguen funcionando.</i>\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    def notify_resumed(self):
        text = (
            f"▶️ <b>Volví</b>\n\n"
            f"Sigo buscando oportunidades para operar.\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    def notify_command_received(self, command):
        text = f"✅ Recibí <code>{command}</code>"
        self._send(text)

    # ─────────────────────────────────────────
    #  PANORAMA periódico (cada 30 min)
    # ─────────────────────────────────────────

    def notify_panorama(self, stats: dict, snapshot, decision, mtf=None) -> None:
        """Resumen de mediados de turno. No es un evento, es un check-in."""
        cap = stats.get("capital", 0)
        ret = stats.get("total_return_pct", 0)
        n = stats.get("total_trades", 0)
        wr = stats.get("win_rate_pct", 0)
        dd = stats.get("max_drawdown_pct", 0)
        pos_abierta = stats.get("open_position", False)

        trend = {"BULL": "alcista", "BEAR": "bajista", "LATERAL": "lateral"}.get(
            snapshot.trend, snapshot.trend
        )
        if decision.accion == "ESPERAR":
            dec_line = "Sin señal — esperando setup"
        elif decision.accion == "COMPRAR":
            dec_line = f"Señal COMPRA ({decision.confianza:.0%})"
        else:
            dec_line = f"Señal VENTA ({decision.confianza:.0%})"

        wr_s = f"{wr:.0f}%" if n > 0 else "—"
        macro = ""
        if mtf is not None:
            macro = f"Macro      1h {mtf.trend_1h} · 4h {mtf.trend_4h}\n"

        text = (
            f"📊 <b>Panorama — {self.base_asset}/USDT</b>\n\n"
            f"<pre>\n"
            f"Precio     ${snapshot.price:,.2f} ({snapshot.price_change_1h:+.2f}% 1h)\n"
            f"Tendencia  {trend} · ADX {snapshot.adx:.0f}\n"
            f"{macro}"
            f"Capital    ${cap:,.2f} ({ret:+.1f}%)\n"
            f"Trades     {n} · WR {wr_s} · DDmax {dd:.1f}%\n"
            f"Posición   {'sí' if pos_abierta else 'no'}\n"
            f"</pre>\n"
            f"{dec_line}  ·  🕐 {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)
