"""Headless UI tests (Textual Pilot) with the network stubbed out."""

import asyncio
import json
import time

import pytest

from crypto_terminal import app as app_module
from crypto_terminal.app import TerminalApp
from crypto_terminal.book import BookFeed
from crypto_terminal.feed import Tick
from crypto_terminal.history import Candle, Product
from crypto_terminal.theme import GREEN, RED
from crypto_terminal.widgets import ChartPane, DepthPane, PairPicker, PriceTable

PRODUCTS = [Product(f"{base}-{quote}", base, quote, name) for base, name in
            (("BTC", "Bitcoin"), ("ETH", "Ethereum"), ("XRP", "XRP"), ("SOL", "Solana"), ("ADA", "Cardano"),
             ("DOGE", "Dogecoin")) for quote in ("BTC", "EUR", "USD") if base != quote]


DESKTOP = []  # desktop notifications sent


class FakeFeed:
    instances = []

    def __init__(self, symbols, on_tick, on_status):
        self.symbols, self.on_tick, self.on_status, self.last_msg = symbols, on_tick, on_status, 0.0
        FakeFeed.instances.append(self)

    async def run(self):
        self.last_msg = time.monotonic()
        self.on_status("live")
        await asyncio.Event().wait()


class FakeBookFeed(BookFeed):
    """The real book logic; only the connection is fake. Tests push messages with `handle`."""

    instances = []

    def __init__(self, symbol):
        super().__init__(symbol)
        FakeBookFeed.instances.append(self)

    async def run(self):
        self.last_msg = time.monotonic()
        self._status("live")
        await asyncio.Event().wait()


@pytest.fixture
def offline(tmp_path, monkeypatch):
    FakeFeed.instances, FakeBookFeed.instances = [], []
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(app_module, "WATCHLIST_FILE", path)
    monkeypatch.setattr(app_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "ALERTS_FILE", tmp_path / "alerts.json")
    monkeypatch.setattr(app_module, "desktop_notify", lambda title, message: DESKTOP.append(message))
    DESKTOP.clear()
    monkeypatch.setattr(app_module, "Feed", FakeFeed)
    monkeypatch.setattr(app_module, "BookFeed", FakeBookFeed)
    monkeypatch.setattr(app_module, "BOOK_DELAY", 0.05)

    async def fake_exists(client, symbol):  # only used when the product list failed to load
        return symbol == "DOGE-USD"

    async def fake_products(client):
        return PRODUCTS

    async def fake_candles(client, symbol, granularity):
        now = time.time()
        start = now - now % granularity - 9 * granularity
        return [Candle(start + i * granularity, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 3.0) for i in range(10)]

    monkeypatch.setattr(app_module, "product_exists", fake_exists)
    monkeypatch.setattr(app_module, "fetch_candles", fake_candles)
    monkeypatch.setattr(app_module, "fetch_products", fake_products)
    return path


async def type_command(pilot, text):
    await pilot.press("slash")
    await pilot.press(*text)
    await pilot.press("enter")
    await pilot.pause(0.2)


async def test_ticks_update_table(offline):
    app = TerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        feed = FakeFeed.instances[-1]
        assert feed.symbols == app_module.DEFAULT_WATCHLIST
        feed.on_tick(Tick("BTC-USD", 100.0, 90.0))
        feed.on_tick(Tick("BTC-USD", 101.0, 90.0))
        table = app.query_one(PriceTable)
        last = table.get_cell("BTC-USD", "last")
        assert last.plain == "101.00" and GREEN in str(last.style)
        assert table.get_cell("BTC-USD", "chg").plain == "▲ +12.22%"
        await pilot.pause(0.7)  # flash wears off, colour stays
        assert f"on {GREEN}" not in str(table.get_cell("BTC-USD", "last").style)


async def test_add_remove_persist(offline):
    app = TerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await type_command(pilot, "add DOGE-USD")
        assert app.symbols[-1] == "DOGE-USD"
        assert FakeFeed.instances[-1].symbols[-1] == "DOGE-USD"  # feed restarted with the new list
        await type_command(pilot, "rm ADA-USD")
        assert "ADA-USD" not in app.symbols
        assert app.query_one(PriceTable).row_count == 5
    saved = json.loads(offline.read_text())["symbols"]
    assert saved == ["BTC-USD", "ETH-USD", "XRP-USD", "SOL-USD", "DOGE-USD"]

    app = TerminalApp()  # restart: list comes back from disk
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.symbols == saved


