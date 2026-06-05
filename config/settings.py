"""
config/settings.py
Carga y valida todas las variables de entorno.
Estrategia: Trend Following + Donchian + Claude (o sin Claude en TESTING_MODE).
"""

import os
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

load_dotenv()


class Settings(BaseModel):
    # Exchange
    binance_api_key: str
    binance_api_secret: str
    binance_testnet: bool = True

    # Claude
    anthropic_api_key: str

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str
    # Listas opcionales separadas por coma. Si vacías, sólo telegram_chat_id manda.
    telegram_admin_chat_ids: str = ""       # acceso total
    telegram_readonly_chat_ids: str = ""    # sólo lectura

    # Trading
    symbol: str = "BTC/USDT"                 # símbolo "primario" (retrocompat / single-symbol)
    # Multi-symbol: lista de pares a escanear y operar. Si vacía, cae a [symbol].
    symbols: list[str] = ["BTC/USDT"]
    # Candado de exposición global: máximo de posiciones abiertas en simultáneo
    # en todo el portafolio. Protege el capital compartido (210 USDT).
    max_concurrent_trades: int = 2
    timeframe: str = "15m"
    initial_capital: float = 1000.0
    max_risk_per_trade: float = 0.015
    daily_drawdown_limit: float = 0.10
    min_claude_confidence: float = 0.55
    trade_reserve_pct: float = 0.30

    # MODO TESTING (sin Claude, reglas técnicas duras, más agresivo)
    testing_mode: bool = False
    testing_loop_seconds: int = 60          # ciclo cada 1 minuto en testing

    # Indicadores - Trend
    ema_fast: int = 50
    ema_slow: int = 200

    # Indicadores - Momentum
    rsi_period: int = 14
    rsi_oversold: float = 35.0
    rsi_overbought: float = 65.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # Donchian Channel
    donchian_period: int = 10

    # Volatilidad
    atr_period: int = 14
    atr_sl_multiplier: float = 2.0
    min_risk_reward: float = 2.0

    # Trailing Stop
    trailing_stop_enabled: bool = True
    trailing_activation_pct: float = 0.02
    trailing_distance_pct: float = 0.015
    # Trailing dinámico (recomendado): activa al alcanzar el RR original (1:1 vs SL).
    # Mueve el SL a breakeven en activación y luego sigue al precio a 1.5 × ATR.
    dynamic_trailing_enabled: bool = False
    dynamic_trailing_atr_mult: float = 1.5
    # Si dynamic_trailing_enabled, ignoramos TP fijo: dejamos correr con el trailing.
    disable_fixed_tp_with_trailing: bool = True

    # ─── TP escalado + breakeven shift ───
    # Toma ganancia parcial en TP1 (a tp1_r_multiple × la distancia del SL) y,
    # al tocarlo, mueve el SL a breakeven. El remanente corre al TP completo
    # (o al trailing si está activo). "Trade gratis" para la segunda mitad.
    scaled_tp_enabled: bool = False
    tp1_r_multiple: float = 1.0            # TP1 a 1× el riesgo (R:R 1:1)
    tp1_size_pct: float = 0.5             # fracción de la posición cerrada en TP1
    breakeven_after_tp1: bool = True      # tras TP1, mover SL al entry
    breakeven_offset_pct: float = 0.0005  # colchón sobre el entry (cubre fees/slippage)

    # ─── Mejoras de precisión (off por default; se activan con env vars) ───
    # Cooldown: N velas mínimas entre cerrar una posición y abrir otra.
    cooldown_bars: int = 0
    # Filtro ADX: no operar si ADX < umbral (mercado lateral / sin fuerza).
    adx_period: int = 14
    adx_min_trending: float = 0.0          # 0 = filtro off
    # Confirmación macro: setup A LONG sólo si precio > EMA200, SHORT si <.
    require_macro_trend: bool = False
    # MTF confluence: descartar setups contra tendencia 1h.
    require_mtf_confluence: bool = False
    mtf_strict_4h: bool = False             # también exigir 4h alineado
    # Horario activo en UTC. Formato: "11-23" o "11-23,2-5" (multi-rango).
    # Vacío = 24/7. AR = UTC-3 → para operar 9-22 AR usar "12-1" (cruza medianoche).
    active_hours_utc: str = ""
    # Kelly fraccionado: tamaño dinámico de posición según edge reciente.
    use_kelly_sizing: bool = False
    kelly_min_trades: int = 10
    kelly_fraction: float = 0.5            # half-Kelly (más conservador)
    kelly_min_risk_pct: float = 0.005      # piso 0.5%
    kelly_max_risk_pct: float = 0.02       # techo 2%

    # Warm-up
    warmup_candles: int = 220

    @field_validator(
        "binance_api_key", "binance_api_secret", "anthropic_api_key",
        "telegram_bot_token", "telegram_chat_id"
    )
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or v.startswith("your_"):
            raise ValueError(f"Variable no configurada: '{v}'. Revisá tu .env")
        return v


