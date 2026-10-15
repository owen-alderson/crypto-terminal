"""Perpetual futures: message parsing from captured live frames (2026-10-15), open interest, liquidations."""

import httpx
import pytest

from crypto_terminal import derivs
from crypto_terminal.derivs import OI_WINDOW, BinancePerp, BybitPerp, Liquidation, perp_symbol
from crypto_terminal.feed import Unlisted
from crypto_terminal.widgets import fmt_amount, fmt_until

BINANCE_MARK = {"e": "markPriceUpdate", "E": 1792074305000, "s": "BTCUSDT", "p": "81206.30000000",
                "ap": "81206.30000000", "P": "80802.16954988", "i": "81236.81043478", "r": "-0.00000753",
                "T": 1792080000000, "st": 1}
BINANCE_LIQ = {"e": "forceOrder", "E": 1792074622230, "o": {
    "s": "BTCUSDT", "S": "SELL", "o": "LIMIT", "f": "IOC", "q": "0.136", "p": "80744.00", "ap": "81055.99",
    "X": "FILLED", "l": "0.067", "z": "0.136", "T": 1792074621218, "ps": "BTCUSDT", "st": 1}}
BYBIT_TICKER = {"topic": "tickers.BTCUSDT", "type": "snapshot", "data": {
    "symbol": "BTCUSDT", "lastPrice": "81193.80", "markPrice": "81205.60", "indexPrice": "81232.92",
    "openInterest": "60661.283", "openInterestValue": "4926035882.78", "fundingIntervalHour": "8",
    "singleOpenInterest": "30330.642", "singleOpenInterestValue": "2463017982.00", "nextFundingTime": "1792080000000",
    "fundingRate": "0.00004385", "bid1Price": "81193.00", "ask1Price": "81193.10"}, "cs": 826419469534,
    "ts": 1792074302229}
# price falling: Bybit's "Buy" with a bankruptcy price below the market is a closed long
BYBIT_LIQ = {"topic": "allLiquidation.ETHUSDT", "type": "snapshot", "ts": 1792074622814,
             "data": [{"T": 1792074622444, "s": "ETHUSDT", "S": "Buy", "v": "0.02", "p": "2358.95"}]}


@pytest.mark.parametrize("symbol,pair", [
    ("BTC-USD", "BTCUSDT"), ("ETH-USDT", "ETHUSDT"), ("BTC-EUR", ""), ("ETH-BTC", ""), ("USDT-USD", ""),
])
def test_perp_symbol(symbol, pair):
    assert perp_symbol(symbol) == pair


def test_binance_mark_price_funding_and_premium():
    rates = []
    feed = BinancePerp("BTC-USD", on_funding=rates.append)
    assert feed.subscriptions() == [{"method": "SUBSCRIBE", "params": ["btcusdt@markPrice@1s", "btcusdt@forceOrder"],
                                     "id": 1}]
    assert feed.URL == "wss://fstream.binance.com/market/ws"  # the old /ws subscribes, then sends nothing
    assert feed.handle({"result": None, "id": 1})
    assert feed.handle(BINANCE_MARK)
    assert (feed.mark, feed.index, feed.funding, feed.next_funding) == (81206.3, 81236.81043478, -0.00000753,
                                                                        1792080000.0)
    assert rates == [-0.00000753]
    assert feed.premium == pytest.approx(-3.756, abs=0.001)
    assert not feed.handle({**BINANCE_MARK, "s": "ETHUSDT"})  # another pair's frame
    assert not feed.handle({**BINANCE_MARK, "r": "NaN"})
    assert not feed.handle({**BINANCE_MARK, "p": "0"})
    assert feed.funding == -0.00000753  # the last good numbers stay


def test_binance_liquidation_counts_finished_orders_once():
    got = []
    feed = BinancePerp("BTC-USD", on_liq=got.append)
    assert feed.handle(BINANCE_LIQ)
    assert got == [Liquidation("Binance", 1792074621.218, True, 0.136, 81055.99)]  # SELL closes a long
    assert got[0].time == "14:30:21" and got[0].notional == pytest.approx(11023.61)
    assert feed.handle({**BINANCE_LIQ, "o": {**BINANCE_LIQ["o"], "X": "PARTIALLY_FILLED"}})  # not done yet
    assert feed.handle({**BINANCE_LIQ, "o": {**BINANCE_LIQ["o"], "X": "EXPIRED", "S": "BUY", "z": "0.05"}})
    assert [(liq.long, liq.size) for liq in got] == [(True, 0.136), (False, 0.05)]
    assert not feed.handle({**BINANCE_LIQ, "o": {**BINANCE_LIQ["o"], "s": "ETHUSDT"}})
    assert not feed.handle({**BINANCE_LIQ, "o": {**BINANCE_LIQ["o"], "ap": "0"}})  # nothing filled: no price
    assert list(feed.liqs) == got[::-1]  # newest first


