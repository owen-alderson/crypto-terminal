"""Order book + trade tape: parsing, integrity, and the feed against a local websocket server."""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from crypto_terminal.book import (BookFeed, OrderBook, Trade, big_threshold, default_step, flow, group_steps, parse_trade,
                                  prints, step_decimals)
from crypto_terminal.widgets import fmt_sizes

SNAPSHOT = {"type": "snapshot", "product_id": "BTC-USD",
            "bids": [["100.00", "1.5"], ["99.50", "2"], ["99.00", "0.25"]],
            "asks": [["100.01", "0.5"], ["100.50", "3"]]}


def match(trade_id, side="sell", price="100.01", size="0.1", kind="match"):
    return {"type": kind, "trade_id": trade_id, "side": side, "price": price, "size": size,
            "product_id": "BTC-USD", "time": "2026-10-09T23:13:41.322560Z"}


def test_book_snapshot_updates_and_top():
    book = OrderBook()
    book.load(SNAPSHOT)
    book.update([["buy", "100.00", "0"], ["buy", "100.005", "4"], ["sell", "100.50", "1"]])
    asks, bids = book.top(2)
    assert asks == [(100.01, 0.5), (100.5, 1.0)]  # lowest ask first
    assert bids == [(100.005, 4.0), (99.5, 2.0)]  # highest bid first; size 0 removed 100.00
    assert book.dp == 3  # widest price Coinbase sent


@pytest.mark.parametrize("change", [["buy", "100", "NaN"], ["buy", "-1", "2"], ["hold", "100", "1"],
                                    ["buy", 100, "1"], ["buy", "100"], "junk", None])
def test_malformed_change_raises_and_changes_nothing(change):
    book = OrderBook()
    book.load(SNAPSHOT)
    before = (dict(book.bids), dict(book.asks))
    with pytest.raises(ValueError):
        book.update([["sell", "100.02", "9"], change])
    assert (book.bids, book.asks) == before


@pytest.mark.parametrize("bad", [{"bids": [["100", "1"]]}, {"bids": [], "asks": [["100", "0"]]},
                                 {"bids": [["100", "x"]], "asks": []}])
def test_malformed_snapshot_raises(bad):
    book = OrderBook()
    with pytest.raises(ValueError):
        book.load({"type": "snapshot", **bad})
    assert not book.ready


def test_trade_side_is_the_takers():
    hit_the_ask = parse_trade(match(1, side="sell"))  # resting sell order filled: a buyer crossed the spread
    assert hit_the_ask.buy and hit_the_ask.time == "23:13:41" and hit_the_ask.price == 100.01
    assert not parse_trade(match(2, side="buy")).buy
    assert parse_trade(match(3, kind="last_match")).id == 3


@pytest.mark.parametrize("bad", [{"trade_id": "7"}, {"side": "up"}, {"price": "inf"}, {"size": "0"},
                                 {"time": "yesterday"}, {"price": None}])
def test_malformed_trade_is_dropped(bad):
    assert parse_trade({**match(1), **bad}) is None


def test_book_feed_resets_when_not_live_and_ignores_early_updates():
    feed = BookFeed("BTC-USD")
    feed._status("live")
    assert feed.handle({"type": "l2update", "product_id": "BTC-USD", "changes": [["buy", "1", "1"]]})
    assert not feed.book.ready and not feed.book.bids  # nothing to apply it to yet
    assert not feed.handle({**SNAPSHOT, "product_id": "ETH-USD"})  # another pair's data is ignored
    feed.handle(SNAPSHOT)
    assert feed.book.ready
    feed._status("reconnecting in 1s (boom)")
    assert not feed.book.ready and feed.status == "reconnecting in 1s (boom)"


def test_tape_skips_repeats_and_marks_missed_trades():
    feed = BookFeed("BTC-USD")
    feed.handle(match(10, kind="last_match"))
    feed.handle(match(11))
    feed.handle(match(11, kind="last_match"))  # repeated after a reconnect
    feed.handle({**match(12), "price": "bad"})  # malformed: dropped, so 13 shows one missed
    feed.handle(match(13))
    feed.handle(match(20))
    assert [(t.id, t.missed) for t in feed.trades] == [(20, 6), (13, 1), (11, 0), (10, 0)]


def test_fmt_sizes_aligns_and_never_shows_zero():
    assert fmt_sizes([1.5, 0.00000039], 12) == ["1.50000000", "0.00000039"]
    assert fmt_sizes([20753.447505, 0.00004], 12) == ["20,753.4475", "<0.0001"]
    assert fmt_sizes([12345678.9, 0.2], 10) == ["12,345,679", "<1"]


