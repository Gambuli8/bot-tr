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


def test_status_renders_positions_and_setups():
    n = Narrator("DEMO 🧪", "America/Argentina/Buenos_Aires")
    now_ms = time.time() * 1000
    text = n.status(
        paused=False,
        balance={"balance": 100000.0, "available": 99998.1, "unrealized_pnl": 0.021},
        balance_error="", positions_error="",
        positions=[Position("BTC-USDT", "LONG", 0.0001, 76014.1, 76200.0, 0.0186, 4, 0, 1.9, "1")],
        open_trades={"BTC-USDT": {"stop_loss": 75500.0, "take_profit": 77000.0, "margin_used": 1.9,
                                  "opened_at": now_ms - 3_600_000}},
        setups={"ETH-USDT:SHORT": {"symbol": "ETH-USDT", "side": "SHORT", "stage": "cambio 1H",
                                   "fib_618": 2400.5, "fib_75": 2420.0, "fib_sl": 2425.0,
                                   "updated": time.time() - 600}},
        day_pnl=-0.12, day_count=1, daily_limit=3, margin=2, max_positions=3,
        symbols=["BTC-USDT", "ETH-USDT"], demo=True,
    )
    assert "$100.000,00" in text and "+$0,021" in text
    assert "−$0,12" in text and "−$3,00" in text
    assert "SL $75.500,0 (−0,92%)" in text and "TP $77.000,0 (+1,05%)" in text
    assert "ETH" in text and "0,618 $2.400,50" in text
    assert "hace 1 h 0 min" in text


def test_choch_message_has_signed_distance():
    n = Narrator("DEMO 🧪", "America/Argentina/Buenos_Aires")
    s = Signal(event="choch", id="x", symbol="BTCUSDT.P", side="LONG", price=77000,
               fib_start=74000, fib_end=78000, fib_618=75528, fib_75=75000, fib_sl=74856)
    text = n.setup_event(s)
    assert "$75.528,0" in text and "(−1,91% desde acá)" in text
