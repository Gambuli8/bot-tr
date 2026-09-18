"""
Resúmenes semanales y mensuales.

- Semanal: se envía el lunes (hora local) con la semana anterior (lun→dom).
- Mensual: se envía el día 1 con el mes anterior.
Cada resumen va a Telegram (versión corta) y a Google Drive como Google Doc
(versión completa) si Drive está configurado.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot.fmt import money, num, pct, price
from bot.narrator import duration, esc, side_short
from bot.store import Store

log = logging.getLogger(__name__)

MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]


@dataclass
class Period:
    kind: str  # weekly | monthly
    key: str   # 2026-W38 | 2026-09
    start: datetime
    end: datetime

    @property
    def title(self) -> str:
        if self.kind == "weekly":
            last = self.end - timedelta(days=1)
            return f"Resumen semanal {self.start:%d/%m} – {last:%d/%m/%Y}"
        return f"Resumen mensual {MONTHS[self.start.month - 1]} {self.start.year}"


def previous_week(now: datetime) -> Period:
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start = this_monday - timedelta(days=7)
    iso = start.isocalendar()
    return Period("weekly", f"{iso.year}-W{iso.week:02d}", start, this_monday)


def previous_month(now: datetime) -> Period:
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev = first_this - timedelta(days=1)
    start = last_prev.replace(day=1)
    return Period("monthly", f"{start:%Y-%m}", start, first_this)


def carry_stats(events: list[dict]) -> dict:
    """Funding, comisiones y PnL del modo carry en el período (eventos `carry_income`)."""
    rows = [e for e in events if e.get("kind") == "carry_income"]
    by_symbol: dict[str, dict] = defaultdict(lambda: {"funding": 0.0, "fees": 0.0, "realized": 0.0})
    funding = fees = realized = 0.0
    payments = 0
    for e in rows:
        amount = float(e.get("amount") or 0.0)
        row = by_symbol[e.get("symbol", "—")]
        kind = e.get("income")
        if kind == "FUNDING_FEE":
            funding += amount
            row["funding"] += amount
            payments += 1
        elif kind in ("TRADING_FEE", "SPOT_FEE"):
            fees -= amount          # llegan en negativo (son un costo)
            row["fees"] -= amount
        elif kind == "REALIZED_PNL":
            realized += amount
            row["realized"] += amount
    return {
        "active": bool(rows),
        "paper": any(e.get("paper") for e in rows),
        "funding": funding,
        "fees": fees,
        "realized": realized,
        "net": funding + realized - fees,
        "payments": payments,
        "by_symbol": {k: dict(v, net=v["funding"] + v["realized"] - v["fees"]) for k, v in by_symbol.items()},
    }


def compute_stats(trades: list[dict], events: list[dict]) -> dict:
    trades = sorted(trades, key=lambda t: t["closed_at"])
    pnls = [t["pnl_usdt"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    equity = peak = max_dd = 0.0
    streak = max_streak = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        streak = streak + 1 if p <= 0 else 0
        max_streak = max(max_streak, streak)

    by_symbol: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        row = by_symbol[t["symbol"]]
        row["trades"] += 1
        row["wins"] += 1 if t["pnl_usdt"] > 0 else 0
        row["pnl"] += t["pnl_usdt"]

    signal_events = Counter(e.get("event") for e in events if e.get("kind") == "signal")
    rejections = Counter(e.get("reason", "")[:80] for e in events if e.get("kind") == "rejected")

    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "net": sum(pnls),
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "best": max(trades, key=lambda t: t["pnl_usdt"]) if trades else None,
        "worst": min(trades, key=lambda t: t["pnl_usdt"]) if trades else None,
        "fees": sum(t.get("fees_usdt", 0.0) for t in trades),
        "max_drawdown": max_dd,
        "max_loss_streak": max_streak,
        "exit_reasons": Counter(t.get("exit_reason", "OTRO") for t in trades),
        "by_symbol": dict(by_symbol),
        "zones": signal_events.get("zone", 0),
        "chochs": signal_events.get("choch", 0),
        "fibs": signal_events.get("fib", 0),
        "cancels": signal_events.get("cancel", 0),
        "entries_signaled": signal_events.get("entry", 0),
        "rejections": rejections,
        "carry": carry_stats(events),
        "list": trades,
    }


def telegram_text(period: Period, st: dict, mode_label: str, drive_link: Optional[str]) -> str:
    pf = num(st["profit_factor"]) if st["profit_factor"] is not None else "—"
    emoji = "📈" if st["net"] > 0 else "📉" if st["net"] < 0 else "➖"
    lines = [
        f"🗓️ <b>{esc(period.title)}</b> · <i>{mode_label}</i>",
        "",
        f"{emoji} Resultado: <b>{money(st['net'], True)}</b>",
        f"🔢 Operaciones: {st['trades']} (✅ {st['wins']} · 🔴 {st['losses']})",
        f"🎯 Acierto: {pct(st['win_rate'], decimals=0)} · ⚖️ Profit factor: {pf}",
        f"📉 Drawdown máx.: {money(-st['max_drawdown'], True)}",
        f"🧾 Comisiones y funding: {money(-st['fees'], True)}",
    ]
    if st["by_symbol"]:
        lines.append("")
        lines.append("🪙 <b>Por par</b>")
        for sym, row in sorted(st["by_symbol"].items(), key=lambda kv: -kv[1]["pnl"]):
            icon = "🟢" if row["pnl"] > 0 else "🔴" if row["pnl"] < 0 else "⚪"
            lines.append(f"{icon} {esc(sym.split('-')[0])}: {row['trades']} ops · <b>{money(row['pnl'], True)}</b>")
    carry = st.get("carry") or {}
    if carry.get("active"):
        tag = " · <i>SIMULADO 🧪</i>" if carry["paper"] else ""
        lines += [
            "",
            f"🧲 <b>Carry (captura de funding)</b>{tag}",
            f"💸 Funding cobrado: <b>{money(carry['funding'], True)}</b> en {carry['payments']} cobros",
            f"🧾 Comisiones: {money(-carry['fees'], True)} · 📊 Resultado del carry: "
            f"<b>{money(carry['net'], True)}</b>",
        ]
        for sym, row in sorted(carry["by_symbol"].items(), key=lambda kv: -kv[1]["net"]):
            lines.append(f"• {esc(sym.split('-')[0])}: funding {money(row['funding'], True)} · "
                         f"neto {money(row['net'], True)}")
        lines.append(f"💼 <b>Total del período</b> (operaciones + carry): "
                     f"<b>{money(st['net'] + carry['net'], True)}</b>")
    lines += [
        "",
        "🔎 <b>Análisis</b>",
        f"📍 Zonas diarias: {st['zones']} · 📐 Cambios 1H: {st['chochs']}",
        f"🎯 Retrocesos al 0,618: {st['fibs']} · ❌ Cancelados: {st['cancels']}",
        f"🚀 Señales de entrada: {st['entries_signaled']}",
    ]
    if drive_link:
        lines += ["", f'📄 <a href="{esc(drive_link)}">Informe completo en Drive</a>']
    return "\n".join(lines)


def html_report(period: Period, st: dict, mode_label: str, tz: ZoneInfo) -> str:
    pf = num(st["profit_factor"]) if st["profit_factor"] is not None else "—"

    def row(cells: list[str], tag: str = "td") -> str:
        return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"

    trades_rows = "".join(
        row([
            datetime.fromtimestamp(t["opened_at"] / 1000, tz).strftime("%d/%m %H:%M"),
            esc(t["symbol"]), side_short(t["direction"]), price(t["entry_price"]), price(t.get("exit_price")),
            t.get("exit_reason", ""), money(t["pnl_usdt"], True), duration(t["closed_at"] - t["opened_at"]),
        ])
        for t in st["list"]
    ) or row(["Sin operaciones en el período"] + [""] * 7)

    symbol_rows = "".join(
        row([esc(sym), str(r["trades"]), pct(r["wins"] / r["trades"] * 100, decimals=0), money(r["pnl"], True)])
        for sym, r in sorted(st["by_symbol"].items(), key=lambda kv: -kv[1]["pnl"])
    ) or row(["—", "0", "—", "$0,00"])

    rejection_rows = "".join(row([esc(reason), str(n)]) for reason, n in st["rejections"].most_common(8)) \
        or row(["Ninguna", "0"])

    carry = st.get("carry") or {}
    carry_html = ""
    if carry.get("active"):
        carry_rows = "".join(
            row([esc(sym), money(r["funding"], True), money(-r["fees"], True), money(r["realized"], True),
                 money(r["net"], True)])
            for sym, r in sorted(carry["by_symbol"].items(), key=lambda kv: -kv[1]["net"]))
        carry_html = f"""<h2>Carry (captura de funding){' — SIMULADO' if carry['paper'] else ''}</h2>
