"""
Servidor HTTP: recibe las alertas de TradingView y arranca monitor + Telegram.

  POST /tv/webhook   ← alerta de TradingView (JSON con "secret")
  GET  /tv/health    ← chequeo de vida (Docker / Caddy)

TradingView corta a los ~3 s: se responde enseguida y la señal se procesa en
segundo plano.

Arranque:  uvicorn bot.server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import hmac
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from bot.bingx import BingXClient, BingXError
from bot.config import load_settings
from bot.drive import DriveUploader
from bot.executor import Executor
from bot.monitor import Monitor
from bot.narrator import Narrator
from bot.reports import Reporter
from bot.scanner import Scanner
from bot.signals import Signal
from bot.store import Store
from bot.telegram import TelegramBot

# IPs desde las que TradingView manda webhooks (documentación oficial de TradingView).
TRADINGVIEW_IPS = {"52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"}

log = logging.getLogger("bot")


def setup_logging(data_dir) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):  # consolas Windows (cp1252) no rompen con emojis
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    file = RotatingFileHandler(data_dir / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(fmt)
    root.addHandler(stream)
    root.addHandler(file)


class App:
    """Contenedor de todas las piezas, armado al arrancar."""

    def __init__(self):
        self.settings = load_settings()
        errors = self.settings.validate()
        if errors:
            raise SystemExit("Configuración inválida:\n- " + "\n- ".join(errors))
        s = self.settings
        setup_logging(s.data_dir)

        self.store = Store(s.data_dir)
        self.client = BingXClient(s.bingx_api_key, s.bingx_api_secret, s.mode)
        self.narrator = Narrator(s.mode_label, s.timezone)
        self.telegram = TelegramBot(s)
        drive = DriveUploader(s.google_client_id, s.google_client_secret, s.google_refresh_token,
                              self.store, s.google_drive_folder_id) if s.drive_enabled else None
        self.reporter = Reporter(s, self.store, self.telegram.send, drive)
        self.executor = Executor(s, self.client, self.store, self.narrator, self.telegram.send)
        self.monitor = Monitor(s, self.client, self.store, self.executor, self.narrator,
                               self.reporter, self.telegram.send)
        self.scanner = None if s.uses_tradingview else Scanner(
            s, self.client, self.store, self.executor, self.telegram.send, delay_s=s.scan_delay_s,
            market=BingXClient("", "", "live"))
        self.telegram.client, self.telegram.store = self.client, self.store
        self.telegram.executor, self.telegram.reporter = self.executor, self.reporter
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="signal")

    def startup(self) -> None:
        s = self.settings
        log.info("Arrancando bot BingX en modo %s con %s", s.mode, s.symbols)
        specs = self.client.contracts(force=True)
        missing = [sym for sym in s.symbols if sym not in specs]
        if missing:
            self.telegram.send(self.narrator.alert(f"Estos pares no existen en BingX ({s.mode}): {missing}"))

        try:
            if self.client.is_hedge_mode():
                if self.client.positions():
                    self.telegram.send(self.narrator.alert(
                        "La cuenta está en modo cobertura (hedge) y tiene posiciones abiertas. "
                        "Cerralas y reiniciá el bot: necesita modo unidireccional.", critical=True))
                else:
                    self.client.set_one_way_mode()
                    log.info("Cuenta pasada a modo unidireccional (one-way)")
        except BingXError as exc:
            log.warning("No pude verificar el modo de posición: %s", exc)

        balance = None
        try:
            balance = self.client.balance()["balance"]
        except BingXError as exc:
            self.telegram.send(self.narrator.alert(f"No pude leer el saldo de BingX: {exc}", critical=True))

        self.monitor.start()
        if self.scanner is not None:
            self.scanner.start()
        self.telegram.start_listener()
        self.telegram.send(self.narrator.started(balance, s.symbols, s.margin_per_trade_usdt))

    def shutdown(self) -> None:
        self.monitor.stop()
        if self.scanner is not None:
            self.scanner.stop()
        self.telegram.stop()
        self.pool.shutdown(wait=True, cancel_futures=False)


state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    bot = App()
    bot.startup()
    state["bot"] = bot
    yield
    bot.shutdown()


app = FastAPI(title="Bot BingX", lifespan=lifespan, docs_url=None, redoc_url=None)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else ""


@app.get("/tv/health")
def health():
    bot: App = state["bot"]
    beat = bot.store.heartbeat_path
    age = time.time() - int(beat.read_text()) if beat.exists() else None
    ok = age is not None and age < bot.settings.monitor_interval_s * 5
    body = {"ok": ok, "mode": bot.settings.mode, "source": bot.settings.strategy_source, "heartbeat_age_s": age}
    scanner = getattr(bot, "scanner", None)
    if scanner is not None:
        # El warm-up tarda ~1 min; después el scanner tiene que correr al menos cada 5 min.
        scan_age = time.time() - scanner.last_scan_ok if scanner.last_scan_ok else None
        body["scan_age_s"] = scan_age
        if scanner.ready and (scan_age is None or scan_age > 15 * 60):
            ok = body["ok"] = False
    return JSONResponse(body, status_code=200 if ok else 503)


@app.post("/tv/webhook")
async def tradingview_webhook(request: Request):
    bot: App = state["bot"]
    ip = _client_ip(request)
    # Loopback permitido para `python -m bot.cli test-signal` dentro del container.
    loopback = ip in ("127.0.0.1", "::1")
    if not bot.settings.uses_tradingview and not loopback:
        # Con el motor interno, TradingView no puede disparar operaciones.
        log.warning("Webhook ignorado (STRATEGY_SOURCE=internal) desde %s", ip)
        return JSONResponse({"error": "tradingview webhook disabled"}, status_code=403)
    if bot.settings.enforce_tv_ips and ip not in TRADINGVIEW_IPS and not loopback:
        log.warning("Webhook rechazado por IP %s", ip)
        return JSONResponse({"error": "forbidden"}, status_code=403)

    raw = await request.body()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Webhook con cuerpo no-JSON: %r", raw[:200])
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not hmac.compare_digest(str(payload.get("secret", "")), bot.settings.webhook_secret):
        log.warning("Webhook con secret inválido desde %s", ip)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        signal = Signal.model_validate(payload)
    except ValidationError as exc:
        log.warning("Webhook inválido: %s", exc)
        bot.store.log_event("invalid_signal", error=str(exc)[:500])
        return JSONResponse({"error": "invalid payload"}, status_code=422)

    bot.pool.submit(_process, bot, signal)
    return {"status": "accepted", "id": signal.id, "event": signal.event}


def _process(bot: App, signal: Signal) -> None:
    try:
        result = bot.executor.handle(signal)
        log.info("Señal %s/%s → %s", signal.id, signal.event, result.get("status"))
    except Exception as exc:
        log.exception("Error procesando señal %s", signal.id)
        bot.telegram.send(bot.narrator.alert(f"Error interno procesando señal de {signal.symbol}: {exc}",
                                             critical=True))
