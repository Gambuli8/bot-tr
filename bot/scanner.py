"""
Scanner: el bot analiza solo (sin alertas de TradingView).

Cada 5 minutos, apenas cierra la vela:
  1. Baja velas cerradas de 1D, 1H y 5m de cada par (API pública de BingX).
  2. Pasa las velas nuevas de 5m por el motor (bot/strategy.py).
  3. Cada evento (zona, cambio 1H, 0,618, cancelado, entrada) va al ejecutor, que
     narra por Telegram y, si es entrada, valida y opera.

Al arrancar relee ~5 días de historia EN SILENCIO para reconstruir los setups en
curso (no manda mensajes ni opera por velas viejas).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from bot.bingx import BingXClient, BingXError
from bot.config import Settings
from bot.executor import Executor
from bot.signals import Signal
from bot.store import Store
from bot.strategy import (MS_5M, STAGE_LABELS, StrategyParams, SymbolStrategy, build_bars,
                          build_daily_zones, build_hourly)

log = logging.getLogger(__name__)

WARMUP_5M_BARS = 1500      # ~5,2 días: cubre los vencimientos de 48 h + 48 h
LIVE_5M_BARS = 60          # ventana en cada ciclo (pivotes necesitan velas previas)
HOURLY_BARS = 400
DAILY_BARS = 400
LIVE_GRACE_MS = 10 * 60_000  # tras un reinicio, sólo se narran velas cerradas hace < 10 min


class Scanner:
    def __init__(self, settings: Settings, client: BingXClient, store: Store, executor: Executor,
                 notify: Callable[[str], None], params: Optional[StrategyParams] = None,
                 delay_s: float = 8.0, market: Optional[BingXClient] = None):
        self.s = settings
        # Velas del mercado REAL aunque se opere en demo: es lo mismo que muestra TradingView.
        self.client = market or client
        self.store = store
        self.executor = executor
        self.notify = notify
        self.params = params or settings.strategy_params()
        self.delay_s = delay_s
        self.engines = {sym: SymbolStrategy(sym, self.params) for sym in settings.symbols}
        self.last_index: dict[str, int] = {}
        self._zones_cache: dict[str, tuple[int, object]] = {}
        self._last_close: dict[str, float] = {}
        self._stop = threading.Event()
        self.last_scan_ok: Optional[float] = None
        self.ready = False

    # ───────── ciclo de vida ─────────

    def start(self) -> None:
        threading.Thread(target=self._loop, name="scanner", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        self.warmup()
        while not self._stop.is_set():
            self._stop.wait(self.seconds_to_next_close())
            if self._stop.is_set():
                break
            self.scan_all()

    def seconds_to_next_close(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        next_close = (int(now * 1000) // MS_5M + 1) * MS_5M / 1000
        return max(1.0, next_close + self.delay_s - now)

    # ───────── datos ─────────

    def _context(self, symbol: str, m5_bars: int):
        daily = self.client.klines_history(symbol, "1d", DAILY_BARS)
        last_day = daily[-1]["time"] if daily else 0
        cached = self._zones_cache.get(symbol)
        if cached and cached[0] == last_day:
            zones = cached[1]
        else:
            zones = build_daily_zones(daily, self.params)
            self._zones_cache[symbol] = (last_day, zones)
        hourly = build_hourly(self.client.klines_history(symbol, "1h", HOURLY_BARS), self.params)
        m5 = self.client.klines_history(symbol, "5m", m5_bars)
        return build_bars(m5, hourly, zones, self.params)

    # ───────── arranque ─────────

    def warmup_symbol(self, symbol: str) -> bool:
        """Reconstruye el estado de un par releyendo su historia. False si no hubo datos."""
        engine = self.engines[symbol] = SymbolStrategy(symbol, self.params)
        try:
            bars = self._context(symbol, WARMUP_5M_BARS)
        except BingXError as exc:
            log.warning("Warm-up %s: %s", symbol, exc)
            return False
        if not bars:
            return False
        persisted = self.store.state.setdefault("scanner", {}).get("last_index", {})
        prev = persisted.get(symbol)
        now_ms = int(time.time() * 1000)
        for bar in bars:
            events = engine.on_bar(bar)
            # Tras un reinicio corto, las velas que el bot no llegó a ver y son recientes sí se narran.
            if prev is not None and bar.index > prev and now_ms - bar.close_time <= LIVE_GRACE_MS:
                self._dispatch(events)
        self.last_index[symbol] = bars[-1].index
        self._last_close[symbol] = bars[-1].close
        log.info("Warm-up %s: %d velas 5m, setups activos: %d",
                 symbol, len(bars), sum(1 for s in engine.setups() if s.state > 0))
        return True

    def warmup(self) -> None:
        failed = []
        for symbol in list(self.engines):
            if not self.warmup_symbol(symbol):
                failed.append(symbol.split("-")[0])

        self._persist()
        self.sync_setups()
        self.ready = True
        self.last_scan_ok = time.time()
        pairs = " · ".join(sym.split("-")[0] for sym in self.engines)
        warn = (f"\n\n⚠️ No pude leer el historial de {', '.join(failed)}: lo reintento en cada ciclo."
                if failed else "")
        self.notify(f"🧠 <b>Motor de análisis listo</b>\n\n"
                    f"📚 Revisé los últimos ~5 días de {pairs}.\n"
                    f"⏱️ A partir de ahora analizo cada vela de 5 minutos.\n\n"
                    f"👇 Qué quedó en curso en cada moneda:{warn}")
        narrator = getattr(self.executor, "narrator", None)
        if narrator is None:
            return
        setups = self.store.state.get("setups", {})
        for symbol in self.engines:
            if symbol.split("-")[0] in failed:
                continue
            last = self._last_close.get(symbol)
            self.notify(narrator.coin_status(
                symbol=symbol, last_price=last, position=None, trade=None,
                setups=[v for v in setups.values() if v.get("symbol") == symbol]))

    # ───────── ciclo en vivo ─────────

    def scan_all(self) -> None:
        ok = True
        for symbol in list(self.engines):
            if symbol not in self.last_index:  # el warm-up de este par falló: se reintenta
                ok = self.warmup_symbol(symbol) and ok
                continue
            engine = self.engines[symbol]
            try:
                bars = self._context(symbol, LIVE_5M_BARS)
            except BingXError as exc:
                log.warning("Scan %s: %s", symbol, exc)
                ok = False
                continue
            last = self.last_index.get(symbol)
            fresh = [b for b in bars if last is None or b.index > last]
            if last is not None and fresh and fresh[0].index > last + 1:
                log.warning("Scan %s: faltan %d velas de 5m (hueco de datos o bot frenado)",
                            symbol, fresh[0].index - last - 1)
            now_ms = int(time.time() * 1000)
            for bar in fresh:
                events = engine.on_bar(bar)
                if now_ms - bar.close_time <= LIVE_GRACE_MS:
                    self._dispatch(events)
                self.last_index[symbol] = bar.index
        self._persist()
        self.sync_setups()
        if ok:
            self.last_scan_ok = time.time()

    def _dispatch(self, events: list[dict]) -> None:
        for ev in events:
            try:
                signal = Signal(**ev)
            except Exception as exc:  # un evento mal formado no puede frenar el scanner
                log.error("Evento inválido del motor %s: %s", ev, exc)
                continue
            try:
                result = self.executor.handle(signal)
                log.info("Evento %s %s %s → %s", signal.symbol, signal.side, signal.event, result.get("status"))
            except Exception:
                log.exception("Error manejando evento %s", ev)

    # ───────── estado visible ─────────

    def _persist(self) -> None:
        self.store.state.setdefault("scanner", {})["last_index"] = dict(self.last_index)
        self.store.save()

    def sync_setups(self) -> None:
        """Deja /estado igual al motor (incluye setups reconstruidos en el warm-up)."""
        for symbol, engine in self.engines.items():
            for s in (engine.long, engine.short):
                key = f"{symbol}:{s.side}"
                if s.state == 0 or not any(x is s for x in engine.setups()):
                    if key in self.store.state.get("setups", {}):
                        self.store.set_setup(key, None)
                    continue
                self.store.set_setup(key, {
                    "id": s.id, "symbol": symbol, "side": s.side, "stage": STAGE_LABELS[s.state],
                    "fib_618": s.f618, "fib_75": s.f75, "fib_sl": s.fsl,
                    "zone_low": s.zone_lo, "zone_high": s.zone_hi,
                    "updated": (s.updated or s.stage_time or 0) / 1000 or time.time(),
                })
