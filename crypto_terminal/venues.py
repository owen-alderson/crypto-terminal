"""The highlighted pair on other exchanges, beside Coinbase: the view no single exchange's own screen gives you.

Each venue streams its best bid and ask for one pair over its public websocket (no keys). Binance, Bybit and OKX quote
USD pairs in USDT, a dollar stablecoin that trades a little above or below $1, so their prices are converted to USD at
Coinbase's live USDT-USD price before comparing, and that rate is shown. Comparing BTCUSDT with BTC-USD directly would
show the stablecoin's drift as a premium that isn't there.
"""

import json
import math

from .feed import Feed, Unlisted

STABLE = "USDT"  # what USDT-margined venues quote "USD" pairs in
SILENCE = 90  # a quiet pair can go a minute with no change at the top of the book; pings catch dead sockets


def positive(value) -> float:
    """A price from JSON (Kraken sends numbers, the rest strings). ValueError unless positive and finite."""
    if isinstance(value, bool):
        raise ValueError(value)
    number = float(value)
    if not 0 < number < math.inf:
        raise ValueError(value)
    return number


class VenueFeed(Feed):
    """Best bid/ask for one Coinbase-style pair (`BTC-USD`) on another exchange. Never shows a quote from a dropped
    connection: bid and ask are cleared whenever the link isn't live."""

    NAME = ""
    URL = ""
    USD_IN_STABLE = False  # quotes USD pairs in USDT

    def __init__(self, symbol: str, url: str | None = None):
        base, _, quote = symbol.partition("-")
        self.quote = STABLE if quote == "USD" and self.USD_IN_STABLE else quote
        self.pair = self.venue_symbol(base, self.quote) if base != self.quote else ""
        self.status, self.bid, self.ask = "connecting", None, None
        super().__init__([self.pair], lambda tick: None, self._status, url=url or self.URL, reconnect_after=SILENCE)

    def _status(self, status: str):
        if status != "live":
            self.bid = self.ask = None
        self.status = status

    def set_quote(self, bid: float | None, ask: float | None):
        if bid and ask and bid > ask:  # crossed: a stale side, not a real market
            return
        self.bid, self.ask = bid, ask

    def venue_symbol(self, base: str, quote: str) -> str:
        raise NotImplementedError

    def handle(self, msg: dict) -> bool:
        try:
            return self.read(msg)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            return False  # malformed: skip it, keep the last good quote

    def read(self, msg: dict) -> bool:
        raise NotImplementedError

    async def run(self):
        if not self.pair:  # e.g. USDT-USD on a venue that quotes in USDT
            self._status("not listed")
            return
        await super().run()


class Kraken(VenueFeed):
    NAME, URL = "Kraken", "wss://ws.kraken.com/v2"  # heartbeats every second: no keepalive needed

    def venue_symbol(self, base, quote):
        return f"{base}/{quote}"

    def subscriptions(self):
        return [{"method": "subscribe", "params": {"channel": "ticker", "symbol": [self.pair], "event_trigger": "bbo"}}]

    def read(self, msg):
        if msg.get("method") == "subscribe":
            if msg.get("success") is False:
                error = str(msg.get("error"))
                raise Unlisted(self.pair) if "not supported" in error else RuntimeError(error)
            return True
        if msg.get("channel") == "heartbeat":
            return True
        if msg.get("channel") == "ticker":
            for row in msg["data"]:
                if row["symbol"] == self.pair:
                    self.set_quote(positive(row["bid"]), positive(row["ask"]))
                    return True
        return False


class Binance(VenueFeed):
    # the server pings and the websockets library answers; an unlisted pair just never sends a quote
    NAME, URL, USD_IN_STABLE = "Binance", "wss://stream.binance.com:9443/ws", True

    def venue_symbol(self, base, quote):
        return f"{base}{quote}"

    def subscriptions(self):
        return [{"method": "SUBSCRIBE", "params": [f"{self.pair.lower()}@bookTicker"], "id": 1}]

    def read(self, msg):
        if msg.get("id") == 1:
            if msg.get("error"):
                raise RuntimeError(str(msg["error"]))
            return True
        if msg.get("s") == self.pair:
            self.set_quote(positive(msg["b"]), positive(msg["a"]))
            return True
        return False


class Bybit(VenueFeed):
    NAME, URL, USD_IN_STABLE = "Bybit", "wss://stream.bybit.com/v5/public/spot", True
    KEEPALIVE = json.dumps({"op": "ping"})

    def venue_symbol(self, base, quote):
        return f"{base}{quote}"

    def subscriptions(self):
        return [{"op": "subscribe", "args": [f"orderbook.1.{self.pair}"]}]

    def read(self, msg):
        if msg.get("op") == "subscribe":
            if not msg.get("success"):
                error = str(msg.get("ret_msg"))
                raise Unlisted(self.pair) if "Invalid symbol" in error else RuntimeError(error)
            return True
        if msg.get("op") == "ping":  # our keepalive's pong
            return True
        if msg.get("topic") == f"orderbook.1.{self.pair}":
            data = msg["data"]
            bids, asks = data.get("b") or [], data.get("a") or []
            if msg.get("type") == "delta":  # only the side that changed
                self.set_quote(positive(bids[0][0]) if bids else self.bid, positive(asks[0][0]) if asks else self.ask)
            else:
                self.set_quote(positive(bids[0][0]) if bids else None, positive(asks[0][0]) if asks else None)
            return True
        return False


class OKX(VenueFeed):
    NAME, URL, USD_IN_STABLE = "OKX", "wss://ws.okx.com:8443/ws/v5/public", True
    KEEPALIVE = "ping"  # OKX drops a connection that's quiet for 30s; it answers a plain-text pong

    def venue_symbol(self, base, quote):
        return f"{base}-{quote}"

    def subscriptions(self):
        return [{"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": self.pair}]}]

    def read(self, msg):
        if msg.get("event") == "error":
            raise Unlisted(self.pair) if msg.get("code") == "60018" else RuntimeError(str(msg.get("msg")))
        if msg.get("event") == "subscribe":
            return True
        if msg.get("arg", {}).get("instId") == self.pair:
            row = msg["data"][0]
            self.set_quote(positive(row["bids"][0][0]) if row["bids"] else None,
                           positive(row["asks"][0][0]) if row["asks"] else None)
            return True
        return False


VENUES = (Kraken, Binance, Bybit, OKX)


def rate_symbol(symbol: str) -> str | None:
    """The Coinbase pair that converts USDT venues' prices for `symbol` into its quote, if one is needed."""
    return f"{STABLE}-USD" if symbol.endswith("-USD") and symbol != f"{STABLE}-USD" else None


def mid(bid, ask) -> float | None:
    return (bid + ask) / 2 if bid and ask else None


def vs_coinbase(feed: VenueFeed, coinbase_mid: float | None, rate: float | None, quote: str) -> float | None:
    """The venue's mid against Coinbase's, in basis points, after converting USDT to USD. None when unknown."""
    venue_mid = mid(feed.bid, feed.ask)
    if venue_mid is None or coinbase_mid is None:
        return None
    if feed.quote != quote:  # USDT venue against a USD pair
        if rate is None:
            return None
        venue_mid *= rate
    return (venue_mid - coinbase_mid) / coinbase_mid * 10_000
