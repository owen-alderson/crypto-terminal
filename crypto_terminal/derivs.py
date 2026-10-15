"""Perpetual futures for the highlighted pair on Binance and Bybit: what moves spot that a spot screen never shows.

A perpetual future ("perp") is a futures contract with no expiry. Four things from it explain a lot of spot moves:

- Funding rate: every few hours longs pay shorts (positive rate) or shorts pay longs (negative) so the perp's price
  stays near spot. A high positive rate means a crowded long. Both exchanges publish the rate that will be charged at
  the next funding time, as it stands now, and the interval between fundings (8h for most pairs, some 4h or 1h).
- Premium: the perp's mark price against the exchange's own spot index, in basis points. The perp is a separate
  market, so its price is never shown as if it were spot.
- Open interest: contracts open, each long/short pair counted once, in the base coin. Rising with the price means new
  money coming in; falling means positions closing. Bybit's `openInterest` field still counts both sides (twice
  Binance's basis) since its June 2026 switch; `singleOpenInterest` is the comparable number, so only that is used.
- Liquidations: positions the exchange closed by force. Binance sends at most one per pair per second (the latest),
  so the tape is a lower bound. Bybit gives the bankruptcy price, not the fill price, so its notional is approximate.

All from public feeds, no keys. Perps trade against USDT, so `BTC-USD` maps to `BTCUSDT`; pairs quoted in anything
else (EUR, BTC) have no perp here.
"""

import asyncio
import json
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx

from .feed import Feed, Unlisted
from .venues import SILENCE, positive

OI_EVERY = 60  # seconds between open-interest samples (Binance is polled this often)
OI_WINDOW = 15 * 60  # open-interest change is measured against a sample 15 to 16 minutes old
LIQ_KEEP = 50  # liquidations kept per feed for the tape


@dataclass(frozen=True)
class Liquidation:
    venue: str
    at: float  # exchange time, epoch seconds
    long: bool  # a long position was closed (by a forced sell)
    size: float  # base coin
    price: float  # average fill price (Binance) or bankruptcy price (Bybit), in USDT

    @property
    def notional(self) -> float:
        return self.size * self.price

    @property
    def time(self) -> str:
        return datetime.fromtimestamp(self.at, timezone.utc).strftime("%H:%M:%S")


def perp_symbol(symbol: str) -> str:
    """`BTC-USD` -> `BTCUSDT`; "" when there is no USDT perp to compare with (EUR or BTC quotes, USDT itself)."""
    base, _, quote = symbol.partition("-")
    return f"{base}USDT" if quote in ("USD", "USDT") and base != "USDT" else ""


def finite(value) -> float:
    """A rate from JSON: may be zero or negative, never NaN or infinite."""
    if isinstance(value, bool):
        raise ValueError(value)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(value)
    return number


class PerpFeed(Feed):
    """One pair's perp on one exchange. Never shows numbers from a dropped connection: they clear when it isn't live."""

    NAME = ""
    URL = ""

    def __init__(self, symbol: str, on_liq: Callable[[Liquidation], None] = lambda liq: None,
                 on_funding: Callable[[float | None], None] = lambda rate: None,
                 on_status: Callable[[str], None] = lambda status: None, url: str | None = None):
        self.symbol, self.pair = symbol, perp_symbol(symbol)
        self.on_liq, self.on_funding, self.report = on_liq, on_funding, on_status
        self.status = "connecting"
        self.funding = self.next_funding = self.interval = None  # rate (0.0001 = 0.01%), epoch seconds, hours
        self.mark = self.index = self.oi = None
        self.oi_samples: deque = deque()  # (monotonic time, open interest), one a minute
        self.liqs: deque = deque(maxlen=LIQ_KEEP)  # newest first
        super().__init__([self.pair], lambda tick: None, self._status, url=url or self.URL, reconnect_after=SILENCE)

    def _status(self, status: str):
        if status != "live":
            self.funding = self.next_funding = self.mark = self.index = None
            self.on_funding(None)
        self.status = status
        self.report(status)

    @property
    def premium(self) -> float | None:
        """Mark price against the index, in basis points."""
        return (self.mark - self.index) / self.index * 10_000 if self.mark and self.index else None

    def set_funding(self, rate: float, next_ms: int):
        self.funding, self.next_funding = rate, next_ms / 1000
        self.on_funding(rate)

    def add_oi(self, oi: float, now: float | None = None):
        now = time.monotonic() if now is None else now
        self.oi = oi
        if not self.oi_samples or now - self.oi_samples[-1][0] >= OI_EVERY - 1:  # -1: a poll can land early
            self.oi_samples.append((now, oi))
        # keep the newest sample that is at least OI_WINDOW old, as the one to compare against
        while len(self.oi_samples) > 1 and self.oi_samples[1][0] <= now - OI_WINDOW:
            self.oi_samples.popleft()

    def oi_change(self, now: float | None = None) -> float | None:
        """Percent change in open interest over the last 15 minutes; None until there are 15 minutes of samples."""
        now = time.monotonic() if now is None else now
        if self.oi is None or not self.oi_samples or now - self.oi_samples[0][0] < OI_WINDOW:
            return None
        return (self.oi - self.oi_samples[0][1]) / self.oi_samples[0][1] * 100

    def add_liq(self, liq: Liquidation):
        self.liqs.appendleft(liq)
        self.on_liq(liq)

    def handle(self, msg: dict) -> bool:
        try:
            return self.read(msg)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            return False  # malformed: skip it, keep the last good numbers

    def read(self, msg: dict) -> bool:
        raise NotImplementedError

    async def run(self):
        if not self.pair:
            self._status("no USDT perp")
            return
        await super().run()


