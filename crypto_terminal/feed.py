"""Coinbase Exchange public websocket feed (no API key): ticker + heartbeat (+ trades for pairs with trade alerts)
here, order book + trades for the highlighted pair in book.py."""

import asyncio
import json
import math
import random
import ssl
import time
from dataclasses import dataclass
from typing import Callable

import certifi
from websockets.asyncio.client import connect

FEED_URL = "wss://ws-feed.exchange.coinbase.com"
RECONNECT_AFTER = 30  # seconds of silence (heartbeats arrive every 1s) before the socket is presumed dead
MAX_BACKOFF = 30
STALE_AFTER = 10  # seconds without any frame before data is shown as stale
MAX_FRAME = 32 * 2**20  # a BTC-USD order book snapshot is ~1.1 MB, over the websockets default of 1 MiB

# python.org macOS builds ship without a CA bundle; use certifi's so wss:// works everywhere
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class Unlisted(Exception):
    """The exchange says it doesn't list the pair: the feed stops rather than retry forever."""


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: float
    open_24h: float
    bid: float | None = None  # best bid / ask, when the message carries a valid pair of them
    ask: float | None = None


def parse_tick(msg: dict) -> Tick | None:
    """Validate a `ticker` message. Returns None for anything malformed."""
    if msg.get("type") != "ticker":
        return None
    try:
        symbol = msg["product_id"]
        price = float(msg["price"])
        open_24h = float(msg["open_24h"])
    except (KeyError, TypeError, ValueError):
        return None
    # chained comparisons reject NaN, inf, zero and negatives
    if not isinstance(symbol, str) or not (0 < price < math.inf and 0 < open_24h < math.inf):
        return None
    try:
        bid, ask = float(msg["best_bid"]), float(msg["best_ask"])
    except (KeyError, TypeError, ValueError):
        bid = ask = None
    if bid is None or not (0 < bid <= ask < math.inf):  # missing, malformed or crossed: no spread rather than a wrong one
        bid = ask = None
    return Tick(symbol, price, open_24h, bid, ask)


class Feed:
    """Streams ticks for `symbols` forever, reconnecting with exponential backoff.
    `trade_symbols` (a subset) also stream every trade to `on_trade(symbol, Trade)`, for trade alerts.

    Subclasses pick other `CHANNELS` (or `subscriptions` for other exchanges) and override `handle`.

    `status` is one of: connecting, live, reconnecting in Ns (reason).
    `last_msg` is the monotonic time of the last frame received (ticks or heartbeats).
    """

    CHANNELS = ("ticker", "heartbeat")
    KEEPALIVE: str | None = None  # sent every KEEPALIVE_EVERY seconds, for exchanges that drop a silent client
    KEEPALIVE_EVERY = 15

    def __init__(self, symbols: list[str], on_tick: Callable[[Tick], None],
                 on_status: Callable[[str], None] = lambda s: None, url: str = FEED_URL,
                 reconnect_after: float = RECONNECT_AFTER, trade_symbols=(), on_trade=None):
        self.symbols = list(symbols)
        self.trade_symbols = list(trade_symbols)
        self.on_trade = on_trade
        self.on_tick = on_tick
        self.on_status = on_status
        self.url = url
        self.reconnect_after = reconnect_after
        self.last_msg = 0.0

    def handle(self, msg: dict) -> bool:
        """Deliver one message. True if it carried data (which proves the link is live)."""
        if msg.get("type") == "match":  # not `last_match`: that one can be an old trade, replayed on subscribe
            from .book import parse_trade  # book.py imports this module
            trade = parse_trade(msg)
            if trade and msg.get("product_id") in self.trade_symbols:
                self.on_trade(msg["product_id"], trade)
            return trade is not None
        tick = parse_tick(msg)
        if tick:
            self.on_tick(tick)
        return tick is not None

    def channels(self) -> list:
        trades = [{"name": "matches", "product_ids": self.trade_symbols}] if self.trade_symbols else []
        return [*self.CHANNELS, *trades]

    def subscriptions(self) -> list[dict]:
        """Messages sent after connecting."""
        return [{"type": "subscribe", "product_ids": self.symbols, "channels": self.channels()}]

    async def keepalive(self, ws):
        while True:
            await asyncio.sleep(self.KEEPALIVE_EVERY)
            await ws.send(self.KEEPALIVE)

    async def run(self):
        self.delay = 1
        while True:
            self.on_status("connecting")
            try:
                async with connect(self.url, ssl=SSL_CONTEXT if self.url.startswith("wss") else None,
                                   open_timeout=10, close_timeout=1, max_size=MAX_FRAME) as ws:
                    # close_timeout: a slow close handshake (Binance takes ~8s) never holds up whoever cancelled us
                    for message in self.subscriptions():
                        await ws.send(json.dumps(message))
                    pinger = asyncio.create_task(self.keepalive(ws)) if self.KEEPALIVE else None
                    try:
                        await self.receive(ws)
                    finally:
                        if pinger:
                            pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Unlisted as e:
                self.on_status(f"not listed ({e})")
                return
            except Exception as e:  # network errors, closes, timeouts, server errors: all mean reconnect
                self.on_status(f"reconnecting in {self.delay}s ({str(e) or type(e).__name__})")
            await asyncio.sleep(self.delay + random.uniform(0, self.delay / 4))
            self.delay = min(self.delay * 2, MAX_BACKOFF)

    async def receive(self, ws):
        live = False
        while True:
            # wait_for catches a half-open socket that never errors
            raw = await asyncio.wait_for(ws.recv(), self.reconnect_after)
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "error":
                raise RuntimeError(str(msg.get("reason") or msg.get("message")))
            if self.handle(msg) or msg.get("type") == "heartbeat":
                self.last_msg = time.monotonic()
                if not live:
                    # only data proves the link works, so backoff resets here, not on connect
                    live, self.delay = True, 1
                    self.on_status("live")
