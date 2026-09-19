"""Modo carry contra un exchange simulado (spot + perpetuo aislado + transferencias + funding)."""

import itertools
import time
from dataclasses import replace

import pytest

from bot.bingx import BingXError, ContractSpec, Position
from bot.carry import CarryManager
from bot.narrator import Narrator
from bot.store import Store
from tests.conftest import make_settings

PERP = {
    "BTC-USDT": ContractSpec("BTC-USDT", 0.0001, 2, 4, 1, 0.0005, 0.0002, True),
    "DOGE-USDT": ContractSpec("DOGE-USDT", 25, 2, 0, 5, 0.0005, 0.0002, True),
}
SPOT = {
    "BTC-USDT": {"symbol": "BTC-USDT", "stepSize": 0.000001, "minQty": 0.0001, "minNotional": 0.5},
    "DOGE-USDT": {"symbol": "DOGE-USDT", "stepSize": 0.1, "minQty": 1, "minNotional": 0.5},
}
FEE = 0.001


class FakeExchange:
    def __init__(self, prices):
        self.prices = dict(prices)
        self.wallet = 10_000.0                 # USDT libre en futuros
        self.spot = {"VST": 0.0}
        self.pos: dict[str, dict] = {}         # symbol → {qty, entry, margin}
        self.leverage: dict[str, int] = {}
        self.income_rows: list[dict] = []
        self.calls: list[tuple] = []
        self.fail_short = False
        self._t = itertools.count(int(time.time() * 1000) + 60_000)   # siempre posterior al cursor del bot

    # ── helpers de simulación
    def _income(self, symbol, kind, amount):
        self.income_rows.append({"symbol": symbol, "incomeType": kind, "income": str(amount), "time": next(self._t)})

    def pay_funding(self, symbol, rate):
        p = self.pos[symbol]
        amount = rate * p["qty"] * self.prices[symbol]
        self.wallet += amount
        self._income(symbol, "FUNDING_FEE", amount)

    # ── API usada por CarryManager
    def contract(self, symbol):
        return PERP[symbol]

    def spot_symbols(self):
        return SPOT

    def price(self, symbol):
        return self.prices[symbol]

    def positions(self, symbol=None):
        out = []
        for s, p in self.pos.items():
            if p["qty"] <= 0 or symbol not in (None, s):
                continue
            px = self.prices[s]
            upnl = p["qty"] * (p["entry"] - px)
            liq = (p["margin"] + p["qty"] * p["entry"]) / (p["qty"] * 1.005)
            out.append(Position(s, "SHORT", p["qty"], p["entry"], px, upnl, self.leverage.get(s, 1), liq,
                                p["margin"], "1"))
        return out

    def spot_balances(self):
        return dict(self.spot)

    def transfer(self, asset, amount, frm, to):
        if asset != "VST":
            raise BingXError(80001, "asset not supported", "/transfer")
        self.calls.append(("transfer", amount, frm, to))
        if frm == "USDTMPerp":
            assert self.wallet >= amount - 1e-9, "sin saldo en futuros"
            self.wallet -= amount
            self.spot["VST"] = self.spot.get("VST", 0) + amount
        else:
            assert self.spot["VST"] >= amount - 1e-9, "sin saldo en spot"
            self.spot["VST"] -= amount
            self.wallet += amount

    def spot_market_order(self, symbol, side, qty):
        q, px, coin = float(qty), self.prices[symbol], symbol.split("-")[0]
        self.calls.append(("spot", symbol, side, q))
        if side == "BUY":
            cost = q * px
            assert self.spot["VST"] >= cost - 1e-9, "sin saldo en spot para comprar"
            self.spot["VST"] -= cost
            self.spot[coin] = self.spot.get(coin, 0) + q * (1 - FEE)
        else:
            assert self.spot.get(coin, 0) >= q - 1e-9, "sin monedas para vender"
            self.spot[coin] -= q
            self.spot["VST"] += q * px * (1 - FEE)
        return {"executedQty": str(q), "cummulativeQuoteQty": str(q * px)}

    def set_margin_type(self, symbol, margin_type):
        self.calls.append(("margin_type", symbol, margin_type))

    def set_leverage(self, symbol, leverage, side="BOTH"):
        self.leverage[symbol] = leverage

    def place_market(self, symbol, side, qty, reduce_only=False):
        q, px = float(qty), self.prices[symbol]
        self.calls.append(("perp", symbol, side, q, reduce_only))
        p = self.pos.setdefault(symbol, {"qty": 0.0, "entry": px, "margin": 0.0})
        fee = q * px * 0.0005
        self.wallet -= fee
        self._income(symbol, "TRADING_FEE", -fee)
        if side == "SELL" and not reduce_only:
            if self.fail_short:
                raise BingXError(101, "rejected", "/order")
            margin = q * px / self.leverage.get(symbol, 1)
            self.wallet -= margin
            p["entry"] = (p["entry"] * p["qty"] + px * q) / (p["qty"] + q) if p["qty"] else px
            p["qty"] += q
            p["margin"] += margin
        else:
            q = min(q, p["qty"])
            released = p["margin"] * q / p["qty"]
            realized = q * (p["entry"] - px)
            p["qty"] -= q
            p["margin"] -= released
            self.wallet += released + realized
            self._income(symbol, "REALIZED_PNL", realized)
        return {"orderId": 1}

    def add_isolated_margin(self, symbol, amount):
        assert self.wallet >= amount - 1e-9
        self.wallet -= amount
        self.pos[symbol]["margin"] += amount

    def income(self, symbol, start_ms, end_ms=None):
        return [r for r in self.income_rows if r["symbol"] == symbol and r["time"] >= start_ms]


