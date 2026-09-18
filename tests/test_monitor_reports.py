import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.executor import Executor
from bot.monitor import Monitor, classify_exit
from bot.reports import Reporter, compute_stats, html_report, previous_month, previous_week, telegram_text
from bot.signals import Signal
from tests.conftest import FakeClient

PRICE = 75832.5


@pytest.fixture
def setup(settings, store, narrator, messages, monkeypatch):
    monkeypatch.setattr("bot.executor.time.sleep", lambda s: None)
    monkeypatch.setattr("bot.monitor.time.sleep", lambda s: None)
    client = FakeClient({"BTC-USDT": PRICE, "ETH-USDT": 3000.0, "DOGE-USDT": 0.25, "ZEC-USDT": 40.0})
    executor = Executor(settings, client, store, narrator, messages.append)
    reporter = Reporter(settings, store, messages.append)
    monitor = Monitor(settings, client, store, executor, narrator, reporter, messages.append)
    return client, executor, monitor


def test_detects_tp_close_and_records_pnl(setup, store, messages):
    client, executor, monitor = setup
    executor.handle(Signal(event="entry", id="t1", symbol="BTCUSDT.P", side="LONG", price=PRICE,
                           sl=74700.0, tp=78500.0, time=int(time.time() * 1000)))
    # TP ejecutado en el exchange:
    client._positions.clear()
    client.prices["BTC-USDT"] = 78500.0
    client.income_rows = [
        {"incomeType": "REALIZED_PNL", "income": "0.2667"},
        {"incomeType": "TRADING_FEE", "income": "-0.0078"},
    ]
    monitor.run_once()
    trades = store.closed_trades()
    assert len(trades) == 1
    assert trades[0]["pnl_usdt"] == pytest.approx(0.2589)
    assert trades[0]["fees_usdt"] == pytest.approx(0.0078)
    assert trades[0]["exit_reason"] == "TP"
    assert store.open_trades == {}
    assert "GANANCIA" in messages[-1]


def test_unknown_position_alerts_once(setup, messages):
    from bot.bingx import Position
    client, _, monitor = setup
    client._positions["ETH-USDT"] = Position("ETH-USDT", "LONG", 0.01, 3000, 3000, 0, 5, 0, 0, "77")
    monitor.run_once()
    monitor.run_once()
    assert sum("no abrió el bot" in m for m in messages) == 1


def test_classify_exit():
    trade = {"stop_loss": 100.0, "take_profit": 130.0}
    assert classify_exit(trade, 129.8) == "TP"
    assert classify_exit(trade, 100.3) == "SL"
    assert classify_exit(trade, 115.0) == "OTRO"
    assert classify_exit({**trade, "manual_close": True}, 129.8) == "MANUAL"


def test_stats():
    trades = [
        {"symbol": "BTC-USDT", "pnl_usdt": 0.30, "closed_at": 1, "exit_reason": "TP", "fees_usdt": 0.01},
        {"symbol": "BTC-USDT", "pnl_usdt": -0.10, "closed_at": 2, "exit_reason": "SL", "fees_usdt": 0.01},
        {"symbol": "ETH-USDT", "pnl_usdt": -0.12, "closed_at": 3, "exit_reason": "SL", "fees_usdt": 0.01},
    ]
    events = [{"kind": "signal", "event": "zone"}, {"kind": "signal", "event": "entry"},
              {"kind": "rejected", "reason": "R:R bajo"}]
    st = compute_stats(trades, events)
    assert st["trades"] == 3 and st["wins"] == 1
    assert st["net"] == pytest.approx(0.08)
    assert st["profit_factor"] == pytest.approx(0.30 / 0.22)
    assert st["max_drawdown"] == pytest.approx(0.22)
    assert st["max_loss_streak"] == 2
    assert st["zones"] == 1 and st["entries_signaled"] == 1


def test_periods():
    tz = ZoneInfo("America/Argentina/Buenos_Aires")
    now = datetime(2026, 9, 16, 10, 0, tzinfo=tz)  # miércoles
    week = previous_week(now)
    assert week.start.date().isoformat() == "2026-09-07" and week.end.date().isoformat() == "2026-09-14"
    month = previous_month(now)
    assert month.key == "2026-08" and month.end.date().isoformat() == "2026-09-01"


def test_first_run_does_not_send_old_reports(settings, store, messages):
    reporter = Reporter(settings, store, messages.append)
    reporter.maybe_send_scheduled()
    assert messages == []
    assert set(store.state["reports"]) == {"weekly", "monthly"}
    # Simula que cambió la semana: se manda el resumen una sola vez.
    store.state["reports"]["weekly"] = "2000-W01"
    reporter.maybe_send_scheduled()
    reporter.maybe_send_scheduled()
    assert sum("Resumen semanal" in m for m in messages) == 1
    assert list((settings.data_dir / "reports").glob("*.html"))


def test_carry_income_enters_the_summary():
    trades = [{"symbol": "BTC-USDT", "direction": "LONG", "pnl_usdt": 0.10, "opened_at": 1_789_599_912_589,
               "closed_at": 1_789_655_129_834, "entry_price": 76000.0, "exit_price": 77000.0,
               "exit_reason": "TP", "fees_usdt": 0.01}]
    events = [
        {"kind": "carry_income", "symbol": "BTC-USDT", "income": "FUNDING_FEE", "amount": 0.02, "paper": True},
        {"kind": "carry_income", "symbol": "BTC-USDT", "income": "FUNDING_FEE", "amount": 0.03, "paper": True},
        {"kind": "carry_income", "symbol": "BTC-USDT", "income": "SPOT_FEE", "amount": -0.04, "paper": True},
        {"kind": "carry_income", "symbol": "DOGE-USDT", "income": "FUNDING_FEE", "amount": 0.05, "paper": True},
        {"kind": "carry_income", "symbol": "DOGE-USDT", "income": "TRADING_FEE", "amount": -0.01, "paper": True},
        {"kind": "carry_income", "symbol": "DOGE-USDT", "income": "REALIZED_PNL", "amount": -0.02, "paper": True},
    ]
    st = compute_stats(trades, events)
    carry = st["carry"]
    assert carry["active"] and carry["paper"] and carry["payments"] == 3
    assert carry["funding"] == pytest.approx(0.10)
    assert carry["fees"] == pytest.approx(0.05)
    assert carry["realized"] == pytest.approx(-0.02)
    assert carry["net"] == pytest.approx(0.03)
    assert carry["by_symbol"]["BTC-USDT"]["net"] == pytest.approx(0.01)

    period = previous_week(datetime(2026, 9, 16, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires")))
    text = telegram_text(period, st, "DEMO", None)
    assert "Carry (captura de funding)" in text and "SIMULADO" in text
    assert "Total del período" in text
    html = html_report(period, st, "DEMO", ZoneInfo("America/Argentina/Buenos_Aires"))
    assert "Carry (captura de funding)" in html and "DOGE-USDT" in html


def test_summary_without_carry_has_no_carry_section():
    st = compute_stats([], [{"kind": "signal", "event": "zone"}])
    assert st["carry"]["active"] is False
    period = previous_week(datetime(2026, 9, 16, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires")))
    assert "Carry" not in telegram_text(period, st, "DEMO", None)
    assert "Carry" not in html_report(period, st, "DEMO", ZoneInfo("America/Argentina/Buenos_Aires"))
