import time

from bot.bingx import Position
from bot.fmt import money, pct, price, qty
from bot.narrator import Narrator
from bot.signals import Signal


def test_money_argentine_format_with_sign():
    assert money(100000) == "$100.000,00"
    assert money(0.335, True) == "+$0,335"
    assert money(-0.122, True) == "−$0,122"
    assert money(0) == "$0,00"
    assert money(0.0001, True) == "+$0,0001"
    assert money(-0.0000001, True) == "$0,0000"  # redondea a cero: sin signo


def test_price_decimals_by_magnitude():
    assert price(76014.1) == "$76.014,1"
    assert price(2392.4) == "$2.392,40"
    assert price(98.379) == "$98,379"
    assert price(1.2922) == "$1,2922"
    assert price(0.08059) == "$0,08059"


def test_pct_and_qty():
    assert pct(-0.68, True) == "−0,68%"
    assert pct(1.3, True) == "+1,30%"
    assert pct(0.0, True) == "0,00%"
    assert qty(0.0001) == "0,0001"
    assert qty(49.0) == "49"
    assert qty(1250.5) == "1.250,5"


def test_status_header_and_coin_messages():
    n = Narrator("DEMO 🧪", "America/Argentina/Buenos_Aires")
    now_ms = time.time() * 1000
    header = n.status_header(
        paused=False, balance={"balance": 100000.0, "available": 99998.1, "unrealized_pnl": 0.021},
        balance_error="", open_count=1, positions_error="", setups_count=1,
        day_pnl=-0.12, day_count=1, daily_limit=3, margin=2, max_positions=3, demo=True)
    assert "$100.000,00" in header and "+$0,021" in header
    assert "−$0,12" in header and "−$3,00" in header and "1/3" in header

    btc = n.coin_status(
        symbol="BTC-USDT", last_price=76200.0,
        position=Position("BTC-USDT", "LONG", 0.0001, 76014.1, 76200.0, 0.0186, 4, 0, 1.9, "1"),
        trade={"stop_loss": 75500.0, "take_profit": 77000.0, "margin_used": 1.9, "opened_at": now_ms - 3_600_000},
        setups=[])
    assert btc.startswith("🪙 <b>BTC</b>")
    assert "SL $75.500,0 (−0,92%)" in btc and "TP $77.000,0 (+1,05%)" in btc
    assert "hace 1 h 0 min" in btc

    eth = n.coin_status(symbol="ETH-USDT", last_price=2390.0, setups=[
        {"symbol": "ETH-USDT", "side": "SHORT", "stage": "cambio 1H", "fib_618": 2400.5, "fib_75": 2420.0,
         "fib_sl": 2425.0, "updated": time.time() - 600}])
    assert "Sin operación abierta" in eth and "Entrada 0,618: $2.400,50" in eth
    assert "Distancia a la entrada: +0,44%" in eth


def test_choch_message_has_signed_distance():
    n = Narrator("DEMO 🧪", "America/Argentina/Buenos_Aires")
    s = Signal(event="choch", id="x", symbol="BTCUSDT.P", side="LONG", price=77000,
               fib_start=74000, fib_end=78000, fib_618=75528, fib_75=75000, fib_sl=74856)
    text = n.setup_event(s)
    assert "$75.528,0" in text and "(−1,91% desde acá)" in text