async def test_bad_commands_notify_and_do_not_crash(offline):
    app = TerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        messages = set()  # collected as we go: toasts expire after a few seconds
        for text in ("add FAKE-USD", "add ../x", "add zzzz", "rm DOGE-USD", "launch rockets", "add BTC-USD"):
            await type_command(pilot, text)
            messages |= {n.message for n in app._notifications}
        assert app.symbols == app_module.DEFAULT_WATCHLIST
        assert "unknown pair: FAKE-USD" in messages
        assert any(m.startswith("not a pair: ../x") for m in messages)
        assert "no matches for ZZZZ" in messages
        assert "DOGE-USD is not on the watchlist" in messages
        assert not offline.exists()  # nothing changed, nothing written


async def test_keys_in_command_bar_do_not_trigger_bindings(offline):
    app = TerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("slash", "3")
        assert app.timeframe == "1"
        await pilot.press("escape")
        await pilot.press("3")
        assert app.timeframe == "3"


async def test_stale_status(offline):
    app = TerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.query_one("#status")
        assert "LIVE" in str(status.render())
        app.feed.last_msg = 0.0  # no frame for a long time
        app.refresh_status()
        assert "STALE" in str(status.render())


async def test_empty_watchlist_stops_feed(offline):
    offline.write_text('{"symbols": ["BTC-USD"]}')
    app = TerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await type_command(pilot, "rm BTC-USD")
        assert app.symbols == [] and app.feed is None
        assert "IDLE" in str(app.query_one("#status").render())


async def test_live_tick_moves_last_candle_of_selected_pair_only(offline):
    app = TerminalApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(0.3)
        chart = app.query_one(ChartPane)
        assert app.selected == "BTC-USD" and chart.symbol == "BTC-USD" and len(chart.candles) == 10
        feed = FakeFeed.instances[-1]
        feed.on_tick(Tick("ETH-USD", 500.0, 90.0))  # not the charted pair: ignored
        assert chart.candles[-1].h == 110.0
        feed.on_tick(Tick("BTC-USD", 120.0, 90.0))
        assert chart.candles[-1].c == 120.0 and chart.candles[-1].h == 120.0 and len(chart.candles) == 10
        await pilot.pause(0.6)  # redraw timer picks it up
        assert not chart._dirty


async def test_indicators_toggle_persist_and_draw_on_short_history(offline):
    app = TerminalApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(0.3)
        chart = app.query_one(ChartPane)
        await type_command(pilot, "ind sma20 ema50 vwap rsi")  # only 10 candles: fewer than 20/50
        assert chart.indicators == {"sma20", "ema50", "vwap", "rsi"}
        await pilot.pause(0.6)
        assert not chart._dirty  # redraw ran without raising
        await type_command(pilot, "ind ema50 rsi")  # toggles off
        assert chart.indicators == {"sma20", "vwap"}
        await type_command(pilot, "ind macd")
        assert "usage: ind" in " ".join(n.message for n in app._notifications)
        assert chart.indicators == {"sma20", "vwap"}
    assert json.loads((offline.parent / "config.json").read_text()) == {"indicators": ["sma20", "vwap"]}

    app = TerminalApp()  # restart: choice comes back from disk
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(ChartPane).indicators == {"sma20", "vwap"}
        await type_command(pilot, "ind off")
        assert app.query_one(ChartPane).indicators == set()


async def test_add_by_coin_name_opens_picker(offline):
    app = TerminalApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        await type_command(pilot, "add dogecoin")
        assert isinstance(app.screen, PairPicker)
        assert [p.id for p in app.screen.products] == ["DOGE-USD", "DOGE-EUR", "DOGE-BTC"]
        await pilot.press("down", "enter")  # second option
        await pilot.pause(0.2)
        assert not isinstance(app.screen, PairPicker)
        assert app.symbols[-1] == "DOGE-EUR"

        await type_command(pilot, "add sol")
        assert isinstance(app.screen, PairPicker)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, PairPicker) and app.symbols.count("SOL-USD") == 1


async def test_add_falls_back_to_rest_lookup_without_product_list(offline, monkeypatch):
    async def broken_products(client):
        raise app_module.httpx.ConnectError("offline")

    monkeypatch.setattr(app_module, "fetch_products", broken_products)
    app = TerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.products is None
        await type_command(pilot, "add DOGE-USD")
        assert app.symbols[-1] == "DOGE-USD"
        await type_command(pilot, "add solana")
        assert any(n.message.startswith("coin search unavailable") for n in app._notifications)