def manager(tmp_path, exchange, symbols=("BTC-USDT",), capital=200.0, **kw):
    settings = make_settings(tmp_path, carry_enabled=True, carry_symbols=list(symbols), carry_capital_usdt=capital,
                             carry_leverage=2.0, carry_interval_s=300, symbols=["BTC-USDT", "ETH-USDT", "DOGE-USDT"])
    settings = replace(settings, **kw)
    notes = []
    m = CarryManager(settings, exchange, Store(settings.data_dir), Narrator(settings.mode_label, settings.timezone),
                     notes.append, sleep=lambda s: None)
    return m, notes


def hedged(ex, symbol):
    coin = symbol.split("-")[0]
    short = ex.pos[symbol]["qty"]
    return abs(ex.spot.get(coin, 0) - short) <= float(PERP[symbol].qty_precision and 10 ** -PERP[symbol].qty_precision or 1)


def test_build_opens_equal_spot_and_short(tmp_path):
    ex = FakeExchange({"BTC-USDT": 76000.0})
    m, notes = manager(tmp_path, ex, capital=200)
    m.run_once()
    pair = m.pairs["BTC-USDT"]
    assert pair["status"] == "active"
    assert ex.pos["BTC-USDT"]["qty"] == pytest.approx(0.0017)          # 200×2/3 ≈ 133 USDT
    assert ex.spot["BTC"] >= ex.pos["BTC-USDT"]["qty"]                 # la comisión en BTC no descalza
    assert ex.spot["BTC"] - ex.pos["BTC-USDT"]["qty"] < 0.00001
    assert ex.leverage["BTC-USDT"] == 2
    assert ("margin_type", "BTC-USDT", "ISOLATED") in ex.calls
    assert "Carry armado" in notes[-1]


def test_directional_config_excludes_carry_symbols(tmp_path):
    ex = FakeExchange({"BTC-USDT": 76000.0})
    m, _ = manager(tmp_path, ex)
    assert "BTC-USDT" not in m.s.directional_symbols and "ETH-USDT" in m.s.directional_symbols


def test_build_waits_if_directional_position_open(tmp_path):
    ex = FakeExchange({"BTC-USDT": 76000.0})
    ex.leverage["BTC-USDT"] = 4
    ex.place_market("BTC-USDT", "SELL", "0.0001")                      # posición previa en el par
    m, notes = manager(tmp_path, ex)
    m.run_once()
    assert m.pairs["BTC-USDT"]["status"] == "pending"
    assert not any(c[0] == "spot" for c in ex.calls)
    assert "cuando se cierre" in notes[-1]


