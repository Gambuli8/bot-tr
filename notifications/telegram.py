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
            f"💵 BTC: <b>${snapshot.price:,.2f}</b>  "
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
        reason, confidence, direction: str = "LONG", symbol: str = "",
    ):
        base = symbol.split("/")[0] if symbol else "BTC"
        tag = f" {symbol}" if symbol else ""
        invertido = price * amount_btc
        if direction == "SHORT":
            ganamos_si = take_profit
            perdemos_si = stop_loss
            apuesta = "que <b>baje</b>"
            emoji = "🔻"
            ganamos_dir = "baja"
            perdemos_dir = "sube"
        else:
            ganamos_si = take_profit
            perdemos_si = stop_loss
            apuesta = "que <b>suba</b>"
            emoji = "🟢"
            ganamos_dir = "sube"
            perdemos_dir = "baja"

        # Cuánto ganaríamos/perderíamos si toca TP/SL
        if direction == "SHORT":
            ganancia = (price - take_profit) * amount_btc
            perdida = (stop_loss - price) * amount_btc
        else:
            ganancia = (take_profit - price) * amount_btc
            perdida = (price - stop_loss) * amount_btc

        ganancia_pct = (ganancia / invertido) * 100
        perdida_pct = (perdida / invertido) * 100

        text = (
            f"{emoji} <b>Abrí una operación{tag}</b> apostando {apuesta}\n\n"
            f"💵 Le metí <b>${invertido:,.2f}</b> ({amount_btc:.6f} {base})\n"
            f"📍 Precio de entrada: <b>${price:,.2f}</b>\n\n"
            f"🎯 Si {ganamos_dir} a <b>${ganamos_si:,.2f}</b> → <b>ganamos +${ganancia:,.2f}</b> ({ganancia_pct:+.2f}%)\n"
            f"🛑 Si {perdemos_dir} a <b>${perdemos_si:,.2f}</b> → cerramos con <b>-${perdida:,.2f}</b> ({-perdida_pct:.2f}%)\n\n"
            f"📝 Motivo: <i>{reason}</i>\n"
            f"🧠 Confianza: {confidence:.0%}\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    def notify_sell(
        self, price, pnl_usdt, pnl_pct, reason, direction: str = "LONG",
        symbol: str = "",
    ):
        ganamos = pnl_usdt > 0
        if ganamos:
            head_emoji = "🎉"
            sign = "+"
            verb = "Ganamos"
            tail = "Buenísimo che."
        else:
            head_emoji = "😔"
            sign = "-"
            verb = "Perdimos"
            tail = "Mala, sale la próxima."

        dir_txt = "SHORT" if direction == "SHORT" else "LONG"
        tag = f" {symbol}" if symbol else ""

        text = (
            f"{head_emoji} <b>Cerré la operación{tag} ({dir_txt})</b>\n\n"
            f"💸 {verb}: <b>{sign}${abs(pnl_usdt):,.2f}</b> ({sign}{abs(pnl_pct):.2f}%)\n"
            f"📍 Precio de salida: <b>${price:,.2f}</b>\n\n"
            f"📝 ¿Por qué cerré? <i>{reason}</i>\n\n"
            f"<i>{tail}</i>\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
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

    def notify_partial_tp(
        self, price, portion_btc, pnl_usdt, new_stop, direction: str = "LONG",
        symbol: str = "",
    ):
        base = symbol.split("/")[0] if symbol else "BTC"
        tag = f" {symbol}" if symbol else ""
        text = (
            f"🎯 <b>¡Tomé ganancia parcial (TP1){tag}!</b>\n\n"
            f"💰 Cerré <b>{portion_btc:.6f} {base}</b> a <b>${price:,.2f}</b> → "
            f"<b>+${pnl_usdt:,.2f}</b>\n"
            f"🔒 Moví el stop a <b>breakeven (${new_stop:,.2f})</b>: "
            f"el resto de la operación ya es <b>trade gratis</b>.\n\n"
            f"<i>Dejo correr la otra mitad para buscar el TP completo.</i>\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    def notify_warning(self, message):
        text = f"⚠️ <b>Atención</b>\n\n{message}\n\n⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        self._send(text)

    def notify_critical(self, message):
        text = f"🚨 <b>Algo grave pasó</b>\n\n{message}\n\n⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        self._send(text)

    def notify_daily_report(self, stats):
        ret = stats.get("total_return_pct", 0)
        if ret > 0:
            head = f"📈 <b>Resumen del día</b> — vamos ganando"
        elif ret < 0:
            head = f"📉 <b>Resumen del día</b> — vamos perdiendo"
        else:
            head = f"➖ <b>Resumen del día</b> — empate"
        text = (
            f"{head}\n\n"
            f"💵 Capital actual: <b>${stats.get('capital', 0):,.2f}</b>\n"
            f"📊 Ganancia/pérdida total: <b>{ret:+.2f}%</b>\n"
            f"✅ Acertamos en: <b>{stats.get('win_rate_pct', 0):.1f}%</b> de las operaciones\n"
            f"🔢 Operaciones hechas: <b>{stats.get('total_trades', 0)}</b>\n"
            f"📉 Mayor caída en el día: <b>{stats.get('max_drawdown_pct', 0):.2f}%</b>\n"
            f"⚡ ¿Hay operación abierta?: <b>{'sí' if stats.get('open_position') else 'no'}</b>\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    def notify_bot_started(self, mode="PAPER TRADING"):
        text = (
            f"🤖 <b>¡Arranqué!</b>\n\n"
            f"Voy a operar BTC en modo: <b>{mode}</b>\n"
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

        if ret > 0.5:
            mood = "📈 vamos ganando"
        elif ret < -0.5:
            mood = "📉 vamos perdiendo"
        else:
            mood = "➖ andamos parejos"

        # Resumen del mercado
        trend_emoji = {"BULL": "📈 alcista", "BEAR": "📉 bajista", "LATERAL": "➡️ lateral"}.get(
            snapshot.trend, snapshot.trend
        )
        mercado = (
            f"BTC <b>${snapshot.price:,.2f}</b> "
            f"({snapshot.price_change_1h:+.2f}% 1h)\n"
            f"Tendencia: {trend_emoji}  •  ADX {snapshot.adx:.0f}"
        )
        if mtf is not None:
            mercado += (
                f"\nMacro: 1h <b>{mtf.trend_1h}</b> (ADX {mtf.adx_1h:.0f})  "
                f"•  4h <b>{mtf.trend_4h}</b> (ADX {mtf.adx_4h:.0f})"
            )

        # Decisión
        if decision.accion == "ESPERAR":
            dec_line = "💤 No veo oportunidad clara"
        elif decision.accion == "COMPRAR":
            dec_line = f"🟢 Veo señal de COMPRA ({decision.confianza:.0%})"
        else:
            dec_line = f"🔻 Veo señal de VENTA ({decision.confianza:.0%})"

        if pos_abierta:
            pos_line = "💼 Tengo una operación abierta — mirá /position"
        else:
            pos_line = "💼 Sin operación abierta"

        text = (
            f"<b>📊 Panorama</b> — {mood}\n\n"
            f"💵 Capital: <b>${cap:,.2f}</b> ({ret:+.2f}%)\n"
            f"🎯 Acierto: {wr:.0f}% en {n} operaciones  •  DD máx {dd:.2f}%\n"
            f"{pos_line}\n\n"
            f"<b>Mercado ahora</b>\n{mercado}\n{dec_line}\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)

    def notify_multi_panorama(self, stats: dict, per_symbol: list) -> None:
        """
        Panorama de PORTAFOLIO (multi-symbol): un solo mensaje con el estado del
        capital compartido + una línea por símbolo escaneado en el ciclo.
        """
        cap = stats.get("capital", 0)
        ret = stats.get("total_return_pct", 0)
        n = stats.get("total_trades", 0)
        wr = stats.get("win_rate_pct", 0)
        dd = stats.get("max_drawdown_pct", 0)
        n_open = stats.get("open_positions_count", 0)
        n_max = stats.get("max_concurrent_trades", 0)

        if ret > 0.5:
            mood = "📈 vamos ganando"
        elif ret < -0.5:
            mood = "📉 vamos perdiendo"
        else:
            mood = "➖ andamos parejos"

        lines = []
        for info in per_symbol:
            sym = info.get("symbol", "?")
            price = info.get("price", 0)
            dec = info.get("decision", "ESPERAR")
            held = "💼" if info.get("has_position") else "  "
            dec_emoji = {"COMPRAR": "🟢", "VENDER": "🔻", "ESPERAR": "💤"}.get(dec, "💤")
            act = info.get("action", "ESPERAR")
            act_txt = "" if act == "ESPERAR" else f"  → <b>{act}</b>"
            lines.append(f"{held} <b>{sym}</b> ${price:,.2f}  {dec_emoji}{act_txt}")
        cuerpo = "\n".join(lines) if lines else "<i>(sin datos de símbolos)</i>"

        candado = "🔒" if n_open >= n_max else "🔓"
        text = (
            f"<b>📊 Panorama de portafolio</b> — {mood}\n\n"
            f"💵 Capital: <b>${cap:,.2f}</b> ({ret:+.2f}%)\n"
            f"🎯 Acierto: {wr:.0f}% en {n} ops  •  DD máx {dd:.2f}%\n"
            f"{candado} Exposición: <b>{n_open}/{n_max}</b> trades abiertos\n\n"
            f"<b>Monedas</b>\n{cuerpo}\n\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M')} UTC"
        )
        self._send(text)
