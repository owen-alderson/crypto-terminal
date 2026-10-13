"""Other exchanges: message parsing from captured live frames, symbol mapping, USDT conversion."""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from crypto_terminal.feed import Unlisted
from crypto_terminal.venues import OKX, Binance, Bybit, Kraken, rate_symbol, vs_coinbase


@pytest.mark.parametrize("venue,symbol,pair,quote", [
    (Kraken, "BTC-USD", "BTC/USD", "USD"), (Kraken, "ETH-EUR", "ETH/EUR", "EUR"),
    (Binance, "BTC-USD", "BTCUSDT", "USDT"), (Binance, "ETH-BTC", "ETHBTC", "BTC"),
    (Bybit, "SOL-USD", "SOLUSDT", "USDT"), (OKX, "BTC-USD", "BTC-USDT", "USDT"), (OKX, "BTC-EUR", "BTC-EUR", "EUR"),
    (Binance, "USDT-USD", "", "USDT"),  # would be USDTUSDT: not a pair
])
def test_symbol_mapping(venue, symbol, pair, quote):
    feed = venue(symbol)
    assert (feed.pair, feed.quote) == (pair, quote)


def test_kraken():
    feed = Kraken("BTC-USD")
    assert feed.handle({"method": "subscribe", "success": True, "result": {"symbol": "BTC/USD"}})
    assert feed.handle({"channel": "heartbeat"})
    assert feed.handle({"channel": "ticker", "type": "snapshot", "data": [
        {"symbol": "BTC/USD", "bid": 83186.3, "bid_qty": 0.18, "ask": 83186.4, "ask_qty": 0.72, "last": 83186.3}]})
    assert (feed.bid, feed.ask) == (83186.3, 83186.4)
    with pytest.raises(Unlisted):
        feed.handle({"error": "Currency pair not supported ZZQ/USD", "method": "subscribe", "success": False})
    with pytest.raises(RuntimeError):
        feed.handle({"error": "Exceeded msg rate", "method": "subscribe", "success": False})


def test_binance():
    feed = Binance("BTC-USD")
    assert feed.subscriptions() == [{"method": "SUBSCRIBE", "params": ["btcusdt@bookTicker"], "id": 1}]
    assert feed.handle({"result": None, "id": 1})
    assert feed.handle({"u": 1, "s": "BTCUSDT", "b": "83279.34000000", "B": "5.03", "a": "83279.35000000", "A": "3.39"})
    assert (feed.bid, feed.ask) == (83279.34, 83279.35)
    assert not feed.handle({"u": 2, "s": "BTCUSDT", "b": "NaN", "a": "1"})  # malformed: ignored
    assert not feed.handle({"u": 3, "s": "BTCUSDT", "b": True, "a": "1"})
    assert (feed.bid, feed.ask) == (83279.34, 83279.35)


def test_bybit_snapshot_delta_and_unlisted():
    feed = Bybit("BTC-USD")
    assert feed.handle({"success": True, "ret_msg": "subscribe", "op": "subscribe"})
    topic = "orderbook.1.BTCUSDT"
    assert feed.handle({"topic": topic, "type": "snapshot", "data": {"s": "BTCUSDT", "b": [["83276", "0.73"]],
                                                                    "a": [["83276.1", "1.39"]]}})
    assert (feed.bid, feed.ask) == (83276.0, 83276.1)
    assert feed.handle({"topic": topic, "type": "delta", "data": {"s": "BTCUSDT", "b": [["83276.05", "1"]], "a": []}})
    assert (feed.bid, feed.ask) == (83276.05, 83276.1)  # only the bid changed
    assert feed.handle({"success": True, "ret_msg": "pong", "op": "ping"})
    assert json.loads(Bybit.KEEPALIVE) == {"op": "ping"}
    with pytest.raises(Unlisted):
        feed.handle({"success": False, "ret_msg": "Invalid symbol :[orderbook.1.ZZQUSDT]", "op": "subscribe"})


def test_okx():
    feed = OKX("BTC-USD")
    assert feed.handle({"event": "subscribe", "arg": {"channel": "bbo-tbt", "instId": "BTC-USDT"}})
    assert feed.handle({"arg": {"channel": "bbo-tbt", "instId": "BTC-USDT"}, "data": [
        {"asks": [["83276.9", "0.57", "0", "9"]], "bids": [["83276.8", "0.92", "0", "14"]], "ts": "1"}]})
    assert (feed.bid, feed.ask) == (83276.8, 83276.9)
    with pytest.raises(Unlisted):
        feed.handle({"event": "error", "msg": "Subscribe failed ... doesn't exist", "code": "60018"})


def test_crossed_quotes_and_dropped_connections_never_show():
    feed = Kraken("BTC-USD")
    feed.set_quote(100.0, 101.0)
    feed.set_quote(102.0, 101.0)  # crossed: kept the last good one
    assert (feed.bid, feed.ask) == (100.0, 101.0)
    feed.on_status("reconnecting in 1s (closed)")
    assert (feed.bid, feed.ask) == (None, None)


def test_vs_coinbase_converts_usdt_and_never_guesses_the_rate():
    binance, kraken = Binance("BTC-USD"), Kraken("BTC-USD")
    binance.set_quote(83200.00, 83200.01)
    kraken.set_quote(83105.50, 83105.60)
    coinbase_mid = 83101.565
    assert vs_coinbase(binance, coinbase_mid, 0.99896, "USD") == pytest.approx(1.43, abs=0.01)
    assert vs_coinbase(binance, coinbase_mid, None, "USD") is None  # unconverted it would read +11.9 bp
    assert vs_coinbase(kraken, coinbase_mid, None, "USD") == pytest.approx(0.48, abs=0.01)  # USD: no rate needed
    assert vs_coinbase(kraken, None, 1.0, "USD") is None
    assert rate_symbol("BTC-USD") == "USDT-USD" and rate_symbol("BTC-EUR") is None and rate_symbol("USDT-USD") is None


async def test_unlisted_pair_stops_the_feed_and_keepalive_pings():
    received = []

    async def handler(ws):
        received.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"success": True, "ret_msg": "subscribe", "op": "subscribe"}))
        received.append(await ws.recv())  # the keepalive
        await ws.send(json.dumps({"success": False, "ret_msg": "Invalid symbol :[orderbook.1.BTCUSDT]",
                                  "op": "subscribe"}))
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        feed = Bybit("BTC-USD", url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        feed.KEEPALIVE_EVERY = 0.05
        async with asyncio.timeout(5):
            await feed.run()  # returns: no retrying a pair the exchange doesn't list
    assert received == [{"op": "subscribe", "args": ["orderbook.1.BTCUSDT"]}, '{"op": "ping"}']
    assert feed.status == "not listed (BTCUSDT)"
