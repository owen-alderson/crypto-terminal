"""Reconnect behaviour against a local websocket server (no internet needed)."""

import asyncio
import json

from websockets.asyncio.server import serve

from crypto_terminal.feed import Feed

TICK = {"type": "ticker", "product_id": "BTC-USD", "price": "100", "open_24h": "90"}


async def run_until(feed: Feed, ticks: list, n: int, timeout: float = 10):
    task = asyncio.create_task(feed.run())
    try:
        async with asyncio.timeout(timeout):
            while len(ticks) < n:
                await asyncio.sleep(0.05)
    finally:
        task.cancel()


async def test_reconnects_after_server_drops_connection():
    async def handler(ws):
        sub = json.loads(await ws.recv())
        assert sub["type"] == "subscribe" and sub["product_ids"] == ["BTC-USD"]
        for junk in ("garbage", "[1, 2]", b"\xff"):  # malformed frames are ignored, not fatal
            await ws.send(junk)
        await ws.send(json.dumps(TICK))
        # returning closes the connection

    ticks, statuses = [], []
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        feed = Feed(["BTC-USD"], ticks.append, statuses.append, url=f"ws://127.0.0.1:{port}")
        await run_until(feed, ticks, 2)
    assert [t.price for t in ticks] == [100.0, 100.0]
    assert statuses[:2] == ["connecting", "live"]
    assert any(s.startswith("reconnecting in 1s") for s in statuses)


async def test_silent_connection_is_treated_as_dead():
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        await ws.recv()
        if connections == 1:
            await ws.wait_closed()  # half-open: socket stays up but nothing arrives
        await ws.send(json.dumps(TICK))
        await ws.wait_closed()

    ticks, statuses = [], []
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        feed = Feed(["BTC-USD"], ticks.append, statuses.append, url=f"ws://127.0.0.1:{port}",
                    reconnect_after=0.5)
        await run_until(feed, ticks, 1)
    assert connections == 2
    assert any("TimeoutError" in s for s in statuses)


async def test_server_error_triggers_reconnect_with_reason():
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "error", "message": "Failed to subscribe",
                                  "reason": "NOPE-USD is not a valid product"}))
        await ws.wait_closed()

    statuses = []
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        feed = Feed(["NOPE-USD"], lambda t: None, statuses.append, url=f"ws://127.0.0.1:{port}")
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.5)
        task.cancel()
    assert "reconnecting in 1s (NOPE-USD is not a valid product)" in statuses
    assert "live" not in statuses


async def test_trades_stream_only_for_trade_alert_pairs_and_skip_the_replayed_last_match():
    def match(kind, symbol, trade_id):
        return {"type": kind, "trade_id": trade_id, "taker_order_id": "t1", "side": "sell", "size": "0.5",
                "price": "100", "product_id": symbol, "time": "2026-10-13T16:47:27.671220Z"}

    subs = []

    async def handler(ws):
        subs.append(json.loads(await ws.recv()))
        for msg in (match("last_match", "BTC-USD", 1), match("match", "ETH-USD", 7), match("match", "BTC-USD", 2), TICK):
            await ws.send(json.dumps(msg))
        await ws.wait_closed()

    ticks, trades = [], []
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        feed = Feed(["BTC-USD", "ETH-USD"], ticks.append, url=f"ws://127.0.0.1:{port}",
                    trade_symbols=["BTC-USD"], on_trade=lambda symbol, trade: trades.append((symbol, trade)))
        await run_until(feed, ticks, 1)
    assert subs[0]["channels"] == ["ticker", "heartbeat", {"name": "matches", "product_ids": ["BTC-USD"]}]
    assert [(symbol, t.id, t.taker, t.buy) for symbol, t in trades] == [("BTC-USD", 2, "t1", True)]
    assert Feed(["BTC-USD"], ticks.append).channels() == ["ticker", "heartbeat"]  # no trade alerts: no trades