def _parse_symbols(raw: str, primary: str) -> list[str]:
    """
    Parsea la lista de símbolos de la env SYMBOLS (separados por coma).
    Normaliza a formato ccxt "BASE/QUOTE" (acepta "ETHUSDT" o "ETH/USDT").
    Si la lista queda vacía, cae al símbolo primario. De-duplica preservando orden.
    """
    out: list[str] = []
    for tok in (raw or "").split(","):
        s = tok.strip().upper().replace(" ", "")
        if not s:
            continue
        if "/" not in s and s.endswith("USDT"):
            s = f"{s[:-4]}/USDT"
        if s not in out:
            out.append(s)
    if not out:
        out = [primary]
    return out


def load_settings() -> Settings:
    primary_symbol = os.getenv("TRADING_SYMBOL", "BTC/USDT")
    return Settings(
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
        binance_testnet=os.getenv("BINANCE_TESTNET", "true").lower() == "true",
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        telegram_admin_chat_ids=os.getenv("TELEGRAM_ADMIN_CHAT_IDS", os.getenv("TELEGRAM_CHAT_ID", "")),
        telegram_readonly_chat_ids=os.getenv("TELEGRAM_READONLY_CHAT_IDS", ""),
        symbol=primary_symbol,
        symbols=_parse_symbols(os.getenv("SYMBOLS", ""), primary_symbol),
        max_concurrent_trades=int(os.getenv("MAX_CONCURRENT_TRADES", "2")),
        timeframe=os.getenv("TRADING_TIMEFRAME", "15m"),
        initial_capital=float(os.getenv("INITIAL_CAPITAL", "1000")),
        max_risk_per_trade=float(os.getenv("MAX_RISK_PER_TRADE", "0.015")),
        daily_drawdown_limit=float(os.getenv("DAILY_DRAWDOWN_LIMIT", "0.10")),
        min_claude_confidence=float(os.getenv("MIN_CLAUDE_CONFIDENCE", "0.55")),
        testing_mode=os.getenv("TESTING_MODE", "false").lower() == "true",
        testing_loop_seconds=int(os.getenv("TESTING_LOOP_SECONDS", "60")),
        cooldown_bars=int(os.getenv("COOLDOWN_BARS", "0")),
        adx_min_trending=float(os.getenv("ADX_MIN_TRENDING", "0")),
        require_macro_trend=os.getenv("REQUIRE_MACRO_TREND", "false").lower() == "true",
        use_kelly_sizing=os.getenv("USE_KELLY_SIZING", "false").lower() == "true",
        require_mtf_confluence=os.getenv("REQUIRE_MTF_CONFLUENCE", "false").lower() == "true",
        active_hours_utc=os.getenv("ACTIVE_HOURS_UTC", ""),
        dynamic_trailing_enabled=os.getenv("DYNAMIC_TRAILING", "false").lower() == "true",
        scaled_tp_enabled=os.getenv("SCALED_TP", "false").lower() == "true",
        tp1_r_multiple=float(os.getenv("TP1_R_MULTIPLE", "1.0")),
        tp1_size_pct=float(os.getenv("TP1_SIZE_PCT", "0.5")),
        breakeven_after_tp1=os.getenv("BREAKEVEN_AFTER_TP1", "true").lower() == "true",
    )
