"""
tests/test_health.py
Heartbeat local + dead-man's switch externo (health.py).
"""

import time

import pytest

import health


@pytest.fixture
def hb_file(tmp_path, monkeypatch):
    f = tmp_path / "heartbeat"
    monkeypatch.setattr(health, "HEARTBEAT_FILE", f)
    return f


def test_beat_writes_epoch(hb_file):
    health.beat()
    assert hb_file.exists()
    val = int(hb_file.read_text().strip())
    assert abs(val - int(time.time())) <= 2


def test_beat_explicit_ts(hb_file):
    health.beat(ts=1_000_000)
    assert hb_file.read_text().strip() == "1000000"


def test_seconds_since_beat(hb_file):
    health.beat(ts=time.time() - 42)
    age = health.seconds_since_beat()
    assert age is not None and 40 <= age <= 45


def test_seconds_since_beat_missing(hb_file):
    assert health.seconds_since_beat() is None   # archivo no existe todavía


def test_beat_never_raises(monkeypatch):
    # Si el path es inescribible, beat() loguea pero NO raisea.
    monkeypatch.setattr(health, "HEARTBEAT_FILE", health.Path("/no/such/dir/heartbeat"))
    health.beat()   # no debe tirar excepción


def test_ping_noop_without_url(monkeypatch):
    # Sin HEALTHCHECK_URL configurado, ping() es no-op y devuelve False.
    monkeypatch.setattr(health, "_HC_URL", "")
    assert health.enabled() is False
    assert health.ping() is False


def test_ping_hits_url_when_configured(monkeypatch):
    called = {}

    def fake_get(url, timeout=None):
        called["url"] = url
        return None

    monkeypatch.setattr(health, "_HC_URL", "https://hc-ping.com/uuid")
    monkeypatch.setattr(health.requests, "get", fake_get)
    assert health.ping("/start") is True
    assert called["url"] == "https://hc-ping.com/uuid/start"


def test_ping_swallows_errors(monkeypatch):
    def boom(url, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(health, "_HC_URL", "https://hc-ping.com/uuid")
    monkeypatch.setattr(health.requests, "get", boom)
    assert health.ping() is False   # no raisea
