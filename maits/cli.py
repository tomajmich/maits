"""Console app: `maits <command>` (or `python -m maits <command>`)."""
from maits import runtime  # noqa: F401  (must be imported before anything that loads the cTrader SDK)

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from contextlib import aclosing, asynccontextmanager

from maits.config import ConfigError, Settings, load_settings
from maits.ctrader import auth
from maits.ctrader.auth import TokenStore
from maits.ctrader.client import CTraderClient, CTraderError
from maits.ctrader.trader import PERIOD_MINUTES, Trader, list_accounts

# ---- output -----------------------------------------------------------------------------------


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.5f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def show(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    elif isinstance(data, dict):
        width = max(len(k) for k in data)
        for key, value in data.items():
            print(f"{key:<{width}}  {_fmt(value)}")
    elif not data:
        print("(none)")
    else:
        columns = list(data[0])
        rows = [[_fmt(row.get(c)) for c in columns] for row in data]
        widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(columns)]
        print("  ".join(c.upper().ljust(w) for c, w in zip(columns, widths)))
        for row in rows:
            print("  ".join(v.ljust(w) for v, w in zip(row, widths)))


@asynccontextmanager
async def open_trader(settings: Settings):
    trader = await Trader.open(settings)
    try:
        yield trader
    finally:
        await trader.close()


# ---- commands ---------------------------------------------------------------------------------


async def cmd_auth(args, settings: Settings) -> None:
    settings.require_app_credentials()
    print("1. Open this URL, log in with your cTrader ID and allow access:\n")
    print(f"   {auth.authorize_url(settings, args.scope)}\n")
    print("2. The browser then redirects to your redirect URI (the page itself may fail to load - that's fine).")
    print("   Copy the full URL from the address bar (or just the code=... value). The code expires in 1 minute.\n")
    text = await asyncio.to_thread(input, "Paste it here: ")
    tokens = await auth.exchange_code(settings, auth.extract_code(text))
    TokenStore(settings.token_file).save(tokens)
    print(f"\nTokens saved to {settings.token_file} (valid ~{tokens.seconds_left / 86400:.0f} days, refreshed automatically).\n")
    await cmd_accounts(args, settings)


async def cmd_accounts(args, settings: Settings) -> None:
    # Ask both servers: which one lists which accounts isn't something to rely on.
    found: dict[int, dict] = {}
    errors: list[CTraderError] = []
    for env in ("demo", "live"):
        client = CTraderClient(dataclasses.replace(settings, env=env), TokenStore(settings.token_file))
        try:
            await client.start()
            for account in await list_accounts(client):
                found[account["account_id"]] = account
        except CTraderError as exc:
            errors.append(exc)
            print(f"warning: could not query the {env} server: {exc}", file=sys.stderr)
        finally:
            await client.stop()
    if len(errors) == 2:  # nothing could be queried: don't pretend there are simply no accounts
        raise errors[0]
    rows = [{**a, "environment": "live" if a["is_live"] else "demo"} for a in found.values()]
    show(rows, args.json)
    if rows and not args.json:
        print("\nPut the account_id you want into CTRADER_ACCOUNT_ID, and set CTRADER_ENV to match.")


async def cmd_account(args, settings: Settings) -> None:
    async with open_trader(settings) as trader:
        show(await trader.account(), args.json)


async def cmd_positions(args, settings: Settings) -> None:
    async with open_trader(settings) as trader:
        show(await trader.positions(), args.json)


async def cmd_orders(args, settings: Settings) -> None:
    async with open_trader(settings) as trader:
        show(await trader.orders(), args.json)


async def cmd_symbols(args, settings: Settings) -> None:
    async with open_trader(settings) as trader:
        show(await trader.symbols(args.filter), args.json)


