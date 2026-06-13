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
    symbol: str = "BTC/USDT"
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
    # TP escalado: al alcanzar TP1 cierra X% y mueve SL a breakeven.
    # Defaults: óptimo del sweep 30d (rr=1.3, pct=0.3 → WR 70.7%, PF 2.14).
    tp_scaling_enabled: bool = False
    tp1_partial_pct: float = 0.3            # fracción cerrada en TP1
    tp1_rr_multiple: float = 1.3            # TP1 = SL_pct × este multiplicador
    breakeven_buffer_pct: float = 0.0005    # SL al breakeven + buffer (5 bps)
    # Comisión por lado (Binance Spot taker = 0.10% sin BNB, 0.075% con BNB).
    # Usada en el backtester para tener PnL realista.
    commission_pct_per_side: float = 0.001
    # Taker fee: aplicado a SL y liquidaciones (órdenes Market). Default
    # 0.05% (Futures USDT-M taker).
    commission_taker_pct: float = 0.0005

    # ─── Price Action Engine (TF 1h trigger + 4h structure) ───
    pa_fractal_n: int = 3              # ventana del fractal Williams
    pa_vol_mult: float = 1.5           # volumen mínimo de la vela de sweep
    pa_atr_sl_mult: float = 1.5        # SL = mecha_sweep ± atr_sl_mult × ATR
    pa_tp_rr: float = 2.5              # TP en múltiplo del riesgo
    pa_sl_min_pct: float = 0.003       # SL mínimo en % del entry (filtro ruido)
    pa_sl_max_pct: float = 0.05        # SL máximo en % del entry (filtro extremos)

    # ─── Scalping Engine (BB squeeze + expansion, TF 5m) ───
    scalp_bb_window: int = 20
    scalp_bb_dev: int = 2
    scalp_squeeze_lookback: int = 100
    scalp_squeeze_pct: float = 20.0      # percentil del BB width para squeeze
    scalp_vol_spike: float = 2.0         # vol > N × MA(20) para confirmar breakout
    scalp_atr_window: int = 14
    scalp_sl_atr_mult: float = 1.0       # SL = 1.0 × ATR (tight)
    scalp_tp_atr_mult: float = 1.5       # TP = 1.5 × ATR (R:R 1.5)
    scalp_sl_min_pct: float = 0.001
    scalp_sl_max_pct: float = 0.02
    scalp_cooldown_bars: int = 3         # anti-ruido tras cierre
    scalp_max_fee_to_gain: float = 0.20  # rechazar señal si fees > 20% de la ganancia bruta

    # ─── Binance Futures USDT-M (Fase 3, 2026-06-08) ───
    # Leverage para todas las posiciones del bot. Aprobado por WFA con valor 7×
    # (BTC/SOL/AVAX todos ✅ con PF mediano 1.18-1.68 y OS retorno +12-19% en 180d).
    # Cambiar requiere re-validación por WFA.
    leverage: int = 7
    # Margin mode: 'isolated' aisla el capital de cada trade (si liquidan, no
    # se llevan el resto del wallet). 'cross' usa todo el wallet como margen
    # (más capital efectivo pero contagio total).
    margin_mode: str = "isolated"
    # User Data Stream (WebSocket): detecta fills de SL/TP en tiempo real y
    # dispara un reconcile inmediato (en vez de esperar el throttle de 5 min).
    # Puro enhancement, opt-in. Si está off, el bot funciona igual que siempre.
    user_stream_enabled: bool = False
    # Listener de comandos Telegram (getUpdates). Telegram permite UN solo poller
    # por bot token. En deploy multi-bot que comparte token, SOLO uno debe tenerlo
    # en true (los demás en false) para evitar el 409 Conflict constante. Las
    # notificaciones salientes siguen funcionando en todos los bots igual.
    telegram_listener_enabled: bool = True
    # Vista de portafolio en Telegram: si está seteado a un directorio que
    # contiene los data/<sym>/ de todos los bots (montado read-only en el bot
    # listener), /status muestra los 3 bots juntos. Vacío = solo este bot.
    portfolio_data_dir: str = ""
    # Alertas de riesgo proactivas (push al cruzar umbral, sin spam).
    risk_alerts_enabled: bool = True
    risk_liq_alert_pct: float = 3.0    # alerta si el precio está a <= X% de la liquidación
    risk_dd_warn_ratio: float = 0.8    # alerta si el drawdown diario >= ratio × límite
    # Filtro de horario: lista de horas UTC en las que NO operar. Default
    # empty = todas las horas habilitadas. Ej: "6,7,8,9,10,11" descarta EU AM
    # (validado en aud F: +10× retorno backtest 60d). Cargado vía env
    # SCALP_SKIP_HOURS_UTC="6-11" o lista coma-separada.
    scalp_skip_hours_utc: str = ""

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
    # Horario activo en UTC. Vacío = 24/7 (modo producción autónomo).
    active_hours_utc: str = ""
    # Cantidad máxima de posiciones abiertas en simultáneo.
    max_concurrent_trades: int = 2
    # Motor activo: "scalping" (5m BB squeeze) | "price_action" (1h sweeps) | "technical" | "claude"
    engine: str = "scalping"
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


def load_settings() -> Settings:
    return Settings(
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
        binance_testnet=os.getenv("BINANCE_TESTNET", "true").lower() == "true",
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        telegram_admin_chat_ids=os.getenv("TELEGRAM_ADMIN_CHAT_IDS", os.getenv("TELEGRAM_CHAT_ID", "")),
        telegram_readonly_chat_ids=os.getenv("TELEGRAM_READONLY_CHAT_IDS", ""),
        symbol=os.getenv("TRADING_SYMBOL", "BTC/USDT"),
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
        max_concurrent_trades=int(os.getenv("MAX_CONCURRENT_TRADES", "2")),
        engine=os.getenv("ENGINE", "scalping"),
        tp_scaling_enabled=os.getenv("TP_SCALING_ENABLED", "false").lower() == "true",
        dynamic_trailing_enabled=os.getenv("DYNAMIC_TRAILING", "false").lower() == "true",
        scalp_skip_hours_utc=os.getenv("SCALP_SKIP_HOURS_UTC", ""),
        leverage=int(os.getenv("LEVERAGE", "7")),
        margin_mode=os.getenv("MARGIN_MODE", "isolated").lower(),
        user_stream_enabled=os.getenv("USER_STREAM_ENABLED", "false").lower() == "true",
        telegram_listener_enabled=os.getenv("TELEGRAM_LISTENER_ENABLED", "true").lower() == "true",
        portfolio_data_dir=os.getenv("PORTFOLIO_DATA_DIR", ""),
        risk_alerts_enabled=os.getenv("RISK_ALERTS_ENABLED", "true").lower() == "true",
        risk_liq_alert_pct=float(os.getenv("RISK_LIQ_ALERT_PCT", "3.0")),
        risk_dd_warn_ratio=float(os.getenv("RISK_DD_WARN_RATIO", "0.8")),
    )
