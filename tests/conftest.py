from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from bot.bingx import BingXError, ContractSpec, Position
from bot.config import Settings
from bot.narrator import Narrator
from bot.store import Store

SPECS = {
    "BTC-USDT": ContractSpec("BTC-USDT", 0.0001, 2, 4, 1, 0.0005, 0.0002, True),
    "ETH-USDT": ContractSpec("ETH-USDT", 0.001, 2, 3, 2, 0.0005, 0.0002, True),
    "DOGE-USDT": ContractSpec("DOGE-USDT", 25, 2, 0, 5, 0.0005, 0.0002, True),
    "ZEC-USDT": ContractSpec("ZEC-USDT", 0.002, 2, 3, 2, 0.0005, 0.0002, True),
}


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        bingx_api_key="k", bingx_api_secret="s", mode="demo",
        symbols=["BTC-USDT", "ETH-USDT", "DOGE-USDT", "ZEC-USDT"],
        margin_per_trade_usdt=2.0, max_leverage=20, margin_type="ISOLATED", min_rr=1.5,
        max_open_positions=3, daily_loss_limit_usdt=3.0, max_signal_age_s=180, max_slippage_pct=0.4,
        webhook_secret="x" * 32, enforce_tv_ips=False,
        telegram_bot_token="", telegram_chat_id="", telegram_admin_ids=set(),
        google_client_id="", google_client_secret="", google_refresh_token="", google_drive_folder_id="",
        data_dir=tmp_path / "data", timezone="America/Argentina/Buenos_Aires", monitor_interval_s=20, port=8080,
    )
    base.update(overrides)
    return Settings(**base)


class FakeClient:
    """Exchange en memoria con el mismo contrato que BingXClient."""

    def __init__(self, prices: dict[str, float]):
        self.prices = dict(prices)
        self._positions: dict[str, Position] = {}
        self.orders: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self.attach_brackets = True
        self.fail_protective = False
        self.fail_order: BingXError | None = None
        self.income_rows: list[dict] = []
        self._ids = itertools.count(1)

    def contract(self, symbol):
        return SPECS[symbol]

    def contracts(self, force=False):
        return SPECS

    def price(self, symbol):
        return self.prices[symbol]

    def positions(self, symbol=None):
        return [p for s, p in self._positions.items() if symbol in (None, s)]

    def set_margin_type(self, symbol, margin_type):
        self.calls.append(("margin", symbol, margin_type))

    def set_leverage(self, symbol, leverage, side="BOTH"):
        self.calls.append(("leverage", symbol, leverage, side))

    def place_market_with_brackets(self, symbol, direction, qty, sl, tp, client_id):
        if self.fail_order:
            raise self.fail_order
        self.calls.append(("market", symbol, direction, qty, sl, tp, client_id))
        price = self.prices[symbol]
        self._positions[symbol] = Position(symbol, direction, float(qty), price, price, 0.0, 4, 0.0, 0.0,
                                           str(next(self._ids)))
        if self.attach_brackets:
            self.orders[symbol] = [{"type": "STOP_MARKET", "stopPrice": sl},
                                   {"type": "TAKE_PROFIT_MARKET", "stopPrice": tp}]
        return {"orderId": next(self._ids)}

    def place_protective_order(self, symbol, direction, kind, qty, stop_price):
        if self.fail_protective:
            raise BingXError(101, "rejected", "/order")
        self.calls.append(("protective", symbol, kind, qty, stop_price))
        self.orders.setdefault(symbol, []).append(
            {"type": "STOP_MARKET" if kind == "SL" else "TAKE_PROFIT_MARKET", "stopPrice": stop_price})
        return {"orderId": next(self._ids)}

    def close_position_market(self, symbol, direction, qty):
        self.calls.append(("close", symbol, direction, qty))
        self._positions.pop(symbol, None)
        return {}

    def open_orders(self, symbol):
        return self.orders.get(symbol, [])

    def cancel_all_orders(self, symbol):
        self.orders.pop(symbol, None)

    def order_by_client_id(self, symbol, client_id):
        return None

    def income(self, symbol, start_ms, end_ms=None):
        return self.income_rows

    def position_history(self, symbol, start_ms, end_ms):
        return []

    def balance(self):
        return {"asset": "VST", "balance": 100.0, "equity": 100.0, "available": 100.0, "unrealized_pnl": 0.0}


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def store(settings):
    return Store(settings.data_dir)


@pytest.fixture
def narrator(settings):
    return Narrator(settings.mode_label, settings.timezone)


@pytest.fixture
def messages():
    return []
