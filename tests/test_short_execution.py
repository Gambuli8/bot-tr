"""
tests/test_short_execution.py
Regresión del bug de unidades en place_market_order (SHORTs rotos):
- Abrir SHORT mandaba `trade_size_usdt` como cantidad de BTC (rechazo o tamaño ×precio).
- Cerrar SHORT mandaba `amount_btc / precio` (cierre microscópico → posición fantasma).
Ahora OrderManager pasa siempre qty_base en BTC, idéntica para entrada, SL, TP y cierre.
"""

import math
from types import SimpleNamespace

import pytest

import execution.order_manager as om_module
from config.settings import Settings
from execution.order_manager import OrderManager

PRICE = 60_000.0
STEP = 0.001


class FakeExchange:
    """Registra cada orden. Cantidades truncadas a STEP como Binance."""

    def __init__(self):
        self.market_orders: list[dict] = []
        self.sl_orders: list[dict] = []
        self.tp_orders: list[dict] = []

    def round_amount(self, qty, symbol=None):
        return math.floor(qty / STEP + 1e-9) * STEP

    def validate_order_filters(self, amount_btc, amount_usdt, symbol=None):
        return True, "OK"

    def place_market_order(self, side, client_order_id, *, notional_usdt=None,
                           qty_base=None, reduce_only=False):
        self.market_orders.append(dict(side=side, cid=client_order_id,
                                       notional_usdt=notional_usdt, qty_base=qty_base,
                                       reduce_only=reduce_only))
        return {"id": f"m{len(self.market_orders)}"}

    def place_stop_loss_market(self, side, amount_btc, stop_price, client_order_id):
        self.sl_orders.append(dict(side=side, qty=amount_btc, stop=stop_price))
        return {"id": "sl1"}

    def place_take_profit_limit(self, side, amount_btc, limit_price, client_order_id):
        self.tp_orders.append(dict(side=side, qty=amount_btc, limit=limit_price))
        return {"id": "tp1"}

    def cancel_order(self, oid):
        return {}


@pytest.fixture
def om(tmp_path, monkeypatch):
    monkeypatch.setattr(om_module, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(om_module, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    s = Settings(
        binance_api_key="k", binance_api_secret="s", anthropic_api_key="a",
        telegram_bot_token="t", telegram_chat_id="1",
    )
    s.initial_capital = 10_000.0
    s.max_risk_per_trade = 0.01
    s.trade_reserve_pct = 0.30
    s.atr_sl_multiplier = 1.5
    s.tp_scaling_enabled = False
    return OrderManager(s, exchange_client=FakeExchange())


def _decision(direction: str):
    return SimpleNamespace(
        accion="COMPRAR" if direction == "LONG" else "VENDER",
        direction=direction, confianza=0.9, razon="test",
        stop_loss_pct=0.01, take_profit_pct=0.02,
    )


SNAP = SimpleNamespace(price=PRICE, atr_pct=0.5)  # 0.5% × 1.5 = 0.75% < 1% → SL 1%


def test_short_abre_con_qty_en_btc_no_en_usdt(om):
    pos = om.open_position(SNAP, _decision("SHORT"))
    assert pos is not None and pos.direction == "SHORT"
    entry = om.exchange.market_orders[0]
    assert entry["side"] == "sell"
    assert entry["reduce_only"] is False
    # Riesgo 1% de 10k = 100 USDT / SL 1% = 10k → capeado a 70% = 7000 USDT ≈ 0.116 BTC
    assert entry["qty_base"] == pytest.approx(0.116)
    assert entry["notional_usdt"] is None
    # La misma qty para entrada, SL, TP y estado local
    assert om.exchange.sl_orders[0]["qty"] == pytest.approx(entry["qty_base"])
    assert om.exchange.tp_orders[0]["qty"] == pytest.approx(entry["qty_base"])
    assert pos.amount_btc == pytest.approx(entry["qty_base"])
    # SL arriba y TP abajo del entry, del lado de compra
    assert om.exchange.sl_orders[0]["side"] == "buy"
    assert pos.stop_loss > PRICE > pos.take_profit


def test_short_cierra_la_cantidad_completa(om):
    pos = om.open_position(SNAP, _decision("SHORT"))
    om.close_position(SNAP, "Cierre manual via Telegram")
    close = om.exchange.market_orders[-1]
    assert close["side"] == "buy"
    assert close["reduce_only"] is True
    assert close["qty_base"] == pytest.approx(pos.amount_btc)  # antes: amount_btc / 60000
    assert om.state.open_positions == []


def test_long_y_short_mismo_tamano(om):
    """Mismo capital y mismo SL → misma qty, sin importar la dirección."""
    long_pos = om.open_position(SNAP, _decision("LONG"))
    om.close_position(SNAP, "flat")  # mismo precio → PnL 0, capital restaurado
    short_pos = om.open_position(SNAP, _decision("SHORT"))
    buy, sell = om.exchange.market_orders[0], om.exchange.market_orders[2]
    assert (buy["side"], sell["side"]) == ("buy", "sell")
    assert buy["qty_base"] == pytest.approx(sell["qty_base"])
    assert long_pos.amount_btc == pytest.approx(short_pos.amount_btc)


def test_capital_local_usa_notional_redondeado(om):
    cap0 = om.state.capital
    pos = om.open_position(SNAP, _decision("SHORT"))
    # El capital descontado coincide con lo que realmente se mandó (qty × precio)
    assert cap0 - om.state.capital == pytest.approx(pos.amount_btc * PRICE, abs=0.01)


def test_rollback_si_falla_sl_cierra_short_completo(om):
    def boom(**kw):
        raise RuntimeError("SL rechazado")
    om.exchange.place_stop_loss_market = boom
    pos = om.open_position(SNAP, _decision("SHORT"))
    assert pos is None
    entry, rollback = om.exchange.market_orders
    assert rollback["side"] == "buy"
    assert rollback["reduce_only"] is True
    assert rollback["qty_base"] == pytest.approx(entry["qty_base"])


def test_tamano_menor_a_un_step_no_opera(om):
    om.state.capital = 5.0  # 70% de 5 USDT = 3.5 USDT << 1 step (60 USDT)
    assert om.open_position(SNAP, _decision("SHORT")) is None
    assert om.exchange.market_orders == []