async def test_alert_fires_once_with_bell_toast_and_desktop_notification(offline, monkeypatch):
    app = TerminalApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        bells = []
        monkeypatch.setattr(app, "bell", lambda: bells.append(1))
        table, feed = app.query_one(PriceTable), FakeFeed.instances[-1]
        await type_command(pilot, "alert BTC-USD > 100")
        await type_command(pilot, "alert ETH-USD move 5%")  # no ETH price yet
        assert "no price for ETH-USD yet: try again in a moment" in [n.message for n in app._notifications]
        await type_command(pilot, "alert DOGE-USD < 1")  # not on the watchlist
        assert app.alerts == [app_module.Alert("BTC-USD", ">", 100)]
        assert table.get_cell("BTC-USD", "sym").plain == "BTC-USD 🔔"

        feed.on_tick(Tick("BTC-USD", 99.0, 90.0))
        assert app.alerts and not bells
        feed.on_tick(Tick("BTC-USD", 100.5, 90.0))
        feed.on_tick(Tick("BTC-USD", 101.0, 90.0))
        assert app.alerts == [] and bells == [1] and len(DESKTOP) == 1
        assert DESKTOP[0] == "BTC-USD > 100.00 · now 100.50"
        await pilot.pause()
        assert any(n.severity == "error" and n.message == DESKTOP[0] for n in app._notifications)
        assert table.get_cell("BTC-USD", "sym").plain == "BTC-USD"
        assert json.loads((offline.parent / "alerts.json").read_text()) == []


async def test_alerts_list_unalert_and_persist(offline):
    app = TerminalApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        FakeFeed.instances[-1].on_tick(Tick("ETH-USD", 2000.0, 1900.0))
        for text in ("alert BTC-USD > 90000", "alert ETH-USD move 5%", "alert BTC-USD < 80000"):
            await type_command(pilot, text)
        await type_command(pilot, "alerts")
        assert ("1. BTC-USD > 90,000.00\n2. ETH-USD moves ±5% from 2,000.00\n3. BTC-USD < 80,000.00"
                in [n.message for n in app._notifications])
        await type_command(pilot, "unalert 1")
        await type_command(pilot, "unalert 9")
        assert "no alert 9 (see `alerts`)" in [n.message for n in app._notifications]
    assert app.alerts == [app_module.Alert("ETH-USD", "move", 5, 2000.0), app_module.Alert("BTC-USD", "<", 80000)]

    app2 = TerminalApp()  # restart: alerts come back from disk, markers too
    async with app2.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        assert app2.alerts == app.alerts
        assert app2.query_one(PriceTable).get_cell("ETH-USD", "sym").plain == "ETH-USD 🔔"


async def test_bracket_keys_step_timeframe_and_clamp(offline):
    app = TerminalApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        await pilot.press("left_square_bracket")  # already the shortest
        assert app.timeframe == "1"
        await pilot.press("right_square_bracket", "right_square_bracket")
        assert app.timeframe == "3"
        await pilot.press("6", "right_square_bracket")  # already the longest
        assert app.timeframe == "6"
        await pilot.press("left_square_bracket")
        assert app.timeframe == "5"
        bar = app.query_one("#bar").render()
        assert str(bar).startswith(" BTC-USD │  1m  5m  15m  1h  6h  1d  │ zoom ×2")
        highlighted = [str(bar)[s.start:s.end] for s in bar.spans if "on" in str(s.style)]
        assert highlighted == [" 6h "]


