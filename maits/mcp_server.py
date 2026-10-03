"""MCP server over stdio: lets an AI agent read the account and trade on a *demo* account.

Run with `maits mcp` (stdout carries the protocol; logs go to stderr). Every action goes through
`Trader`, so MAITS_MAX_LOTS, MAITS_ALLOWED_SYMBOLS and the CTRADER_ENV/account check apply as
everywhere else. On top of that, the trading tools refuse to run unless CTRADER_ENV=demo: an agent
must never be able to move real money, whatever its client's approval settings are.

The cTrader connection is opened on the first tool call, not at startup, so the server answers
the MCP handshake immediately and a failed login is reported as a tool error (and retried on the
next call) instead of killing the server.
"""
import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from maits.config import ConfigError, Settings
from maits.ctrader.auth import AuthError
from maits.ctrader.client import CTraderError
from maits.ctrader.trader import PERIOD_MINUTES, Trader

log = logging.getLogger(__name__)

T = TypeVar("T")
# Typed (not bare `dict`) so the SDK also sends structured content; lists are wrapped in an object
# because the SDK renders a list as one text block per item, i.e. nothing at all for an empty list.
Result = dict[str, Any]
Period = Literal[tuple(PERIOD_MINUTES)]  # type: ignore[valid-type]

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
TRADE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)

INSTRUCTIONS = """\
Access to a cTrader (Forex/CFD) account through maits. Units are human: lots, pips, prices, money in
the account currency. Only market orders exist; stop loss / take profit are distances in pips.
Trading tools work only when the server runs against a demo account; call `status` first to see the
environment and the safety limits (max lots per order, allowed symbols)."""


class _TraderSlot:
    """One lazily opened, shared Trader for the lifetime of the server."""

    def __init__(self, settings: Settings, factory: Callable[[Settings], Awaitable[Trader]]):
        self.settings = settings
        self._factory = factory
        self._trader: Trader | None = None
        self._lock = asyncio.Lock()

    @property
    def trader(self) -> Trader | None:
        return self._trader

    async def get(self) -> Trader:
        async with self._lock:
            if self._trader is None:
                self._trader = await self._factory(self.settings)
                log.info("MCP connected (%s account %s)", self.settings.env, self._trader.account_id)
            return self._trader

    async def close(self) -> None:
        if self._trader is not None:
            await self._trader.close()
            self._trader = None


def create_server(settings: Settings, trader_factory: Callable[[Settings], Awaitable[Trader]] = Trader.open) -> MCPServer:
    slot = _TraderSlot(settings, trader_factory)

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await slot.close()

    server = MCPServer("maits", instructions=INSTRUCTIONS, lifespan=lifespan)

    async def use(op: Callable[[Trader], Awaitable[T]], *, trading: bool = False) -> T:
        # The tool error text goes to the agent; anything else would be masked as an internal error.
        if trading and settings.is_live:
            raise ToolError("trading through MCP is disabled: CTRADER_ENV is 'live' (MCP may only trade demo accounts)")
        try:
            return await op(await slot.get())
        except (ConfigError, AuthError, CTraderError, ValueError) as exc:
            raise ToolError(str(exc)) from None

    # ---- read ----------------------------------------------------------------------------

    @server.tool(annotations=READ)
    async def status() -> Result:
        """Environment, connection state and the server-side safety limits. Does not connect."""
        trader = slot.trader
        return {
            "environment": settings.env,
            "trading_enabled": not settings.is_live,
            "connected": bool(trader and trader.connected),
            "account_id": trader.account_id if trader else settings.account_id,
            "max_lots": settings.max_lots,
            "allowed_symbols": sorted(settings.allowed_symbols) or None,
        }

    @server.tool(annotations=READ)
    async def account() -> Result:
        """Balance, equity, margin and currency of the configured account."""
        return await use(lambda t: t.account())

    @server.tool(annotations=READ)
    async def positions() -> Result:
        """Open positions with volume, entry price, SL/TP and unrealized P&L."""
        return {"positions": await use(lambda t: t.positions())}

    @server.tool(annotations=READ)
    async def orders() -> Result:
        """Pending (limit/stop) orders."""
        return {"orders": await use(lambda t: t.orders())}

    @server.tool(annotations=READ)
    async def symbols(contains: Annotated[str, Field(description="case-insensitive substring, e.g. 'EUR'")] = "") -> Result:
        """Tradable symbols, optionally filtered by a substring."""
        return {"symbols": await use(lambda t: t.symbols(contains))}

    @server.tool(annotations=READ)
    async def quote(symbol: Annotated[str, Field(description="e.g. 'EURUSD'")]) -> Result:
        """Current bid/ask for a symbol."""
        return await use(lambda t: t.quote(symbol))

    @server.tool(annotations=READ)
    async def bars(
        symbol: Annotated[str, Field(description="e.g. 'EURUSD'")],
        period: Period = "M15",
        count: Annotated[int, Field(ge=1, le=1000)] = 20,
    ) -> Result:
        """Most recent historical OHLC bars, oldest first."""
        return {"bars": await use(lambda t: t.bars(symbol, period, count))}

    # ---- trade (demo only) ---------------------------------------------------------------

    @server.tool(annotations=TRADE)
    async def place_market_order(
        symbol: Annotated[str, Field(description="e.g. 'EURUSD'")],
        side: Literal["buy", "sell"],
        lots: Annotated[float, Field(gt=0, description="size in lots, e.g. 0.01; capped by MAITS_MAX_LOTS")],
        stop_loss_pips: Annotated[float | None, Field(gt=0, description="stop loss distance in pips")] = None,
        take_profit_pips: Annotated[float | None, Field(gt=0, description="take profit distance in pips")] = None,
        comment: Annotated[str, Field(max_length=512)] = "mcp",
    ) -> Result:
        """Place a market order (demo accounts only). Returns the fill, or status 'submitted' if no fill was seen yet."""
        return await use(
            lambda t: t.place_market_order(symbol, side, lots, stop_loss_pips, take_profit_pips, comment), trading=True
        )

    @server.tool(annotations=TRADE)
    async def close_position(position_id: int) -> Result:
        """Close one position completely (demo accounts only)."""
        return await use(lambda t: t.close_position(position_id), trading=True)

    @server.tool(annotations=TRADE)
    async def close_positions(
        symbol: Annotated[str | None, Field(description="close only positions on this symbol")] = None,
        all: Annotated[bool, Field(description="must be true to close every open position")] = False,
    ) -> Result:
        """Close all positions on a symbol, or every position with all=true (demo accounts only)."""
        if not symbol and not all:
            raise ToolError("give a symbol, or all=true to close every open position")
        return {"closed": await use(lambda t: t.close_positions(symbol or None), trading=True)}

    return server


async def serve(settings: Settings) -> None:
    await create_server(settings).run_stdio_async()
