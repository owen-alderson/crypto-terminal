"""MCP server: lets Claude (or any MCP client) read live markets and set alerts through the terminal's own code.

  claude mcp add crypto-terminal -- crypto-terminal mcp
"""

import functools
import logging

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import app as config  # read paths at call time: tests point them elsewhere
from . import snapshot
from .alerts import PERP_KINDS, USAGE, Alert, parse_alert
from .derivs import perp_symbol
from .history import product_exists

INSTRUCTIONS = """Live crypto market data from public exchange feeds (no keys), and the user's crypto-terminal alerts.
Pairs are Coinbase-style: BTC-USD, ETH-EUR, SOL-USDC.

`price` is Coinbase's last trade, 24h change and best bid/ask. `book` is Coinbase's order book, grouped into price
steps (asks round up, bids round down). `venues` compares the pair on Kraken, Binance, Bybit and OKX with Coinbase:
USDT venues are converted to USD at Coinbase's USDT-USD price first, so read `vs_coinbase_bp`, not the raw prices.
`perps` is the USDT perpetual future on Binance and Bybit: funding, premium, open interest. Perp prices are not spot;
say so when quoting them. A venue whose status isn't "live" has no number: report that, never fill it in.

Alerts fire only while the terminal is open with the pair on its watchlist, or while `crypto-terminal watch` runs;
`set_alert` says which applies. Alert syntax: """ + USAGE.removeprefix("usage: ")


def _explained(fn):
    """Let the model see why a call was refused (unknown pair, bad alert...); a real crash stays generic."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (LookupError, ValueError, httpx.HTTPError, OSError) as e:
            raise ToolError(str(e) or type(e).__name__) from e
    return wrapper


def listed(alerts: list[Alert]) -> list[dict]:
    return [{"number": i, "alert": str(a), "repeats": a.repeats, **vars(a)} for i, a in enumerate(alerts, 1)]


def fires(symbol: str) -> str:
    if pid := config.watch_pid():
        return f"checked now by crypto-terminal watch (pid {pid})"
    if symbol in config.load_watchlist():
        return f"fires while the terminal is open ({symbol} is on its watchlist), or run `crypto-terminal watch`"
    return (f"won't fire yet: {symbol} isn't on the terminal's watchlist and `crypto-terminal watch` isn't running. "
            "Start `crypto-terminal watch`, or add the pair in the terminal")


def build() -> MCPServer:
    server = MCPServer("crypto-terminal", instructions=INSTRUCTIONS)

    @server.tool()
    @_explained
    async def price(symbols: list[str]) -> list[dict]:
        """Last price, 24h change, best bid/ask and spread on Coinbase, for one or more pairs (e.g. ["BTC-USD"])."""
        async with httpx.AsyncClient() as client:
            return [await snapshot.price(client, symbol) for symbol in symbols]

    @server.tool()
    @_explained
    async def book(symbol: str, depth: int = 10, group: float | None = None) -> dict:
        """Coinbase order book: best `depth` levels a side as [price, size in the base coin].

        group: price step to group levels by, e.g. 10 for $10 on BTC (default: about 1 basis point of the price).
        An unsupported step is refused with the list of valid ones."""
        async with httpx.AsyncClient() as client:
            return await snapshot.book(client, symbol, depth, group)

    @server.tool()
    @_explained
    async def venues(symbol: str) -> dict:
        """The pair's best bid/ask on Kraken, Binance, Bybit and OKX, and each mid against Coinbase's in basis points
        (USDT venues converted to USD first). Takes up to 15 seconds: those exchanges only stream."""
        async with httpx.AsyncClient() as client:
            return await snapshot.venues(client, symbol)

    @server.tool()
    @_explained
    async def perps(symbol: str) -> dict:
        """Perpetual futures on Binance and Bybit for the pair (BTC-USD -> BTCUSDT): funding rate and next funding
        time, premium of mark over index in basis points, open interest in the base coin. Takes up to 15 seconds."""
        return await snapshot.perps(symbol)

    @server.tool()
    @_explained
    async def list_alerts() -> list[dict]:
        """The user's alerts, numbered as `remove_alert` takes them. repeats=false: fires once, then is removed."""
        return listed(config.load_alerts())

    @server.tool()
    @_explained
    async def set_alert(alert: str) -> dict:
        """Set an alert, written like the terminal's command: "BTC-USD > 90000", "ETH-USD move 2% 15m",
        "BTC-USD spread 5bp", "SOL-USD print 500k", "BTC-USD volume 3x", "BTC-USD funding 0.05%",
        "BTC-USD liq 1m". The result says whether something is running that will check it."""
        words = alert.split()
        if words[:1] == ["alert"]:
            words = words[1:]
        new = parse_alert(words)
        if new.kind in PERP_KINDS and not perp_symbol(new.symbol):  # the perp feeds would only say "no USDT perp"
            raise ValueError(f"{new.kind} alerts need a USD or USDT pair: there's no USDT perp for {new.symbol}")
        async with httpx.AsyncClient() as client:
            if not await product_exists(client, new.symbol):
                raise LookupError(f"unknown pair: {new.symbol}")
            if new.kind == "move" and not new.window:  # a one-shot move counts from the price now
                now = await snapshot.price(client, new.symbol)
                new = Alert(new.symbol, new.kind, new.level, now["price"])
        alerts = config.load_alerts()  # fresh: the terminal or watch may have changed the file
        alerts.append(new)
        config.save_alerts(alerts)
        return {"set": str(new), "number": len(alerts), "fires": fires(new.symbol)}

    @server.tool()
    @_explained
    async def remove_alert(number: int, alert: str) -> dict:
        """Remove alert `number`, whose `alert` text from `list_alerts` must match (a one-shot alert that fired since
        then renumbers the rest: the wrong one is never removed). Returns what was removed and what's left."""
        alerts = config.load_alerts()
        if not 1 <= number <= len(alerts):
            raise LookupError(f"no alert {number}: there {'is' if len(alerts) == 1 else 'are'} {len(alerts)}")
        if str(alerts[number - 1]) != alert:
            raise LookupError(f"alert {number} is now {alerts[number - 1]}, not {alert}: call list_alerts again")
        removed = alerts.pop(number - 1)
        config.save_alerts(alerts)
        return {"removed": str(removed), "alerts": listed(alerts)}

    return server


def main() -> None:
    server = build()  # sets up logging; then quiet httpx, which logs every request
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.run()
