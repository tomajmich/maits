# maits – cTrader (Forex) connector

Console app + HTTP API for a BlackBull Markets cTrader account, built on Spotware's
[cTrader Open API](https://openapi.ctrader.com) through the official Python SDK
([OpenApiPy](https://github.com/spotware/OpenApiPy), PyPI: `ctrader-open-api`).

```
maits auth          log in (OAuth) and store tokens
maits accounts      list your trading accounts (demo + live)
maits account       balance / equity / margin
maits positions     open positions          maits orders   pending orders
maits symbols eur   find symbols            maits quote EURUSD -n 5
maits bars EURUSD -p H1 -c 50
maits buy EURUSD 0.01 --sl-pips 15 --tp-pips 30
maits close 12345   |  maits close --symbol EURUSD  |  maits close --all
maits serve         HTTP API + TradingView webhook
maits mcp           MCP server on stdio, for AI agents (trading tools: demo only)
```
Add `--json` to any command for machine-readable output.

## Setup

1. **Register an application** at <https://openapi.ctrader.com> → Applications. Add a redirect URI
   (e.g. `http://localhost:8080/callback`; it never has to be reachable). You get a client id and secret.
   Spotware may need to activate the application before it works.
2. `uv venv && uv pip install -e ".[dev]"` (Python 3.11+), then `cp .env.example .env` and fill it in.
3. `maits auth` – open the URL, log in with your cTrader ID, paste the redirect URL back. Tokens go to
   `tokens.json` (mode 600) and are refreshed automatically (access token lives ~30 days; each refresh
   invalidates the previous pair, so don't copy `tokens.json` to two machines that both run).
   Use `--scope accounts` for a **read-only** token that cannot place orders.
4. `maits accounts`, put the `account_id` into `CTRADER_ACCOUNT_ID`, and set `CTRADER_ENV` to match.
5. **Start on a demo account.** `CTRADER_ENV` defaults to `demo` and is checked against the account, so a
   demo setting can never trade a live account.

## HTTP API

`maits serve` (needs `MAITS_API_KEY`; binds to 127.0.0.1 by default). Send `X-API-Key: <key>`.

| | |
|---|---|
| `GET /health` (no key), `/account`, `/positions`, `/orders`, `/symbols?q=`, `/quote/{symbol}`, `/bars/{symbol}?period=&count=` | read |
| `POST /orders` `{"symbol":"EURUSD","side":"buy","lots":0.01,"stop_loss_pips":15,"take_profit_pips":30}` | market order |
| `POST /positions/{id}/close`, `POST /positions/close-all?symbol=EURUSD` | close |
| `POST /webhook/tradingview` | TradingView alerts (secret in body) |

Interactive docs at `/docs`. Guards enforced for every entry point: `MAITS_MAX_LOTS` (default 0.10),
optional `MAITS_ALLOWED_SYMBOLS`, and the demo/live check.

### TradingView

Alert message (JSON), webhook URL `https://your-host/webhook/tradingview`:

```json
{"secret": "<MAITS_WEBHOOK_SECRET>", "action": "{{strategy.order.action}}", "symbol": "{{ticker}}", "lots": 0.01, "sl_pips": 15}
```
`action` is `buy`, `sell` or `close` (closes all positions on the symbol). TradingView only calls ports
80/443, needs a paid plan with 2FA, and gives up after ~3 seconds – put the app behind a reverse proxy
with HTTPS (e.g. Caddy: `your-host { reverse_proxy 127.0.0.1:8000 }`). Anyone who learns the secret can
trade your account, so use a long random one and keep `MAITS_MAX_LOTS` low.

## MCP server (AI agents)

`maits mcp` speaks the Model Context Protocol over stdio (stdout is the protocol; logs go to stderr).
Tools: `status` (environment and limits, no connection), `account`, `positions`, `orders`, `symbols`,
`quote`, `bars`, and `place_market_order`, `close_position`, `close_positions` (needs `symbol` or `all=true`).
The trading tools refuse to run unless `CTRADER_ENV=demo`. The usual guards (`MAITS_MAX_LOTS`,
`MAITS_ALLOWED_SYMBOLS`, demo/live check) still apply. The cTrader connection opens on the first tool call;
if login fails, the call returns an error and the next call tries again.

Register it with any MCP client as a stdio server whose working directory is the repo, so `.env` and
`tokens.json` are found. The omp config in this repo (`.omp/mcp.json`) does this:

```json
{"mcpServers": {"maits": {"type": "stdio", "command": "/path/to/venv/bin/maits", "args": ["mcp"],
  "cwd": "/path/to/maits", "timeout": 90000}}}
```

Use a long timeout. The first call logs in, which can take up to 30 s, and an order then waits up to 15 s
for its fill.

## Where to run it

cTrader does not host Open API programs: this is your own process that connects out to
`live.ctraderapi.com:5035` / `demo.ctraderapi.com:5035` (no inbound port needed unless you want the
webhook). Any small Linux VPS works. cTrader's own hosted-automation route is cBots (C#, cTrader Automate),
which is a different tool; ask BlackBull what hosting they offer for it.

```ini
# /etc/systemd/system/maits.service
[Service]
WorkingDirectory=/opt/maits
ExecStart=/opt/maits/.venv/bin/maits serve
Restart=always
User=maits
[Install]
WantedBy=multi-user.target
```
The connection re-authenticates automatically after network drops.

## Using it from a strategy

Import `Trader` (see `maits/ctrader/trader.py`) instead of going through HTTP:
`trader = await Trader.open(load_settings())`, then `trader.bars(...)`, `trader.stream_quotes(...)`,
`trader.place_market_order(...)`. Run with `maits.runtime.run(coro)` – the SDK's Twisted reactor and asyncio
share one loop.

## Tests

`pytest` – unit tests for lot/pip conversion, the API's auth and error mapping, the MCP tools' demo-only gate and
error reporting, and the order flow against a scripted fake of the cTrader connection.

## Known limits

- Market orders only (no limit/stop/amend yet). SL/TP are given in pips because the protocol does not allow
  absolute SL/TP on market orders.
- Not yet run against a real account: the connection, TLS and auth-error path were verified against
  Spotware's demo server, everything after login only against the fake. Expect to fix small things on the
  first demo run, especially historical bars (Spotware's per-period range limits are not in the docs I could read).
- `equity`/`free_margin` are derived from balance and open positions, so treat them as approximate.
