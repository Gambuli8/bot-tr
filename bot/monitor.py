"""
Monitor en segundo plano (cada MONITOR_INTERVAL_S):
  - Detecta operaciones que se cerraron en BingX (TP, SL, manual) → calcula el
    resultado real (PnL + comisiones + funding) → lo registra y avisa.
  - Detecta posiciones en el exchange que el bot no conoce (abiertas a mano o
    estado perdido) → las adopta y avisa.
  - Verifica cada tanto que las posiciones abiertas sigan teniendo Stop Loss.
  - Dispara los resúmenes semanales y mensuales.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from bot.bingx import BingXClient, BingXError, Position
from bot.config import Settings
from bot.executor import Executor
from bot.narrator import Narrator
from bot.reports import Reporter
from bot.store import Store

log = logging.getLogger(__name__)

PROTECTION_CHECK_EVERY = 6  # ciclos (~2 min con intervalo de 20s)


def classify_exit(trade: dict, exit_price: Optional[float]) -> str:
    if trade.get("manual_close"):
        return "MANUAL"
    if not exit_price:
        return "OTRO"
    sl, tp = trade.get("stop_loss"), trade.get("take_profit")
    if not sl or not tp:
        return "OTRO"
    d_sl, d_tp = abs(exit_price - sl), abs(exit_price - tp)
    tolerance = abs(tp - sl) * 0.15
    if d_tp <= tolerance and d_tp <= d_sl:
        return "TP"
    if d_sl <= tolerance:
        return "SL"
    return "OTRO"


class Monitor:
    def __init__(self, settings: Settings, client: BingXClient, store: Store, executor: Executor,
                 narrator: Narrator, reporter: Reporter, notify: Callable[[str], None]):
        self.s = settings
        self.client = client
        self.store = store
        self.executor = executor
        self.narrator = narrator
        self.reporter = reporter
        self.notify = notify
        self._stop = threading.Event()
        self._cycle = 0
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # el monitor no puede morir por un error puntual
                log.exception("Error en ciclo del monitor")
            self.store.beat()
            self._stop.wait(self.s.monitor_interval_s)

    def run_once(self) -> None:
        self._cycle += 1
        try:  # los resúmenes no dependen de que BingX responda
            self.reporter.maybe_send_scheduled()
        except Exception:
            log.exception("Error enviando resúmenes programados")
        try:
            positions = {p.symbol: p for p in self.client.positions()}
        except BingXError as exc:
            log.warning("Monitor: no pude leer posiciones: %s", exc)
            return

        for symbol in list(self.store.open_trades):
            if symbol not in positions:
                self._on_closed(symbol)

        for symbol, pos in positions.items():
            if symbol not in self.store.open_trades:
                self._adopt(pos)
            elif self._cycle % PROTECTION_CHECK_EVERY == 0:
                trade = self.store.open_trades[symbol]
                self.executor.ensure_protection(symbol, pos, trade["sl_str"], trade["tp_str"])

    # ───────── cierres ─────────

    def _on_closed(self, symbol: str) -> None:
        trade = self.store.open_trades.get(symbol)
        if trade is None:
            return
        now_ms = int(time.time() * 1000)
        pnl, fees, exit_price = self._realized(symbol, trade["opened_at"], now_ms)

        try:  # restos: SL o TP que quedó colgado tras el cierre
            self.client.cancel_all_orders(symbol)
        except BingXError as exc:
            log.warning("No pude limpiar órdenes de %s: %s", symbol, exc)

        record = {
            **trade,
            "closed_at": now_ms,
            "exit_price": exit_price,
            "pnl_usdt": round(pnl, 6),
            "fees_usdt": round(fees, 6),
            "exit_reason": classify_exit(trade, exit_price),
        }
        self.store.log_closed_trade(record)
        self.store.pop_open_trade(symbol)
        self.store.log_event("closed", symbol=symbol, pnl_usdt=record["pnl_usdt"], reason=record["exit_reason"])
        self.notify(self.narrator.trade_closed(record, day_total=self.executor.todays_realized_pnl()))

    def _realized(self, symbol: str, start_ms: int, end_ms: int) -> tuple[float, float, Optional[float]]:
        """(pnl_neto, comisiones+funding, precio_de_salida). Espera unos segundos a que BingX lo registre."""
        pnl = fees = 0.0
        exit_price: Optional[float] = None
        for _ in range(3):
            try:
                rows = self.client.income(symbol, start_ms - 60_000, end_ms + 60_000)
            except BingXError as exc:
                log.warning("income %s: %s", symbol, exc)
                rows = []
            realized = [r for r in rows if r.get("incomeType") == "REALIZED_PNL"]
            if realized:
                pnl_gross = sum(float(r.get("income", 0)) for r in realized)
                fees = sum(float(r.get("income", 0)) for r in rows
                           if r.get("incomeType") in ("TRADING_FEE", "FUNDING_FEE"))
                pnl = pnl_gross + fees  # las comisiones vienen negativas
                fees = -fees
                break
            time.sleep(3)

        history = self.client.position_history(symbol, start_ms - 60_000, end_ms + 60_000)
        if history:
            last = max(history, key=lambda h: int(h.get("updateTime", 0) or 0))
            exit_price = float(last.get("avgClosePrice", 0) or 0) or None
            if not pnl and last.get("netProfit") is not None:
                pnl = float(last["netProfit"])
        if exit_price is None:
            try:
                exit_price = self.client.price(symbol)
            except BingXError:
                exit_price = None
        return pnl, fees, exit_price

    # ───────── posiciones desconocidas ─────────

    def _adopt(self, pos: Position) -> None:
        alerted = self.store.state.setdefault("alerts", {})
        key = f"adopt:{pos.symbol}:{pos.position_id}"
        if key in alerted:
            return
        alerted[key] = int(time.time())
        self.store.save()
        self.notify(self.narrator.alert(
            f"Hay una posición {pos.side} en {pos.symbol} que no abrió el bot "
            f"(cantidad {pos.qty}, entrada {pos.entry_price}). No la toco: gestionala en BingX "
            f"o cerrala con /cerrar {pos.symbol.split('-')[0]}."))
        self.store.log_event("unknown_position", symbol=pos.symbol, side=pos.side, qty=pos.qty)
