from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maits.api import create_app
from maits.config import ConfigError, Settings
from maits.ctrader.client import CTraderError

KEY, SECRET = "test-key", "hook-secret"


def make_settings(**overrides) -> Settings:
    base = dict(
        client_id="id", client_secret="secret", redirect_uri="http://localhost", env="demo", account_id=1,
        token_file=Path("unused.json"), max_lots=0.1, allowed_symbols=frozenset(), api_key=KEY,
        webhook_secret=SECRET, api_host="127.0.0.1", api_port=8000,
    )  # fmt: skip
    return Settings(**{**base, **overrides})


class FakeTrader:
    account_id = 1
    connected = True

    def __init__(self):
        self.calls = []

    async def close(self):
        pass

    async def account(self):
        return {"balance": 1000.0}

    async def place_market_order(self, *args, **kwargs):
        self.calls.append(("order", args, kwargs))
        if args[0] == "BAD":
            raise ValueError("unknown symbol 'BAD'")
        if args[0] == "DOWN":
            raise CTraderError("NOT_CONNECTED", "not connected")
        return {"status": "filled"}

    async def close_positions(self, symbol=None):
        self.calls.append(("close", symbol))
        return []


@pytest.fixture
def env():
    trader = FakeTrader()

    async def factory(_settings):
        return trader

    with TestClient(create_app(make_settings(), factory)) as client:
        yield client, trader


H = {"X-API-Key": KEY}


def test_refuses_to_start_without_api_key():
    with pytest.raises(ConfigError):
        create_app(make_settings(api_key=""))


def test_health_is_open_but_data_needs_key(env):
    client, _ = env
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/account").status_code == 401
    assert client.get("/account", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/account", headers=H).json() == {"balance": 1000.0}


def test_place_order(env):
    client, trader = env
    body = {"symbol": "EURUSD", "side": "buy", "lots": 0.01, "stop_loss_pips": 10}
    assert client.post("/orders", json=body).status_code == 401
    r = client.post("/orders", json=body, headers=H)
    assert r.status_code == 200 and r.json() == {"status": "filled"}
    assert trader.calls[0][1][:5] == ("EURUSD", "buy", 0.01, 10, None)


def test_order_validation_and_error_mapping(env):
    client, _ = env
    assert client.post("/orders", json={"symbol": "EURUSD", "side": "hold", "lots": 1}, headers=H).status_code == 422
    assert client.post("/orders", json={"symbol": "EURUSD", "side": "buy", "lots": -1}, headers=H).status_code == 422
    assert client.post("/orders", json={"symbol": "BAD", "side": "buy", "lots": 0.01}, headers=H).status_code == 400
    assert client.post("/orders", json={"symbol": "DOWN", "side": "buy", "lots": 0.01}, headers=H).status_code == 503


def test_webhook_secret_and_text_plain_body(env):
    client, trader = env
    alert = '{"secret": "%s", "action": "sell", "symbol": "EURUSD", "lots": 0.02}'
    # TradingView sends text/plain unless the alert is flagged as JSON
    r = client.post("/webhook/tradingview", content=alert % "nope", headers={"Content-Type": "text/plain"})
    assert r.status_code == 401 and not trader.calls
    r = client.post("/webhook/tradingview", content=alert % SECRET, headers={"Content-Type": "text/plain"})
    assert r.status_code == 200 and trader.calls[0][1][:3] == ("EURUSD", "sell", 0.02)


def test_webhook_close_and_missing_lots(env):
    client, trader = env
    r = client.post("/webhook/tradingview", json={"secret": SECRET, "action": "close", "symbol": "EURUSD"})
    assert r.status_code == 200 and trader.calls == [("close", "EURUSD")]
    r = client.post("/webhook/tradingview", json={"secret": SECRET, "action": "buy", "symbol": "EURUSD"})
    assert r.status_code == 400


def test_webhook_validation_error_does_not_echo_secret(env):
    client, _ = env
    r = client.post("/webhook/tradingview", json={"secret": SECRET, "action": "explode", "symbol": "X"})
    assert r.status_code == 422 and SECRET not in r.text


def test_webhook_disabled_without_secret():
    async def factory(_s):
        return FakeTrader()

    with TestClient(create_app(make_settings(webhook_secret=""), factory)) as client:
        assert client.post("/webhook/tradingview", json={"secret": "", "action": "close", "symbol": "X"}).status_code == 404
