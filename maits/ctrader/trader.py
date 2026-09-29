"""Trading operations on one cTrader account, in human units (lots, pips, money).

Everything here returns plain dicts so the console app and the HTTP API can share it. The safety
limits (max lots, symbol allow-list, demo/live check) are enforced here so no front-end can skip them.

cTrader protocol units, for reference:
  * volume: 1/100 of a unit, so 1 lot of EURUSD (100,000 units) is 10,000,000. `lotSize` on the
    symbol gives the volume of one lot.
  * prices in spot events / relative SL+TP: 1/100000 of a price unit.
  * money: integers scaled by 10^moneyDigits.
  * timestamps: milliseconds since epoch.
"""
import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from ctrader_open_api.messages import OpenApiMessages_pb2 as oa
from ctrader_open_api.messages import OpenApiModelMessages_pb2 as model

from maits.config import ConfigError, Settings
from maits.ctrader.auth import TokenStore
from maits.ctrader.client import CTraderClient, CTraderError

log = logging.getLogger(__name__)

PRICE_SCALE = 100_000
ORDER_LABEL = "maits"  # tags positions opened by this system
SIDES = {"buy": model.ProtoOATradeSide.Value("BUY"), "sell": model.ProtoOATradeSide.Value("SELL")}
PERIOD_MINUTES = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M10": 10, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "H12": 720, "D1": 1440, "W1": 10080, "MN1": 43200,
}  # fmt: skip
_EXEC = model.ProtoOAExecutionType
_FINAL_EXEC = {_EXEC.Value(n) for n in ("ORDER_FILLED", "ORDER_REJECTED", "ORDER_CANCELLED", "ORDER_EXPIRED")}


# ---- pure helpers (unit-tested) ---------------------------------------------------------------


def normalize_symbol(name: str) -> str:
    """'oanda:eur/usd' -> 'EURUSD' (TradingView tickers may carry an exchange prefix)."""
    return name.strip().upper().split(":")[-1].replace("/", "")


def lots_to_volume(lots: float, lot_size: int, step: int, min_volume: int, max_volume: int) -> int:
    """Convert lots to protocol volume, rounded down to the symbol's volume step."""
    if lots <= 0:
        raise ValueError("lots must be positive")
    volume = int((Decimal(str(lots)) * lot_size).to_integral_value())
    if step > 0:
        volume -= volume % step
    if volume < min_volume:
        raise ValueError(f"{lots} lots is below the minimum volume of {min_volume / lot_size:g} lots")
    if max_volume and volume > max_volume:
        raise ValueError(f"{lots} lots is above the maximum volume of {max_volume / lot_size:g} lots")
    return volume


def pips_to_relative(pips: float, pip_position: int, digits: int) -> int:
    """Pips -> protocol relative distance (1/100000 of price), rounded to the symbol's price grid."""
    if pips <= 0:
        raise ValueError("pips must be positive")
    pip = 10 ** max(0, 5 - pip_position)  # size of one pip in protocol units
    step = 10 ** max(0, 5 - digits)  # smallest allowed price increment in protocol units
    return max(step, round(round(pips * pip) / step) * step)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _money(value: int, digits: int) -> float:
    return value / 10**digits


# ---- account discovery (works before an account is chosen) ------------------------------------


async def list_accounts(client: CTraderClient) -> list[dict]:
    res = await client.request(oa.ProtoOAGetAccountListByAccessTokenReq(accessToken=await client.access_token()))
    return [
        {"account_id": a.ctidTraderAccountId, "login": a.traderLogin, "is_live": a.isLive}
        for a in res.ctidTraderAccount
    ]


# ---- the trader -------------------------------------------------------------------------------