async def test_zoom_redraws_without_fetching_and_f_cycles_views(offline, monkeypatch):
    fetches = []

    async def many_candles(client, symbol, granularity):
        fetches.append(granularity)
        now = time.time()
        start = now - now % granularity - 299 * granularity
        return [Candle(start + i * granularity, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 3.0) for i in range(300)]

    monkeypatch.setattr(app_module, "fetch_candles", many_candles)
    app = TerminalApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(0.3)
        chart, table = app.query_one(ChartPane), app.query_one(PriceTable)

        def visible():  # candles in view = solid volume cells on the bottom volume row
            row = chart.render().split("\n")[-2]
            return sum(s.end - s.start for s in row.spans if str(s.style).startswith("on ")) // max(1, chart.slot - 1)

        counts = {}
        for key, slot in (("plus", 4), ("equals_sign", 4), ("minus", 2), ("minus", 1), ("minus", 1)):
            await pilot.press(key)
            assert chart.slot == slot
            counts[slot] = visible()
        assert counts[4] < counts[2] < counts[1] and len(fetches) == 1
        assert "zoom ×1" in str(app.query_one("#bar").render())

        column = app.query_one("#chart-col")
        await pilot.press("f")  # chart only: the chart gets the whole width
        await pilot.pause()
        assert not table.display and column.display and visible() > counts[1]
        await pilot.press("f")  # watchlist only, stretched across the screen: the sparkline takes the spare width
        await pilot.pause()
        assert table.display and not column.display and table.outer_size.width == 150
        assert table.virtual_size.width == table.scrollable_content_region.width  # no blank columns at the side
        await pilot.press("f")  # back to split: the watchlist is only as wide as its columns
        await pilot.pause()
        widths = sum(c.get_render_width(table) for c in table.columns.values())
        assert table.display and column.display and table.outer_size.width == widths + 1  # + its border
        assert visible() == counts[1] and len(fetches) == 1


async def test_book_follows_selection_and_skips_pairs_scrolled_past(offline, monkeypatch):
    monkeypatch.setattr(app_module, "BOOK_DELAY", 0.5)
    app = TerminalApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(0.8)
        pane = app.query_one(DepthPane)
        assert [f.symbol for f in FakeBookFeed.instances] == ["BTC-USD"] and pane.feed is FakeBookFeed.instances[0]
        await pilot.press("down", "down", "down")  # scrolled past ETH and XRP within the delay
        assert pane.feed is None and pane.symbol == "SOL-USD"  # the old pair's book is gone at once
        await pilot.pause(0.8)
        assert [f.symbol for f in FakeBookFeed.instances] == ["BTC-USD", "SOL-USD"]
        assert pane.feed is FakeBookFeed.instances[-1]


async def test_b_and_watchlist_view_hide_the_book_and_close_its_connection(offline):
    app = TerminalApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(0.3)
        pane = app.query_one(DepthPane)
        await pilot.press("b")
        await pilot.pause(0.2)
        assert not pane.display and pane.feed is None
        assert not [w for w in app.workers if w.group == "book" and w.is_running]  # no hidden connection left open
        await pilot.press("b")
        await pilot.pause(0.2)
        assert pane.display and pane.feed is FakeBookFeed.instances[-1] and len(FakeBookFeed.instances) == 2
        await pilot.press("f")  # chart only: same pair, same connection
        await pilot.pause(0.2)
        assert pane.display and len(FakeBookFeed.instances) == 2
        await pilot.press("f")  # watchlist only
        await pilot.pause(0.2)
        assert not pane.display and pane.feed is None
        await pilot.press("f")  # back to split: the book comes back
        await pilot.pause(0.2)
        assert pane.display and len(FakeBookFeed.instances) == 3


