import pytest

from bot.sizing import build_plan
from tests.conftest import SPECS


def plan(symbol="BTC-USDT", direction="LONG", entry=75832.5, sl=74700.0, tp=78500.0, margin=2.0, **kw):
    params = dict(symbol=symbol, direction=direction, entry=entry, stop_loss=sl, take_profit=tp,
                  spec=SPECS[symbol], margin_usdt=margin, max_leverage=20, min_rr=1.5)
    params.update(kw)
    return build_plan(**params)


def test_btc_two_usdt_uses_minimum_leverage():
    p = plan()
    assert p.ok, p.reason
    assert p.leverage == 4           # 0.0001 BTC ≈ 7.58 USDT → 2 USDT × 4
    assert p.qty_str == "0.0001"
    assert p.notional == pytest.approx(7.58325)
    assert p.margin_used <= 2.0


def test_btc_one_usdt_needs_eight_x():
    p = plan(margin=1.0)
    assert p.ok, p.reason
    assert p.leverage == 8


def test_doge_quantity_rounded_to_integer_contracts():
    p = plan(symbol="DOGE-USDT", entry=0.25, sl=0.245, tp=0.265)
    assert p.ok, p.reason
    assert float(p.qty_str) >= 25 and "." not in p.qty_str


def test_short_requires_sl_above():
    bad = plan(direction="SHORT", sl=74000, tp=73000)
    assert not bad.ok and "en SHORT" in bad.reason
    good = plan(direction="SHORT", sl=76900, tp=73000)
    assert good.ok, good.reason


def test_rejects_wrong_side_levels_for_long():
    p = plan(sl=76000)
    assert not p.ok and "en LONG" in p.reason


def test_rejects_when_liquidation_before_stop():
    # 1 USDT → 8x → liquidación a ~12%; un SL a 15% nunca se ejecutaría.
    p = plan(margin=1.0, sl=75832.5 * 0.85, tp=75832.5 * 1.6)
    assert not p.ok and "liquida" in p.reason


def test_rejects_low_reward_to_risk():
    p = plan(sl=74700, tp=76300)
    assert not p.ok and "R:R" in p.reason


def test_rejects_when_leverage_cap_exceeded():
    p = plan(margin=0.3, max_leverage=20)  # necesitaría ~26x
    assert not p.ok and "haría falta" in p.reason


def test_prices_rounded_to_contract_precision():
    p = plan(sl=74700.04, tp=78500.06)
    assert p.sl_str == "74700.0" and p.tp_str == "78500.1"
