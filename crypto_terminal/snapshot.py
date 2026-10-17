"""One-shot snapshots for scripts, pipes and the MCP server: `crypto-terminal price | book | venues | perps`.

Each returns plain data (dicts of numbers and strings) from the same sources and checks as the terminal's panes.
Coinbase prices and the book come from its public REST API. The other exchanges and the perps only stream, so their
feeds run until each has sent its numbers (or SETTLE seconds pass) and are then closed; one that hasn't answered by
then says so, rather than being left out or guessed.
"""

import asyncio
from datetime import datetime, timezone

import httpx

from .book import OrderBook, decimals, default_step, group_steps, step_decimals
from .derivs import OI_WINDOW, PERPS, perp_symbol
from .feed import parse_tick
from .history import REST_BASE, SYMBOL_RE, product_exists
from .venues import VENUES, mid, rate_symbol, vs_coinbase

SETTLE = 10  # seconds the streaming exchanges get to send a first full set of numbers
MAX_DEPTH = 500  # book levels per side


def pair(symbol: str) -> str:
    """`btc-usd` -> `BTC-USD`. ValueError for anything that isn't a pair."""
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError(f"not a pair: {symbol} (expected e.g. BTC-USD)")
    return symbol


async def coinbase(client: httpx.AsyncClient, symbol: str, path: str, **params):
    response = await client.get(f"{REST_BASE}/products/{symbol}/{path}", params=params, timeout=10)
    if response.status_code in (400, 404):
        raise LookupError(f"unknown pair: {symbol}")
    response.raise_for_status()
    return response.json()


def spread_bp(bid, ask) -> float | None:
    return (ask - bid) / mid(bid, ask) * 10_000 if bid and ask else None


async def price(client: httpx.AsyncClient, symbol: str) -> dict:
    """Last trade price, 24h change and the best bid / ask on Coinbase."""
    symbol = pair(symbol)
    ticker, stats = await asyncio.gather(coinbase(client, symbol, "ticker"), coinbase(client, symbol, "stats"))
    try:  # the websocket ticker's checks: the same numbers pass or fail as in the terminal
        tick = parse_tick({"type": "ticker", "product_id": symbol, "price": ticker["price"], "open_24h": stats["open"],
                           "best_bid": ticker.get("bid"), "best_ask": ticker.get("ask")})
    except (KeyError, TypeError, AttributeError):
        tick = None
    if tick is None:
        raise ValueError(f"Coinbase sent an unreadable ticker for {symbol}")
    return {"symbol": symbol, "price": tick.price, "change_24h_pct": (tick.price / tick.open_24h - 1) * 100,
            "open_24h": tick.open_24h, "bid": tick.bid, "ask": tick.ask, "spread_bp": spread_bp(tick.bid, tick.ask),
            "last_trade": ticker.get("time"), "exchange": "Coinbase"}


async def book(client: httpx.AsyncClient, symbol: str, depth: int = 10, group: float | None = None) -> dict:
    """Best `depth` levels a side on Coinbase, grouped into price steps of `group` (default: the terminal's, about
    1 bp of the price). Grouped asks round up and bids round down, so a level never looks better than it is."""
    symbol = pair(symbol)
    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be 1 to {MAX_DEPTH}")
    data = await coinbase(client, symbol, "book", level=2)  # the whole book: Coinbase ignores a depth parameter
    levels = OrderBook()
    try:
        levels.load({side: [row[:2] for row in data[side]] for side in ("bids", "asks")})  # rows: price, size, orders
        size_dp = max((decimals(row[1]) for side in ("bids", "asks") for row in data[side]), default=0)
    except (KeyError, TypeError) as e:
        raise ValueError(f"Coinbase sent an unreadable book for {symbol}") from e
    bid, ask = levels.best()
    middle = mid(bid, ask) or bid or ask
    if not middle:
        raise LookupError(f"{symbol}: the book is empty")
    steps, tick = group_steps(middle, levels.dp), 10 ** -levels.dp
    if group is None:
        step = default_step(steps, middle, levels.dp)
    else:
        step = round(group / tick)
        if step not in steps or abs(step * tick - group) > tick / 1e6:
            offered = ", ".join(f"{s * tick:.{step_decimals(s, levels.dp)}f}" for s in steps)
            raise ValueError(f"group for {symbol} must be one of: {offered}")
    asks, bids = levels.top(depth, step)
    dp = step_decimals(step, levels.dp)  # sums of grouped sizes are rounded back to Coinbase's decimals: no float noise
    return {"symbol": symbol, "group": round(step * tick, levels.dp), "bid": bid, "ask": ask, "mid": mid(bid, ask),
            "spread_bp": spread_bp(bid, ask), "asks": [[round(p, dp), round(s, size_dp)] for p, s in asks],
            "bids": [[round(p, dp), round(s, size_dp)] for p, s in bids], "time": data.get("time"), "exchange": "Coinbase"}


