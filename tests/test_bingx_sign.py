import hashlib
import hmac
from urllib.parse import parse_qsl

from bot.bingx import sign_params


def test_signature_is_hmac_of_sorted_raw_params():
    params = {"symbol": "BTC-USDT", "side": "BUY", "quantity": "0.0001", "timestamp": 1700000000000}
    query, payload = sign_params(params, "secret")
    assert payload == "quantity=0.0001&side=BUY&symbol=BTC-USDT&timestamp=1700000000000"
    expected = hmac.new(b"secret", payload.encode(), hashlib.sha256).hexdigest()
    assert query.endswith("&signature=" + expected)


def test_json_params_signed_raw_and_sent_urlencoded():
    params = {"symbol": "BTC-USDT", "stopLoss": {"type": "STOP_MARKET", "stopPrice": 74950.0}}
    query, payload = sign_params(params, "s")
    assert 'stopLoss={"type":"STOP_MARKET","stopPrice":74950.0}' in payload
    assert "%22STOP_MARKET%22" in query  # comillas url-encodeadas en la URL
    decoded = dict(parse_qsl(query))
    assert decoded["stopLoss"] == '{"type":"STOP_MARKET","stopPrice":74950.0}'


def test_booleans_and_none():
    _, payload = sign_params({"reduceOnly": True, "price": None, "a": 1}, "s")
    assert payload == "a=1&reduceOnly=true"
