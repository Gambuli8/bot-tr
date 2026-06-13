"""
tests/test_portfolio.py
Cubre las funciones puras de notifications/portfolio.py y un smoke test del
render de /status (portafolio y single) sin red ni exchange real.
"""

import json
from pathlib import Path

import pytest

from notifications import portfolio as pf


# ───────── funciones puras ─────────

def test_dir_to_symbol():
    assert pf.dir_to_symbol("btc") == "BTC/USDT"
    assert pf.dir_to_symbol("avax") == "AVAX/USDT"


def test_discover_y_load(tmp_path):
    for name, cap in (("btc", 15.0), ("sol", 20.0)):
        d = tmp_path / name
        d.mkdir()
        (d / "state.json").write_text(json.dumps({"capital": cap, "total_trades": 1}))
    # un dir sin state.json no debe aparecer
    (tmp_path / "vacio").mkdir()

    bots = pf.load_portfolio(tmp_path)
    names = {b["name"] for b in bots}
    assert names == {"btc", "sol"}
    btc = next(b for b in bots if b["name"] == "btc")
    assert btc["symbol"] == "BTC/USDT"
    assert btc["state"]["capital"] == 15.0


def test_load_portfolio_dir_inexistente():
    assert pf.load_portfolio("/no/existe/nada") == []


def test_load_bot_state_json_invalido(tmp_path):
    f = tmp_path / "state.json"
    f.write_text("{ esto no es json")
    assert pf.load_bot_state(f) is None  # escritura concurrente → None, no crash


def test_position_pnl_long():
    pos = {"entry_price": 100.0, "amount_btc": 2.0, "direction": "LONG"}
    pnl, pct = pf.position_pnl(pos, 110.0)
    assert pnl == pytest.approx(20.0)
    assert pct == pytest.approx(10.0)


def test_position_pnl_short():
    pos = {"entry_price": 100.0, "amount_btc": 2.0, "direction": "SHORT"}
    pnl, pct = pf.position_pnl(pos, 90.0)
    assert pnl == pytest.approx(20.0)
    assert pct == pytest.approx(10.0)


def test_position_pnl_datos_invalidos():
    assert pf.position_pnl({}, 100.0) == (0.0, 0.0)
    assert pf.position_pnl({"entry_price": 100, "amount_btc": 1}, 0) == (0.0, 0.0)


# ───────── smoke test del render ─────────

class _FakeController:
    def __init__(self, om):
        self.order_manager = om
        self.exchange = None
        self.telegram = None
        self.is_paused = False


class _FakeOM:
    class _S:
        is_stopped = False
        open_positions = []
        open_position = None
    def __init__(self):
        self.state = self._S()
    def get_stats(self):
        return {"capital": 15.0, "total_return_pct": 0.0, "total_trades": 0,
                "win_rate_pct": 0.0, "max_drawdown_pct": 0.0, "is_stopped": False,
                "open_position": False}


def _make_listener(monkeypatch, portfolio_dir=""):
    from config.settings import Settings
    from notifications.telegram_listener import TelegramListener
    s = Settings(
        binance_api_key="k", binance_api_secret="x", anthropic_api_key="a",
        telegram_bot_token="t", telegram_chat_id="1",
    )
    s.portfolio_data_dir = portfolio_dir
    lst = TelegramListener(s, _FakeController(_FakeOM()))
    # Evitar red: precios y balance mockeados.
    monkeypatch.setattr(lst, "_live_price", lambda symbol=None: 62750.0)
    monkeypatch.setattr(lst, "_real_balance", lambda: 50.0)
    return lst


def test_render_single_status(monkeypatch):
    lst = _make_listener(monkeypatch)
    msg = lst._render_single_status()
    assert "BTC/USDT" in msg
    assert "$62,750.00" in msg     # precio en vivo
    assert "$50.00" in msg          # balance real
    assert "Sin posición abierta" in msg


def test_render_portfolio(monkeypatch, tmp_path):
    for name in ("btc", "sol", "avax"):
        d = tmp_path / name
        d.mkdir()
        (d / "state.json").write_text(json.dumps({
            "capital": 15.0, "total_trades": 0, "winning_trades": 0,
            "open_positions": [], "open_position": None,
        }))
    lst = _make_listener(monkeypatch, portfolio_dir=str(tmp_path))
    bots = pf.load_portfolio(tmp_path)
    msg = lst._render_portfolio(bots)
    assert "PORTAFOLIO" in msg
    assert "BTC" in msg and "SOL" in msg and "AVAX" in msg
    assert "Wallet real" in msg