async def test_book_pane_draws_levels_spread_depth_and_tape(offline):
    app = TerminalApp()
    async with app.run_test(size=(150, 30)) as pilot:
        await pilot.pause(0.3)
        pane, feed = app.query_one(DepthPane), FakeBookFeed.instances[-1]
        assert pane.render().plain.splitlines()[1] == "loading book…"
        feed.handle({"type": "snapshot", "product_id": "BTC-USD", "bids": [["100.04", "1.5"], ["99.97", "0.5"]],
                     "asks": [["100.11", "0.5"], ["100.12", "1"], ["100.19", "0.00000001"]]})
        for trade_id, side, size in ((6, "sell", "0.25"), (7, "sell", "0.5"), (9, "buy", "0.25")):
            feed.handle({"type": "match", "trade_id": trade_id, "side": side, "price": "100.11", "size": size,
                         "product_id": "BTC-USD", "time": "2026-10-09T23:13:41.322560Z"})

        def screen():
            lines = pane.render().split("\n")
            return lines, [line.plain for line in lines]

        lines, plain = screen()
        assert plain[0] == "BTC-USD book · by 0.01"  # ~$100 pair: 1 bp is one cent, so it opens ungrouped
        middle = plain.index(next(p for p in plain if "spread" in p))
        assert plain[middle - 3:middle + 3] == [
            "          100.19        0.00000001",  # 1e-8 still shows: never rounded to 0
            "          100.12        1.00000000",
            "          100.11        0.50000000",  # best ask right above the spread
            "─────── spread 0.07 · 7 bp ───────",
            "          100.04        1.50000000",
            "           99.97        0.50000000"]
        deepest_bid = lines[middle + 2]  # 2.0 total on the bid side is the deepest: its bar spans the whole row
        assert any(s.start == 0 and s.end == pane.size.width and "on " in str(s.style) for s in deepest_bid.spans)

        tape = plain[plain.index(next(p for p in plain if p.startswith("trades"))):]
        assert tape[0] == "trades  last 1m: 75% buys"  # 0.75 of 1.0 bought (resting sells hit)
        assert tape[2:5] == ["23:13:41     100.11  0.25000000   ",  # newest first: resting buy hit = a sale
                             "············ 1 missed ············",  # trade 8 never arrived
                             "23:13:41     100.11  0.75000000 ×2"]  # 6 and 7: same second, side and price
        assert RED in str(lines[plain.index(tape[2])].spans[-1].style)

        await pilot.press("g")  # group by 0.05: asks round up, bids round down, sizes add up
        lines, plain = screen()
        middle = plain.index(next(p for p in plain if "spread" in p))
        assert plain[0] == "BTC-USD book · by 0.05"
        assert plain[middle - 2:middle + 3] == [
            "          100.20        0.00000001",
            "          100.15        1.50000000",
            "─────── spread 0.07 · 7 bp ───────",  # the real spread, not the grouped one
            "          100.00        1.50000000",
            "           99.95        0.50000000"]
        for _ in range(3):
            await pilot.press("g")  # 0.10, 0.50, then back to ungrouped
        assert pane.render().plain.splitlines()[0] == "BTC-USD book · by 0.01"
        await pilot.press("g", "down")
        await pilot.pause(0.3)
        assert pane.group is None  # the next pair opens at its own default

        feed = FakeBookFeed.instances[-1]
        feed.last_msg = 0.0  # no frame for a long time: the book is hidden, not shown stale
        assert pane.render().plain.splitlines()[1].startswith("STALE: no data for")
        feed._status("reconnecting in 2s (boom)")
        assert pane.render().plain.splitlines()[1] == "reconnecting in 2s (boom)"


async def test_narrow_terminal_starts_with_the_book_hidden(offline):
    app = TerminalApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause(0.3)
        pane = app.query_one(DepthPane)
        assert not pane.display and not FakeBookFeed.instances
        await pilot.press("b")
        await pilot.pause(0.3)
        assert pane.display and [f.symbol for f in FakeBookFeed.instances] == ["BTC-USD"]


async def test_watchlist_fits_its_content_and_sparkline_fills_when_alone(offline):
    app = TerminalApp()
    async with app.run_test(size=(150, 30)) as pilot:
        await pilot.pause(0.3)
        table, feed = app.query_one(PriceTable), FakeFeed.instances[-1]
        before = table.outer_size.width
        for i in range(60):
            feed.on_tick(Tick("BTC-USD", 100_000.0 + i, 90_000.0))
        await pilot.pause(0.2)
        assert table.outer_size.width > before  # "100,059.00" is wider than the placeholder: the table grew to fit
        assert len(table.get_cell("BTC-USD", "spark").plain) == 20
        await pilot.press("f", "f")  # watchlist only
        await pilot.pause(0.2)
        spark = table.get_cell("BTC-USD", "spark").plain
        assert len(spark) > 20 and spark.rstrip() == spark[:60]  # all 60 ticks shown, then room for more
        assert table.virtual_size.width == table.scrollable_content_region.width


async def test_count_column_grows_so_counts_are_never_cut(offline):
    app = TerminalApp()
    async with app.run_test(size=(150, 30)) as pilot:
        await pilot.pause(0.3)
        pane, feed = app.query_one(DepthPane), FakeBookFeed.instances[-1]
        feed.handle({"type": "snapshot", "product_id": "BTC-USD", "bids": [["100.00", "1"]], "asks": [["100.10", "1"]]})
        for i in range(295):
            feed.handle({"type": "match", "trade_id": i, "side": "sell", "price": "100.10", "size": "0.25",
                         "product_id": "BTC-USD", "time": "2026-10-09T23:13:41.322560Z"})
        line = next(line for line in pane.render().split("\n") if "×" in line.plain)
        assert line.plain.endswith("×295") and line.cell_len == pane.size.width
