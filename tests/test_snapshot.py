"""One-shot snapshots: Coinbase REST stubbed with MockTransport, the streaming exchanges with fake feeds."""

import asyncio
import time

import httpx
import pytest

from crypto_terminal import snapshot
from crypto_terminal.derivs import PerpFeed
from crypto_terminal.venues import VenueFeed

TICKERS = {  # captured shapes, trimmed
    "BTC-USD": {"ask": "81266.22", "bid": "81266.21", "volume": "2669.3", "trade_id": 1107426048, "price": "81266.22",
                "size": "0.00119584", "time": "2026-10-17T17:54:48.202780239Z"},
    "USDT-USD": {"ask": "0.99880", "bid": "0.99870", "price": "0.99875", "time": "2026-10-17T17:54:48Z"},
}
STATS = {"BTC-USD": {"open": "81089.2", "high": "81720.1", "low": "80796.96", "last": "81246.47", "volume": "2668.3"},
         "USDT-USD": {"open": "0.99875", "last": "0.99875", "volume": "1"}}
BOOK = {"bids": [["81266.21", "0.30799343", 6], ["81264.58", "0.02461096", 2], ["81259.99", "0.1", 1],
                 ["81250", "1.5", 3]],
        "asks": [["81266.22", "0.12217633", 3], ["81267.85", "0.02460998", 2], ["81270.01", "0.2", 1]],
        "sequence": 137893414452, "time": "2026-10-17T17:54:50.245763831Z"}


def client(tickers=TICKERS, stats=STATS, book=BOOK) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")  # /products/BTC-USD[/ticker]
        symbol, what = parts[2], parts[3] if len(parts) > 3 else ""
        if symbol not in tickers:
            return httpx.Response(404, json={"message": "NotFound"})
        body = {"ticker": tickers, "stats": stats}.get(what, {}).get(symbol) if what != "book" else book
        return httpx.Response(200, json=body if what else {"id": symbol})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_price():
    async with client() as c:
        got = await snapshot.price(c, " btc-usd ")
    assert got["symbol"] == "BTC-USD" and got["price"] == 81266.22 and got["open_24h"] == 81089.2
    assert (got["bid"], got["ask"]) == (81266.21, 81266.22)
    assert got["change_24h_pct"] == pytest.approx((81266.22 / 81089.2 - 1) * 100)
    assert got["spread_bp"] == pytest.approx(0.01 / 81266.215 * 1e4)


async def test_price_refuses_what_it_cannot_trust():
    async with client() as c:
        with pytest.raises(ValueError, match="not a pair"):
            await snapshot.price(c, "BTCUSD")
        with pytest.raises(LookupError, match="unknown pair: ZZQ-USD"):
            await snapshot.price(c, "ZZQ-USD")
    crossed = {"BTC-USD": {**TICKERS["BTC-USD"], "bid": "81300", "ask": "81200"}}
    async with client(tickers=crossed) as c:
        got = await snapshot.price(c, "BTC-USD")
    assert got["bid"] is got["ask"] is got["spread_bp"] is None  # no spread rather than a wrong one
    for broken in ({**TICKERS["BTC-USD"], "price": "NaN"}, {"bid": "1"}, ["not", "a", "dict"]):
        async with client(tickers={"BTC-USD": broken}) as c:
            with pytest.raises(ValueError, match="unreadable ticker"):
                await snapshot.price(c, "BTC-USD")


async def test_book_groups_like_the_terminal():
    async with client() as c:
        got = await snapshot.book(c, "BTC-USD", depth=2)
    assert got["group"] == 10  # the terminal's default: the step nearest 1 bp of the price
    # asks round up, bids round down: a grouped level never shows a better price than is on offer
    assert got["asks"] == [[81270.0, 0.14678631], [81280.0, 0.2]]
    assert got["bids"] == [[81260.0, 0.33260439], [81250.0, 1.6]]  # 0.1 at 81,259.99 counts toward 81,250
    assert (got["bid"], got["ask"], got["time"]) == (81266.21, 81266.22, BOOK["time"])
    async with client() as c:
        raw = await snapshot.book(c, "BTC-USD", depth=1, group=0.01)
    assert raw["group"] == 0.01 and raw["asks"] == [[81266.22, 0.12217633]] and raw["bids"] == [[81266.21, 0.30799343]]


async def test_book_sizes_have_no_float_noise():
    rows = [[f"100.0{i}", "0.1", 1] for i in range(1, 4)]  # 0.1 + 0.1 + 0.1 = 0.30000000000000004 in floats
    async with client(book={"bids": [["99", "1", 1]], "asks": rows}) as c:
        got = await snapshot.book(c, "BTC-USD", depth=1, group=0.05)
    assert got["asks"] == [[100.05, 0.3]]


async def test_book_refusals():
    async with client() as c:
        with pytest.raises(ValueError, match="must be one of: 0.01, 0.05, 0.1, 0.5, 1, 5, 10, 50, 100"):
            await snapshot.book(c, "BTC-USD", group=7)
        with pytest.raises(ValueError, match="must be one of"):
            await snapshot.book(c, "BTC-USD", group=0.015)  # not a whole number of ticks
        for group in (float("inf"), float("nan"), -10, 0):
            with pytest.raises(ValueError, match="must be one of"):
                await snapshot.book(c, "BTC-USD", group=group)
        for depth in (0, snapshot.MAX_DEPTH + 1):
            with pytest.raises(ValueError, match="depth"):
                await snapshot.book(c, "BTC-USD", depth=depth)
    async with client(book={"bids": [], "asks": []}) as c:
        with pytest.raises(LookupError, match="empty"):
            await snapshot.book(c, "BTC-USD")
    for broken in ({"bids": []}, {"bids": [["1", "-2", 1]], "asks": []}, {"bids": [5], "asks": []}):
        async with client(book=broken) as c:
            with pytest.raises(ValueError):
                await snapshot.book(c, "BTC-USD")


