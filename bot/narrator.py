"""
Traduce lo que hace el bot a mensajes cortos y fáciles de entender (Telegram, HTML).
Sólo arma textos: no manda nada ni toca el exchange.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from bot.signals import Signal
from bot.sizing import TradePlan


def esc(value) -> str:
    """Escape HTML: sin esto un '<' en un motivo rompe el mensaje (HTTP 400)."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def px(value: Optional[float]) -> str:
    """Precio con decimales según magnitud: 75,832.5 · 3,012.45 · 0.1234 · 0.08123."""
    if value is None:
        return "—"
    v = float(value)
    a = abs(v)
    decimals = 1 if a >= 10000 else 2 if a >= 100 else 3 if a >= 10 else 4 if a >= 1 else 5
    return f"{v:,.{decimals}f}"


def usd(value: float, signed: bool = False) -> str:
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.2f} USDT" if abs(value) >= 0.01 or value == 0 else f"{sign}{value:.4f} USDT"


def side_word(side: str) -> str:
    return "COMPRA (LONG) 📈" if side == "LONG" else "VENTA (SHORT) 📉"


def duration(ms: int) -> str:
    minutes = int(ms / 60000)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


class Narrator:
    def __init__(self, mode_label: str, tz: str):
        self.mode_label = mode_label
        self.tz = ZoneInfo(tz)

    def _head(self, emoji: str, title: str) -> str:
        return f"{emoji} <b>{esc(title)}</b>  <i>[{self.mode_label}]</i>"

    def now(self) -> str:
        return datetime.now(self.tz).strftime("%d/%m %H:%M")

    # ───────── análisis (etapas del setup) ─────────

    def setup_event(self, s: Signal) -> str:
        asset = esc(s.base_asset)
        bias = "alcista" if s.side == "LONG" else "bajista"
        zone_kind = "soporte" if s.side == "LONG" else "resistencia"
        note = f"\n<i>{esc(s.note)}</i>" if s.note else ""

        if s.event == "zone":
            zona = f"{px(s.zone_low)} – {px(s.zone_high)}" if s.zone_low and s.zone_high else px(s.price)
            return (f"{self._head('🔎', f'{asset}: llegó a una zona diaria')}\n"
                    f"El precio ({px(s.price)}) está en una zona de <b>{zone_kind}</b> del gráfico diario "
                    f"({zona}) y está perdiendo fuerza.\n"
                    f"👉 Todavía no hago nada: espero que en 1H cambie la tendencia.{note}")
        if s.event == "choch":
            return (f"{self._head('📐', f'{asset}: cambio de tendencia en 1H')}\n"
                    f"La estructura de 1H giró a <b>{bias}</b>. Tracé Fibonacci del impulso "
                    f"({px(s.fib_start)} → {px(s.fib_end)}).\n"
                    f"• Zona de entrada (0.618): <b>{px(s.fib_618)}</b>\n"
                    f"• Invalidación (0.75): <b>{px(s.fib_75)}</b>\n"
                    f"👉 Espero que el precio retroceda hasta el 0.618.{note}")
        if s.event == "fib":
            return (f"{self._head('🎯', f'{asset}: retroceso en zona de entrada')}\n"
                    f"El precio ({px(s.price)}) llegó al 0.618 ({px(s.fib_618)}) sin romper el 0.75 "
                    f"({px(s.fib_75)}).\n"
                    f"👉 Ahora miro 5 minutos: si rompe la diagonal del retroceso, entro en "
                    f"{side_word(s.side)}.{note}")
        if s.event == "cancel":
            return (f"{self._head('❌', f'{asset}: setup cancelado')}\n"
                    f"Descarto la oportunidad {bias}. Motivo: {esc(s.note or 'se invalidó la estructura')}.\n"
                    f"👉 Sigo buscando.")
        return f"{self._head('ℹ️', asset)} evento {esc(s.event)}"

    # ───────── operaciones ─────────

    def entry_opened(self, s: Signal, plan: TradePlan, fill_price: float) -> str:
        asset = esc(s.base_asset)
        return (f"{self._head('🟢', f'ENTRÉ en {asset}')}\n"
                f"<b>{side_word(plan.direction)}</b> a <b>{px(fill_price)}</b>\n\n"
                f"💰 Margen: {usd(plan.margin_used)} · Apalancamiento {plan.leverage}x · "
                f"Posición {usd(plan.notional)}\n"
                f"🛑 Stop Loss: {px(plan.stop_loss)} ({plan.sl_distance_pct:.2f}%) → pierdo ~{usd(plan.risk_usdt)}\n"
                f"🎯 Take Profit: {px(plan.take_profit)} ({plan.tp_distance_pct:.2f}%) → gano ~{usd(plan.reward_usdt)}\n"
                f"⚖️ Riesgo/Beneficio: 1 : {plan.rr:.1f}\n\n"
                f"<b>Por qué entré:</b> zona diaria + cambio de tendencia en 1H + retroceso al 0.618 "
                f"sin romper 0.75 + ruptura de la diagonal en 5m."
                + (f"\n<i>{esc(s.note)}</i>" if s.note else ""))

    def entry_rejected(self, s: Signal, reason: str) -> str:
        return (f"{self._head('⚠️', f'{esc(s.base_asset)}: señal de entrada, pero NO entré')}\n"
                f"Motivo: {esc(reason)}")

    def entry_failed(self, s: Signal, error: str) -> str:
        return (f"{self._head('🚨', f'{esc(s.base_asset)}: error al abrir la operación')}\n"
                f"{esc(error)}\nRevisá BingX. El bot no reintenta solo para no duplicar órdenes.")

    def trade_closed(self, rec: dict) -> str:
        pnl = rec["pnl_usdt"]
        won = pnl > 0
        emoji = "✅" if won else "🔴"
        why = {"TP": "llegó al Take Profit 🎯", "SL": "tocó el Stop Loss 🛑",
               "MANUAL": "cierre manual", "LIQ": "liquidación ⚠️"}.get(rec.get("exit_reason"), "cierre")
        margin = rec.get("margin_used") or 0
        pct = f" ({pnl / margin * 100:+.1f}% del margen)" if margin else ""
        asset = rec["symbol"].split("-")[0]
        title = f"CERRÉ {asset} — {'GANANCIA' if won else 'PÉRDIDA'}"
        return (f"{self._head(emoji, title)}\n"
                f"{side_word(rec['direction'])}: {px(rec['entry_price'])} → {px(rec.get('exit_price'))}\n"
                f"Motivo: {why}\n"
                f"Resultado: <b>{usd(pnl, signed=True)}</b>{pct}\n"
                f"Comisiones y funding: {usd(rec.get('fees_usdt', 0))}\n"
                f"Duración: {duration(rec['closed_at'] - rec['opened_at'])}")

    # ───────── sistema ─────────

    def started(self, balance: Optional[float], symbols: list[str], margin: float) -> str:
        bal = usd(balance) if balance is not None else "no disponible"
        pares = ", ".join(s.split("-")[0] for s in symbols)
        return (f"{self._head('🤖', 'Bot encendido')}\n"
                f"Saldo: {bal}\nPares: {esc(pares)}\nMargen por operación: {usd(margin)}\n"
                f"Escribí /ayuda para ver los comandos.")

    def alert(self, text: str, critical: bool = False) -> str:
        return f"{self._head('🚨' if critical else '⚠️', 'Atención')}\n{esc(text)}"
