"""
tests/test_user_stream.py
Cubre:
  - El contrato request_reconcile / consume_reconcile del BotController.
  - La lógica de _handle_orders del UserStreamWatcher (sin WebSocket real):
    sólo los fills/cierres disparan un reconcile; las órdenes abiertas no.
"""

import threading
import time

import pytest

from core import bot_controller as bc_module
from core.bot_controller import BotController
from core.user_stream import UserStreamWatcher


@pytest.fixture(autouse=True)
def _temp_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bc_module, "STATE_FILE", tmp_path / "controller_state.json")
    yield


# ───────── BotController.request_reconcile / consume_reconcile ─────────

def test_reconcile_one_shot():
    c = BotController()
    assert c.consume_reconcile() is False
    c.request_reconcile()
    assert c.consume_reconcile() is True
    assert c.consume_reconcile() is False  # ya consumido


def test_request_reconcile_despierta_event():
    c = BotController()
    t0 = time.time()
    threading.Timer(0.05, c.request_reconcile).start()
    woken = c.wait_for_next_cycle(timeout=2.0)
    elapsed = time.time() - t0
    assert woken is True
    assert elapsed < 0.5


# ───────── UserStreamWatcher._handle_orders ─────────

class _FakeController:
    """Controller mínimo que sólo cuenta los pedidos de reconcile."""
    def __init__(self):
        self.reconcile_calls = 0

    def request_reconcile(self):
        self.reconcile_calls += 1


class _FakeSettings:
    symbol = "BTC/USDT"
    binance_api_key = "k"
    binance_api_secret = "s"
    binance_testnet = True


def _make_watcher():
    ctrl = _FakeController()
    w = UserStreamWatcher(_FakeSettings(), ctrl)
    return w, ctrl


@pytest.mark.parametrize("status", ["closed", "filled", "canceled", "cancelled", "FILLED"])
def test_handle_orders_dispara_reconcile_en_fill(status):
    w, ctrl = _make_watcher()
    w._handle_orders([{"clientOrderId": "bot_x_sl", "status": status}])
    assert ctrl.reconcile_calls == 1


def test_handle_orders_ignora_ordenes_abiertas():
    w, ctrl = _make_watcher()
    w._handle_orders([{"clientOrderId": "bot_x", "status": "open"}])
    w._handle_orders([{"clientOrderId": "bot_x", "status": "new"}])
    assert ctrl.reconcile_calls == 0


def test_handle_orders_un_solo_reconcile_por_tanda():
    """Varios fills en la misma tanda → un solo reconcile (barre todo)."""
    w, ctrl = _make_watcher()
    w._handle_orders([
        {"clientOrderId": "bot_x_sl", "status": "filled"},
        {"clientOrderId": "bot_y_tp", "status": "filled"},
    ])
    assert ctrl.reconcile_calls == 1


def test_handle_orders_lista_vacia_no_hace_nada():
    w, ctrl = _make_watcher()
    w._handle_orders([])
    w._handle_orders(None)
    assert ctrl.reconcile_calls == 0


def test_handle_orders_no_crashea_si_request_reconcile_falla():
    """Si el controller tira excepción, el watcher la traga (fail-safe)."""
    class _BoomController:
        def request_reconcile(self):
            raise RuntimeError("boom")

    w = UserStreamWatcher(_FakeSettings(), _BoomController())
    # No debe propagar
    w._handle_orders([{"clientOrderId": "bot_x_sl", "status": "filled"}])
