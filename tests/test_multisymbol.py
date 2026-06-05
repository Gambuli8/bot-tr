"""
tests/test_multisymbol.py
Tests del modelo Multi-Symbol:
  - Candado de exposición global (max_concurrent_trades).
  - Una sola posición por símbolo.
  - Apertura/cierre independientes por moneda (sin mezclar datos).
  - Pool de capital COMPARTIDO: sizing sobre el equity total + reserva 30%.
  - Migración del state.json viejo (single-symbol) al nuevo formato.
"""

from types import SimpleNamespace

import pytest

import execution.order_manager as om_module
from execution.order_manager import OrderManager


def _settings(**overrides):
    s = SimpleNamespace(
        symbol="BTC/USDT",
        symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "AVAX/USDT"],
        max_concurrent_trades=2,
        timeframe="15m",
        initial_capital=210.0,
        max_risk_per_trade=0.025,
        daily_drawdown_limit=0.05,
        trade_reserve_pct=0.30,
        min_claude_confidence=0.55,
        min_risk_reward=2.0,
        atr_sl_multiplier=2.0,
        cooldown_bars=0,
        use_kelly_sizing=False,
        kelly_min_trades=10,
        kelly_fraction=0.5,
        kelly_min_risk_pct=0.005,
        kelly_max_risk_pct=0.02,
        trailing_stop_enabled=False,
        trailing_activation_pct=0.02,
        trailing_distance_pct=0.015,
        scaled_tp_enabled=False,
        tp1_r_multiple=1.0,
        tp1_size_pct=0.5,
        breakeven_after_tp1=True,
        breakeven_offset_pct=0.0005,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def om(tmp_path):
    om_module.STATE_FILE = tmp_path / "state.json"
    om_module.JOURNAL_FILE = tmp_path / "journal.jsonl"
    return OrderManager(_settings())


def _decision(direction="LONG"):
    return SimpleNamespace(
        accion="COMPRAR" if direction == "LONG" else "VENDER",
        confianza=0.8, razon="setup test",
        stop_loss_pct=0.02, take_profit_pct=0.05, direction=direction,
    )


def _snapshot(price=100.0, atr_pct=1.0):
    return SimpleNamespace(price=price, atr_pct=atr_pct, symbol="BTC/USDT")


# ─────────────────────────────────────────
#  Inventario / helpers
# ─────────────────────────────────────────

def test_starts_empty(om):
    assert om.count_open() == 0
    assert om.open_symbols() == []
    assert om.has_position("ETH/USDT") is False


# ─────────────────────────────────────────
#  Candado de exposición global
# ─────────────────────────────────────────

class TestGlobalLock:

    def test_opens_until_max(self, om):
        # Con 0 abiertas, se puede abrir.
        assert om.should_open(_decision(), _snapshot(), "BTC/USDT") is True
        om.open_position(_snapshot(price=100.0), _decision(), "BTC/USDT")
        # Con 1 abierta (< 2), se puede abrir otra moneda.
        assert om.should_open(_decision(), _snapshot(), "ETH/USDT") is True
        om.open_position(_snapshot(price=50.0), _decision(), "ETH/USDT")
        assert om.count_open() == 2

    def test_lock_blocks_when_full(self, om):
        om.open_position(_snapshot(price=100.0), _decision(), "BTC/USDT")
        om.open_position(_snapshot(price=50.0), _decision(), "ETH/USDT")
        # Candado: ya hay 2/2 → ignora señal en una tercera moneda.
        assert om.should_open(_decision(), _snapshot(), "SOL/USDT") is False

    def test_one_position_per_symbol(self, om):
        om.open_position(_snapshot(price=100.0), _decision(), "BTC/USDT")
        # Mismo símbolo con posición abierta → no reabre.
        assert om.should_open(_decision(), _snapshot(), "BTC/USDT") is False

    def test_lock_releases_after_close(self, om):
        om.open_position(_snapshot(price=100.0), _decision(), "BTC/USDT")
        om.open_position(_snapshot(price=50.0), _decision(), "ETH/USDT")
        assert om.should_open(_decision(), _snapshot(), "SOL/USDT") is False
        # Cerramos una → se libera un cupo del candado.
        om.close_position(_snapshot(price=101.0), "manual", "BTC/USDT")
        assert om.count_open() == 1
        assert om.should_open(_decision(), _snapshot(), "SOL/USDT") is True


