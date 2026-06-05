"""
tests/test_scaled_tp.py
Tests deterministas (sin red) de la mecánica del TP escalado + breakeven shift:
  - TP1 cierra la fracción correcta y contabiliza la ganancia parcial.
  - El SL se mueve a breakeven tras TP1.
  - El PnL total del trade = parcial (TP1) + remanente.
  - "Trade gratis": si tras TP1 el precio vuelve al entry, igual cerramos en verde.

Cubre las dos implementaciones que tienen que quedar consistentes:
  1. El simulador del backtester (scripts/backtest.py) — el motor de validación.
  2. El OrderManager vivo (execution/order_manager.py) — el bot real.
"""

from types import SimpleNamespace

import pytest

import scripts.backtest as bt
import execution.order_manager as om_module
from execution.order_manager import OrderManager


# ─────────────────────────────────────────
#  Settings mínimos para el TP escalado
# ─────────────────────────────────────────

def _scaled_settings(**overrides):
    s = SimpleNamespace(
        scaled_tp_enabled=True,
        tp1_r_multiple=1.0,
        tp1_size_pct=0.5,
        breakeven_after_tp1=True,
        breakeven_offset_pct=0.0,        # sin colchón: breakeven exacto en el entry
        initial_capital=200.0,
        daily_drawdown_limit=0.10,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ─────────────────────────────────────────
#  1) Simulador del backtester
# ─────────────────────────────────────────

class TestScaledTPSimulator:

    def _make_sim(self, settings):
        # Evitamos Simulator.__init__ (instancia TechnicalEngine, pesado).
        sim = bt.Simulator.__new__(bt.Simulator)
        sim.s = settings
        sim.capital = 100.0          # como si 100 USDT estuvieran en la posición
        sim.trades = []
        sim.equity_curve = []
        sim.peak = 200.0
        sim.max_dd = 0.0
        sim.last_close_idx = -10**9
        sim.position = None
        return sim

    def _long_pos(self):
        return bt.SimPosition(
            direction="LONG", entry_price=100.0, amount_btc=1.0, amount_usdt=100.0,
            stop_loss=95.0, original_stop_loss=95.0, take_profit=110.0,
            entry_idx=0, entry_reason="test",
            initial_amount_btc=1.0, initial_amount_usdt=100.0, tp1_price=105.0,
        )

    def test_tp1_partial_long(self):
        sim = self._make_sim(_scaled_settings())
        sim.position = self._long_pos()
        sim._take_partial_tp1(105.0, idx=5)
        p = sim.position
        # Cerramos 50% del tamaño original a 105 → +5 USDT sobre 0.5 BTC = +2.5
        assert p.amount_btc == pytest.approx(0.5)
        assert p.realized_pnl == pytest.approx(2.5)
        assert p.tp1_done is True
        # SL movido a breakeven (entry)
        assert p.stop_loss == pytest.approx(100.0)
        # Capital recibió el notional parcial (50) + ganancia parcial (2.5)
        assert sim.capital == pytest.approx(152.5)

    def test_free_trade_breakeven_close(self):
        """Tras TP1, si el precio vuelve al entry, igual cerramos en verde."""
        sim = self._make_sim(_scaled_settings())
        sim.position = self._long_pos()
        sim._take_partial_tp1(105.0, idx=5)
        sim._close(100.0, "Breakeven post-TP1", idx=10)
        t = sim.trades[-1]
        assert t.took_tp1 is True
        # PnL total = parcial (+2.5) + remanente (0) = +2.5 → trade gratis ganador
        assert t.pnl_usdt == pytest.approx(2.5)
        assert t.pnl_pct == pytest.approx(2.5)   # 2.5 sobre notional original de 100

    def test_tp1_then_full_tp(self):
        sim = self._make_sim(_scaled_settings())
        sim.position = self._long_pos()
        sim._take_partial_tp1(105.0, idx=5)
        sim._close(110.0, "Take-profit", idx=12)
        t = sim.trades[-1]
        # parcial +2.5 (0.5 BTC @ +5) + remanente +5 (0.5 BTC @ +10) = +7.5
        assert t.pnl_usdt == pytest.approx(7.5)

    def test_short_tp1_partial(self):
        sim = self._make_sim(_scaled_settings())
        sim.position = bt.SimPosition(
            direction="SHORT", entry_price=100.0, amount_btc=1.0, amount_usdt=100.0,
            stop_loss=105.0, original_stop_loss=105.0, take_profit=90.0,
            entry_idx=0, entry_reason="test",
            initial_amount_btc=1.0, initial_amount_usdt=100.0, tp1_price=95.0,
        )
        sim._take_partial_tp1(95.0, idx=5)
        p = sim.position
        assert p.amount_btc == pytest.approx(0.5)
        assert p.realized_pnl == pytest.approx(2.5)   # short: (100-95)*0.5
        assert p.stop_loss == pytest.approx(100.0)    # breakeven baja al entry


# ─────────────────────────────────────────
#  2) OrderManager vivo
# ─────────────────────────────────────────

class TestScaledTPOrderManager:

    @pytest.fixture
    def om(self, tmp_path):
        om_module.STATE_FILE = tmp_path / "state.json"
        om_module.JOURNAL_FILE = tmp_path / "journal.jsonl"
        manager = OrderManager(_scaled_settings())
        manager.state.capital = 100.0
        return manager

    def _open_long(self, om):
        om.state.open_position = {
            "order_id": "PAPER", "client_order_id": "bot_test", "direction": "LONG",
            "entry_price": 100.0, "amount_btc": 1.0, "amount_usdt": 100.0,
            "stop_loss": 95.0, "original_stop_loss": 95.0, "take_profit": 110.0,
            "entry_time": "2026-01-01T00:00:00", "entry_reason": "test",
            "claude_confidence": 0.8, "trailing_active": False, "highest_price_seen": 100.0,
            "take_profit_1": 105.0, "initial_amount_btc": 1.0,
            "tp1_done": False, "realized_pnl_usdt": 0.0,
        }

    def test_no_partial_when_disabled(self, om):
        om.settings.scaled_tp_enabled = False
        self._open_long(om)
        snap = SimpleNamespace(price=106.0)
        assert om.maybe_take_partial_tp1(snap) is None
        assert om.state.open_position["tp1_done"] is False

    def test_no_partial_before_tp1(self, om):
        self._open_long(om)
        snap = SimpleNamespace(price=104.0)   # todavía no tocó 105
        assert om.maybe_take_partial_tp1(snap) is None

    def test_partial_fill_and_breakeven(self, om):
        self._open_long(om)
        cap_before = om.state.capital
        snap = SimpleNamespace(price=106.0)
        event = om.maybe_take_partial_tp1(snap)
        assert event is not None
        pos = om.state.open_position
        assert pos["tp1_done"] is True
        assert pos["amount_btc"] == pytest.approx(0.5)
        # ganancia parcial: (106-100)*0.5 = 3.0
        assert pos["realized_pnl_usdt"] == pytest.approx(3.0)
        assert pos["stop_loss"] == pytest.approx(100.0)   # breakeven
        assert om.state.capital == pytest.approx(cap_before + 50.0 + 3.0)

    def test_partial_is_idempotent(self, om):
        self._open_long(om)
        snap = SimpleNamespace(price=106.0)
        assert om.maybe_take_partial_tp1(snap) is not None
        # segundo intento: ya está tp1_done → no vuelve a cerrar
        assert om.maybe_take_partial_tp1(snap) is None

    def test_close_includes_realized_pnl(self, om):
        self._open_long(om)
        om.maybe_take_partial_tp1(SimpleNamespace(price=106.0))
        # cerramos el remanente en breakeven (100) → total = realized 3.0
        rec = om.close_position(SimpleNamespace(price=100.0), "Breakeven post-TP1")
        assert rec["took_tp1"] is True
        assert rec["realized_tp1_pnl"] == pytest.approx(3.0)
        assert rec["pnl"] == pytest.approx(3.0)
        assert om.state.winning_trades == 1