async def test_book_feed_rebuilds_from_a_fresh_snapshot_after_a_malformed_update():
    big = {**SNAPSHOT, "bids": [[f"{50 + i / 1000:.3f}", "1"] for i in range(40_000)]}  # ~1.2 MB frame
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        sub = json.loads(await ws.recv())
        assert sub["product_ids"] == ["BTC-USD"] and "level2_batch" in sub["channels"] and "matches" in sub["channels"]
        await ws.send(json.dumps(big if connections == 1 else SNAPSHOT))
        if connections == 1:
            await ws.send(json.dumps({"type": "l2update", "product_id": "BTC-USD", "changes": [["buy", "x", "1"]]}))
        await ws.wait_closed()

    statuses = []
    async with serve(handler, "127.0.0.1", 0, max_size=None) as server:
        port = server.sockets[0].getsockname()[1]
        feed = BookFeed("BTC-USD", statuses.append, url=f"ws://127.0.0.1:{port}")
        task = asyncio.create_task(feed.run())
        try:
            async with asyncio.timeout(10):
                while connections < 2 or not feed.book.ready:
                    await asyncio.sleep(0.05)
        finally:
            task.cancel()
    assert any("malformed l2update change" in s for s in statuses)
    assert len(feed.book.bids) == 3  # the second connection's snapshot, not the big one


def test_grouping_never_shows_a_better_price_than_exists():
    book = OrderBook()
    book.load({"type": "snapshot", "bids": [["82617.99", "1"], ["82617.00", "2"], ["82610.00", "0.5"], ["82609.99", "4"]],
               "asks": [["82618.00", "1"], ["82620.00", "2"], ["82620.01", "3"], ["82700.00", "5"]]})
    asks, bids = book.top(2, 1000)  # $10 buckets (1000 ticks of $0.01)
    assert asks == [(82620.0, 3.0), (82630.0, 3.0)]  # 82,618.00 rounds up to 82,620; 82,620.01 up to 82,630
    assert bids == [(82610.0, 3.5), (82600.0, 4.0)]  # 82,617.99 rounds down to 82,610
    assert book.top(3, 1000)[0][2] == (82700.0, 5.0)  # sparse: the 3rd non-empty bucket is far out, still found
    assert book.best() == (82617.99, 82618.0)


def test_group_steps_and_defaults():
    btc = group_steps(82_600, 2)
    assert [s / 100 for s in btc] == [0.01, 0.05, 0.1, 0.5, 1, 5, 10, 50, 100]  # up to 0.5% of the price
    assert default_step(btc, 82_600, 2) == 1000  # $10 ≈ 1 bp
    xrp = group_steps(1.39, 4)
    assert xrp == [1, 5, 10, 50] and default_step(xrp, 1.39, 4) == 1  # 1 bp is ~one tick: ungrouped
    assert [step_decimals(s, 2) for s in (1, 5, 50, 1000)] == [2, 2, 1, 0]


def trade(id, time="23:13:41", price=100.0, size=1.0, buy=True, missed=0, at=0.0):
    return Trade(id, time, price, size, buy, 2, missed, at)


def test_prints_combine_same_second_side_and_price_but_never_across_a_gap():
    tape = [trade(9), trade(8), trade(7, missed=2), trade(4), trade(3, buy=False), trade(2, price=99.0),
            trade(1, time="23:13:40")]
    assert [(t.id, t.count, t.size, t.missed) for t in prints(tape)] == [
        (9, 3, 3.0, 2),  # 9, 8, 7 combined; the gap before 7 stays under the line
        (4, 1, 1.0, 0), (3, 1, 1.0, 0), (2, 1, 1.0, 0), (1, 1, 1.0, 0)]


def test_flow_counts_only_the_last_minute():
    tape = [trade(3, size=3, at=100), trade(2, size=1, buy=False, at=50), trade(1, size=9, at=30)]
    assert flow(tape, now=100) == (3.0, 1.0)  # trade 1 is 70s old


def test_big_threshold_is_the_largest_two_percent():
    lines = [trade(i, size=1.0) for i in range(98)] + [trade(98, size=50), trade(99, size=60)]
    cutoff = big_threshold(lines)
    assert [t.id for t in lines if t.price * t.size > cutoff] == [98, 99]  # not the 98 identical small ones
    assert big_threshold(lines[:10]) == float("inf")  # too few trades to judge
