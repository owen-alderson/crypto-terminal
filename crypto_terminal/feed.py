"""Coinbase Exchange public websocket feed (no API key): ticker + heartbeat here, order book + trades in book.py."""

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


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: float
    open_24h: float


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
    return Tick(symbol, price, open_24h)


class Feed:
    """Streams ticks for `symbols` forever, reconnecting with exponential backoff.

    Subclasses pick other `CHANNELS` and override `handle`.

    `status` is one of: connecting, live, reconnecting in Ns (reason).
    `last_msg` is the monotonic time of the last frame received (ticks or heartbeats).
    """

    CHANNELS = ("ticker", "heartbeat")

    def __init__(self, symbols: list[str], on_tick: Callable[[Tick], None],
                 on_status: Callable[[str], None] = lambda s: None, url: str = FEED_URL,
                 reconnect_after: float = RECONNECT_AFTER):
        self.symbols = list(symbols)
        self.on_tick = on_tick
        self.on_status = on_status
        self.url = url
        self.reconnect_after = reconnect_after
        self.last_msg = 0.0

    def handle(self, msg: dict) -> bool:
        """Deliver one message. True if it carried data (which proves the link is live)."""
        tick = parse_tick(msg)
        if tick:
            self.on_tick(tick)
        return tick is not None

    async def run(self):
        delay = 1
        while True:
            self.on_status("connecting")
            try:
                async with connect(self.url, ssl=SSL_CONTEXT if self.url.startswith("wss") else None,
                                   open_timeout=10, max_size=MAX_FRAME) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "product_ids": self.symbols,
                                              "channels": list(self.CHANNELS)}))
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
                                live, delay = True, 1
                                self.on_status("live")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, closes, timeouts, server errors: all mean reconnect
                self.on_status(f"reconnecting in {delay}s ({str(e) or type(e).__name__})")
            await asyncio.sleep(delay + random.uniform(0, delay / 4))
            delay = min(delay * 2, MAX_BACKOFF)