class BinancePerp(PerpFeed):
    # market streams moved to /market in June 2026: the old /ws still takes the subscription, then sends nothing
    NAME, URL = "Binance", "wss://fstream.binance.com/market/ws"
    REST = "https://fapi.binance.com/fapi/v1"
    DEFAULT_INTERVAL = 8  # hours, for pairs missing from fundingInfo (it lists only adjusted ones)

    def subscriptions(self):
        pair = self.pair.lower()
        return [{"method": "SUBSCRIBE", "params": [f"{pair}@markPrice@1s", f"{pair}@forceOrder"], "id": 1}]

    def read(self, msg):
        if msg.get("id") == 1:
            if msg.get("error"):
                raise RuntimeError(str(msg["error"]))
            return True
        if msg.get("e") == "markPriceUpdate" and msg.get("s") == self.pair:
            self.mark, self.index = positive(msg["p"]), positive(msg["i"])
            self.set_funding(finite(msg["r"]), int(msg["T"]))
            return True
        if msg.get("e") == "forceOrder" and msg["o"]["s"] == self.pair:
            order = msg["o"]
            # a liquidation is an IOC order: count it once, when it is done (filled, or expired after a partial fill)
            if order["X"] not in ("FILLED", "EXPIRED"):
                return True
            # the forced order's side: SELL closes a long
            self.add_liq(Liquidation(self.NAME, int(order["T"]) / 1000, order["S"] == "SELL",
                                     positive(order["z"]), positive(order["ap"])))
            return True
        return False

    async def poll_oi(self, client: httpx.AsyncClient):
        response = await client.get(f"{self.REST}/openInterest", params={"symbol": self.pair}, timeout=10)
        if response.status_code == 400 and response.json().get("code") == -1121:  # "Invalid symbol."
            raise Unlisted(self.pair)
        response.raise_for_status()
        self.add_oi(positive(response.json()["openInterest"]))

    async def fetch_interval(self, client: httpx.AsyncClient) -> int:
        response = await client.get(f"{self.REST}/fundingInfo", timeout=10)
        response.raise_for_status()
        for row in response.json():
            if row["symbol"] == self.pair:
                return int(row["fundingIntervalHours"])
        return self.DEFAULT_INTERVAL

    async def poll(self, client: httpx.AsyncClient):
        while True:
            await asyncio.sleep(OI_EVERY)
            try:
                await self.poll_oi(client)
            except (httpx.HTTPError, Unlisted, KeyError, TypeError, ValueError, AttributeError):
                self.oi = None  # shows as unknown rather than a number that is going stale

    async def run(self):
        if not self.pair:
            return await super().run()
        async with httpx.AsyncClient() as client:
            try:
                await self.poll_oi(client)  # first: Binance's websocket says nothing about a pair it doesn't list
            except Unlisted:
                self._status(f"not listed ({self.pair})")
                return
            except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
                pass  # open interest unknown for now; the poll retries
            try:
                self.interval = await self.fetch_interval(client)
            except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
                pass  # interval unknown: shown as such, never guessed
            await asyncio.gather(super().run(), self.poll(client))


class BybitPerp(PerpFeed):
    NAME, URL = "Bybit", "wss://stream.bybit.com/v5/public/linear"
    KEEPALIVE = json.dumps({"op": "ping"})

    def __init__(self, *args, **kwargs):
        self.ticker: dict = {}  # snapshot, then deltas carry only the fields that changed
        super().__init__(*args, **kwargs)

    def _status(self, status: str):
        if status != "live":
            self.ticker, self.oi = {}, None
        super()._status(status)

    def subscriptions(self):
        return [{"op": "subscribe", "args": [f"tickers.{self.pair}", f"allLiquidation.{self.pair}"]}]

    def read(self, msg):
        if msg.get("op") == "subscribe":
            if not msg.get("success"):
                error = str(msg.get("ret_msg"))
                raise Unlisted(self.pair) if "handler not found" in error else RuntimeError(error)
            return True
        if msg.get("op") == "ping":  # our keepalive's pong
            return True
        topic = msg.get("topic")
        if topic == f"tickers.{self.pair}":
            self.ticker = {**(self.ticker if msg.get("type") == "delta" else {}), **msg["data"]}
            row = self.ticker
            self.mark, self.index = positive(row["markPrice"]), positive(row["indexPrice"])
            self.interval = int(row["fundingIntervalHour"])
            if "singleOpenInterest" in row:  # never `openInterest`: that one counts both sides
                self.add_oi(positive(row["singleOpenInterest"]))
            self.set_funding(finite(row["fundingRate"]), int(row["nextFundingTime"]))
            return True
        if topic == f"allLiquidation.{self.pair}":
            for row in msg["data"]:
                if row["s"] == self.pair:
                    # the position's side: "When you receive a Buy update, this means that a long position has been
                    # liquidated" (Bybit's docs). The opposite of Binance, which sends the forced order's side.
                    self.add_liq(Liquidation(self.NAME, int(row["T"]) / 1000, row["S"] == "Buy",
                                             positive(row["v"]), positive(row["p"])))
            return True
        return False


PERPS = (BinancePerp, BybitPerp)
