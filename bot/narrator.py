"""
Traduce lo que hace el bot a mensajes cortos y fáciles de entender (Telegram, HTML).
Sólo arma textos: no manda nada ni toca el exchange.

Números en formato argentino ($76.014,1 · +$0,335 · −0,68%): ver bot/fmt.py.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from bot.fmt import money, pct, price, qty, ratio
from bot.signals import Signal
from bot.sizing import TradePlan


def esc(value) -> str:
    """Escape HTML: sin esto un '<' en un motivo rompe el mensaje (HTTP 400)."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def side_word(side: str) -> str:
    return "📈 COMPRA (LONG)" if side == "LONG" else "📉 VENTA (SHORT)"


def side_short(side: str) -> str:
    return "📈 LONG" if side == "LONG" else "📉 SHORT"


def move_pct(reference: Optional[float], target: Optional[float]) -> Optional[float]:
    """Variación % con signo de `reference` a `target`."""
    if not reference or target is None:
        return None
    return (target - reference) / reference * 100


def duration(ms: float) -> str:
    minutes = int(ms / 60000)
    if minutes < 1:
        return "menos de 1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


def asset_of(symbol: str) -> str:
    return symbol.split("-")[0]


class Narrator:
    def __init__(self, mode_label: str, tz: str):
        self.mode_label = mode_label
        self.tz = ZoneInfo(tz)

    def _head(self, emoji: str, title: str) -> str:
        return f"{emoji} <b>{esc(title)}</b> · <i>{self.mode_label}</i>"

    def stamp(self, ts: Optional[float] = None) -> str:
        return datetime.fromtimestamp(ts or time.time(), self.tz).strftime("%d/%m %H:%M")

    # ───────── análisis (etapas del setup) ─────────

    def setup_event(self, s: Signal) -> str:
        asset = s.base_asset
        bias = "alcista" if s.side == "LONG" else "bajista"
        zone_kind = "soporte" if s.side == "LONG" else "resistencia"
        note = f"\n<i>{esc(s.note)}</i>" if s.note else ""

        if s.event == "zone":
            zona = (f"{price(s.zone_low)} – {price(s.zone_high)}"
                    if s.zone_low and s.zone_high else price(s.price))
            return (f"{self._head('🔎', f'{asset}: llegó a una zona diaria')}\n\n"
                    f"El precio ({price(s.price)}) entró en una zona de <b>{zone_kind}</b> del diario:\n"
                    f"📍 Zona: <b>{zona}</b>\n\n"
                    f"👉 Todavía no opero: espero que en 1H cambie la tendencia a {bias}.{note}")

        if s.event == "choch":
            lines = [f"{self._head('📐', f'{asset}: cambio de tendencia en 1H')}", "",
                     f"La estructura de 1H giró a <b>{bias}</b>. Tracé Fibonacci del impulso "
                     f"{price(s.fib_start)} → {price(s.fib_end)}:",
                     f"🎯 Entrada (0,618): <b>{price(s.fib_618)}</b>",
                     f"⛔ Invalidación (cierre 1H pasando 0,75): <b>{price(s.fib_75)}</b>"]
            if s.fib_sl:
                lines.append(f"🛑 Stop Loss si entro (0,786): <b>{price(s.fib_sl)}</b>")
            if s.fib_end:
                lines.append(f"🏁 Take Profit si entro (techo del impulso): <b>{price(s.fib_end)}</b>")
            lines += ["", f"👉 Espero que el precio retroceda hasta el 0,618 "
                          f"({pct(move_pct(s.price, s.fib_618), signed=True)} desde acá).{note}"]
            return "\n".join(lines)

        if s.event == "fib":
            return (f"{self._head('🎯', f'{asset}: retroceso en zona de entrada')}\n\n"
                    f"El precio ({price(s.price)}) llegó al 0,618 ({price(s.fib_618)}) "
                    f"sin romper el 0,75 ({price(s.fib_75)}).\n\n"
                    f"👉 Miro 5 minutos: si rompe la diagonal del retroceso, entro en "
                    f"<b>{side_word(s.side)}</b>.{note}")

        if s.event == "cancel":
            return (f"{self._head('❌', f'{asset}: setup cancelado')}\n\n"
                    f"🗑️ Descarto la oportunidad {bias}.\n"
                    f"📝 Motivo: {esc(s.note or 'se invalidó la estructura')}.\n\n"
                    f"👉 Sigo buscando.")
        return f"{self._head('ℹ️', asset)}\nEvento {esc(s.event)}"

    # ───────── operaciones ─────────

    def entry_opened(self, s: Signal, plan: TradePlan, fill_price: float) -> str:
        asset = s.base_asset
        return (f"{self._head('🟢', f'ENTRÉ en {asset}')}\n"
                f"<b>{side_word(plan.direction)}</b> a <b>{price(fill_price)}</b>\n\n"
                f"💰 Margen <b>{money(plan.margin_used)}</b> · Apalancamiento <b>×{plan.leverage}</b>\n"
                f"📦 Posición {money(plan.notional)} ({qty(plan.qty)} {asset})\n\n"
                f"🛑 Stop Loss <b>{price(plan.stop_loss)}</b> ({pct(move_pct(fill_price, plan.stop_loss), True)})\n"
                f"     → pérdida máx. <b>{money(-plan.risk_usdt, True)}</b>\n"
                f"🎯 Take Profit <b>{price(plan.take_profit)}</b> ({pct(move_pct(fill_price, plan.take_profit), True)})\n"
                f"     → ganancia <b>{money(plan.reward_usdt, True)}</b>\n"
                f"⚖️ Riesgo/Beneficio <b>1 : {ratio(plan.rr)}</b>\n\n"
                f"<b>Por qué entré:</b> zona diaria ✔ · cambio de tendencia 1H ✔ · "
                f"retroceso al 0,618 sin romper 0,75 ✔ · ruptura de la diagonal en 5m ✔"
                + (f"\n<i>{esc(s.note)}</i>" if s.note else ""))

    def entry_rejected(self, s: Signal, reason: str) -> str:
        return (f"{self._head('⚠️', f'{s.base_asset}: señal de entrada, pero NO entré')}\n\n"
                f"📝 Motivo: {esc(reason)}")

    def entry_failed(self, s: Signal, error: str) -> str:
        return (f"{self._head('🚨', f'{s.base_asset}: error al abrir la operación')}\n\n"
                f"{esc(error)}\n\nRevisá BingX. El bot no reintenta solo para no duplicar órdenes.")

    def trade_closed(self, rec: dict, day_total: Optional[float] = None) -> str:
        pnl = rec["pnl_usdt"]
        won = pnl > 0
        why = {"TP": "llegó al Take Profit 🎯", "SL": "tocó el Stop Loss 🛑",
               "MANUAL": "cierre manual ✋", "LIQ": "liquidación ⚠️"}.get(rec.get("exit_reason"), "cierre")
        margin = rec.get("margin_used") or 0
        on_margin = f" ({pct(pnl / margin * 100, True, 1)} del margen)" if margin else ""
        asset = asset_of(rec["symbol"])
        title = f"CERRÉ {asset} — {'GANANCIA' if won else 'PÉRDIDA'}"
        lines = [
            f"{self._head('✅' if won else '🔴', title)}",
            f"{side_short(rec['direction'])}: {price(rec['entry_price'])} → {price(rec.get('exit_price'))} "
            f"({pct(move_pct(rec['entry_price'], rec.get('exit_price')), True)})",
            "",
            f"📝 Motivo: {why}",
            f"💵 Resultado: <b>{money(pnl, True)}</b>{on_margin}",
            f"🧾 Comisiones y funding: {money(-abs(rec.get('fees_usdt', 0)), True)}",
            f"⏱️ Duración: {duration(rec['closed_at'] - rec['opened_at'])}",
        ]
        if day_total is not None:
            lines.append(f"\n📅 Acumulado de hoy: <b>{money(day_total, True)}</b>")
        return "\n".join(lines)

    # ───────── /estado (un mensaje general + uno por moneda) ─────────

    def status_header(self, *, paused: bool, balance: Optional[dict], balance_error: str, open_count: int,
                      positions_error: str, setups_count: int, day_pnl: float, day_count: int,
                      daily_limit: float, margin: float, max_positions: int, demo: bool) -> str:
        lines = [f"📊 <b>Estado general</b> · <i>{self.mode_label}</i> · {self.stamp()}", "", "💼 <b>Cuenta</b>"]
        if balance:
            suffix = " <i>(saldo de prueba)</i>" if demo else ""
            lines += [f"💵 Saldo: <b>{money(balance['balance'])}</b>{suffix}",
                      f"🟢 Disponible: {money(balance['available'])}",
                      f"📊 PnL abierto: {money(balance['unrealized_pnl'], True)}"]
        else:
            lines.append(f"💵 Saldo: no disponible ({esc(balance_error)})")
        ops = "operación" if day_count == 1 else "operaciones"
        lines.append(f"📅 Hoy: <b>{money(day_pnl, True)}</b> ({day_count} {ops})")
        lines.append(f"🛡️ Límite de pérdida diaria: {money(-abs(daily_limit), True)}")
        lines += ["", "⚙️ <b>Operativa</b>",
                  f"🚦 Nuevas entradas: {'⏸️ en pausa' if paused else '▶️ activas'}",
                  f"💰 Margen por operación: {money(margin)}",
                  f"📂 Operaciones abiertas: {'no disponible' if positions_error else f'{open_count}/{max_positions}'}",
                  f"🔎 Setups en análisis: {setups_count}",
                  "", "👇 Detalle por moneda:"]
        return "\n".join(lines)

    def coin_status(self, *, symbol: str, last_price: Optional[float], position=None,
                    trade: Optional[dict] = None, setups: list[dict]) -> str:
        asset = asset_of(symbol)
        lines = [f"🪙 <b>{asset}</b> · {price(last_price)}"]

        if position is not None:
            trade = trade or {}
            margin_used = trade.get("margin_used") or (position.margin or None)
            pnl_margin = (f" ({pct(position.unrealized_pnl / margin_used * 100, True, 1)} del margen)"
                          if margin_used else "")
            opened = trade.get("opened_at")
            since = f" · hace {duration(time.time() * 1000 - opened)}" if opened else ""
            lines += ["", f"📂 <b>Operación abierta</b>: {side_short(position.side)} ×{position.leverage:.0f}{since}",
                      f"🔹 Entrada {price(position.entry_price)} → ahora {price(position.mark_price)}",
                      f"💹 PnL: <b>{money(position.unrealized_pnl, True)}</b>{pnl_margin}"]
            if trade:
                lines.append(f"🛑 SL {price(trade.get('stop_loss'))} "
                             f"({pct(move_pct(position.mark_price, trade.get('stop_loss')), True)}) · "
                             f"🎯 TP {price(trade.get('take_profit'))} "
                             f"({pct(move_pct(position.mark_price, trade.get('take_profit')), True)})")
            else:
                lines.append("⚠️ Esta posición no la abrió el bot")
        else:
            lines += ["", "📂 Sin operación abierta"]

        lines.append("")
        if not setups:
            lines.append("👀 Sin setups: espero que llegue a una zona diaria.")
        for info in sorted(setups, key=lambda i: i.get("side", "")):
            ago = duration((time.time() - info.get("updated", time.time())) * 1000)
            lines.append(f"🔎 {side_short(info['side'])} — ⏳ {esc(info['stage'])} <i>(hace {ago})</i>")
            if info.get("fib_618"):
                lines.append(f"   🎯 Entrada 0,618: {price(info['fib_618'])}")
                lines.append(f"   ⛔ Invalida 0,75: {price(info.get('fib_75'))}")
                if info.get("fib_sl"):
                    lines.append(f"   🛑 SL 0,786: {price(info['fib_sl'])}")
                if last_price:
                    lines.append(f"   📏 Distancia a la entrada: {pct(move_pct(last_price, info['fib_618']), True)}")
            elif info.get("zone_low") and info.get("zone_high"):
                lines.append(f"   📍 Zona: {price(info['zone_low'])} – {price(info['zone_high'])}")
        return "\n".join(lines)

    # ───────── sistema ─────────

    def started(self, balance: Optional[float], symbols: list[str], margin: float) -> str:
        bal = money(balance) if balance is not None else "no disponible"
        return (f"{self._head('🤖', 'Bot encendido')}\n\n"
                f"💵 Saldo: <b>{bal}</b>\n"
                f"🪙 Pares: {' · '.join(asset_of(s) for s in symbols)}\n"
                f"💰 Margen por operación: {money(margin)}\n\n"
                f"💬 Escribí /ayuda para ver los comandos.")

    def alert(self, text: str, critical: bool = False) -> str:
        return f"{self._head('🚨' if critical else '⚠️', 'Atención')}\n\n{esc(text)}"