# ─────────────────────────────────────────
#  Independencia por símbolo
# ─────────────────────────────────────────

class TestIndependence:

    def test_open_close_dont_mix(self, om):
        om.open_position(_snapshot(price=100.0), _decision("LONG"), "BTC/USDT")
        om.open_position(_snapshot(price=50.0), _decision("SHORT"), "ETH/USDT")
        btc = om.get_open_position("BTC/USDT")
        eth = om.get_open_position("ETH/USDT")
        assert btc.direction == "LONG" and btc.symbol == "BTC/USDT"
        assert eth.direction == "SHORT" and eth.symbol == "ETH/USDT"
        assert btc.entry_price == 100.0 and eth.entry_price == 50.0
        # Cerrar BTC no toca ETH.
        om.close_position(_snapshot(price=110.0), "tp", "BTC/USDT")
        assert om.has_position("BTC/USDT") is False
        assert om.has_position("ETH/USDT") is True

    def test_journal_records_symbol(self, om):
        om.open_position(_snapshot(price=100.0), _decision(), "SOL/USDT")
        rec = om.close_position(_snapshot(price=105.0), "tp", "SOL/USDT")
        assert rec["symbol"] == "SOL/USDT"


# ─────────────────────────────────────────
#  Pool de capital compartido
# ─────────────────────────────────────────

class TestSharedPool:

    def test_sizing_caps_per_trade_slot(self, om):
        # Flat: equity 210, tradeable 147. Risk-based size = 262.5 (2.5%/2%),
        # pero el cupo por trade = 147/2 = 73.5 → debe topear en 73.5 para que
        # entren las 2 posiciones del candado.
        size = om._calculate_position_size(_snapshot(price=100.0, atr_pct=1.0), _decision())
        assert size == pytest.approx(73.5)

    def test_two_positions_fit_within_reserve(self, om):
        # Las 2 posiciones del candado deben caber dentro del 70% tradeable.
        om.open_position(_snapshot(price=100.0), _decision(), "BTC/USDT")
        size2 = om._calculate_position_size(_snapshot(price=50.0, atr_pct=1.0), _decision())
        assert size2 == pytest.approx(73.5)
        om.open_position(_snapshot(price=50.0), _decision(), "ETH/USDT")
        assert om._committed_notional() == pytest.approx(147.0)
        assert om._equity_basis() == pytest.approx(210.0, abs=1.0)

    def test_reserve_never_exceeded(self, om):
        om.open_position(_snapshot(price=100.0), _decision(), "BTC/USDT")
        om.open_position(_snapshot(price=50.0), _decision(), "ETH/USDT")
        # La suma de notionals comprometidos no puede pasar el 70% del equity.
        assert om._committed_notional() <= om._equity_basis() * 0.70 + 1e-6


# ─────────────────────────────────────────
#  Migración de estado
# ─────────────────────────────────────────

class TestStateMigration:

    def test_migrates_legacy_single_position(self):
        legacy = {
            "capital": 210.0, "capital_initial": 210.0, "capital_peak": 210.0,
            "daily_capital_start": 210.0, "last_reset_date": "2026-06-01",
            "open_position": {"symbol": "BTC/USDT", "entry_price": 100.0,
                              "amount_usdt": 50.0, "direction": "LONG"},
            "total_trades": 3, "winning_trades": 2,
            "consecutive_failures": 0, "is_stopped": False,
        }
        migrated = OrderManager._migrate_state(dict(legacy))
        assert "open_position" not in migrated
        assert "BTC/USDT" in migrated["open_positions"]
        assert migrated["open_positions"]["BTC/USDT"]["entry_price"] == 100.0

    def test_migrates_legacy_flat(self):
        legacy = {"open_position": None, "capital": 210.0}
        migrated = OrderManager._migrate_state(dict(legacy))
        assert migrated["open_positions"] == {}

    def test_idempotent_on_new_format(self):
        new = {"open_positions": {"ETH/USDT": {"entry_price": 50.0}}, "capital": 210.0}
        migrated = OrderManager._migrate_state(dict(new))
        assert migrated["open_positions"] == {"ETH/USDT": {"entry_price": 50.0}}
