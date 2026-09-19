import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import bot.server as server
from tests.conftest import make_settings


class FakeApp:
    def __init__(self, settings):
        from bot.store import Store
        self.settings = settings
        self.store = Store(settings.data_dir)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.handled = []

        class _Exec:
            def handle(inner, sig):
                self.handled.append(sig)
                return {"status": "narrated"}

        self.executor = _Exec()

    def startup(self):
        self.store.beat()

    def shutdown(self):
        self.pool.shutdown(wait=True)


@pytest.fixture
def app_and_client(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, enforce_tv_ips=True)
    fake = FakeApp(settings)
    monkeypatch.setattr(server, "App", lambda: fake)
    with TestClient(server.app) as http:
        yield fake, http


def payload(**kw):
    base = {"secret": "x" * 32, "event": "zone", "id": "s1", "symbol": "BTCUSDT.P", "side": "LONG", "price": 1.0}
    base.update(kw)
    return base


TV_IP = {"X-Forwarded-For": "52.89.214.238"}


def test_valid_webhook_is_accepted(app_and_client):
    fake, http = app_and_client
    r = http.post("/tv/webhook", json=payload(), headers=TV_IP)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    fake.pool.shutdown(wait=True)
    assert fake.handled[0].symbol == "BTC-USDT"


def test_wrong_secret_rejected(app_and_client):
    _, http = app_and_client
    assert http.post("/tv/webhook", json=payload(secret="nope"), headers=TV_IP).status_code == 401


def test_foreign_ip_rejected(app_and_client):
    _, http = app_and_client
    r = http.post("/tv/webhook", json=payload(), headers={"X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 403


def test_invalid_payload(app_and_client):
    _, http = app_and_client
    r = http.post("/tv/webhook", json=payload(event="entry"), headers=TV_IP)  # entry sin sl/tp
    assert r.status_code == 422


def test_health(app_and_client):
    _, http = app_and_client
    r = http.get("/tv/health")
    assert r.status_code == 200 and r.json()["ok"] is True