class Trader:
    def __init__(self, settings: Settings, client: CTraderClient, account_id: int):
        self.settings = settings
        self.client = client
        self.account_id = account_id
        self._light: dict[str, model.ProtoOALightSymbol] | None = None
        self._details: dict[int, model.ProtoOASymbol] = {}
        self._assets: dict[int, str] | None = None

    @classmethod
    async def open(cls, settings: Settings) -> "Trader":
        """Connect, pick the account, and verify it matches CTRADER_ENV."""
        settings.require_app_credentials()
        client = CTraderClient(settings, TokenStore(settings.token_file))
        try:
            await client.start()
            accounts = [a for a in await list_accounts(client) if a["is_live"] == settings.is_live]
            if settings.account_id:
                match = [a for a in accounts if a["account_id"] == settings.account_id]
                if not match:
                    raise ConfigError(
                        f"account {settings.account_id} is not a {settings.env} account on this token "
                        f"(run `maits accounts`; CTRADER_ENV={settings.env})"
                    )
                account_id = settings.account_id
            elif len(accounts) == 1:
                account_id = accounts[0]["account_id"]
            else:
                raise ConfigError(
                    f"found {len(accounts)} {settings.env} accounts - set CTRADER_ACCOUNT_ID "
                    f"(run `maits accounts` to list them)"
                )
            await client.authorize_account(account_id)
        except BaseException:
            await client.stop()
            raise
        return cls(settings, client, account_id)

    async def close(self) -> None:
        await self.client.stop()

    @property
    def connected(self) -> bool:
        return self.client.is_ready

    async def _req(self, message, **kwargs):
        if "ctidTraderAccountId" in message.DESCRIPTOR.fields_by_name:
            message.ctidTraderAccountId = self.account_id
        return await self.client.request(message, **kwargs)

    # ---- reference data ---------------------------------------------------------------------

    async def _symbol_list(self) -> dict[str, model.ProtoOALightSymbol]:
        if self._light is None:
            res = await self._req(oa.ProtoOASymbolsListReq())
            self._light = {s.symbolName.upper(): s for s in res.symbol}
        return self._light

    async def _resolve(self, name: str) -> model.ProtoOALightSymbol:
        symbols = await self._symbol_list()
        key = normalize_symbol(name)
        if key not in symbols:
            close = [n for n in symbols if key in n][:5]
            hint = f" - similar: {', '.join(close)}" if close else ""
            raise ValueError(f"unknown symbol '{name}'{hint}")
        return symbols[key]

    async def _symbol_details(self, symbol_ids: list[int]) -> dict[int, model.ProtoOASymbol]:
        missing = [i for i in set(symbol_ids) if i not in self._details]
        if missing:
            res = await self._req(oa.ProtoOASymbolByIdReq(symbolId=missing))
            self._details.update({s.symbolId: s for s in res.symbol})
        return {i: self._details[i] for i in symbol_ids}

    async def _asset_name(self, asset_id: int) -> str:
        if self._assets is None:
            res = await self._req(oa.ProtoOAAssetListReq())
            self._assets = {a.assetId: a.name for a in res.asset}
        return self._assets.get(asset_id, str(asset_id))

    async def _symbol_name(self, symbol_id: int) -> str:
        return next((n for n, s in (await self._symbol_list()).items() if s.symbolId == symbol_id), str(symbol_id))

    async def symbols(self, contains: str = "") -> list[dict]:
        needle = contains.upper()
        return [
            {"symbol": n, "symbol_id": s.symbolId, "description": s.description, "enabled": s.enabled}
            for n, s in sorted((await self._symbol_list()).items())
            if needle in n
        ]

    # ---- account state ----------------------------------------------------------------------

    async def positions(self) -> list[dict]:
        res = await self._req(oa.ProtoOAReconcileReq())
        if not res.position:
            return []
        pnl_res = await self._req(oa.ProtoOAGetPositionUnrealizedPnLReq())
        pnl = {p.positionId: _money(p.netUnrealizedPnL, pnl_res.moneyDigits) for p in pnl_res.positionUnrealizedPnL}
        details = await self._symbol_details([p.tradeData.symbolId for p in res.position])
        out = []
        for p in res.position:
            t = p.tradeData
            out.append({
                "position_id": p.positionId,
                "symbol": await self._symbol_name(t.symbolId),
                "side": model.ProtoOATradeSide.Name(t.tradeSide).lower(),
                "lots": t.volume / details[t.symbolId].lotSize,
                "entry_price": p.price,
                "stop_loss": p.stopLoss or None,
                "take_profit": p.takeProfit or None,
                "unrealized_pnl": pnl.get(p.positionId),
                "swap": _money(p.swap, p.moneyDigits),
                "commission": _money(p.commission, p.moneyDigits),
                "used_margin": _money(p.usedMargin, p.moneyDigits),
                "opened": _iso(t.openTimestamp),
                "label": t.label,
                "comment": t.comment,
            })  # fmt: skip
        return out

    async def orders(self) -> list[dict]:
        """Pending (limit/stop) orders."""
        res = await self._req(oa.ProtoOAReconcileReq())
        details = await self._symbol_details([o.tradeData.symbolId for o in res.order]) if res.order else {}
        return [
            {
                "order_id": o.orderId,
                "symbol": await self._symbol_name(o.tradeData.symbolId),
                "type": model.ProtoOAOrderType.Name(o.orderType).lower(),
                "side": model.ProtoOATradeSide.Name(o.tradeData.tradeSide).lower(),
                "lots": o.tradeData.volume / details[o.tradeData.symbolId].lotSize,
                "limit_price": o.limitPrice or None,
                "stop_price": o.stopPrice or None,
                "stop_loss": o.stopLoss or None,
                "take_profit": o.takeProfit or None,
            }
            for o in res.order
        ]

    async def account(self) -> dict:
        trader = (await self._req(oa.ProtoOATraderReq())).trader
        positions = await self.positions()
        balance = _money(trader.balance, trader.moneyDigits)
        unrealized = sum(p["unrealized_pnl"] or 0 for p in positions)
        used_margin = sum(p["used_margin"] for p in positions)
        equity = balance + unrealized
        return {
            "account_id": self.account_id,
            "login": trader.traderLogin,
            "broker": trader.brokerName,
            "environment": self.settings.env,
            "type": model.ProtoOAAccountType.Name(trader.accountType).lower(),
            "currency": await self._asset_name(trader.depositAssetId),
            "leverage": trader.leverageInCents / 100,
            "balance": balance,
            "unrealized_pnl": unrealized,
            "equity": equity,  # approximate: balance + net unrealized P&L
            "used_margin": used_margin,
            "free_margin": equity - used_margin,
            "margin_level_pct": round(equity / used_margin * 100, 1) if used_margin else None,
            "open_positions": len(positions),
        }  # fmt: skip

    # ---- market data ------------------------------------------------------------------------

    async def stream_quotes(self, symbol: str) -> AsyncIterator[dict]:
        """Yield {'bid','ask','time'} on every tick. Close the generator to unsubscribe."""
        sym = await self._resolve(symbol)
        queue: asyncio.Queue[dict] = asyncio.Queue()
        last: dict[str, float] = {}

        def on_event(payload) -> None:
            if not isinstance(payload, oa.ProtoOASpotEvent) or payload.symbolId != sym.symbolId:
                return
            for side in ("bid", "ask"):  # events may carry only the side that changed
                if payload.HasField(side):
                    last[side] = getattr(payload, side) / PRICE_SCALE
            if len(last) == 2:
                queue.put_nowait({"symbol": sym.symbolName, **last, "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds")})

        self.client.subscribe(on_event)
        try:
            await self._req(oa.ProtoOASubscribeSpotsReq(symbolId=[sym.symbolId]))
            while True:
                yield await queue.get()
        finally:
            self.client.unsubscribe(on_event)
            try:
                await self._req(oa.ProtoOAUnsubscribeSpotsReq(symbolId=[sym.symbolId]))
            except Exception:
                log.debug("unsubscribe failed", exc_info=True)

    async def quote(self, symbol: str, timeout: float = 10) -> dict:
        async with aclosing(self.stream_quotes(symbol)) as ticks:
            try:
                return await asyncio.wait_for(anext(ticks), timeout)
            except asyncio.TimeoutError:
                raise CTraderError("NO_QUOTE", f"no price for {symbol} within {timeout}s - is the market open?") from None

    async def bars(self, symbol: str, period: str = "M15", count: int = 20) -> list[dict]:
        period = period.upper()
        if period not in PERIOD_MINUTES:
            raise ValueError(f"period must be one of {', '.join(PERIOD_MINUTES)}")
        sym = await self._resolve(symbol)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # `count` limits the response to the last N bars before `to`; the window only has to be wide
        # enough to contain them (x1.5 + 2 days covers weekend gaps).
        span_ms = int(count * PERIOD_MINUTES[period] * 60_000 * 1.5) + 2 * 86_400_000
        res = await self._req(
            oa.ProtoOAGetTrendbarsReq(
                symbolId=sym.symbolId,
                period=model.ProtoOATrendbarPeriod.Value(period),
                fromTimestamp=now_ms - span_ms,
                toTimestamp=now_ms,
                count=count,
            )
        )
        return [
            {
                "time": _iso(b.utcTimestampInMinutes * 60_000),
                "open": (b.low + b.deltaOpen) / PRICE_SCALE,
                "high": (b.low + b.deltaHigh) / PRICE_SCALE,
                "low": b.low / PRICE_SCALE,
                "close": (b.low + b.deltaClose) / PRICE_SCALE,
                "volume": b.volume,
            }
            for b in res.trendbar
        ]

    # ---- trading ----------------------------------------------------------------------------

    @contextmanager
    def _watch(self, predicate):
        """Yield a future that resolves with the first server message matching `predicate`.

        Register *before* sending the request: the outcome event can arrive before the reply.
        """
        future: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_event(payload) -> None:
            if not future.done() and predicate(payload):
                future.set_result(payload)

        self.client.subscribe(on_event)
        try:
            yield future
        finally:
            self.client.unsubscribe(on_event)

    def _check_symbol_allowed(self, sym: model.ProtoOALightSymbol) -> None:
        allowed = self.settings.allowed_symbols
        if allowed and sym.symbolName.upper() not in allowed:
            raise ValueError(f"{sym.symbolName} is not in MAITS_ALLOWED_SYMBOLS")

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        lots: float,
        stop_loss_pips: float | None = None,
        take_profit_pips: float | None = None,
        comment: str = "",
        wait_seconds: float = 15,
    ) -> dict:
        side = side.lower()
        if side not in SIDES:
            raise ValueError("side must be 'buy' or 'sell'")
        if lots > self.settings.max_lots:
            raise ValueError(f"{lots} lots exceeds MAITS_MAX_LOTS={self.settings.max_lots}")
        sym = await self._resolve(symbol)
        self._check_symbol_allowed(sym)
        d = (await self._symbol_details([sym.symbolId]))[sym.symbolId]
        volume = lots_to_volume(lots, d.lotSize, d.stepVolume, d.minVolume, d.maxVolume)

        client_order_id = uuid.uuid4().hex
        request = oa.ProtoOANewOrderReq(
            symbolId=sym.symbolId,
            orderType=model.ProtoOAOrderType.Value("MARKET"),
            tradeSide=SIDES[side],
            volume=volume,
            label=ORDER_LABEL,
            comment=comment[:512],
            clientOrderId=client_order_id,
        )
        # Market orders can't carry absolute SL/TP, only distances relative to the fill price.
        if stop_loss_pips:
            request.relativeStopLoss = pips_to_relative(stop_loss_pips, d.pipPosition, d.digits)
        if take_profit_pips:
            request.relativeTakeProfit = pips_to_relative(take_profit_pips, d.pipPosition, d.digits)

        def is_outcome(p) -> bool:
            return (
                isinstance(p, oa.ProtoOAExecutionEvent)
                and p.order.clientOrderId == client_order_id
                and p.executionType in _FINAL_EXEC
            )

        log.info("ORDER %s %s lots=%s sl=%s tp=%s id=%s", side, sym.symbolName, lots, stop_loss_pips, take_profit_pips, client_order_id)
        with self._watch(is_outcome) as outcome:
            try:
                await self._req(request, instant=True)
            except CTraderError as exc:
                if exc.code == "TIMEOUT":
                    raise CTraderError(
                        "UNKNOWN_OUTCOME",
                        f"no reply to the order (client order id {client_order_id}); it may or may not have "
                        "been placed - check `maits positions` before retrying",
                    ) from None
                raise
            try:
                event = await asyncio.wait_for(outcome, wait_seconds)
            except asyncio.TimeoutError:
                return {"status": "submitted", "client_order_id": client_order_id, "note": "accepted, no fill confirmation yet"}

        if event.executionType != _EXEC.Value("ORDER_FILLED"):
            raise CTraderError(event.errorCode or _EXEC.Name(event.executionType), "order was not filled")
        return {
            "status": "filled",
            "symbol": sym.symbolName,
            "side": side,
            "lots": event.deal.filledVolume / d.lotSize,
            "price": event.deal.executionPrice,
            "position_id": event.position.positionId,
            "order_id": event.order.orderId,
            "client_order_id": client_order_id,
        }

    async def close_position(self, position_id: int, wait_seconds: float = 15) -> dict:
        """Close a position completely."""
        res = await self._req(oa.ProtoOAReconcileReq())
        position = next((p for p in res.position if p.positionId == position_id), None)
        if position is None:
            raise ValueError(f"no open position with id {position_id}")

        def is_outcome(p) -> bool:
            return (
                isinstance(p, oa.ProtoOAExecutionEvent)
                and p.position.positionId == position_id
                and p.executionType in _FINAL_EXEC
            )

        log.info("CLOSE position %s", position_id)
        with self._watch(is_outcome) as outcome:
            await self._req(
                oa.ProtoOAClosePositionReq(positionId=position_id, volume=position.tradeData.volume), instant=True
            )
            try:
                event = await asyncio.wait_for(outcome, wait_seconds)
            except asyncio.TimeoutError:
                return {"status": "close_requested", "position_id": position_id}
        if event.executionType != _EXEC.Value("ORDER_FILLED"):
            raise CTraderError(event.errorCode or _EXEC.Name(event.executionType), "close was not filled")
        detail = event.deal.closePositionDetail
        return {
            "status": "closed",
            "position_id": position_id,
            "price": event.deal.executionPrice,
            "realized_pnl": _money(detail.grossProfit, detail.moneyDigits),  # gross, before swap/commission
        }

    async def close_positions(self, symbol: str | None = None) -> list[dict]:
        """Close every open position (optionally only on one symbol)."""
        wanted = normalize_symbol(symbol) if symbol else None
        results = []
        for p in await self.positions():
            if wanted is None or p["symbol"] == wanted:
                results.append(await self.close_position(p["position_id"]))
        return results
