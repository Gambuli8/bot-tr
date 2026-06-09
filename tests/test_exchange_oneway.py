"""
tests/test_exchange_oneway.py
Guard de One-way mode en Futures (core/exchange.py::ensure_one_way_mode).

Crítico con plata real: el bot manda reduceOnly sin positionSide → solo válido
en One-way. Si la cuenta está en Hedge, el bot debe ABORTAR el arranque.
"""

import ccxt
import pytest

from core.exchange import ExchangeClient


class FakeEx:
    """Exchange mínimo para probar ensure_one_way_mode sin red."""

    def __init__(self, set_exc=None, hedged=False, fetch_exc=None):
        self._set_exc = set_exc
        self._hedged = hedged
        self._fetch_exc = fetch_exc
        self.set_calls = []

    def set_position_mode(self, hedged, symbol=None, params={}):
        self.set_calls.append(hedged)
        if self._set_exc:
            raise self._set_exc

    def fetch_position_mode(self, symbol=None, params={}):
        if self._fetch_exc:
            raise self._fetch_exc
        return {"hedged": self._hedged}


def _client(fake) -> ExchangeClient:
    # Bypass __init__ (que abriría conexión real) e inyectamos el fake.
    c = ExchangeClient.__new__(ExchangeClient)
    c.exchange = fake
    return c


def test_sets_one_way_when_account_is_hedge():
    fake = FakeEx(hedged=False)
    _client(fake).ensure_one_way_mode()
    assert fake.set_calls == [False]   # pidió one-way (hedged=False)


def test_already_one_way_is_ok():
    # Binance devuelve -4059 'No need to change' si ya está en ese modo.
    fake = FakeEx(set_exc=ccxt.ExchangeError("-4059 No need to change position side"))
    _client(fake).ensure_one_way_mode()   # no debe raisear


def test_hedge_with_open_positions_aborts():
    # Hedge + posiciones abiertas → Binance no deja cambiar → abortamos.
    fake = FakeEx(set_exc=ccxt.ExchangeError("-4068 cannot change with open position"))
    with pytest.raises(RuntimeError, match="One-way"):
        _client(fake).ensure_one_way_mode()


def test_verification_catches_residual_hedge():
    # set "pasa" pero la verificación dura ve que sigue en hedge → abortamos.
    fake = FakeEx(hedged=True)
    with pytest.raises(RuntimeError, match="HEDGE"):
        _client(fake).ensure_one_way_mode()


def test_fetch_not_supported_is_tolerated():
    # Si la versión de ccxt no soporta fetch_position_mode, confiamos en el set.
    fake = FakeEx(fetch_exc=ccxt.NotSupported("nope"))
    _client(fake).ensure_one_way_mode()   # no debe raisear