class Forever:
    """A feed that connects and never sends anything."""
    status = "connecting"

    async def run(self):
        await asyncio.Event().wait()


async def test_streaming_waits_for_data_and_gives_up_on_silence():
    class Quick(Forever):
        data = None

        async def run(self):
            await asyncio.sleep(0.05)
            self.data = 1
            await asyncio.Event().wait()

    class Stops(Forever):
        async def run(self):
            self.status = "not listed"

    quick, stops = Quick(), Stops()
    start = time.monotonic()
    async with snapshot.streaming([quick, stops], lambda f: getattr(f, "data", None), timeout=5):
        pass
    assert time.monotonic() - start < 1 and quick.data == 1  # didn't wait out the timeout
    start = time.monotonic()
    forever = Forever()
    async with snapshot.streaming([quick, forever], lambda f: getattr(f, "data", None), timeout=0.2):
        pass
    assert 0.2 <= time.monotonic() - start < 1


def fake_venue(name, stable, bid=None, ask=None, status=None):
    class Fake(VenueFeed):
        NAME, USD_IN_STABLE = name, stable

        def venue_symbol(self, base, quote):
            return f"{base}{quote}"

        async def run(self):
            self._status(status or "live")
            if bid:
                self.set_quote(bid, ask)
            await asyncio.Event().wait()
    return Fake


async def test_venues_converts_usdt_and_says_who_did_not_answer(monkeypatch):
    monkeypatch.setattr(snapshot, "VENUES", (
        fake_venue("Kraken", False, 81270.0, 81270.2),
        fake_venue("Binance", True, 81370.0, 81370.2),  # USDT, at USDT-USD 0.99875
        fake_venue("Bybit", True, status="not listed (BTCUSDT)"),
        fake_venue("OKX", True, status="reconnecting in 1s (timeout)"),
        fake_venue("Quiet", True)))  # live but no quote
    async with client() as c:
        got = await snapshot.venues(c, "BTC-USD", timeout=0.2)
    assert got["usdt_rate"] == pytest.approx(0.99875) and got["coinbase"]["mid"] == pytest.approx(81266.215)
    rows = {row["exchange"]: row for row in got["venues"]}
    assert rows["Kraken"]["vs_coinbase_bp"] == pytest.approx((81270.1 - 81266.215) / 81266.215 * 1e4)
    assert rows["Binance"]["quote"] == "USDT" and rows["Binance"]["bid"] == 81370.0  # raw, as the venue quotes it
    assert rows["Binance"]["vs_coinbase_bp"] == pytest.approx((81370.1 * 0.99875 - 81266.215) / 81266.215 * 1e4)
    assert rows["Bybit"]["status"] == "not listed (BTCUSDT)" and rows["Bybit"]["vs_coinbase_bp"] is None
    assert rows["OKX"]["status"] == "no data within 0.2s (reconnecting in 1s (timeout))"
    assert rows["Quiet"]["status"] == "no data within 0.2s" and rows["Quiet"]["bid"] is None
    async with client() as c:
        with pytest.raises(LookupError, match="unknown pair"):
            await snapshot.venues(c, "ZZQ-USD", timeout=0.2)


def fake_perp(name, funding=None, oi=None):
    class Fake(PerpFeed):
        NAME = name

        async def run(self):
            if not self.pair:
                return await super().run()
            self._status("live")
            if funding is not None:
                self.mark, self.index, self.interval = 81300.0, 81340.0, 8
                self.set_funding(funding, 1_792_281_600_000)
            if oi:
                self.add_oi(oi)
            await asyncio.Event().wait()
    return Fake


async def test_perps(monkeypatch):
    monkeypatch.setattr(snapshot, "PERPS", (fake_perp("Binance", 0.0001, 94138.6), fake_perp("Bybit")))
    got = await snapshot.perps("btc-usd", timeout=0.2)
    assert got["perp"] == "BTCUSDT"
    binance, bybit = got["venues"]
    assert binance["funding_pct"] == pytest.approx(0.01) and binance["open_interest"] == 94138.6
    assert binance["next_funding"] == "2026-10-18T00:00:00+00:00" and binance["interval_h"] == 8
    assert binance["premium_bp"] == pytest.approx((81300 - 81340) / 81340 * 1e4) and binance["status"] == "live"
    assert bybit["status"] == "no data within 0.2s" and bybit["funding_pct"] is None
    none = await snapshot.perps("BTC-EUR", timeout=5)
    assert none["perp"] is None and [v["status"] for v in none["venues"]] == ["no USDT perp"] * 2


async def test_venue_quotes_are_from_the_moment_coinbase_answers(monkeypatch):
    class Moving(fake_venue("Kraken", False)):
        async def run(self):
            self._status("live")
            self.set_quote(81000.0, 81000.2)
            await asyncio.sleep(0.15)  # the market moves while Coinbase is being asked
            self.set_quote(81266.0, 81266.2)
            await asyncio.Event().wait()

    monkeypatch.setattr(snapshot, "VENUES", (Moving,))
    sync = client()

    async def slow(request):
        await asyncio.sleep(0.3 if request.url.path.endswith(("ticker", "stats")) else 0)
        return sync._transport.handle_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as c:
        got = await snapshot.venues(c, "BTC-USD", timeout=1)
    assert got["venues"][0]["bid"] == 81266.0  # not the quote from when the venues first answered