<p>Funding cobrado: <b>{money(carry['funding'], True)}</b> en {carry['payments']} cobros ·
Comisiones: {money(-carry['fees'], True)} · PnL de ajustes: {money(carry['realized'], True)} ·
Resultado del carry: <b>{money(carry['net'], True)}</b></p>
<table border="1" cellpadding="6">{row(["Par", "Funding", "Comisiones", "PnL ajustes", "Neto"], "th")}{carry_rows}</table>
<p>Total del período (operaciones + carry): <b>{money(st['net'] + carry['net'], True)}</b></p>"""

    best, worst = st["best"], st["worst"]
    return f"""<html><head><meta charset="utf-8"><title>{esc(period.title)}</title></head><body>
<h1>{esc(period.title)}</h1>
<p>Modo: <b>{esc(mode_label)}</b> · Generado: {datetime.now(tz):%d/%m/%Y %H:%M}</p>
<h2>Resultado</h2>
<table border="1" cellpadding="6">
{row(["Resultado neto", f"<b>{money(st['net'], True)}</b>"])}
{row(["Operaciones", f"{st['trades']} (ganadas {st['wins']} / perdidas {st['losses']})"])}
{row(["Tasa de acierto", pct(st['win_rate'], decimals=1)])}
{row(["Profit factor", pf])}
{row(["Ganancia promedio / pérdida promedio", f"{money(st['avg_win'], True)} / {money(st['avg_loss'], True)}"])}
{row(["Mejor operación", f"{esc(best['symbol'])} {money(best['pnl_usdt'], True)}" if best else "—"])}
{row(["Peor operación", f"{esc(worst['symbol'])} {money(worst['pnl_usdt'], True)}" if worst else "—"])}
{row(["Drawdown máximo", money(-st['max_drawdown'], True)])}
{row(["Racha máxima de pérdidas", str(st['max_loss_streak'])])}
{row(["Comisiones + funding", money(-st['fees'], True)])}
{row(["Salidas", " · ".join(f"{k}: {v}" for k, v in st['exit_reasons'].items()) or "—"])}
</table>
<h2>Por par</h2>
<table border="1" cellpadding="6">{row(["Par", "Operaciones", "Acierto", "Resultado"], "th")}{symbol_rows}</table>
{carry_html}
<h2>Actividad de análisis</h2>
<p>Zonas diarias detectadas: {st['zones']} · Cambios de tendencia 1H: {st['chochs']} ·
Retrocesos al 0.618: {st['fibs']} · Setups cancelados: {st['cancels']} · Señales de entrada: {st['entries_signaled']}</p>
<h3>Entradas rechazadas por el bot</h3>
<table border="1" cellpadding="6">{row(["Motivo", "Veces"], "th")}{rejection_rows}</table>
<h2>Detalle de operaciones</h2>
<table border="1" cellpadding="6">
{row(["Apertura", "Par", "Lado", "Entrada", "Salida", "Motivo", "Resultado", "Duración"], "th")}
{trades_rows}
</table>
</body></html>"""


class Reporter:
    def __init__(self, settings: Settings, store: Store, notify: Callable[[str], None], drive=None):
        self.s = settings
        self.store = store
        self.notify = notify
        self.drive = drive
        self.tz = ZoneInfo(settings.timezone)

    def build(self, period: Period) -> tuple[dict, str]:
        start_ms = int(period.start.timestamp() * 1000)
        end_ms = int(period.end.timestamp() * 1000)
        stats = compute_stats(self.store.closed_trades(start_ms, end_ms), self.store.events(start_ms, end_ms))
        return stats, html_report(period, stats, self.s.mode_label, self.tz)

    def send(self, period: Period) -> None:
        stats, html = self.build(period)
        link = None
        if self.drive is not None:
            try:
                name = f"{period.title} [{self.s.mode}]"
                link = self.drive.upload_html_as_doc(name, html)
            except Exception as exc:
                log.exception("No pude subir el resumen a Drive")
                self.notify(f"⚠️ No pude subir el resumen a Google Drive: {esc(exc)}")
        path = self.store.dir / "reports" / f"{period.key}.html"
        path.parent.mkdir(exist_ok=True)
        path.write_text(html, encoding="utf-8")
        self.notify(telegram_text(period, stats, self.s.mode_label, link))
        self.store.log_event("report", period=period.key, net=stats["net"], trades=stats["trades"], drive=link)

    def current_week(self) -> Period:
        now = datetime.now(self.tz)
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        iso = monday.isocalendar()
        return Period("weekly", f"{iso.year}-W{iso.week:02d}", monday, now + timedelta(seconds=1))

    def current_month(self) -> Period:
        now = datetime.now(self.tz)
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return Period("monthly", f"{first:%Y-%m}", first, now + timedelta(seconds=1))

    def maybe_send_scheduled(self) -> None:
        now = datetime.now(self.tz)
        sent = self.store.state.setdefault("reports", {})
        for period in (previous_week(now), previous_month(now)):
            if period.kind not in sent:
                # Primer arranque: no mandar resúmenes de períodos en los que el bot no existía.
                sent[period.kind] = period.key
                self.store.save()
                continue
            # 5 min de gracia para que los cierres de último momento queden registrados.
            if sent[period.kind] != period.key and now - period.end >= timedelta(minutes=5):
                try:
                    self.send(period)
                finally:
                    sent[period.kind] = period.key
                    self.store.save()
