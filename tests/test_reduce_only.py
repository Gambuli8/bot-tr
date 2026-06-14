"""
tests/test_reduce_only.py
Regresión del incidente 2026-06-14 (SHORT fantasma de AVAX):
- Las órdenes de cierre DEBEN llevar reduceOnly → nunca abren posición opuesta.
- reduceOnly debe saltar la validación de MIN_NOTIONAL (hay que poder cerrar dust).
- Escape de HTML en notificaciones (el '<=' del motivo rompía Telegram → HTTP 400).
"""

from types import SimpleNamespace

import pytest

from core.exchange import ExchangeClient


class FakeEx:
    def __init__(self):
        self.created = None

    def amount_to_precision(self, sym, amt):
        return amt

    def fetch_ticker(self, sym):
        return {"last": 6.5}

    def market(self, sym):
        return {"limits": {"amount": {"min": 0.1}, "cost": {"min": 5.0}}}

    def create_market_order(self, symbol, side, amount, params):
        self.created = {"symbol": symbol, "side": side, "amount": amount, "params": params}
        return {"id": "x"}


def _client():
    c = ExchangeClient.__new__(ExchangeClient)
    c.settings = SimpleNamespace(symbol="AVAX/USDT")
    c.leverage = 7
    c.margin_mode = "isolated"
    c._leverage_configured_for = "AVAX/USDT"  # evita API call de leverage
    fe = FakeEx()
    c.exchange = fe
    c.data_exchange = fe
    return c, fe


def test_close_lleva_reduce_only():
    c, fe = _client()
    c.place_market_order("sell", 1.0, "bot_x_close", reduce_only=True)
    assert fe.created["params"].get("reduceOnly") is True


def test_entrada_no_lleva_reduce_only():
    c, fe = _client()
    c.place_market_order("buy", 50.0, "bot_x", reduce_only=False)
    assert "reduceOnly" not in fe.created["params"]


def test_reduce_only_saltea_validacion_de_minimo():
    # Cantidad chica (notional 0.05*6.5=0.325 < cost.min 5): sin reduce_only falla,
    # con reduce_only debe poder cerrar igual.
    c, fe = _client()
    with pytest.raises(ValueError):
        c.place_market_order("sell", 0.05, "bot_x", reduce_only=False)
    c2, fe2 = _client()
    c2.place_market_order("sell", 0.05, "bot_x_close", reduce_only=True)  # no raisea
    assert fe2.created["params"].get("reduceOnly") is True


def test_esc_telegram():
    from notifications.telegram import _esc
    assert _esc("Stop-loss: $1 <= $2") == "Stop-loss: $1 &lt;= $2"
    assert _esc("a & b > c") == "a &amp; b &gt; c"


def test_esc_notifier():
    from notifier import _esc
    assert _esc("x >= y") == "x &gt;= y"
