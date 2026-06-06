"""
tests/test_resilience.py
Tests de:
  - retry decorator selectivo por tipo de error ccxt (roadmap #6).
  - Reconciliación con el exchange: RESUME / DRIFT / MISMATCH / ORPHAN (roadmap #4).
"""

from types import SimpleNamespace

import ccxt
import pytest

import execution.order_manager as om_module
from execution.order_manager import OrderManager
from core.exchange import retry


# ─────────────────────────────────────────
#  retry decorator (roadmap #6)
# ─────────────────────────────────────────

class TestRetryDecorator:

    def _counter_fn(self, exc, succeed_after=None):
        calls = {"n": 0}

        @retry(max_attempts=3, base_delay=0.0, rate_limit_multiplier=1.0)
        def fn():
            calls["n"] += 1
            if succeed_after is not None and calls["n"] >= succeed_after:
                return "ok"
            raise exc
        return fn, calls

    def test_insufficient_funds_no_retry(self):
        fn, calls = self._counter_fn(ccxt.InsufficientFunds("no plata"))
        with pytest.raises(ccxt.InsufficientFunds):
            fn()
        assert calls["n"] == 1   # NO reintenta

    def test_order_not_found_no_retry(self):
        fn, calls = self._counter_fn(ccxt.OrderNotFound("no existe"))
        with pytest.raises(ccxt.OrderNotFound):
            fn()
        assert calls["n"] == 1

    def test_invalid_order_no_retry(self):
        fn, calls = self._counter_fn(ccxt.InvalidOrder("filtro"))
        with pytest.raises(ccxt.InvalidOrder):
            fn()
        assert calls["n"] == 1

    def test_generic_exchange_error_no_retry(self):
        fn, calls = self._counter_fn(ccxt.ExchangeError("raro"))
        with pytest.raises(ccxt.ExchangeError):
            fn()
        assert calls["n"] == 1

    def test_rate_limit_retries_until_max(self):
        fn, calls = self._counter_fn(ccxt.RateLimitExceeded("slow down"))
        with pytest.raises(ccxt.RateLimitExceeded):
            fn()
        assert calls["n"] == 3   # reintenta hasta max_attempts

    def test_network_error_retries_then_succeeds(self):
        fn, calls = self._counter_fn(ccxt.NetworkError("blip"), succeed_after=2)
        assert fn() == "ok"
        assert calls["n"] == 2   # falló 1, reintentó y salió bien


# ─────────────────────────────────────────
#  Reconciliación (roadmap #4)
# ─────────────────────────────────────────

def _settings(**ov):
    s = SimpleNamespace(
        symbol="BTC/USDT", symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        max_concurrent_trades=2, timeframe="15m",
        initial_capital=210.0, daily_drawdown_limit=0.05, trade_reserve_pct=0.30,
        reconcile_enabled=True, reconcile_interval_seconds=300,
    )
    for k, v in ov.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def om(tmp_path):
    om_module.STATE_FILE = tmp_path / "state.json"
    om_module.JOURNAL_FILE = tmp_path / "journal.jsonl"
    return OrderManager(_settings())


def _inject(om, symbol, direction="LONG", amount_usdt=70.0, entry=100.0):
    om.state.open_positions[symbol] = {
        "order_id": "PAPER", "client_order_id": "x", "direction": direction,
        "symbol": symbol, "entry_price": entry, "amount_btc": amount_usdt / entry,
        "amount_usdt": amount_usdt, "stop_loss": entry * 0.98,
        "original_stop_loss": entry * 0.98, "take_profit": entry * 1.05,
        "entry_time": "2026-01-01T00:00:00", "entry_reason": "test",
        "claude_confidence": 0.8, "trailing_active": False, "highest_price_seen": entry,
        "take_profit_1": 0.0, "initial_amount_btc": amount_usdt / entry,
        "tp1_done": False, "realized_pnl_usdt": 0.0,
    }


class TestReconcile:

    def test_resume_when_match(self, om):
        _inject(om, "BTC/USDT", "LONG")
        rep = om.reconcile({"BTC/USDT": 0.5})   # exchange LONG → coincide
        assert rep["ok"] is True
        assert rep["resumed"] == ["BTC/USDT"]
        assert om.has_position("BTC/USDT")

    def test_drift_when_exchange_flat(self, om):
        _inject(om, "BTC/USDT", "LONG")
        cap_before = om.state.capital
        rep = om.reconcile({})                   # exchange plano
        assert rep["ok"] is False
        assert rep["drifts"] == ["BTC/USDT"]
        assert not om.has_position("BTC/USDT")    # soltada
        assert om.state.capital == pytest.approx(cap_before + 70.0)  # notional devuelto

    def test_drift_no_autoheal_keeps_position(self, om):
        _inject(om, "BTC/USDT", "LONG")
        rep = om.reconcile({}, auto_heal=False)
        assert rep["drifts"] == ["BTC/USDT"]
        assert om.has_position("BTC/USDT")        # NO la toca

    def test_mismatch_direction(self, om):
        _inject(om, "BTC/USDT", "LONG")
        rep = om.reconcile({"BTC/USDT": -0.5})    # exchange SHORT, local LONG
        assert rep["ok"] is False
        assert rep["mismatches"] == ["BTC/USDT"]
        assert not om.has_position("BTC/USDT")

    def test_orphan_on_exchange(self, om):
        # local plano, exchange tiene SOL → orphan, no la adoptamos
        rep = om.reconcile({"SOL/USDT": 1.0})
        assert rep["ok"] is False
        assert rep["orphans"] == ["SOL/USDT"]
        assert not om.has_position("SOL/USDT")

    def test_clean_when_all_match(self, om):
        _inject(om, "BTC/USDT", "LONG")
        _inject(om, "ETH/USDT", "SHORT", entry=50.0)
        rep = om.reconcile({"BTC/USDT": 0.7, "ETH/USDT": -1.4})
        assert rep["ok"] is True
        assert set(rep["resumed"]) == {"BTC/USDT", "ETH/USDT"}


# ─────────────────────────────────────────
#  fetch_position_sizes (signo LONG/SHORT)
# ─────────────────────────────────────────

class TestFetchPositionSizes:

    def _client(self, has_positions, raw):
        from core.exchange import ExchangeClient
        c = ExchangeClient.__new__(ExchangeClient)
        c.settings = _settings()
        c.exchange = SimpleNamespace(
            has={"fetchPositions": has_positions},
            fetch_positions=lambda symbols=None: raw,
        )
        return c

    def test_spot_returns_empty(self):
        c = self._client(False, [])
        assert c.fetch_position_sizes(["BTC/USDT"]) == {}

    def test_signs_long_and_short(self):
        raw = [
            {"symbol": "BTC/USDT", "contracts": 0.5, "side": "long"},
            {"symbol": "ETH/USDT", "contracts": 2.0, "side": "short"},
            {"symbol": "SOL/USDT", "contracts": 0.0, "side": "long"},  # flat → se omite
        ]
        c = self._client(True, raw)
        out = c.fetch_position_sizes(["BTC/USDT", "ETH/USDT", "SOL/USDT"])
        assert out == {"BTC/USDT": 0.5, "ETH/USDT": -2.0}
