"""
core/exchange.py
Conexión a Binance (testnet o real) via ccxt.
Manejo de rate limiting, reintentos y validación de conexión.
"""

import time
import math
import functools
from typing import Optional
import ccxt
import pandas as pd
from logs.logger import logger
from config.settings import Settings


# Clasificación de errores de ccxt para el retry (roadmap #6).
#   - NO reintentables: el reintento no va a cambiar el resultado; re-lanzamos
#     para que el caller decida (ej. InsufficientFunds → achicar/saltar trade;
#     OrderNotFound → tratar como ya cerrada; InvalidOrder → bug de filtros).
#   - Rate-limit: reintentar con backoff MÁS largo (el exchange nos está frenando).
#   - Reintentables: problemas de red transitorios → backoff normal.
NON_RETRYABLE_ERRORS = (
    ccxt.InsufficientFunds,   # plata insuficiente
    ccxt.InvalidOrder,        # incluye OrderNotFound, BadSymbol vía mensaje
    ccxt.AuthenticationError, # incluye PermissionDenied — credenciales
    ccxt.BadRequest,          # request mal formado (filtros, params)
)
RATE_LIMIT_ERRORS = (ccxt.DDoSProtection, ccxt.RateLimitExceeded)
RETRYABLE_ERRORS = (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable)