def test_insufficient_capital_reports_minimum(tmp_path):
    ex = FakeExchange({"DOGE-USDT": 0.25})
    m, notes = manager(tmp_path, ex, symbols=("DOGE-USDT",), capital=5)
    m.run_once()
    assert m.pairs["DOGE-USDT"]["status"] == "error"
    assert not any(c[0] in ("spot", "perp") for c in ex.calls)
    assert "mínimo" in notes[-1]


def test_failed_short_sells_spot_back(tmp_path):
    ex = FakeExchange({"BTC-USDT": 76000.0})
    ex.fail_short = True
    m, notes = manager(tmp_path, ex)
    m.run_once()
    assert ex.spot.get("BTC", 0) < 0.00001                             # no queda BTC sin cubrir
    assert m.pairs["BTC-USDT"]["status"] == "error"
    assert "vendí el spot" in notes[-1]


def test_funding_is_booked(tmp_path):
    ex = FakeExchange({"BTC-USDT": 76000.0})
    m, _ = manager(tmp_path, ex)
    m.run_once()
    for _ in range(3):
        ex.pay_funding("BTC-USDT", 0.0001)
    m.run_once()
    pair = m.pairs["BTC-USDT"]
    assert pair["funding"] == pytest.approx(3 * 0.0001 * 0.0017 * 76000)


def test_price_rise_triggers_protection_and_keeps_hedge(tmp_path):
    ex = FakeExchange({"BTC-USDT": 76000.0})
    m, notes = manager(tmp_path, ex)
    m.run_once()
    liq_before = ex.positions("BTC-USDT")[0].liquidation_price
    ex.prices["BTC-USDT"] = 76000 * 1.2                                 # +20 % (umbral ±15 % con ×2)
    m.run_once()
    pos = ex.positions("BTC-USDT")[0]
    assert "protección" in notes[-1]
    assert pos.liquidation_price > liq_before                          # la liquidación se alejó
    assert abs(ex.spot["BTC"] - pos.qty) < 0.0001                       # sigue cubierto
    assert m.pairs["BTC-USDT"]["anchor"] == pytest.approx(76000 * 1.2)


def test_reinvest_reset_grows_position_with_accumulated_funding(tmp_path):
    ex = FakeExchange({"BTC-USDT": 76000.0})
    m, notes = manager(tmp_path, ex, carry_reinvest_pct=0.03)
    m.run_once()
    qty_before = ex.pos["BTC-USDT"]["qty"]
    for _ in range(300):                                               # funding suficiente para +1 paso de BTC
        ex.pay_funding("BTC-USDT", 0.0005)
    m.pairs["BTC-USDT"]["last_reset"] = 0                             # pasaron más de 7 días
    m.run_once()
    assert "reinversión" in notes[-1]
    assert ex.pos["BTC-USDT"]["qty"] > qty_before
    assert abs(ex.spot["BTC"] - ex.pos["BTC-USDT"]["qty"]) < 0.0001


def test_no_reset_when_gain_is_below_one_contract_step(tmp_path):
    # Con BTC a 760.000 el paso mínimo (0,0001 BTC) vale 76 USDT: unos pocos USDT de funding no alcanzan.
    ex = FakeExchange({"BTC-USDT": 760000.0})
    m, notes = manager(tmp_path, ex, carry_reinvest_pct=0.0, carry_min_trade_usdt=1.0)
    m.run_once()
    for _ in range(100):
        ex.pay_funding("BTC-USDT", 0.0005)                             # ~3,8 USDT
    m.pairs["BTC-USDT"]["last_reset"] = 0
    perp_calls = len([c for c in ex.calls if c[0] == "perp"])
    m.run_once()
    assert len([c for c in ex.calls if c[0] == "perp"]) == perp_calls  # no cierra/reabre para nada
    assert not any("reinversión" in n for n in notes)


def test_hedge_mismatch_is_fixed(tmp_path):
    ex = FakeExchange({"BTC-USDT": 76000.0})
    m, notes = manager(tmp_path, ex)
    m.run_once()
    ex.place_market("BTC-USDT", "BUY", "0.0005", reduce_only=True)    # alguien achicó el short
    m.run_once()
    assert abs(ex.spot["BTC"] - ex.pos["BTC-USDT"]["qty"]) < 0.0001
    assert any("cobertura" in n for n in notes)