def test_bybit_uses_single_sided_open_interest_and_merges_deltas():
    feed = BybitPerp("BTC-USD")
    assert feed.handle({"success": True, "ret_msg": "", "op": "subscribe"})
    assert feed.handle(BYBIT_TICKER)
    # `openInterest` (60,661) counts both sides since Bybit's June 2026 change; Binance's basis is the single side
    assert (feed.oi, feed.funding, feed.interval, feed.mark) == (30330.642, 0.00004385, 8, 81205.6)
    assert feed.handle({"topic": "tickers.BTCUSDT", "type": "delta",
                        "data": {"symbol": "BTCUSDT", "indexPrice": "81273.94", "fundingRate": "0.0001"}})
    assert (feed.index, feed.funding, feed.mark) == (81273.94, 0.0001, 81205.6)  # unchanged fields kept
    old = BybitPerp("BTC-USD")
    old_style = {k: v for k, v in BYBIT_TICKER["data"].items() if not k.startswith("single")}
    assert old.handle({**BYBIT_TICKER, "data": old_style})
    assert old.oi is None  # never the double-counted number


def test_bybit_liquidation_side_and_unlisted():
    got = []
    feed = BybitPerp("ETH-USD", on_liq=got.append)
    assert feed.subscriptions() == [{"op": "subscribe", "args": ["tickers.ETHUSDT", "allLiquidation.ETHUSDT"]}]
    assert feed.handle(BYBIT_LIQ)
    assert got == [Liquidation("Bybit", 1792074622.444, True, 0.02, 2358.95)]  # "Buy" = a long was liquidated
    sell = {**BYBIT_LIQ, "data": [{**BYBIT_LIQ["data"][0], "S": "Sell"}]}
    assert feed.handle(sell) and not got[-1].long
    assert feed.handle({"success": True, "ret_msg": "pong", "op": "ping"})
    with pytest.raises(Unlisted):
        feed.handle({"success": False, "ret_msg": "error:handler not found,topic:tickers.NOPEUSDT", "op": "subscribe"})
    with pytest.raises(RuntimeError):
        feed.handle({"success": False, "ret_msg": "too many requests", "op": "subscribe"})


def test_dropped_connection_clears_everything_from_it():
    rates = []
    feed = BybitPerp("BTC-USD", on_funding=rates.append)
    feed.handle(BYBIT_TICKER)
    feed.on_status("reconnecting in 1s (closed)")
    assert (feed.funding, feed.mark, feed.index, feed.oi, feed.premium) == (None,) * 5
    assert rates == [0.00004385, None] and feed.status == "reconnecting in 1s (closed)"


def test_open_interest_change_over_fifteen_minutes():
    feed = BinancePerp("BTC-USD")
    feed.add_oi(1000.0, now=0)
    feed.add_oi(1010.0, now=30)  # within the minute: not a new sample
    assert len(feed.oi_samples) == 1 and feed.oi_change(now=30) is None
    for minute in range(1, 15):
        feed.add_oi(1000.0 + minute, now=minute * 60)
    assert feed.oi_change(now=14 * 60) is None  # not 15 minutes of samples yet
    feed.add_oi(1050.0, now=OI_WINDOW)
    assert feed.oi_change(now=OI_WINDOW) == pytest.approx(5.0)  # against the sample 15 minutes old
    feed.add_oi(1050.0, now=OI_WINDOW + 60)
    assert feed.oi_samples[0] == (60, 1001.0) and feed.oi_change(now=OI_WINDOW + 60) == pytest.approx(4.895, abs=1e-3)


def rest(handler):
    original = httpx.AsyncClient
    return lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)


async def test_binance_unlisted_pair_is_found_by_rest(monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

    monkeypatch.setattr(derivs.httpx, "AsyncClient", rest(handler))
    statuses = []
    feed = BinancePerp("ZZQ-USD", on_status=statuses.append)
    await feed.run()  # returns instead of reconnecting forever
    assert statuses == ["not listed (ZZQUSDT)"]


async def test_binance_rest_open_interest_and_funding_interval():
    def handler(request):
        if request.url.path.endswith("/openInterest"):
            return httpx.Response(200, json={"symbol": "BTCUSDT", "openInterest": "93472.108", "time": 1})
        return httpx.Response(200, json=[{"symbol": "GTCUSDT", "fundingIntervalHours": 4}])

    feed = BinancePerp("BTC-USD")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await feed.poll_oi(client)
        assert feed.oi == 93472.108
        assert await feed.fetch_interval(client) == 8  # not listed there: the default
        feed.pair = "GTCUSDT"
        assert await feed.fetch_interval(client) == 4


async def test_no_perp_for_other_quotes():
    statuses = []
    await BybitPerp("BTC-EUR", on_status=statuses.append).run()
    await BinancePerp("BTC-EUR", on_status=statuses.append).run()
    assert statuses == ["no USDT perp"] * 2


@pytest.mark.parametrize("value,text", [(93472.108, "93.47k"), (4369711716, "4.370b"), (812.34, "812.3"),
                                        (1_250_000, "1.250m"), (0.5, "0.5000")])
def test_fmt_amount(value, text):
    assert fmt_amount(value) == text


def test_fmt_until():
    assert (fmt_until(5532), fmt_until(75), fmt_until(-3)) == ("1h32m", "1m15s", "0m00s")
