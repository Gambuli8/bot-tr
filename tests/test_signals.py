import pytest
from pydantic import ValidationError

from bot.config import normalize_symbol, parse_id_list
from bot.signals import Signal


@pytest.mark.parametrize("raw", ["BINGX:BTCUSDT.P", "BTCUSDT", "btc/usdt", "BTC-USDT", "BTCUSDTPERP"])
def test_normalize_symbol(raw):
    assert normalize_symbol(raw) == "BTC-USDT"


def test_entry_requires_sl_and_tp():
    with pytest.raises(ValidationError):
        Signal(event="entry", id="a", symbol="BTCUSDT.P", side="LONG", price=1.0)


def test_side_aliases_and_symbol():
    s = Signal(event="zone", id="a", symbol="BINGX:ETHUSDT.P", side="buy", price=3000)
    assert s.side == "LONG" and s.symbol == "ETH-USDT" and s.base_asset == "ETH"


def test_client_order_id_is_bingx_safe():
    s = Signal(event="zone", id="BTCUSDT.P|L|1789585800000|extra-largo-para-cortar", symbol="BTCUSDT",
               side="SHORT", price=1)
    cid = s.client_order_id()
    assert len(cid) <= 40 and all(ch.isalnum() or ch in "-_" for ch in cid)


def test_parse_id_list_ignores_garbage():
    assert parse_id_list("123, -456, # comentario, abc") == {"123", "-456"}
