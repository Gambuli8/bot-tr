"""
core/exchange.py
Conexión a Binance (testnet o real) via ccxt.
Manejo de rate limiting, reintentos y validación de conexión.
"""

import time
import functools
from typing import Optional
import ccxt
import pandas as pd
from logs.logger import logger
from config.settings import Settings


def retry(max_attempts: int = 3, base_delay: float = 1.0):
    """Decorator con backoff exponencial para llamadas al exchange."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                    last_error = e
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        f"[{func.__name__}] Intento {attempt}/{max_attempts} falló: {e}. "
                        f"Reintentando en {delay:.1f}s..."
                    )
                    time.sleep(delay)
                except ccxt.ExchangeError as e:
                    # Errores del exchange no se reintentan
                    logger.error(f"[{func.__name__}] Error del exchange (no reintentable): {e}")
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
        # Cache MTF
        self._mtf_cache = None
        self._mtf_cache_at: float = 0.0
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
        """
        # 1. Verificar credenciales y obtener balance
        balance = self.exchange.fetch_balance()
        usdt_balance = balance.get("USDT", {}).get("free", 0)
        logger.info(f"✅ Conexión validada | Balance USDT: {usdt_balance:.2f}")

        # 2. Verificar que el par existe
        markets = self.exchange.load_markets()
        symbol = self.settings.symbol
        if symbol not in markets:
            raise ValueError(f"Par {symbol} no disponible en este exchange")
        logger.info(f"✅ Par {symbol} disponible")

        # 3. Obtener precio actual como último check (de mainnet si estamos en testnet)
        ticker = self.data_exchange.fetch_ticker(symbol)
        source = "mainnet" if self.settings.binance_testnet else "exchange"
        logger.info(f"✅ Precio actual {symbol}: ${ticker['last']:,.2f} ({source})")

        return True

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

    def get_mtf_context(self, cache_seconds: int = 300):
        """
        Devuelve MTFContext (tendencia + ADX en 1h y 4h). Cacheado.
        Lee de mainnet vía data_exchange.
        """
        import time as _time
        from core.mtf_context import build_mtf_context

        now = _time.time()
        if self._mtf_cache is not None and (now - self._mtf_cache_at) < cache_seconds:
            return self._mtf_cache

        symbol = self.settings.symbol
        # 1h: 220 velas (warmup EMA200) ≈ 9 días
        raw_1h = self.data_exchange.fetch_ohlcv(symbol, "1h", limit=300)
        df_1h = pd.DataFrame(raw_1h, columns=["timestamp", "open", "high", "low", "close", "volume"])
        # 4h: 220 velas ≈ 36 días
        raw_4h = self.data_exchange.fetch_ohlcv(symbol, "4h", limit=300)
        df_4h = pd.DataFrame(raw_4h, columns=["timestamp", "open", "high", "low", "close", "volume"])

        mtf = build_mtf_context(df_1h, df_4h)
        self._mtf_cache = mtf
        self._mtf_cache_at = now
        logger.info(
            f"MTF actualizado | 1h: {mtf.trend_1h} adx={mtf.adx_1h:.1f} | "
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
    def place_market_order(
        self,
        side: str,
        amount_usdt: float,
        client_order_id: str,
    ) -> dict:
        """
        Coloca una orden de mercado.
        side: 'buy' o 'sell'
        amount_usdt: capital a usar en USDT
        client_order_id: ID único para idempotencia
        """
        symbol = self.settings.symbol
        price = self.get_price(symbol)

        if side == "buy":
            # Calculamos cuánto BTC podemos comprar
            amount_btc = amount_usdt / price
            # Ajustar a la precisión del par
            amount_btc = self.exchange.amount_to_precision(symbol, amount_btc)
        else:
            # Para vender, amount_usdt representa la cantidad de BTC a vender
            amount_btc = amount_usdt
            amount_btc = self.exchange.amount_to_precision(symbol, amount_btc)

        params = {"newClientOrderId": client_order_id}

        order = self.exchange.create_market_order(
            symbol=symbol,
            side=side,
            amount=float(amount_btc),
            params=params,
        )

        logger.info(
            f"📋 Orden ejecutada | {side.upper()} {amount_btc} BTC "
            f"@ ~${price:,.2f} | ID: {client_order_id}"
        )
        return order

    @retry(max_attempts=3, base_delay=1.0)
    def get_open_orders(self) -> list:
        """Retorna órdenes abiertas del par configurado."""
        return self.exchange.fetch_open_orders(self.settings.symbol)

    @retry(max_attempts=3, base_delay=1.0)
    def cancel_order(self, order_id: str) -> dict:
        """Cancela una orden por ID."""
        return self.exchange.cancel_order(order_id, self.settings.symbol)
