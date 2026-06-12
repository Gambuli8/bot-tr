"""
core/exchange.py
Conexión a Binance Futures USDT-M (testnet o real) via ccxt.
Manejo de rate limiting, reintentos y validación de conexión.

NOTAS DE MERCADO (cambio Fase 3, 2026-06-08):
- Operamos en Futures USDT-M (perpetuos), NO Spot. La decisión de migrar fue
  para tener leverage configurable (WFA aprobado a 7×).
- defaultType = "future" en ccxt → endpoints /fapi/v1 y /fapi/v2.
- Antes de operar se setea leverage e isolated margin via setLeverage() y
  setMarginMode().
- Testnet Futures vive en testnet.binancefuture.com (distinto de Spot testnet).
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
        # Leverage y margin mode vienen del settings (default 7× isolated).
        self.leverage: int = int(getattr(settings, "leverage", 7))
        self.margin_mode: str = str(getattr(settings, "margin_mode", "isolated")).lower()
        self.exchange = self._init_exchange()
        # Cache MTF
        self._mtf_cache = None
        self._mtf_cache_at: float = 0.0
        # Cache para no resetear leverage/margin en cada trade (solo cuando cambia el símbolo).
        self._leverage_configured_for: Optional[str] = None
        # En testnet Futures los datos OHLCV son cuasi-flat. Leemos del mainnet
        # Futures (read-only, sin credenciales) para tener precios y velas reales.
        if settings.binance_testnet:
            self.data_exchange = ccxt.binance({
                "enableRateLimit": True,
                "options": {
                    "defaultType": "future",
                    "adjustForTimeDifference": True,
                },
            })
            logger.info("📡 OHLCV se leerá de Binance Futures MAINNET (read-only, datos reales)")
        else:
            self.data_exchange = self.exchange
        logger.info(
            f"ExchangeClient inicializado | mercado=Futures USDT-M | "
            f"testnet={settings.binance_testnet} | leverage={self.leverage}× | "
            f"margin={self.margin_mode}"
        )

    def _init_exchange(self) -> ccxt.binance:
        exchange = ccxt.binance({
            "apiKey": self.settings.binance_api_key,
            "secret": self.settings.binance_api_secret,
            "enableRateLimit": True,
            "options": {
                # Futures USDT-M perpetuos (Binance llama "future" a este mercado en ccxt)
                "defaultType": "future",
                "adjustForTimeDifference": True,
            },
        })

        if self.settings.binance_testnet:
            exchange.set_sandbox_mode(True)
            logger.info(
                "🔧 Modo TESTNET FUTURES activado — testnet.binancefuture.com (dinero ficticio)"
            )

        # Pre-cargar markets para que amount_to_precision/price_to_precision
        # funcionen siempre, incluso antes de validate_connection.
        try:
            exchange.load_markets()
        except Exception as e:
            logger.warning(f"No pude pre-cargar markets ({e}); se cargarán on-demand")

        return exchange

    def configure_leverage_and_margin(self, symbol: Optional[str] = None) -> None:
        """
        Aplica leverage e isolated-margin sobre el símbolo. Idempotente: se llama
        antes de cada apertura pero solo dispara API call cuando cambia el símbolo
        (el setting persiste en Binance Futures hasta que vos lo cambies).

        Si setMarginMode falla con 'No need to change margin type' (Binance la
        rechaza si ya está en ese modo), lo tratamos como éxito y seguimos.
        """
        sym = symbol or self.settings.symbol
        if self._leverage_configured_for == sym:
            return
        try:
            # Margin mode (isolated por trade, no contagia el resto del capital)
            try:
                self.exchange.set_margin_mode(self.margin_mode.upper(), sym)
                logger.info(f"✅ Margin mode = {self.margin_mode.upper()} para {sym}")
            except ccxt.ExchangeError as e:
                msg = str(e).lower()
                if "no need to change" in msg or "already" in msg:
                    logger.debug(f"Margin mode ya estaba en {self.margin_mode.upper()} para {sym}")
                else:
                    logger.warning(f"set_margin_mode falló para {sym}: {e}")
            # Leverage
            self.exchange.set_leverage(self.leverage, sym)
            logger.info(f"✅ Leverage = {self.leverage}× para {sym}")
            self._leverage_configured_for = sym
        except Exception as e:
            logger.error(f"No pude configurar leverage/margin para {sym}: {e}")
            raise

    def ensure_one_way_mode(self) -> None:
        """
        Garantiza que la cuenta Futures esté en modo One-way (no Hedge).

        CRÍTICO con plata real: el bot manda SL/TP con `reduceOnly` y SIN
        `positionSide`. Eso SOLO es válido en One-way mode. En Hedge mode Binance
        rechaza las reduceOnly → un entry podría abrir SIN stop (posición desnuda).
        Por eso, si no podemos garantizar One-way, ABORTAMOS el arranque.

        El modo es a nivel CUENTA (los bots comparten API key → el primero lo
        setea, el resto recibe -4059 'No need to change' y lo tratamos como éxito).
        """
        try:
            self.exchange.set_position_mode(False)  # False = one-way (hedge off)
            logger.info("✅ Position mode = One-way (hedge desactivado)")
        except ccxt.ExchangeError as e:
            msg = str(e).lower()
            if "no need to change" in msg or "-4059" in msg or "already" in msg:
                logger.info("✅ Position mode ya estaba en One-way")
            else:
                # No se pudo cambiar (ej: Hedge con posiciones/órdenes abiertas).
                raise RuntimeError(
                    f"No se pudo poner la cuenta Futures en One-way mode: {e}. "
                    "El bot usa órdenes reduceOnly sin positionSide, que en Hedge "
                    "mode fallan y dejarían posiciones SIN stop-loss. Cambiá a "
                    "One-Way en Binance (Futures → Preferencias → Modo de posición) "
                    "y reiniciá. Si hay posiciones/órdenes abiertas, cerralas primero."
                ) from e

        # Verificación dura: confirmar que el modo realmente quedó en One-way.
        # Binance Futures en ccxt exige symbol (o params={'subType': 'linear'}) en fetch_position_mode.
        try:
            mode = self.exchange.fetch_position_mode(symbol=self.settings.symbol)
            if mode.get("hedged") is True:
                raise RuntimeError(
                    "La cuenta Futures sigue en HEDGE mode tras el intento de "
                    "cambio. Pasala a One-Way en Binance y reiniciá — el bot NO "
                    "opera en Hedge porque dejaría posiciones sin stop-loss."
                )
            logger.info("✅ Verificado: cuenta en One-way mode")
        except ccxt.NotSupported:
            logger.debug("fetch_position_mode no soportado; confío en set_position_mode")

    @retry(max_attempts=3, base_delay=1.0)
    def validate_connection(self) -> bool:
        """
        Verifica que la conexión es válida antes de arrancar el bot.
        Lanza excepción si algo falla — el bot NO debe arrancar.
        """
        # 1. Verificar credenciales y obtener balance (en Futures, USDT = balance del wallet futures)
        balance = self.exchange.fetch_balance()
        usdt_balance = balance.get("USDT", {}).get("free", 0)
        logger.info(f"✅ Conexión validada | Balance USDT (Futures wallet): {usdt_balance:.2f}")

        # 2. Verificar que el par existe (en Futures es {symbol}:USDT perpetuo)
        markets = self.exchange.load_markets()
        symbol = self.settings.symbol
        if symbol not in markets:
            raise ValueError(
                f"Par {symbol} no disponible en Binance Futures. "
                f"¿Está el símbolo bien escrito? (ej: BTC/USDT, no BTCUSDT)"
            )
        # Resolver vía market(): con defaultType=future, ccxt mapea "BTC/USDT" al
        # perpetuo "BTC/USDT:USDT". Leer markets[symbol] directo devolvería el
        # market SPOT y dispararía un warning falso.
        market = self.exchange.market(symbol)
        # Sanity check: confirmar que es un perpetuo de Futures USDT-M
        if not market.get("swap") and not market.get("linear"):
            logger.warning(
                f"⚠ {symbol} no parece ser un perpetuo USDT-M. "
                f"type={market.get('type')} linear={market.get('linear')}"
            )
        logger.info(f"✅ Par {symbol} disponible (Futures perpetuo)")

        # 3. Garantizar One-way mode ANTES de operar (seguridad de las reduceOnly)
        self.ensure_one_way_mode()

        # 4. Configurar leverage e isolated margin para este símbolo (idempotente)
        self.configure_leverage_and_margin(symbol)

        # 5. Obtener precio actual como último check
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

    def validate_order_filters(
        self,
        amount_btc: float,
        amount_usdt: float,
        symbol: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Valida que la orden cumpla los filtros del exchange ANTES de enviarla.
        - LOT_SIZE (min, step) → cantidad en BTC
        - MIN_NOTIONAL → monto en USDT
        - MARKET_LOT_SIZE → cantidad para órdenes a mercado (si distinto a LOT_SIZE)

        Devuelve (ok, reason). Si ok=False, no mandes la orden.
        """
        symbol = symbol or self.settings.symbol
        try:
            market = self.exchange.market(symbol)
        except Exception as e:
            return False, f"Market {symbol} no disponible: {e}"

        limits = market.get("limits") or {}
        amt_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}

        amt_min = amt_limits.get("min")
        amt_max = amt_limits.get("max")
        cost_min = cost_limits.get("min")
        cost_max = cost_limits.get("max")

        if amt_min is not None and amount_btc < float(amt_min):
            return False, (
                f"Cantidad {amount_btc:.8f} BTC < LOT_SIZE.min {amt_min}"
            )
        if amt_max is not None and amount_btc > float(amt_max):
            return False, (
                f"Cantidad {amount_btc:.8f} BTC > LOT_SIZE.max {amt_max}"
            )
        if cost_min is not None and amount_usdt < float(cost_min):
            return False, (
                f"Notional ${amount_usdt:.2f} < MIN_NOTIONAL ${float(cost_min):.2f}"
            )
        if cost_max is not None and amount_usdt > float(cost_max):
            return False, (
                f"Notional ${amount_usdt:.2f} > NOTIONAL.max ${float(cost_max):.2f}"
            )

        return True, "OK"

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
        # Asegurar leverage/margin configurados en el exchange antes de abrir
        # (idempotente: solo dispara API call si el símbolo cambió)
        self.configure_leverage_and_margin(symbol)
        price = self.get_price(symbol)

        if side == "buy":
            # Calculamos cuánto BTC podemos comprar
            amount_btc = amount_usdt / price
            # Ajustar a la precisión del par
            amount_btc = self.exchange.amount_to_precision(symbol, amount_btc)
            check_notional = amount_usdt
        else:
            # Para vender, amount_usdt representa la cantidad de BTC a vender
            amount_btc = amount_usdt
            amount_btc = self.exchange.amount_to_precision(symbol, amount_btc)
            check_notional = float(amount_btc) * price

        # Pre-flight: validar filtros antes de mandar (evita errores genéricos)
        ok, reason = self.validate_order_filters(
            float(amount_btc), float(check_notional), symbol=symbol,
        )
        if not ok:
            raise ValueError(f"Orden rechazada por filtros: {reason}")

        params = {"newClientOrderId": client_order_id}

        order = self.exchange.create_market_order(
            symbol=symbol,
            side=side,
            amount=float(amount_btc),
            params=params,
        )

        base = symbol.split("/")[0] if "/" in symbol else symbol
        logger.info(
            f"📋 Orden ejecutada | {side.upper()} {amount_btc} {base} "
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

    @retry(max_attempts=3, base_delay=1.0)
    def get_order(self, order_id: str) -> dict:
        """Consulta el estado de una orden por ID."""
        return self.exchange.fetch_order(order_id, self.settings.symbol)

    @retry(max_attempts=3, base_delay=1.0)
    def place_stop_loss_market(
        self,
        side: str,
        amount_btc: float,
        stop_price: float,
        client_order_id: str,
    ) -> dict:
        """
        STOP_MARKET (Futures USDT-M): cuando el precio toca stop_price se ejecuta
        a mercado. reduceOnly garantiza que solo cierra (nunca abre nueva).
        Para cerrar LONG → side='sell'. Para cerrar SHORT → side='buy'.
        """
        symbol = self.settings.symbol
        amount_btc = self.exchange.amount_to_precision(symbol, amount_btc)
        stop_price = self.exchange.price_to_precision(symbol, stop_price)
        params = {
            "stopPrice": float(stop_price),
            "newClientOrderId": client_order_id,
            "reduceOnly": True,           # CRÍTICO en Futures: solo cierra, no abre
            "workingType": "MARK_PRICE",  # trigger por mark price (evita liq por wick spot)
        }
        order = self.exchange.create_order(
            symbol=symbol,
            type="STOP_MARKET",   # tipo de orden de Futures USDT-M
            side=side,
            amount=float(amount_btc),
            params=params,
        )
        base = symbol.split("/")[0] if "/" in symbol else symbol
        logger.info(
            f"🛑 SL colocado en Futures | trigger ${float(stop_price):,.2f} | "
            f"{side.upper()} {amount_btc} {base} | reduceOnly | ID: {client_order_id}"
        )
        return order

    @retry(max_attempts=3, base_delay=1.0)
    def place_take_profit_limit(
        self,
        side: str,
        amount_btc: float,
        limit_price: float,
        client_order_id: str,
    ) -> dict:
        """
        TP en Futures: orden LIMIT con reduceOnly=true. Cuando el precio alcanza
        el limit, se ejecuta como maker (fee bajo). reduceOnly garantiza que solo
        cierra la posición, nunca abre una nueva.
        """
        symbol = self.settings.symbol
        amount_btc = self.exchange.amount_to_precision(symbol, amount_btc)
        limit_price = self.exchange.price_to_precision(symbol, limit_price)
        params = {
            "timeInForce": "GTC",
            "newClientOrderId": client_order_id,
            "reduceOnly": True,   # CRÍTICO en Futures
        }
        order = self.exchange.create_order(
            symbol=symbol,
            type="LIMIT",
            side=side,
            amount=float(amount_btc),
            price=float(limit_price),
            params=params,
        )
        base = symbol.split("/")[0] if "/" in symbol else symbol
        logger.info(
            f"🎯 TP colocado en Futures | limit ${float(limit_price):,.2f} | "
            f"{side.upper()} {amount_btc} {base} | reduceOnly | ID: {client_order_id}"
        )
        return order