async def cmd_quote(args, settings: Settings) -> None:
    async with open_trader(settings) as trader:
        async with aclosing(trader.stream_quotes(args.symbol)) as ticks:
            for _ in range(args.ticks):
                try:
                    tick = await asyncio.wait_for(anext(ticks), 15)
                except asyncio.TimeoutError:
                    raise CTraderError("NO_QUOTE", "no price within 15s - is the market open?") from None
                print(json.dumps(tick) if args.json else f"{tick['time']}  {tick['symbol']}  bid {_fmt(tick['bid'])}  ask {_fmt(tick['ask'])}")


async def cmd_bars(args, settings: Settings) -> None:
    async with open_trader(settings) as trader:
        show(await trader.bars(args.symbol, args.period, args.count), args.json)


async def cmd_trade(args, settings: Settings) -> None:
    if settings.is_live and not args.yes:
        answer = await asyncio.to_thread(
            input, f"LIVE account: {args.command} {args.lots} lots {args.symbol.upper()} - type 'yes' to confirm: "
        )
        if answer.strip().lower() != "yes":
            print("aborted")
            return
    async with open_trader(settings) as trader:
        result = await trader.place_market_order(
            args.symbol, args.command, args.lots, args.sl_pips, args.tp_pips, args.comment
        )
        show(result, args.json)


async def cmd_close(args, settings: Settings) -> None:
    if args.position_id is None and not (args.symbol or args.all):
        raise ValueError("give a position id, --symbol SYMBOL, or --all")
    async with open_trader(settings) as trader:
        if args.position_id is not None:
            show(await trader.close_position(args.position_id), args.json)
        else:
            show(await trader.close_positions(args.symbol), args.json)


async def cmd_serve(args, settings: Settings) -> None:
    from maits.api import serve  # imported lazily: only this command needs FastAPI

    settings = dataclasses.replace(
        settings, api_host=args.host or settings.api_host, api_port=args.port or settings.api_port
    )
    await serve(settings)


# ---- argument parsing -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    parser = argparse.ArgumentParser(prog="maits", description="cTrader (Forex) console for the maits trading system")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def add(name, func, help, **kw):
        p = sub.add_parser(name, parents=[common], help=help, description=help, **kw)
        p.set_defaults(func=func)
        return p

    p = add("auth", cmd_auth, "log in via OAuth and store the access/refresh tokens")
    p.add_argument("--scope", choices=["trading", "accounts"], default="trading",
                   help="'accounts' = read-only token that cannot place orders")  # fmt: skip
    add("accounts", cmd_accounts, "list the trading accounts your token can access")
    add("account", cmd_account, "balance, equity and margin of the configured account")
    add("positions", cmd_positions, "open positions")
    add("orders", cmd_orders, "pending (limit/stop) orders")
    p = add("symbols", cmd_symbols, "list tradable symbols")
    p.add_argument("filter", nargs="?", default="", help="substring, e.g. EUR")
    p = add("quote", cmd_quote, "live bid/ask")
    p.add_argument("symbol")
    p.add_argument("-n", "--ticks", type=int, default=1, help="number of ticks to print (default 1)")
    p = add("bars", cmd_bars, "historical OHLC bars")
    p.add_argument("symbol")
    p.add_argument("-p", "--period", default="M15", choices=list(PERIOD_MINUTES))
    p.add_argument("-c", "--count", type=int, default=20)
    for side in ("buy", "sell"):
        p = add(side, cmd_trade, f"place a market {side} order")
        p.add_argument("symbol")
        p.add_argument("lots", type=float, help="size in lots, e.g. 0.01")
        p.add_argument("--sl-pips", type=float, help="stop loss distance in pips")
        p.add_argument("--tp-pips", type=float, help="take profit distance in pips")
        p.add_argument("--comment", default="cli")
        p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt on live accounts")
    p = add("close", cmd_close, "close a position (by id, all on a symbol, or everything)")
    p.add_argument("position_id", nargs="?", type=int)
    p.add_argument("--symbol")
    p.add_argument("--all", action="store_true")
    p = add("serve", cmd_serve, "run the HTTP API (order endpoint + TradingView webhook)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO if args.command == "serve" else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        runtime.run(args.func(args, load_settings()))
    except (ConfigError, CTraderError, auth.AuthError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