async def settle(feeds, ready, timeout: float = SETTLE):
    """Run `feeds` until each is `ready` or has stopped by itself (not listed), or `timeout` passes; then close them.
    Their last numbers stay readable."""
    tasks = [asyncio.create_task(feed.run()) for feed in feeds]
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    try:
        while loop.time() < end and not all(ready(f) or t.done() for f, t in zip(feeds, tasks)):
            await asyncio.sleep(0.05)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def status(feed, has_data, timeout: float) -> str:
    if has_data:
        return "live"
    if feed.status.startswith(("not listed", "no USDT perp")):  # the exchange said so: not a timeout
        return feed.status
    return f"no data within {timeout:g}s" + (f" ({feed.status})" if feed.status != "live" else "")


async def venues(client: httpx.AsyncClient, symbol: str, timeout: float = SETTLE) -> dict:
    """The pair's best bid / ask on Kraken, Binance, Bybit and OKX against Coinbase's mid, in basis points. USDT
    venues are converted to USD at Coinbase's USDT-USD mid first."""
    symbol = pair(symbol)
    if not await product_exists(client, symbol):
        raise LookupError(f"unknown pair: {symbol}")
    feeds = [venue(symbol) for venue in VENUES]
    await settle(feeds, lambda f: f.bid and f.ask, timeout)
    rate_pair = rate_symbol(symbol)
    here = await price(client, symbol)  # after the venues, so both sides are from the same moment
    rate = await price(client, rate_pair) if rate_pair else None
    usdt = mid(rate["bid"], rate["ask"]) if rate else None
    quote = symbol.partition("-")[2]
    coinbase_mid = mid(here["bid"], here["ask"])
    return {
        "symbol": symbol, "coinbase": {"bid": here["bid"], "ask": here["ask"], "mid": coinbase_mid},
        "usdt_rate": usdt,
        "venues": [{"exchange": f.NAME, "pair": f.pair or None, "quote": f.quote, "bid": f.bid, "ask": f.ask,
                    "vs_coinbase_bp": vs_coinbase(f, coinbase_mid, usdt, quote),
                    "status": status(f, f.bid and f.ask, timeout)}
                   for f in feeds],
        "note": "vs_coinbase_bp: the venue's mid against Coinbase's mid; USDT prices converted at usdt_rate first",
    }


def iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds") if epoch else None


async def perps(symbol: str, timeout: float = SETTLE) -> dict:
    """Funding, premium and open interest of the pair's USDT perpetual future on Binance and Bybit."""
    symbol = pair(symbol)
    feeds = [perp(symbol) for perp in PERPS]
    await settle(feeds, lambda f: f.funding is not None and f.oi is not None, timeout)
    return {
        "symbol": symbol, "perp": perp_symbol(symbol) or None,
        "venues": [{"exchange": f.NAME, "funding_pct": None if f.funding is None else f.funding * 100,
                    "next_funding": iso(f.next_funding), "interval_h": f.interval, "mark": f.mark, "index": f.index,
                    "premium_bp": f.premium, "open_interest": f.oi,
                    "status": status(f, f.funding is not None, timeout)}
                   for f in feeds],
        "note": ("perp prices are not spot. funding_pct: the rate due at next_funding as it stands now (positive = "
                 "longs pay shorts). open_interest: base coin, each long/short pair counted once. The "
                 f"{OI_WINDOW // 60}-minute open-interest change and liquidations need a live feed: the terminal's "
                 "`d` panel, or `funding` / `liq` alerts"),
    }
