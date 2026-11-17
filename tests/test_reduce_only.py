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
    c.place_market_order("sell", "bot_x_close", qty_base=1.0, reduce_only=True)
    assert fe.created["params"].get("reduceOnly") is True


def test_entrada_no_lleva_reduce_only():
    c, fe = _client()
    c.place_market_order("buy", "bot_x", notional_usdt=50.0)
    assert "reduceOnly" not in fe.created["params"]


def test_reduce_only_saltea_validacion_de_minimo():
    # Cantidad chica (notional 0.05*6.5=0.325 < cost.min 5): sin reduce_only falla,
    # con reduce_only debe poder cerrar igual.
    c, fe = _client()
    with pytest.raises(ValueError):
        c.place_market_order("sell", "bot_x", qty_base=0.05)
    c2, fe2 = _client()
    c2.place_market_order("sell", "bot_x_close", qty_base=0.05, reduce_only=True)  # no raisea
    assert fe2.created["params"].get("reduceOnly") is True


# ───── Unidades de cantidad: independientes del side (bug SHORT) ─────

def test_notional_en_sell_abre_short_del_tamano_correcto():
    """Antes: sell con 65 'USDT' mandaba 65 unidades base (~$422). Ahora 65/6.5 = 10."""
    c, fe = _client()
    c.place_market_order("sell", "bot_short", notional_usdt=65.0)
    assert fe.created["side"] == "sell"
    assert fe.created["amount"] == pytest.approx(10.0)


def test_notional_en_buy_y_sell_da_la_misma_qty():
    c1, fe1 = _client()
    c1.place_market_order("buy", "a", notional_usdt=65.0)
    c2, fe2 = _client()
    c2.place_market_order("sell", "b", notional_usdt=65.0)
    assert fe1.created["amount"] == pytest.approx(fe2.created["amount"])


def test_qty_base_en_buy_cierra_short_completo():
    """Antes: buy con qty 2.0 se dividía por el precio (→ 0.31). Ahora manda 2.0."""
    c, fe = _client()
    c.place_market_order("buy", "bot_x_close", qty_base=2.0, reduce_only=True)
    assert fe.created["side"] == "buy"
    assert fe.created["amount"] == pytest.approx(2.0)


@pytest.mark.parametrize("kwargs", [{}, {"notional_usdt": 10.0, "qty_base": 1.0}])
def test_exige_exactamente_una_unidad(kwargs):
    c, fe = _client()
    with pytest.raises(ValueError):
        c.place_market_order("buy", "x", **kwargs)
    assert fe.created is None


def test_side_invalido():
    c, fe = _client()
    with pytest.raises(ValueError):
        c.place_market_order("short", "x", qty_base=1.0)
    assert fe.created is None


def test_esc_telegram():
    from notifications.telegram import _esc
    assert _esc("Stop-loss: $1 <= $2") == "Stop-loss: $1 &lt;= $2"
    assert _esc("a & b > c") == "a &amp; b &gt; c"


def test_esc_notifier():
    from notifier import _esc
    assert _esc("x >= y") == "x &gt;= y"
