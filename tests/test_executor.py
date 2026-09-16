import time

import pytest

from bot.bingx import BingXError, Position
from bot.executor import Executor
from bot.signals import Signal
from tests.conftest import FakeClient

PRICE = 75832.5


@pytest.fixture
def client():
    return FakeClient({"BTC-USDT": PRICE, "ETH-USDT": 3000.0, "DOGE-USDT": 0.25, "ZEC-USDT": 40.0})


@pytest.fixture
def executor(settings, client, store, narrator, messages, monkeypatch):
    monkeypatch.setattr("bot.executor.time.sleep", lambda s: None)
    return Executor(settings, client, store, narrator, messages.append)


def entry(**kw):
    base = dict(event="entry", id=f"BTCUSDT-L-{time.time_ns()}", symbol="BINGX:BTCUSDT.P", side="LONG",
                price=PRICE, sl=74700.0, tp=78500.0, time=int(time.time() * 1000),
                fib_618=75000.0, fib_75=74750.0)
    base.update(kw)
    return Signal(**base)


def test_opens_trade_with_brackets(executor, client, store, messages):
    result = executor.handle(entry())
    assert result["status"] == "opened"
    market = [c for c in client.calls if c[0] == "market"][0]
    assert market[1:6] == ("BTC-USDT", "LONG", "0.0001", "74700.0", "78500.0")
    assert ("leverage", "BTC-USDT", 4, "BOTH") in client.calls
    assert ("margin", "BTC-USDT", "ISOLATED") in client.calls
    assert "BTC-USDT" in store.open_trades
    assert "ENTRÉ" in messages[-1]


def test_duplicate_signal_is_ignored(executor, client):
    sig = entry()
    executor.handle(sig)
    assert executor.handle(sig)["status"] == "duplicate"
    assert len([c for c in client.calls if c[0] == "market"]) == 1


def test_paused_rejects(executor, store, client, messages):
    store.update(paused=True)
    assert executor.handle(entry())["status"] == "rejected"
    assert not [c for c in client.calls if c[0] == "market"]
    assert "pausa" in messages[-1]


def test_existing_position_rejects(executor, client):
    client._positions["BTC-USDT"] = Position("BTC-USDT", "SHORT", 0.0001, PRICE, PRICE, 0, 4, 0, 0, "9")
    assert "abierta" in executor.handle(entry())["reason"]


def test_slippage_rejects(executor, client):
    client.prices["BTC-USDT"] = PRICE * 1.01
    assert "movió" in executor.handle(entry())["reason"]


def test_stale_signal_rejects(executor):
    old = entry(time=int((time.time() - 3600) * 1000))
    assert "tarde" in executor.handle(old)["reason"]


def test_daily_loss_limit(executor, store):
    now = int(time.time() * 1000)
    store.log_closed_trade({"symbol": "ETH-USDT", "pnl_usdt": -3.5, "closed_at": now, "opened_at": now - 1})
    assert "límite" in executor.handle(entry())["reason"]


def test_non_whitelisted_symbol(executor, client):
    sig = entry(symbol="PEPEUSDT")
    assert executor.handle(sig)["status"] == "rejected"


def test_setup_events_are_narrated_and_tracked(executor, store, messages):
    sig = Signal(event="choch", id="setup-1", symbol="ETHUSDT.P", side="SHORT", price=3000,
                 fib_start=2900, fib_end=3200, fib_618=3014.6, fib_75=2975)
    assert executor.handle(sig)["status"] == "narrated"
    assert store.state["setups"]["ETH-USDT:SHORT"]["id"] == "setup-1"
    assert "cambio de tendencia" in messages[-1]
    executor.handle(Signal(event="cancel", id="setup-1", symbol="ETHUSDT.P", side="SHORT", price=3050,
                           note="cerró por encima del 0.75"))
    assert "ETH-USDT:SHORT" not in store.state["setups"]


def test_missing_stop_loss_gets_replaced(executor, client, messages):
    client.attach_brackets = False
    assert executor.handle(entry())["status"] == "opened"
    kinds = [c[2] for c in client.calls if c[0] == "protective"]
    assert kinds == ["SL", "TP"]


def test_position_closed_if_stop_cannot_be_placed(executor, client, messages):
    client.attach_brackets = False
    client.fail_protective = True
    executor.handle(entry())
    assert any(c[0] == "close" for c in client.calls)
    assert client.positions() == []
    assert any("Cerré la posición por seguridad" in m for m in messages)


def test_exchange_error_on_order_is_reported(executor, client, store, messages):
    client.fail_order = BingXError(80001, "insufficient margin", "/order")
    assert executor.handle(entry())["status"] == "error"
    assert "BTC-USDT" not in store.open_trades
    assert "error al abrir" in messages[-1]
