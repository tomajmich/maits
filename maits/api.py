"""HTTP API: read account data, place/close orders, and receive TradingView alerts.

Run with `maits serve`. All endpoints except /health and the webhook need the header
`X-API-Key: <MAITS_API_KEY>`. The webhook authenticates with `secret` in the JSON body instead,
because TradingView alerts cannot set headers.
"""
import hmac
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from maits.config import ConfigError, Settings
from maits.ctrader.client import CTraderError
from maits.ctrader.trader import Trader

log = logging.getLogger(__name__)


class OrderIn(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    lots: float = Field(gt=0)
    stop_loss_pips: float | None = Field(None, gt=0)
    take_profit_pips: float | None = Field(None, gt=0)
    comment: str = "api"


class TradingViewAlert(BaseModel):
    """Alert message template, e.g.
    {"secret": "...", "action": "{{strategy.order.action}}", "symbol": "{{ticker}}", "lots": 0.01}
    'close' closes every position on the symbol."""

    secret: str
    action: Literal["buy", "sell", "close"]
    symbol: str
    lots: float | None = Field(None, gt=0)
    sl_pips: float | None = Field(None, gt=0)
    tp_pips: float | None = Field(None, gt=0)
    comment: str = "tradingview"


def _secrets_match(given: str | None, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest((given or "").encode(), expected.encode())


def create_app(settings: Settings, trader_factory: Callable[[Settings], Awaitable[Trader]] = Trader.open) -> FastAPI:
    if not settings.api_key:
        raise ConfigError("MAITS_API_KEY must be set - the API will not run without authentication")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.trader = await trader_factory(settings)
        log.info("API ready (%s account %s)", settings.env, app.state.trader.account_id)
        try:
            yield
        finally:
            await app.state.trader.close()

    app = FastAPI(title="maits", lifespan=lifespan)

    def require_key(x_api_key: str | None = Header(None)) -> None:
        if not _secrets_match(x_api_key, settings.api_key):
            raise HTTPException(401, "invalid or missing X-API-Key")

    def get_trader(request: Request) -> Trader:
        return request.app.state.trader

    auth = [Depends(require_key)]

    @app.exception_handler(CTraderError)
    async def _ctrader_error(_request, exc: CTraderError):
        status = 503 if exc.code == "NOT_CONNECTED" else 502
        return JSONResponse({"error": exc.code, "detail": exc.description}, status_code=status)

    @app.exception_handler(ValueError)
    async def _value_error(_request, exc: ValueError):
        return JSONResponse({"error": "BAD_REQUEST", "detail": str(exc)}, status_code=400)

    # ---- read ----------------------------------------------------------------------------

    @app.get("/health")
    async def health(trader: Trader = Depends(get_trader)):
        return {"status": "ok" if trader.connected else "disconnected", "environment": settings.env}

    @app.get("/account", dependencies=auth)
    async def account(trader: Trader = Depends(get_trader)):
        return await trader.account()

    @app.get("/positions", dependencies=auth)
    async def positions(trader: Trader = Depends(get_trader)):
        return await trader.positions()

    @app.get("/orders", dependencies=auth)
    async def orders(trader: Trader = Depends(get_trader)):
        return await trader.orders()

    @app.get("/symbols", dependencies=auth)
    async def symbols(q: str = "", trader: Trader = Depends(get_trader)):
        return await trader.symbols(q)

    @app.get("/quote/{symbol}", dependencies=auth)
    async def quote(symbol: str, trader: Trader = Depends(get_trader)):
        return await trader.quote(symbol)

    @app.get("/bars/{symbol}", dependencies=auth)
    async def bars(symbol: str, period: str = "M15", count: int = Query(20, ge=1, le=1000), trader: Trader = Depends(get_trader)):
        return await trader.bars(symbol, period, count)

    # ---- trade ---------------------------------------------------------------------------

    @app.post("/orders", dependencies=auth)
    async def place_order(order: OrderIn, trader: Trader = Depends(get_trader)):
        return await trader.place_market_order(
            order.symbol, order.side, order.lots, order.stop_loss_pips, order.take_profit_pips, order.comment
        )

    @app.post("/positions/{position_id}/close", dependencies=auth)
    async def close_position(position_id: int, trader: Trader = Depends(get_trader)):
        return await trader.close_position(position_id)

    @app.post("/positions/close-all", dependencies=auth)
    async def close_all(symbol: str | None = None, trader: Trader = Depends(get_trader)):
        return await trader.close_positions(symbol)

    @app.post("/webhook/tradingview")
    async def tradingview(request: Request, trader: Trader = Depends(get_trader)):
        if not settings.webhook_secret:
            raise HTTPException(404)
        try:  # TradingView may send JSON as text/plain, so parse the raw body ourselves
            alert = TradingViewAlert.model_validate_json(await request.body())
        except ValidationError as exc:
            # never echo the input back: it contains the secret
            raise HTTPException(422, exc.errors(include_url=False, include_context=False, include_input=False))
        if not _secrets_match(alert.secret, settings.webhook_secret):
            raise HTTPException(401, "bad secret")
        log.info("TradingView alert: %s %s %s", alert.action, alert.symbol, alert.lots)
        if alert.action == "close":
            return await trader.close_positions(alert.symbol)
        if alert.lots is None:
            raise ValueError("'lots' is required for buy/sell alerts")
        # TradingView gives up on a webhook after ~3s, so don't wait long for the fill
        return await trader.place_market_order(
            alert.symbol, alert.action, alert.lots, alert.sl_pips, alert.tp_pips, alert.comment, wait_seconds=2.5
        )

    return app


async def serve(settings: Settings) -> None:
    config = uvicorn.Config(create_app(settings), host=settings.api_host, port=settings.api_port, log_level="info")
    await uvicorn.Server(config).serve()