def test_unwind_closes_everything(tmp_path):
    ex = FakeExchange({"BTC-USDT": 76000.0})
    m, _ = manager(tmp_path, ex)
    m.run_once()
    msg = m.unwind("BTC-USDT")
    assert ex.pos["BTC-USDT"]["qty"] == 0
    assert ex.spot["BTC"] < 0.000002
    assert ex.spot["VST"] < 1
    assert m.pairs["BTC-USDT"]["status"] == "closed" and "cerrado" in msg


def test_status_text(tmp_path):
    ex = FakeExchange({"BTC-USDT": 76000.0})
    m, _ = manager(tmp_path, ex)
    m.run_once()
    m.run_once()
    text = m.status_text()
    assert "BTC" in text and "Spot" in text and "Funding" in text


# ───────── fallas con devolución de fondos ─────────

def test_failed_spot_buy_refunds_and_backs_off(tmp_path):
    ex = FakeExchange({"BTC-USDT": 76000.0})

    def broken_order(symbol, side, qty):
        raise BingXError(100202, "balance not enough", "/spot/order")

    ex.spot_market_order = broken_order
    m, notes = manager(tmp_path, ex)
    wallet_before = ex.wallet
    m.run_once()
    assert ex.spot["VST"] < 0.01                                       # nada quedó varado en spot
    assert ex.wallet == pytest.approx(wallet_before)
    pair = m.pairs["BTC-USDT"]
    assert pair["status"] == "error" and pair["retry_after"] > time.time() * 1000
    transfers = len([c for c in ex.calls if c[0] == "transfer"])
    m.run_once()                                                       # siguiente ciclo: no reintenta todavía
    assert len([c for c in ex.calls if c[0] == "transfer"]) == transfers
    assert "devolví" in notes[-1]


# ───────── modo simulado (precios/funding reales, órdenes simuladas) ─────────

class FakeMarket:
    def __init__(self, px):
        self.px = px
        self.funding_rows = []

    def contract(self, symbol):
        return PERP[symbol]

    def contracts(self, force=False):
        return PERP

    def price(self, symbol):
        return self.px

    def spot_symbols(self):
        return SPOT

    def _request(self, method, path, params=None, signed=True):
        assert path == "/openApi/swap/v2/quote/fundingRate" and signed is False
        return [r for r in self.funding_rows if int(r["fundingTime"]) >= params["startTime"]]


def test_paper_carry_builds_and_collects_real_funding(tmp_path):
    from bot.carry_paper import PaperCarryExchange
    market = FakeMarket(76000.0)
    settings_store = Store(tmp_path / "paper")
    paper = PaperCarryExchange(market, settings_store, starting_futures_usdt=200)
    settings = make_settings(tmp_path, carry_enabled=True, carry_symbols=["BTC-USDT"], carry_capital_usdt=200,
                             carry_leverage=2.0, carry_paper="true")
    notes = []
    m = CarryManager(settings, paper, settings_store, Narrator(settings.mode_label, settings.timezone),
                     notes.append, sleep=lambda s: None)
    m.run_once()
    assert m.pairs["BTC-USDT"]["status"] == "active"
    assert "SIMULADO" in notes[-1]
    qty = paper.positions("BTC-USDT")[0].qty
    assert paper.spot_balances()["BTC"] >= qty

    now = int(time.time() * 1000)
    market.funding_rows = [{"fundingTime": now - 1000, "fundingRate": "0.0001"}]
    paper.st["funding_checked"]["BTC-USDT"] = now - 5000
    m.pairs["BTC-USDT"]["income_cursor"] = now - 5000                 # el par ya estaba armado antes del pago
    m.run_once()
    assert m.pairs["BTC-USDT"]["funding"] == pytest.approx(0.0001 * qty * 76000)
    assert settings_store.state["carry_paper"]["positions"]["BTC-USDT"]["qty"] == qty   # persistido
    assert "SIMULADO" in m.status_text()
