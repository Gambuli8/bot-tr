"""
Configuración del bot. Todo sale de variables de entorno (.env en local,
env_file en Docker). Nada de secretos en el código.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_SYMBOLS = "BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,ZEC-USDT,DOGE-USDT"


def _clean(raw: str | None) -> str:
    """Corta comentarios inline (`VAR=valor  # comentario`) que python-dotenv deja pasar."""
    if raw is None:
        return ""
    return raw.split(" #", 1)[0].strip()


def _env(name: str, default: str = "") -> str:
    value = _clean(os.getenv(name))
    return value if value else default


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in ("1", "true", "yes", "si", "sí")


def parse_id_list(raw: str) -> set[str]:
    """IDs de Telegram: solo enteros (pueden ser negativos en grupos)."""
    out: set[str] = set()
    for item in (raw or "").split(","):
        token = item.split("#", 1)[0].strip()
        try:
            int(token)
        except ValueError:
            continue
        out.add(token)
    return out


@dataclass
class Settings:
    # BingX
    bingx_api_key: str
    bingx_api_secret: str
    mode: str  # "demo" (VST, dinero falso) | "live"

    # Operativa
    symbols: list[str]
    margin_per_trade_usdt: float
    max_leverage: int
    margin_type: str  # ISOLATED | CROSSED
    min_rr: float
    max_open_positions: int
    daily_loss_limit_usdt: float
    max_signal_age_s: int
    max_slippage_pct: float

    # Origen de las señales: "internal" (el bot analiza solo) | "tradingview" (alertas webhook)
    strategy_source: str
    scan_delay_s: float

    # Tamaño: "margin" (margen fijo, apalancamiento mínimo) | "risk" (riesgo fijo en USDT por operación)
    sizing_mode: str
    risk_per_trade_usdt: float
    # Estrategia: tipo de SL (fib | atr | structure) y filtro de tendencia EMA50 diaria
    sl_mode: str
    filter_trend: bool

    # Webhook TradingView
    webhook_secret: str
    enforce_tv_ips: bool

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_admin_ids: set[str]

    # Google Drive (opcional)
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    google_drive_folder_id: str

    # Runtime
    data_dir: Path
    timezone: str
    monitor_interval_s: int
    port: int

    # Modo carry (captura de funding: spot + short)
    carry_enabled: bool = False
    carry_symbols: list = field(default_factory=list)
    carry_capital_usdt: float = 0.0
    carry_leverage: float = 2.0
    carry_rebalance_pct: float = 0.3
    carry_reinvest_pct: float = 0.03
    carry_min_trade_usdt: float = 5.0
    carry_interval_s: int = 300
    carry_asset: str = ""

    extra: dict = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def mode_label(self) -> str:
        return "REAL 💵" if self.is_live else "DEMO 🧪"

    def sizing_label(self) -> str:
        from bot.fmt import money
        if self.sizing_mode == "risk":
            return f"Riesgo fijo por operación: {money(self.risk_per_trade_usdt)}"
        return f"Margen fijo por operación: {money(self.margin_per_trade_usdt)}"

    def rules_label(self) -> str:
        from bot.fmt import ratio
        sl = {"fib": "SL Fibo 0,786", "atr": "SL 0,75 − ATR 1H", "structure": "SL en inicio del impulso"}.get(
            self.sl_mode, self.sl_mode)
        trend = " · sólo a favor de la EMA50 diaria" if self.filter_trend else ""
        return f"{sl} · TP techo del impulso · R:R mín. 1 : {ratio(self.min_rr)}{trend}"

    def strategy_params(self):
        from bot.strategy import StrategyParams
        return StrategyParams(sl_mode=self.sl_mode, filter_trend=self.filter_trend)

    @property
    def directional_symbols(self) -> list[str]:
        """Pares de la estrategia direccional: se excluyen los del carry (en one-way se anularían)."""
        excluded = set(self.carry_symbols) if self.carry_enabled else set()
        return [s for s in self.symbols if s not in excluded]

    @property
    def uses_tradingview(self) -> bool:
        return self.strategy_source == "tradingview"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def drive_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_refresh_token)

    def validate(self) -> list[str]:
        errors = []
        if self.mode not in ("demo", "live"):
            errors.append("BINGX_MODE debe ser 'demo' o 'live'")
        if not self.bingx_api_key or not self.bingx_api_secret:
            errors.append("Faltan BINGX_API_KEY / BINGX_API_SECRET")
        if self.strategy_source not in ("internal", "tradingview"):
            errors.append("STRATEGY_SOURCE debe ser 'internal' o 'tradingview'")
        if len(self.webhook_secret) < 16:
            errors.append("WEBHOOK_SECRET debe tener al menos 16 caracteres")
        if self.sizing_mode not in ("margin", "risk"):
            errors.append("SIZING_MODE debe ser 'margin' o 'risk'")
        if self.sizing_mode == "margin" and not 0.5 <= self.margin_per_trade_usdt <= 5:
            errors.append("MARGIN_PER_TRADE_USDT fuera de rango seguro (0.5–5)")
        if self.sizing_mode == "risk" and not 0.1 <= self.risk_per_trade_usdt <= 5:
            errors.append("RISK_PER_TRADE_USDT fuera de rango seguro (0.1–5)")
        if self.carry_enabled:
            if not 1 <= self.carry_leverage <= 5:
                errors.append("CARRY_LEVERAGE fuera de rango seguro (1–5)")
            if self.carry_capital_usdt <= 0 or not self.carry_symbols:
                errors.append("CARRY_CAPITAL_USDT y CARRY_SYMBOLS son obligatorios con CARRY_ENABLED=true")
        if self.sl_mode not in ("fib", "atr", "structure"):
            errors.append("SL_MODE debe ser fib, atr o structure")
        if not 1 <= self.max_leverage <= 25:
            errors.append("MAX_LEVERAGE fuera de rango seguro (1–25)")
        if self.margin_type not in ("ISOLATED", "CROSSED"):
            errors.append("MARGIN_TYPE debe ser ISOLATED o CROSSED")
        if not self.symbols:
            errors.append("SYMBOLS vacío")
        return errors


def normalize_symbol(raw: str) -> str:
    """
    Lleva cualquier formato al de BingX: 'BINGX:BTCUSDT.P' / 'BTCUSDT' /
    'BTC/USDT' / 'btc-usdt' → 'BTC-USDT'.
    """
    s = raw.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    for suffix in (".P", ".PS", "PERP", "_PERP"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    s = s.replace("/", "-").replace("_", "-")
    if "-" not in s and s.endswith("USDT"):
        s = s[:-4] + "-USDT"
    return s


def load_settings(env_file: str | None = None) -> Settings:
    load_dotenv(env_file or ".env", override=False)
    chat_id = _env("TELEGRAM_CHAT_ID")
    admins = parse_id_list(_env("TELEGRAM_ADMIN_IDS")) or parse_id_list(chat_id)
    return Settings(
        bingx_api_key=_env("BINGX_API_KEY"),
        bingx_api_secret=_env("BINGX_API_SECRET"),
        mode=_env("BINGX_MODE", "demo").lower(),
        symbols=[normalize_symbol(s) for s in _env("SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()],
        margin_per_trade_usdt=_env_float("MARGIN_PER_TRADE_USDT", 2.0),
        max_leverage=_env_int("MAX_LEVERAGE", 20),
        margin_type=_env("MARGIN_TYPE", "ISOLATED").upper(),
        min_rr=_env_float("MIN_RR", 1.5),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", 3),
        daily_loss_limit_usdt=_env_float("DAILY_LOSS_LIMIT_USDT", 3.0),
        max_signal_age_s=_env_int("MAX_SIGNAL_AGE_S", 180),
        max_slippage_pct=_env_float("MAX_SLIPPAGE_PCT", 0.4),
        strategy_source=_env("STRATEGY_SOURCE", "internal").lower(),
        scan_delay_s=_env_float("SCAN_DELAY_S", 8.0),
        sizing_mode=_env("SIZING_MODE", "margin").lower(),
        risk_per_trade_usdt=_env_float("RISK_PER_TRADE_USDT", 0.5),
        sl_mode=_env("SL_MODE", "fib").lower(),
        filter_trend=_env_bool("FILTER_TREND", False),
        carry_enabled=_env_bool("CARRY_ENABLED", False),
        carry_symbols=[normalize_symbol(s) for s in _env("CARRY_SYMBOLS", "BTC-USDT,ETH-USDT,DOGE-USDT,XRP-USDT").split(",") if s.strip()],
        carry_capital_usdt=_env_float("CARRY_CAPITAL_USDT", 0.0),
        carry_leverage=_env_float("CARRY_LEVERAGE", 2.0),
        carry_rebalance_pct=_env_float("CARRY_REBALANCE_PCT", 0.3),
        carry_reinvest_pct=_env_float("CARRY_REINVEST_PCT", 0.03),
        carry_min_trade_usdt=_env_float("CARRY_MIN_TRADE_USDT", 5.0),
        carry_interval_s=_env_int("CARRY_INTERVAL_S", 300),
        carry_asset=_env("CARRY_ASSET", ""),
        webhook_secret=_env("WEBHOOK_SECRET"),
        enforce_tv_ips=_env_bool("ENFORCE_TV_IPS", True),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=chat_id,
        telegram_admin_ids=admins,
        google_client_id=_env("GOOGLE_CLIENT_ID"),
        google_client_secret=_env("GOOGLE_CLIENT_SECRET"),
        google_refresh_token=_env("GOOGLE_REFRESH_TOKEN"),
        google_drive_folder_id=_env("GOOGLE_DRIVE_FOLDER_ID"),
        data_dir=Path(_env("DATA_DIR", "data")),
        timezone=_env("TIMEZONE", "America/Argentina/Buenos_Aires"),
        monitor_interval_s=_env_int("MONITOR_INTERVAL_S", 20),
        port=_env_int("PORT", 8080),
    )
