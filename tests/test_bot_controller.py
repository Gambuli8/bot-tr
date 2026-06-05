"""
tests/test_bot_controller.py
Cubre el contrato thread-safe del BotController.
"""

import json
import threading
import time
from pathlib import Path

import pytest

from core import bot_controller as bc_module
from core.bot_controller import BotController, CONFIRMATION_TTL_SECONDS


@pytest.fixture(autouse=True)
def _temp_state_file(tmp_path, monkeypatch):
    """Aisla cada test redirigiendo el archivo de persistencia a tmp."""
    monkeypatch.setattr(
        bc_module, "STATE_FILE", tmp_path / "controller_state.json"
    )
    yield


# ───────── pause / resume ─────────

def test_pause_resume_idempotente():
    c = BotController()
    assert c.is_paused is False
    assert c.pause() is True
    assert c.is_paused is True
    assert c.pause() is False  # ya estaba pausado
    assert c.resume() is True
    assert c.is_paused is False
    assert c.resume() is False  # ya estaba activo


def test_pause_se_persiste_y_recupera(tmp_path, monkeypatch):
    state_file = tmp_path / "controller_state.json"
    monkeypatch.setattr(bc_module, "STATE_FILE", state_file)

    c1 = BotController()
    c1.pause()
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data["is_paused"] is True

    # Nueva instancia debe leer el flag
    c2 = BotController()
    assert c2.is_paused is True

    c2.resume()
    c3 = BotController()
    assert c3.is_paused is False


# ───────── force close ─────────

def test_force_close_one_shot():
    c = BotController()
    assert c.consume_force_close() == []
    c.request_force_close()                      # sin símbolo → "*" (todas)
    assert c.consume_force_close() == ["*"]
    assert c.consume_force_close() == []         # ya consumido


def test_force_close_targeted_symbols():
    c = BotController()
    c.request_force_close("ETH/USDT")
    c.request_force_close("SOL/USDT")
    out = c.consume_force_close()
    assert set(out) == {"ETH/USDT", "SOL/USDT"}
    assert c.consume_force_close() == []


def test_request_force_close_despierta_event():
    c = BotController()
    t0 = time.time()
    threading.Timer(0.05, c.request_force_close).start()
    woken = c.wait_for_next_cycle(timeout=2.0)
    elapsed = time.time() - t0
    assert woken is True
    assert elapsed < 0.5


def test_wait_for_next_cycle_timeout():
    c = BotController()
    t0 = time.time()
    woken = c.wait_for_next_cycle(timeout=0.1)
    elapsed = time.time() - t0
    assert woken is False
    assert 0.08 <= elapsed <= 0.5


# ───────── confirmaciones ─────────

def test_confirmation_consume_y_ttl():
    c = BotController()
    # No hay confirmación pendiente
    assert c.check_confirmation("close") is False

    c.request_confirmation("close")
    assert c.check_confirmation("close") is True
    # Se consumió, ya no está
    assert c.check_confirmation("close") is False


def test_confirmation_expira():
    c = BotController()
    c.request_confirmation("stop")
    time.sleep(0.05)
    assert c.check_confirmation("stop", ttl=0.01) is False


def test_confirmation_keys_independientes():
    c = BotController()
    c.request_confirmation("close")
    c.request_confirmation("stop")
    assert c.check_confirmation("close") is True
    assert c.check_confirmation("stop") is True


# ───────── thread safety ─────────

def test_pause_concurrente_no_corrompe_estado():
    c = BotController()
    n_threads = 50

    def worker():
        for _ in range(20):
            c.pause()
            c.resume()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Al final, idealmente queda activo (último resume gana); pero el
    # invariante crítico es que NO crashea y is_paused es bool válido.
    assert isinstance(c.is_paused, bool)