def retry(max_attempts: int = 3, base_delay: float = 1.0, rate_limit_multiplier: float = 3.0):
    """
    Decorator con backoff exponencial y manejo de errores POR TIPO (ccxt):
      - InsufficientFunds / InvalidOrder / OrderNotFound / Auth → NO reintenta.
      - RateLimitExceeded / DDoSProtection → reintenta con backoff más largo.
      - NetworkError / RequestTimeout / ExchangeNotAvailable → reintenta normal.
      - Cualquier otro ExchangeError → NO reintenta (más seguro ante lo desconocido).
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except NON_RETRYABLE_ERRORS as e:
                    logger.error(
                        f"[{func.__name__}] Error NO reintentable "
                        f"({type(e).__name__}): {e}"
                    )
                    raise
                except RATE_LIMIT_ERRORS as e:
                    last_error = e
                    delay = base_delay * (2 ** (attempt - 1)) * rate_limit_multiplier
                    logger.warning(
                        f"[{func.__name__}] Rate limit ({type(e).__name__}) "
                        f"intento {attempt}/{max_attempts}. Esperando {delay:.1f}s..."
                    )
                    time.sleep(delay)
                except RETRYABLE_ERRORS as e:
                    last_error = e
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        f"[{func.__name__}] Red ({type(e).__name__}) "
                        f"intento {attempt}/{max_attempts}: {e}. "
                        f"Reintentando en {delay:.1f}s..."
                    )
                    time.sleep(delay)
                except ccxt.ExchangeError as e:
                    # Errores del exchange desconocidos: no reintentar (fail-safe).
                    logger.error(
                        f"[{func.__name__}] Error del exchange no reintentable "
                        f"({type(e).__name__}): {e}"
                    )
                    raise
            logger.error(f"[{func.__name__}] Todos los reintentos fallaron: {last_error}")
            raise last_error
        return wrapper
    return decorator


class ExchangeClient:
    """
    Cliente del exchange con manejo robusto de errores.
    Soporta Binance testnet y producción.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.exchange = self._init_exchange()
        # Cache MTF por símbolo: {symbol: (mtf, ts)}
        self._mtf_cache: dict = {}
        # Filtros del exchange por símbolo (LOT_SIZE/PRICE_FILTER/MIN_NOTIONAL).
        # Se llenan con load_symbol_filters() al startup.
        self.filters: dict = {}
        # En testnet, los datos OHLCV están sintéticos (precio cuasi-flat, ATR ínfimo).
        # Para que el motor técnico tenga sobre qué decidir, leemos OHLCV de mainnet
        # (read-only, sin credenciales). Las órdenes siguen yendo a self.exchange.
        if settings.binance_testnet:
            self.data_exchange = ccxt.binance({
                "enableRateLimit": True,
                "options": {
                    "defaultType": "spot",
                    "adjustForTimeDifference": True,
                },
            })
            logger.info("📡 OHLCV se leerá de Binance MAINNET (read-only, datos reales)")
        else:
            self.data_exchange = self.exchange
        logger.info(f"ExchangeClient inicializado | testnet={settings.binance_testnet}")

    def _init_exchange(self) -> ccxt.binance:
        exchange = ccxt.binance({
            "apiKey": self.settings.binance_api_key,
            "secret": self.settings.binance_api_secret,
            "enableRateLimit": True,  # ccxt maneja rate limiting automáticamente
            "options": {
                "defaultType": "spot",
                "adjustForTimeDifference": True,
            },
        })

        if self.settings.binance_testnet:
            exchange.set_sandbox_mode(True)
            logger.info("🔧 Modo TESTNET activado — dinero ficticio (órdenes)")

        return exchange

    @retry(max_attempts=3, base_delay=1.0)
    def validate_connection(self) -> bool:
        """
        Verifica que la conexión es válida antes de arrancar el bot.
        Lanza excepción si algo falla — el bot NO debe arrancar.
        Valida TODOS los símbolos del portafolio y cachea sus filtros.
        """
        # 1. Verificar credenciales y obtener balance
        balance = self.exchange.fetch_balance()
        usdt_balance = balance.get("USDT", {}).get("free", 0)
        logger.info(f"✅ Conexión validada | Balance USDT: {usdt_balance:.2f}")

        # 2. Cargar markets + filtros de cada símbolo del portafolio
        markets = self.exchange.load_markets()
        for symbol in self.settings.symbols:
            if symbol not in markets:
                raise ValueError(f"Par {symbol} no disponible en este exchange")
        self.load_symbol_filters(markets)
        logger.info(f"✅ {len(self.settings.symbols)} pares disponibles: "
                    f"{', '.join(self.settings.symbols)}")

        # 3. Precio actual del primario como último check
        symbol = self.settings.symbol
        ticker = self.data_exchange.fetch_ticker(symbol)
        source = "mainnet" if self.settings.binance_testnet else "exchange"
        logger.info(f"✅ Precio actual {symbol}: ${ticker['last']:,.2f} ({source})")

        return True

    def load_symbol_filters(self, markets: Optional[dict] = None) -> dict:
        """
        Carga y cachea en memoria las reglas de cada símbolo del portafolio:
        LOT_SIZE (stepSize/minQty), PRICE_FILTER (tickSize) y MIN_NOTIONAL.
        Se aplican para redondear matemáticamente cantidad/precio antes de ordenar.
        """
        if markets is None:
            markets = self.exchange.load_markets()
        for symbol in self.settings.symbols:
            market = markets.get(symbol)
            if not market:
                logger.warning(f"[{symbol}] sin market data, no puedo cargar filtros")
                continue
            self.filters[symbol] = self._extract_filters(market)
            f = self.filters[symbol]
            logger.info(
                f"[{symbol}] filtros | step={f['step']} minQty={f['min_qty']} "
                f"tick={f['tick']} minNotional={f['min_notional']}"
            )
        return self.filters

    @staticmethod
    def _extract_filters(market: dict) -> dict:
        """Extrae los filtros relevantes del market de ccxt (Binance)."""
        raw = {ft.get("filterType"): ft for ft in market.get("info", {}).get("filters", [])}
        lot = raw.get("LOT_SIZE", {})
        price = raw.get("PRICE_FILTER", {})
        notional = raw.get("NOTIONAL", raw.get("MIN_NOTIONAL", {}))

        def _f(d, *keys):
            for k in keys:
                v = d.get(k)
                if v not in (None, ""):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return 0.0

        return {
            "step": _f(lot, "stepSize") or None,
            "min_qty": _f(lot, "minQty"),
            "tick": _f(price, "tickSize") or None,
            "min_notional": _f(notional, "minNotional", "notional"),
        }

    def round_amount(self, symbol: str, amount: float) -> float:
        """Redondea la cantidad HACIA ABAJO al múltiplo del stepSize (LOT_SIZE)."""
        flt = self.filters.get(symbol)
        if not flt or not flt.get("step"):
            return float(self.exchange.amount_to_precision(symbol, amount))
        step = flt["step"]
        return math.floor(amount / step) * step

    def round_price(self, symbol: str, price: float) -> float:
        """Redondea el precio al múltiplo del tickSize (PRICE_FILTER)."""
        flt = self.filters.get(symbol)
        if not flt or not flt.get("tick"):
            return float(self.exchange.price_to_precision(symbol, price))
        tick = flt["tick"]
        return math.floor(price / tick) * tick

    @retry(max_attempts=3, base_delay=1.0)
    def get_ohlcv(
        self,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        limit: int = 300,
    ) -> pd.DataFrame:
        """
        Obtiene velas OHLCV y las retorna como DataFrame limpio.
        Siempre retorna al menos `limit` velas — si hay NaN, las descarta.
        """
        symbol = symbol or self.settings.symbol
        timeframe = timeframe or self.settings.timeframe

        raw = self.data_exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)

        # Validar que no haya datos corruptos
        initial_len = len(df)
        df.dropna(inplace=True)
        df = df[df["close"] > 0]

        if len(df) < initial_len:
            logger.warning(f"Se descartaron {initial_len - len(df)} velas con datos inválidos")

        if len(df) < self.settings.warmup_candles:
            logger.warning(
                f"Solo {len(df)} velas disponibles, se necesitan {self.settings.warmup_candles} para warm-up"
            )

        return df

    def get_mtf_context(self, symbol: Optional[str] = None, cache_seconds: int = 300):
        """
        Devuelve MTFContext (tendencia + ADX en 1h y 4h) para `symbol`. Cacheado
        por símbolo (5 min) para no hammear el API en el scanner multi-symbol.
        Lee de mainnet vía data_exchange.
        """
        import time as _time
        from core.mtf_context import build_mtf_context

        symbol = symbol or self.settings.symbol
        now = _time.time()
        cached = self._mtf_cache.get(symbol)
        if cached is not None and (now - cached[1]) < cache_seconds:
            return cached[0]

        # 1h: 220 velas (warmup EMA200) ≈ 9 días
        raw_1h = self.data_exchange.fetch_ohlcv(symbol, "1h", limit=300)
        df_1h = pd.DataFrame(raw_1h, columns=["timestamp", "open", "high", "low", "close", "volume"])
        # 4h: 220 velas ≈ 36 días
        raw_4h = self.data_exchange.fetch_ohlcv(symbol, "4h", limit=300)
        df_4h = pd.DataFrame(raw_4h, columns=["timestamp", "open", "high", "low", "close", "volume"])

        mtf = build_mtf_context(df_1h, df_4h)
        self._mtf_cache[symbol] = (mtf, now)
        logger.info(
            f"[{symbol}] MTF actualizado | 1h: {mtf.trend_1h} adx={mtf.adx_1h:.1f} | "
            f"4h: {mtf.trend_4h} adx={mtf.adx_4h:.1f}"
        )
        return mtf

    @retry(max_attempts=3, base_delay=1.0)
    def get_balance(self) -> dict:
        """Retorna balance disponible de USDT y BTC."""
        balance = self.exchange.fetch_balance()
        return {
            "usdt_free": balance.get("USDT", {}).get("free", 0.0),
            "usdt_total": balance.get("USDT", {}).get("total", 0.0),
            "btc_free": balance.get("BTC", {}).get("free", 0.0),
            "btc_total": balance.get("BTC", {}).get("total", 0.0),
        }

    @retry(max_attempts=3, base_delay=1.0)
    def get_price(self, symbol: Optional[str] = None) -> float:
        """Precio actual del par (de mainnet si estamos en testnet)."""
        symbol = symbol or self.settings.symbol
        ticker = self.data_exchange.fetch_ticker(symbol)
        return float(ticker["last"])

    @retry(max_attempts=3, base_delay=1.0)
    def fetch_position_sizes(self, symbols: Optional[list] = None) -> dict:
        """
        Lee del exchange el tamaño REAL de posición por símbolo, para reconciliar
        contra el estado local. Devuelve {symbol: signed_size} (positivo = LONG,
        negativo = SHORT). Símbolos sin posición no aparecen (o aparecen en 0).

        Futures USDT-M: usa fetch_positions(). Spot no tiene "posiciones" como tal
        (la tenencia es el balance del activo base) → en spot devuelve {} y la
        reconciliación de posiciones queda inactiva hasta el switch a Futures (#7).

        NO atrapa la excepción: si falla, propaga para que el caller decida
        (fail-closed en el startup).
        """
        symbols = symbols or self.settings.symbols
        # Spot no soporta fetch_positions de forma fiable.
        if not self.exchange.has.get("fetchPositions"):
            logger.info("fetch_position_sizes: exchange spot sin posiciones; reconciliación de posición inactiva")
            return {}
        raw = self.exchange.fetch_positions(symbols)
        out: dict = {}
        for p in raw or []:
            sym = p.get("symbol")
            if not sym:
                continue
            contracts = p.get("contracts")
            size = float(contracts) if contracts is not None else 0.0
            if size == 0:
                continue
            side = (p.get("side") or "").lower()
            signed = -size if side == "short" else size
            out[sym] = signed
        return out

    @retry(max_attempts=3, base_delay=1.0)
    def place_market_order(
        self,
        side: str,
        amount_usdt: float,
        client_order_id: str,
        symbol: Optional[str] = None,
    ) -> dict:
        """
        Coloca una orden de mercado en `symbol`.
        side: 'buy' o 'sell'
        amount_usdt: notional en USDT (en ambos lados) — se convierte a cantidad
                     del activo base con el precio actual.
        Aplica redondeo dinámico (stepSize) y valida minQty / MIN_NOTIONAL antes
        de mandar. Si no llega a los mínimos, lanza ValueError (no manda la orden).
        """
        symbol = symbol or self.settings.symbol
        price = self.get_price(symbol)

        amount = self.round_amount(symbol, amount_usdt / price)
        flt = self.filters.get(symbol, {})
        if flt.get("min_qty") and amount < flt["min_qty"]:
            raise ValueError(
                f"{symbol}: cantidad {amount} < minQty {flt['min_qty']} (LOT_SIZE)"
            )
        notional = amount * price
        if flt.get("min_notional") and notional < flt["min_notional"]:
            raise ValueError(
                f"{symbol}: notional ${notional:,.2f} < MIN_NOTIONAL "
                f"${flt['min_notional']:,.2f}"
            )

        params = {"newClientOrderId": client_order_id}
        order = self.exchange.create_market_order(
            symbol=symbol,
            side=side,
            amount=float(amount),
            params=params,
        )

        logger.info(
            f"📋 Orden ejecutada | {side.upper()} {amount} {symbol} "
            f"@ ~${price:,.2f} (notional ${notional:,.2f}) | ID: {client_order_id}"
        )
        return order

    @retry(max_attempts=3, base_delay=1.0)
    def get_open_orders(self, symbol: Optional[str] = None) -> list:
        """Retorna órdenes abiertas del par dado (o el primario)."""
        return self.exchange.fetch_open_orders(symbol or self.settings.symbol)

    @retry(max_attempts=3, base_delay=1.0)
    def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> dict:
        """Cancela una orden por ID."""
        return self.exchange.cancel_order(order_id, symbol or self.settings.symbol)
