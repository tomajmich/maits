"""Trader order logic against a scripted fake of the cTrader connection (real protobuf messages)."""
import asyncio

import pytest
from ctrader_open_api.messages import OpenApiMessages_pb2 as oa
from ctrader_open_api.messages import OpenApiModelMessages_pb2 as model

from maits.ctrader.client import CTraderError
from maits.ctrader.trader import Trader
from tests.test_api import make_settings

EXEC = model.ProtoOAExecutionType


class FakeClient:
    """Answers requests like the server would; `fill_mode` scripts what happens to a new order."""

    def __init__(self, fill_mode="fill"):
        self.fill_mode = fill_mode
        self.listeners = []
        self.sent = []
        self.is_ready = True

    def subscribe(self, cb):
        self.listeners.append(cb)

    def unsubscribe(self, cb):
        self.listeners.remove(cb)

    def emit(self, event):
        for cb in list(self.listeners):
            cb(event)

    async def stop(self):
        pass

    async def request(self, msg, *, instant=False, timeout=10):
        self.sent.append(msg)
        if isinstance(msg, oa.ProtoOASymbolsListReq):
            return oa.ProtoOASymbolsListRes(symbol=[model.ProtoOALightSymbol(symbolId=1, symbolName="EURUSD")])
        if isinstance(msg, oa.ProtoOASymbolByIdReq):
            d = model.ProtoOASymbol(symbolId=1, digits=5, pipPosition=4, lotSize=10_000_000,
                                    stepVolume=100_000, minVolume=100_000, maxVolume=10**11)  # fmt: skip
            return oa.ProtoOASymbolByIdRes(symbol=[d])
        if isinstance(msg, oa.ProtoOANewOrderReq):
            order = model.ProtoOAOrder(orderId=77, clientOrderId=msg.clientOrderId)
            accepted = oa.ProtoOAExecutionEvent(executionType=EXEC.Value("ORDER_ACCEPTED"), order=order)
            self.emit(accepted)
            if self.fill_mode == "timeout":
                raise CTraderError("TIMEOUT", "no response")
            if self.fill_mode == "error":
                raise CTraderError("MARKET_CLOSED", "market is closed")
            if self.fill_mode == "reject":
                self.emit(oa.ProtoOAExecutionEvent(executionType=EXEC.Value("ORDER_REJECTED"), order=order, errorCode="NO_LIQUIDITY"))
            elif self.fill_mode == "fill":
                self.emit(oa.ProtoOAExecutionEvent(
                    executionType=EXEC.Value("ORDER_FILLED"), order=order,
                    position=model.ProtoOAPosition(positionId=555),
                    deal=model.ProtoOADeal(filledVolume=msg.volume, executionPrice=1.08123),
                ))  # fmt: skip
            return accepted
        raise AssertionError(f"unexpected request {type(msg).__name__}")


def trader(fill_mode="fill", **settings):
    client = FakeClient(fill_mode)
    return Trader(make_settings(**settings), client, account_id=1), client


async def test_market_order_fill_and_request_shape():
    t, client = trader()
    result = await t.place_market_order("eur/usd", "BUY", 0.02, stop_loss_pips=10, take_profit_pips=20)
    assert result["status"] == "filled"
    assert result["position_id"] == 555 and result["price"] == 1.08123 and result["lots"] == 0.02
    req = next(m for m in client.sent if isinstance(m, oa.ProtoOANewOrderReq))
    assert req.volume == 200_000 and req.ctidTraderAccountId == 1
    assert req.orderType == model.ProtoOAOrderType.Value("MARKET") and req.tradeSide == model.ProtoOATradeSide.Value("BUY")
    assert req.relativeStopLoss == 100 and req.relativeTakeProfit == 200  # 10 / 20 pips on a 5-digit pair
    assert not req.HasField("stopLoss") and not req.HasField("takeProfit")  # absolute SL/TP invalid for market
    assert not client.listeners  # watcher was cleaned up


async def test_rejected_order_raises():
    t, _ = trader("reject")
    with pytest.raises(CTraderError) as exc:
        await t.place_market_order("EURUSD", "sell", 0.01)
    assert exc.value.code == "NO_LIQUIDITY"


async def test_error_reply_propagates_and_cleans_up():
    t, client = trader("error")
    with pytest.raises(CTraderError, match="MARKET_CLOSED"):
        await t.place_market_order("EURUSD", "buy", 0.01)
    assert not client.listeners


async def test_timeout_is_reported_as_unknown_outcome():
    t, _ = trader("timeout")
    with pytest.raises(CTraderError) as exc:
        await t.place_market_order("EURUSD", "buy", 0.01)
    assert exc.value.code == "UNKNOWN_OUTCOME" and "check" in exc.value.description


async def test_accepted_but_no_fill_yet():
    t, _ = trader("accept-only")
    result = await t.place_market_order("EURUSD", "buy", 0.01, wait_seconds=0.05)
    assert result["status"] == "submitted"


async def test_safety_limits_block_before_anything_is_sent():
    t, client = trader(max_lots=0.1)
    with pytest.raises(ValueError, match="MAITS_MAX_LOTS"):
        await t.place_market_order("EURUSD", "buy", 0.5)
    t2, client2 = trader(allowed_symbols=frozenset({"GBPUSD"}))
    with pytest.raises(ValueError, match="ALLOWED_SYMBOLS"):
        await t2.place_market_order("EURUSD", "buy", 0.01)
    with pytest.raises(ValueError, match="unknown symbol"):
        await t.place_market_order("NOPE", "buy", 0.01)
    with pytest.raises(ValueError, match="side"):
        await t.place_market_order("EURUSD", "hold", 0.01)
    assert not any(isinstance(m, oa.ProtoOANewOrderReq) for m in client.sent + client2.sent)
