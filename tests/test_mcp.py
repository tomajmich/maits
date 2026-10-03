import json

from mcp import Client

from maits.ctrader.client import CTraderError
from maits.mcp_server import create_server
from tests.test_api import make_settings


class FakeTrader:
    account_id = 1
    connected = True

    def __init__(self):
        self.calls = []
        self.closed = False

    async def close(self):
        self.closed = True

    async def positions(self):
        return [{"position_id": 5, "symbol": "EURUSD"}]

    async def place_market_order(self, *args):
        self.calls.append(("order", args))
        if args[0] == "BAD":
            raise ValueError("BAD lots exceeds MAITS_MAX_LOTS=0.1")
        return {"status": "filled", "position_id": 555}

    async def close_positions(self, symbol=None):
        self.calls.append(("close", symbol))
        return []


def server(**settings):
    trader = FakeTrader()
    opened = []

    async def factory(s):
        opened.append(s)
        return trader

    return create_server(make_settings(**settings), trader_factory=factory), trader, opened


def text(result) -> str:
    return result.content[0].text


def data(result):
    assert not result.is_error, text(result)
    return json.loads(text(result))


async def test_order_goes_through_trader_and_connection_is_reused():
    srv, trader, opened = server()
    async with Client(srv) as client:
        res = await client.call_tool("place_market_order", {"symbol": "EURUSD", "side": "buy", "lots": 0.01, "stop_loss_pips": 15})
        await client.call_tool("positions", {})
    assert data(res) == {"status": "filled", "position_id": 555}
    assert trader.calls == [("order", ("EURUSD", "buy", 0.01, 15.0, None, "mcp"))]
    assert len(opened) == 1 and trader.closed


async def test_trading_refused_on_live_without_connecting():
    srv, trader, opened = server(env="live")
    async with Client(srv) as client:
        order = await client.call_tool("place_market_order", {"symbol": "EURUSD", "side": "buy", "lots": 0.01})
        close = await client.call_tool("close_positions", {"all": True})
        status = await client.call_tool("status", {})
    assert order.is_error and close.is_error and "live" in text(order)
    assert trader.calls == [] and opened == []
    assert data(status)["trading_enabled"] is False


async def test_close_positions_needs_symbol_or_all():
    srv, trader, _ = server()
    async with Client(srv) as client:
        bare = await client.call_tool("close_positions", {})
        everything = await client.call_tool("close_positions", {"all": True})
        one = await client.call_tool("close_positions", {"symbol": "EURUSD"})
    assert bare.is_error and not everything.is_error and not one.is_error
    assert trader.calls == [("close", None), ("close", "EURUSD")]


async def test_trader_errors_reach_the_agent_verbatim():
    srv, _, _ = server()
    async with Client(srv) as client:
        res = await client.call_tool("place_market_order", {"symbol": "BAD", "side": "buy", "lots": 0.01})
    assert res.is_error and text(res).endswith(": BAD lots exceeds MAITS_MAX_LOTS=0.1")


async def test_failed_connect_is_reported_and_retried():
    attempts = []

    async def flaky(settings):
        attempts.append(settings)
        if len(attempts) == 1:
            raise CTraderError("TIMEOUT", "no auth reply")
        return FakeTrader()

    async with Client(create_server(make_settings(), trader_factory=flaky)) as client:
        first = await client.call_tool("positions", {})
        second = await client.call_tool("positions", {})
    assert first.is_error and "TIMEOUT" in text(first)
    assert not second.is_error and len(attempts) == 2
