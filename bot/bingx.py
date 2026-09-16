"""
Cliente REST mínimo para Futuros Perpetuos USDT-M de BingX.

Firma (verificada contra la implementación de ccxt):
  1. Se agregan `timestamp` (ms) y `recvWindow` a los parámetros.
  2. Se ordenan las claves alfabéticamente y se arma `k1=v1&k2=v2` SIN url-encodear.
  3. signature = HMAC-SHA256(secret, ese string) en hex.
  4. La URL lleva los mismos parámetros url-encodeados + `&signature=...`,
     y el header `X-BX-APIKEY`.

Modo demo = misma API sobre `open-api-vst.bingx.com` (saldo virtual VST).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

BASE_URLS = {
    "live": "https://open-api.bingx.com",
    "demo": "https://open-api-vst.bingx.com",
}


class BingXError(Exception):
    def __init__(self, code: Any, msg: str, path: str = ""):
        super().__init__(f"BingX {path} → code={code} msg={msg}")
        self.code = code
        self.msg = msg
        self.path = path


@dataclass
class ContractSpec:
    symbol: str
    min_qty: float
    min_usdt: float
    qty_precision: int
    price_precision: int
    taker_fee: float
    maker_fee: float
    api_open: bool


@dataclass
class Position:
    symbol: str
    side: str  # LONG | SHORT
    qty: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: float
    liquidation_price: float
    margin: float
    position_id: str


def _fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, float):
        return repr(value)
    return str(value)


def sign_params(params: dict, secret: str) -> tuple[str, str]:
    """Devuelve (query_string_url_encodeada_con_firma, payload_firmado)."""
    items = sorted((k, _fmt_value(v)) for k, v in params.items() if v is not None)
    payload = "&".join(f"{k}={v}" for k, v in items)
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    encoded = "&".join(f"{k}={quote(v, safe='')}" for k, v in items)
    return f"{encoded}&signature={signature}", payload


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class BingXClient:
    def __init__(self, api_key: str, api_secret: str, mode: str = "demo", timeout: float = 10.0):
        if mode not in BASE_URLS:
            raise ValueError(f"mode inválido: {mode}")
        self.api_key = api_key
        self.api_secret = api_secret
        self.mode = mode
        self.base_url = BASE_URLS[mode]
        self.timeout = timeout
        self.session = requests.Session()
        self._contracts: dict[str, ContractSpec] = {}
        self._contracts_ts = 0.0

    # ───────── transporte ─────────

    def _request(self, method: str, path: str, params: Optional[dict] = None,
                 signed: bool = True, retries: int = 2) -> Any:
        params = dict(params or {})
        attempts = retries + 1 if method == "GET" else 1  # nunca reintentar escrituras a ciegas
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            headers = {}
            if signed:
                params["timestamp"] = int(time.time() * 1000)
                params["recvWindow"] = 5000
                query, _ = sign_params(params, self.api_secret)
                headers["X-BX-APIKEY"] = self.api_key
            else:
                query = "&".join(f"{k}={quote(_fmt_value(v), safe='')}" for k, v in sorted(params.items()))
            url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
            try:
                resp = self.session.request(method, url, headers=headers, timeout=self.timeout)
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                log.warning("BingX %s %s intento %d falló: %s", method, path, attempt + 1, exc)
                time.sleep(1.0 * (attempt + 1))
                continue
            code = data.get("code", 0) if isinstance(data, dict) else 0
            if code not in (0, "0"):
                raise BingXError(code, data.get("msg", ""), path)
            return data.get("data") if isinstance(data, dict) and "data" in data else data
        raise BingXError("network", str(last_exc), path)

    # ───────── mercado (público) ─────────

    def contracts(self, force: bool = False) -> dict[str, ContractSpec]:
        if force or not self._contracts or time.time() - self._contracts_ts > 3600:
            raw = self._request("GET", "/openApi/swap/v2/quote/contracts", signed=False)
            specs = {}
            for c in raw or []:
                specs[c["symbol"]] = ContractSpec(
                    symbol=c["symbol"],
                    min_qty=_f(c.get("tradeMinQuantity")),
                    min_usdt=_f(c.get("tradeMinUSDT")),
                    qty_precision=int(c.get("quantityPrecision", 0)),
                    price_precision=int(c.get("pricePrecision", 0)),
                    taker_fee=_f(c.get("takerFeeRate"), 0.0005),
                    maker_fee=_f(c.get("makerFeeRate"), 0.0002),
                    api_open=str(c.get("apiStateOpen", "true")).lower() == "true" and int(c.get("status", 1)) == 1,
                )
            self._contracts = specs
            self._contracts_ts = time.time()
        return self._contracts

    def contract(self, symbol: str) -> ContractSpec:
        spec = self.contracts().get(symbol)
        if spec is None:
            raise BingXError("symbol", f"{symbol} no existe en BingX ({self.mode})")
        return spec

    def price(self, symbol: str) -> float:
        data = self._request("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol}, signed=False)
        return _f(data.get("price"))

    def klines(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        data = self._request("GET", "/openApi/swap/v3/quote/klines",
                             {"symbol": symbol, "interval": interval, "limit": limit}, signed=False)
        out = [
            {"time": int(k["time"]), "open": _f(k["open"]), "high": _f(k["high"]),
             "low": _f(k["low"]), "close": _f(k["close"]), "volume": _f(k["volume"])}
            for k in data or []
        ]
        return sorted(out, key=lambda k: k["time"])

    # ───────── cuenta ─────────

    def balance(self) -> dict:
        data = self._request("GET", "/openApi/swap/v2/user/balance")
        bal = data.get("balance", data) if isinstance(data, dict) else (data[0] if data else {})
        return {
            "asset": bal.get("asset", "USDT"),
            "balance": _f(bal.get("balance")),
            "equity": _f(bal.get("equity")),
            "available": _f(bal.get("availableMargin")),
            "unrealized_pnl": _f(bal.get("unrealizedProfit")),
        }

    def positions(self, symbol: Optional[str] = None) -> list[Position]:
        params = {"symbol": symbol} if symbol else {}
        data = self._request("GET", "/openApi/swap/v2/user/positions", params) or []
        out = []
        for p in data:
            amt = _f(p.get("positionAmt"))
            if amt == 0:
                continue
            side = str(p.get("positionSide", "")).upper()
            if side not in ("LONG", "SHORT"):
                side = "LONG" if amt > 0 else "SHORT"
            out.append(Position(
                symbol=p.get("symbol", ""),
                side=side,
                qty=abs(amt),
                entry_price=_f(p.get("avgPrice") or p.get("entryPrice")),
                mark_price=_f(p.get("markPrice")),
                unrealized_pnl=_f(p.get("unrealizedProfit")),
                leverage=_f(p.get("leverage")),
                liquidation_price=_f(p.get("liquidationPrice")),
                margin=_f(p.get("initialMargin") or p.get("margin")),
                position_id=str(p.get("positionId", "")),
            ))
        return out

    def is_hedge_mode(self) -> bool:
        data = self._request("GET", "/openApi/swap/v1/positionSide/dual")
        return str(data.get("dualSidePosition", "false")).lower() == "true"

    def set_one_way_mode(self) -> None:
        self._request("POST", "/openApi/swap/v1/positionSide/dual", {"dualSidePosition": "false"})

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            self._request("POST", "/openApi/swap/v2/trade/marginType",
                          {"symbol": symbol, "marginType": margin_type})
        except BingXError as exc:
            # Si ya está en ese modo BingX puede devolver error: no es fatal.
            if "same" in exc.msg.lower() or "no need" in exc.msg.lower():
                return
            raise

    def set_leverage(self, symbol: str, leverage: int, side: str = "BOTH") -> None:
        self._request("POST", "/openApi/swap/v2/trade/leverage",
                      {"symbol": symbol, "side": side, "leverage": int(leverage)})

    # ───────── órdenes ─────────

    def place_market_with_brackets(self, symbol: str, direction: str, qty: str,
                                   stop_loss: str, take_profit: str, client_id: str) -> dict:
        """Orden a mercado con SL y TP adjuntos (modo one-way → positionSide BOTH)."""
        params = {
            "symbol": symbol,
            "side": "BUY" if direction == "LONG" else "SELL",
            "positionSide": "BOTH",
            "type": "MARKET",
            "quantity": qty,
            "clientOrderID": client_id,
            "stopLoss": {"type": "STOP_MARKET", "stopPrice": float(stop_loss), "workingType": "MARK_PRICE"},
            "takeProfit": {"type": "TAKE_PROFIT_MARKET", "stopPrice": float(take_profit), "workingType": "MARK_PRICE"},
        }
        data = self._request("POST", "/openApi/swap/v2/trade/order", params)
        return data.get("order", data) if isinstance(data, dict) else {}

    def place_protective_order(self, symbol: str, direction: str, kind: str, qty: str, stop_price: str) -> dict:
        """SL o TP suelto (reduceOnly) por si el adjunto no quedó registrado."""
        params = {
            "symbol": symbol,
            "side": "SELL" if direction == "LONG" else "BUY",
            "positionSide": "BOTH",
            "type": "STOP_MARKET" if kind == "SL" else "TAKE_PROFIT_MARKET",
            "quantity": qty,
            "stopPrice": stop_price,
            "workingType": "MARK_PRICE",
            "reduceOnly": "true",
        }
        data = self._request("POST", "/openApi/swap/v2/trade/order", params)
        return data.get("order", data) if isinstance(data, dict) else {}

    def close_position_market(self, symbol: str, direction: str, qty: str) -> dict:
        """Cierre a mercado SIEMPRE reduceOnly: nunca puede abrir una posición opuesta."""
        params = {
            "symbol": symbol,
            "side": "SELL" if direction == "LONG" else "BUY",
            "positionSide": "BOTH",
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": "true",
        }
        data = self._request("POST", "/openApi/swap/v2/trade/order", params)
        return data.get("order", data) if isinstance(data, dict) else {}

    def open_orders(self, symbol: str) -> list[dict]:
        data = self._request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
        return (data.get("orders", []) if isinstance(data, dict) else data) or []

    def cancel_all_orders(self, symbol: str) -> None:
        self._request("DELETE", "/openApi/swap/v2/trade/allOpenOrders", {"symbol": symbol})

    def order_by_client_id(self, symbol: str, client_id: str) -> Optional[dict]:
        try:
            data = self._request("GET", "/openApi/swap/v2/trade/order",
                                 {"symbol": symbol, "clientOrderID": client_id})
        except BingXError:
            return None
        return data.get("order", data) if isinstance(data, dict) else None

    def income(self, symbol: str, start_ms: int, end_ms: Optional[int] = None) -> list[dict]:
        """Movimientos realizados: REALIZED_PNL, TRADING_FEE, FUNDING_FEE…"""
        params = {"symbol": symbol, "startTime": start_ms, "limit": 1000}
        if end_ms:
            params["endTime"] = end_ms
        return self._request("GET", "/openApi/swap/v2/user/income", params) or []

    def position_history(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        try:
            data = self._request("GET", "/openApi/swap/v1/trade/positionHistory",
                                 {"symbol": symbol, "startTs": start_ms, "endTs": end_ms})
        except BingXError as exc:
            log.warning("positionHistory no disponible: %s", exc)
            return []
        if isinstance(data, dict):
            return data.get("positionHistory", []) or []
        return data or []
