"""
Exchange SIMULADO para el modo carry (CARRY_PAPER).

La demo de BingX no permite operar spot con VST, así que el carry no se puede probar con órdenes reales en
demo. Este adaptador implementa la misma interfaz que usa CarryManager, con:
  - precios reales del mercado (BingX, en vivo) y especificaciones reales de contratos,
  - funding REAL: cada vez que BingX liquida un pago de funding, se aplica a los shorts simulados,
  - órdenes simuladas con comisiones reales (spot 0,10 % en la moneda, perp 0,05 %) y 0,03 % de deslizamiento,
  - transferencias spot ↔ futuros simuladas y liquidación del short si el precio cruza el nivel.
El estado se guarda en data/state.json (clave "carry_paper"), así sobrevive reinicios.
"""

from __future__ import annotations

import time
from typing import Optional

from bot.bingx import BingXClient, BingXError, Position
from bot.store import Store

SPOT_FEE = 0.001
PERP_FEE = 0.0005
SLIPPAGE = 0.0003
MMR = 0.005
MAX_INCOME_ROWS = 2000


class PaperCarryExchange:
    paper = True

    def __init__(self, market: BingXClient, store: Store, starting_futures_usdt: float):
        self.market = market
        self.store = store
        st = store.state.setdefault("carry_paper", {})
        st.setdefault("wallet", float(starting_futures_usdt))
        st.setdefault("spot", {"USDT": 0.0})
        st.setdefault("positions", {})
        st.setdefault("leverage", {})
        st.setdefault("income", [])
        st.setdefault("funding_checked", {})
        self._specs: dict = {}

    @property
    def st(self) -> dict:
        return self.store.state["carry_paper"]

    def _income(self, symbol: str, kind: str, amount: float, t: Optional[int] = None) -> None:
        rows = self.st["income"]
        rows.append({"symbol": symbol, "incomeType": kind, "income": str(amount),
                     "time": int(t or time.time() * 1000)})
        del rows[:-MAX_INCOME_ROWS]

    # ───────── datos reales ─────────

    def contract(self, symbol: str):
        return self.market.contract(symbol)

    def contracts(self, force: bool = False):
        return self.market.contracts(force)

    def price(self, symbol: str) -> float:
        return self.market.price(symbol)

    def spot_symbols(self) -> dict:
        if not self._specs:
            self._specs = self.market.spot_symbols()
        return self._specs

    # ───────── funding y liquidación (se llama al inicio de cada ciclo) ─────────

    def tick(self) -> None:
        now = int(time.time() * 1000)
        for symbol, p in list(self.st["positions"].items()):
            if p["qty"] <= 0:
                continue
            px = self.price(symbol)
            liq = (p["margin"] + p["qty"] * p["entry"]) / (p["qty"] * (1 + MMR))
            if px >= liq:
                realized = -p["margin"]
                self._income(symbol, "REALIZED_PNL", realized)
                p.update(qty=0.0, margin=0.0)
                continue
            since = int(self.st["funding_checked"].get(symbol, p.get("opened", now)))
            try:
                rows = self.market._request("GET", "/openApi/swap/v2/quote/fundingRate",
                                            {"symbol": symbol, "startTime": since + 1, "limit": 100},
                                            signed=False) or []
            except BingXError:
                continue
            for r in sorted(rows, key=lambda x: int(x["fundingTime"])):
                ft = int(r["fundingTime"])
                if ft <= since or ft > now:
                    continue
                amount = float(r["fundingRate"]) * p["qty"] * px   # el short cobra si la tasa es positiva
                self.st["wallet"] += amount
                self._income(symbol, "FUNDING_FEE", amount, ft)
                since = ft
            self.st["funding_checked"][symbol] = since

    # ───────── cuenta simulada ─────────

    def positions(self, symbol: Optional[str] = None) -> list[Position]:
        out = []
        for s, p in self.st["positions"].items():
            if p["qty"] <= 0 or symbol not in (None, s):
                continue
            px = self.price(s)
            liq = (p["margin"] + p["qty"] * p["entry"]) / (p["qty"] * (1 + MMR))
            out.append(Position(s, "SHORT", p["qty"], p["entry"], px, p["qty"] * (p["entry"] - px),
                                self.st["leverage"].get(s, 1), liq, p["margin"], "paper"))
        return out

    def spot_balances(self) -> dict:
        return dict(self.st["spot"])

    def balance(self) -> dict:
        upnl = sum(p.unrealized_pnl for p in self.positions())
        margin = sum(p["margin"] for p in self.st["positions"].values())
        return {"asset": "USDT", "balance": self.st["wallet"] + margin, "equity": self.st["wallet"] + margin + upnl,
                "available": self.st["wallet"], "unrealized_pnl": upnl}

    def transfer(self, asset: str, amount: float, from_account: str, to_account: str) -> dict:
        spot = self.st["spot"]
        if from_account == "USDTMPerp":
            if self.st["wallet"] < amount:
                raise BingXError(80012, "insufficient balance (simulado)", "/transfer")
            self.st["wallet"] -= amount
            spot["USDT"] = spot.get("USDT", 0.0) + amount
        else:
            if spot.get("USDT", 0.0) < amount - 1e-9:
                raise BingXError(80012, "insufficient balance (simulado)", "/transfer")
            spot["USDT"] -= amount
            self.st["wallet"] += amount
        return {"tranId": "paper"}

    def spot_market_order(self, symbol: str, side: str, qty: str) -> dict:
        q, coin, spot = float(qty), symbol.split("-")[0], self.st["spot"]
        px = self.price(symbol) * (1 + SLIPPAGE if side == "BUY" else 1 - SLIPPAGE)
        if side == "BUY":
            cost = q * px
            if spot.get("USDT", 0.0) < cost - 1e-9:
                raise BingXError(100202, "balance not enough (simulado)", "/spot/order")
            spot["USDT"] -= cost
            spot[coin] = spot.get(coin, 0.0) + q * (1 - SPOT_FEE)
        else:
            if spot.get(coin, 0.0) < q - 1e-12:
                raise BingXError(100202, "balance not enough (simulado)", "/spot/order")
            spot[coin] -= q
            spot["USDT"] = spot.get("USDT", 0.0) + q * px * (1 - SPOT_FEE)
            cost = q * px
        return {"executedQty": str(q), "cummulativeQuoteQty": str(cost)}

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        pass

    def set_leverage(self, symbol: str, leverage: int, side: str = "BOTH") -> None:
        self.st["leverage"][symbol] = leverage

    def place_market(self, symbol: str, side: str, qty: str, reduce_only: bool = False) -> dict:
        q = float(qty)
        px = self.price(symbol) * (1 - SLIPPAGE if side == "SELL" else 1 + SLIPPAGE)
        p = self.st["positions"].setdefault(symbol, {"qty": 0.0, "entry": px, "margin": 0.0,
                                                     "opened": int(time.time() * 1000)})
        fee = q * px * PERP_FEE
        if side == "SELL" and not reduce_only:
            margin = q * px / self.st["leverage"].get(symbol, 1)
            if self.st["wallet"] < margin + fee:
                raise BingXError(80012, "insufficient margin (simulado)", "/order")
            self.st["wallet"] -= margin + fee
            if p["qty"] <= 0:
                p.update(entry=px, opened=int(time.time() * 1000))
                self.st["funding_checked"].setdefault(symbol, p["opened"])
            else:
                p["entry"] = (p["entry"] * p["qty"] + px * q) / (p["qty"] + q)
            p["qty"] += q
            p["margin"] += margin
        else:
            q = min(q, p["qty"])
            if q <= 0:
                return {"orderId": "paper"}
            released = p["margin"] * q / p["qty"]
            realized = q * (p["entry"] - px)
            p["qty"] -= q
            p["margin"] -= released
            self.st["wallet"] += released + realized - fee
            self._income(symbol, "REALIZED_PNL", realized)
        self._income(symbol, "TRADING_FEE", -fee)
        return {"orderId": "paper"}

    def add_isolated_margin(self, symbol: str, amount: float) -> None:
        if self.st["wallet"] < amount:
            raise BingXError(80012, "insufficient balance (simulado)", "/positionMargin")
        self.st["wallet"] -= amount
        self.st["positions"][symbol]["margin"] += amount

    def income(self, symbol: str, start_ms: int, end_ms: Optional[int] = None) -> list[dict]:
        return [r for r in self.st["income"] if r["symbol"] == symbol and r["time"] >= start_ms
                and (end_ms is None or r["time"] <= end_ms)]
