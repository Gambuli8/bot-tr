"""
tests/test_inject_capital.py
Cubre la lógica de inject_capital: sube los 4 anclajes de capital y NO toca el
historial (trades, posiciones).
"""

import importlib.util
from pathlib import Path

import pytest

# Cargar el script como módulo (vive en scripts/, no es un paquete).
_SPEC = importlib.util.spec_from_file_location(
    "inject_capital",
    Path(__file__).resolve().parent.parent / "scripts" / "inject_capital.py",
)
inject_capital = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inject_capital)


def _base_state():
    return {
        "capital": 100.0,
        "capital_initial": 100.0,
        "capital_peak": 120.0,
        "daily_capital_start": 110.0,
        "last_reset_date": "2026-06-13",
        "open_positions": [{"client_order_id": "bot_x"}],
        "total_trades": 7,
        "winning_trades": 4,
    }


def test_inject_sube_los_cuatro_anclajes():
    new = inject_capital.inject(_base_state(), 50.0)
    assert new["capital"] == 150.0
    assert new["capital_initial"] == 150.0
    assert new["capital_peak"] == 170.0
    assert new["daily_capital_start"] == 160.0


def test_inject_preserva_historial():
    state = _base_state()
    new = inject_capital.inject(state, 50.0)
    assert new["total_trades"] == 7
    assert new["winning_trades"] == 4
    assert new["open_positions"] == [{"client_order_id": "bot_x"}]
    assert new["last_reset_date"] == "2026-06-13"


def test_inject_negativo_retira():
    new = inject_capital.inject(_base_state(), -30.0)
    assert new["capital"] == 70.0
    assert new["capital_initial"] == 70.0


def test_inject_no_muta_el_original():
    state = _base_state()
    inject_capital.inject(state, 50.0)
    assert state["capital"] == 100.0  # el original queda intacto


def test_inject_campo_faltante_se_trata_como_cero():
    state = {"capital": 100.0, "capital_initial": 100.0,
             "capital_peak": 100.0}  # falta daily_capital_start
    new = inject_capital.inject(state, 10.0)
    assert new["daily_capital_start"] == 10.0


def test_load_state_inexistente_sale(tmp_path):
    with pytest.raises(SystemExit):
        inject_capital.load_state(tmp_path / "no_existe.json")
