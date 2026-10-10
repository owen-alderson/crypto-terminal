import asyncio
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

from .alerts import KINDS, Alert, desktop_notify, valid_alert, valid_level
from .alerts import check as check_alerts
from .book import BookFeed
from .feed import STALE_AFTER, Feed, Tick
from .history import GRANULARITIES, SYMBOL_RE, fetch_candles, fetch_products, product_exists, search_products
from .theme import AMBER
from .widgets import INDICATORS, ChartPane, DepthPane, PairPicker, PriceTable, fmt_price

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "crypto-terminal"
WATCHLIST_FILE = CONFIG_DIR / "watchlist.json"
CONFIG_FILE = CONFIG_DIR / "config.json"
ALERTS_FILE = CONFIG_DIR / "alerts.json"
DEFAULT_WATCHLIST = ["BTC-USD", "ETH-USD", "XRP-USD", "SOL-USD", "ADA-USD"]
QUERY_RE = re.compile(r"[A-Z0-9]{1,20}")  # coin search: `add sol`, `add solana`
ZOOMS = (1, 2, 4)  # chart columns per candle
VIEWS = ("split", "chart", "watchlist")  # `f` cycles through these
BOOK_MIN_WIDTH = 140  # narrower terminals start with the book hidden so the chart keeps its room (`b` shows it)
BOOK_DELAY = 0.3  # seconds a pair must stay selected before its book (a ~1 MB snapshot) is requested


# ── Watchlist, config + commands ──────────────────────────────────────────────

def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_watchlist() -> list[str]:
    try:
        data = json.loads(WATCHLIST_FILE.read_text())
    except (OSError, ValueError):
        return list(DEFAULT_WATCHLIST)
    symbols = data.get("symbols") if isinstance(data, dict) else None
    if not isinstance(symbols, list):
        return list(DEFAULT_WATCHLIST)
    # file is user-editable: keep only well-formed pairs, drop duplicates
    return list(dict.fromkeys(s for s in symbols if isinstance(s, str) and SYMBOL_RE.fullmatch(s)))


def save_watchlist(symbols: list[str]):
    write_json(WATCHLIST_FILE, {"symbols": symbols})


def load_indicators() -> list[str]:
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return []
    names = data.get("indicators") if isinstance(data, dict) else None
    return [n for n in INDICATORS if isinstance(names, list) and n in names]


def save_indicators(names):
    write_json(CONFIG_FILE, {"indicators": [n for n in INDICATORS if n in names]})


def load_alerts() -> list[Alert]:
    try:
        data = json.loads(ALERTS_FILE.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for a in data if isinstance(data, list) else []:
        try:
            alert = Alert(a["symbol"], a["kind"], float(a["level"]), None if a.get("ref") is None else float(a["ref"]))
        except (TypeError, KeyError, ValueError, AttributeError):
            continue
        if valid_alert(alert):
            out.append(alert)
    return out


def save_alerts(alerts: list[Alert]):
    write_json(ALERTS_FILE, [vars(a) for a in alerts])


ALERT_USAGE = "usage: alert BTC-USD > 90000 · alert BTC-USD < 80000 · alert BTC-USD move 5%"


def parse_command(text: str) -> tuple[str, str | list[str] | Alert | int | None]:
    """`add SOL-USD` -> ("add", "SOL-USD"), `add sol` -> ("find", "SOL"), `ind rsi` -> ("ind", ["rsi"]),
    `alert BTC-USD > 90000` -> ("alert", Alert(...)), `unalert 2` -> ("unalert", 2), `quit` -> ("quit", None).

    Raises ValueError with a user-facing message.
    """
    parts = text.split()
    if not parts:
        raise ValueError("empty command")
    cmd, args = parts[0].lower(), parts[1:]
    if cmd == "quit" and not args:
        return "quit", None
    if cmd in ("add", "rm"):
        if len(args) != 1:
            raise ValueError(f"usage: {cmd} BASE-QUOTE, e.g. {cmd} SOL-USD")
        symbol = args[0].upper()
        if SYMBOL_RE.fullmatch(symbol):
            return cmd, symbol
        if cmd == "add" and QUERY_RE.fullmatch(symbol):
            return "find", symbol
        raise ValueError(f"not a pair: {args[0]} (expected e.g. SOL-USD{', or a coin: add solana' if cmd == 'add' else ''})")
    if cmd == "ind":
        names = [a.lower() for a in args]
        if names == ["off"]:
            return "ind", []
        if not names or any(n not in INDICATORS for n in names):
            raise ValueError(f"usage: ind {' '.join(INDICATORS)} (toggles) or ind off")
        return "ind", names
    if cmd == "alert":
        if len(args) != 3 or not SYMBOL_RE.fullmatch(args[0].upper()) or args[1].lower() not in KINDS:
            raise ValueError(ALERT_USAGE)
        kind = args[1].lower()
        try:
            level = float(args[2].removesuffix("%") if kind == "move" else args[2])
        except ValueError:
            level = math.nan
        if not valid_level(kind, level):
            raise ValueError("move must be between 0 and 100%" if kind == "move" else "price must be a positive number")
        return "alert", Alert(args[0].upper(), kind, level)
    if cmd == "alerts" and not args:
        return "alerts", None
    if cmd == "unalert":
        try:
            index = int(args[0]) if len(args) == 1 else 0
        except ValueError:
            index = 0
        if index < 1:
            raise ValueError("usage: unalert N (numbers from `alerts`)")
        return "unalert", index
    raise ValueError(f"unknown command: {text.strip()} (try add / rm / ind / alert / alerts / unalert / quit)")


# ── App ───────────────────────────────────────────────────────────────────────

class TerminalApp(App):
    TITLE = "Crypto Terminal"
    CSS = """
    Screen { background: black; }
    #title { height: 1; padding: 0 1; background: #ffb000; color: black; text-style: bold; }
    #main { height: 1fr; }
    #bar { height: 1; color: #ffb000; }
    PriceTable { width: auto; height: 1fr; background: black; border-right: solid #ffb000; }
    PriceTable.alone { width: 1fr; border-right: none; }
    PriceTable > .datatable--header { background: black; color: #ffb000; text-style: bold; }
    PriceTable > .datatable--cursor { background: #3a2a00; }
    #cmd { display: none; background: black; border: solid #ffb000; }
    #status { height: 1; padding: 0 1; background: #1c1c1c; color: #ffb000; }
    """
    BINDINGS = [
        ("slash", "command", "Command"),
        ("colon", "command", "Command"),
        ("escape", "close_command", "Close command bar"),
        *((key, f"timeframe('{key}')", label) for key, (label, _) in GRANULARITIES.items()),
        ("left_square_bracket", "step_timeframe(-1)", "Shorter candles"),
        ("right_square_bracket", "step_timeframe(1)", "Longer candles"),
        ("plus,equals_sign", "zoom(1)", "Zoom in"),
        ("minus", "zoom(-1)", "Zoom out"),
        ("f", "cycle_view", "Split / chart / watchlist"),
        ("b", "toggle_book", "Order book + trades"),
        ("g", "group_book", "Group book prices"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("CRYPTO TERMINAL · COINBASE SPOT · [1-6] or [ ] timeframe · [+ -] zoom · [f] view · "
                     "[b] book [g] group · [/] command", id="title", markup=False)
        with Horizontal(id="main"):
            yield PriceTable(id="watchlist")
            with Vertical(id="chart-col"):
                yield Static(id="bar", markup=False)
                yield ChartPane(id="chart")
            yield DepthPane(id="depth")
        yield Input(placeholder="add SOL-USD · add solana · rm ADA-USD · ind sma20 ema50 vwap rsi · ind off · "
                                "alert BTC-USD > 90000 · alerts · unalert 1 · quit   (esc closes)", id="cmd")
        yield Static(id="status", markup=False)  # status carries server text: never parse it as markup

    def on_mount(self):
        self.symbols = load_watchlist()
        self.http = httpx.AsyncClient()
        self.products = None  # online Coinbase pairs, loaded once at startup for `add` search
        self.feed: Feed | None = None
        self.feed_status = "idle"
        self.last_tick = 0.0
        self.selected: str | None = None
        self.timeframe = "1"
        self.view = "split"
        self.show_book = self.size.width >= BOOK_MIN_WIDTH
        table = self.query_one(PriceTable)
        table.set_symbols(self.symbols)
        table.focus()
        self.query_one(ChartPane).set_indicators(load_indicators())
        self.alerts = load_alerts()
        table.set_alerts({a.symbol for a in self.alerts})
        self.refresh_bar()
        self.restart_feed()
        self.load_products()
        self.set_interval(1, self.refresh_status)
        self.refresh_status()

    def on_resize(self):
        # Terminal.app keeps the old characters past the right edge when its window narrows and shows them in the
        # sliver beside the last column; clearing the whole screen before the repaint wipes them
        if self._driver is not None:
            self._driver.write("\x1b[2J")
        self.screen.refresh(layout=True)

    async def on_unmount(self):
        await self.http.aclose()

    # ── feed ──

    def restart_feed(self):
        if self.symbols:
            self.run_feed()  # exclusive worker: cancels the previous connection
        else:
            self.workers.cancel_group(self, "feed")
            self.feed = None
            self.handle_feed_status("idle")

    @work(exclusive=True, group="feed")
    async def run_feed(self):
        self.feed = Feed(self.symbols, self.handle_tick, self.handle_feed_status)
        await self.feed.run()

    def handle_tick(self, tick: Tick):
        self.last_tick = time.monotonic()
        self.query_one(PriceTable).update_tick(tick)
        if tick.symbol == self.selected:
            self.query_one(ChartPane).update_price(tick.symbol, tick.price)
        if self.alerts:
            fired, self.alerts = check_alerts(self.alerts, tick.symbol, tick.price)
            if fired:
                self.alerts_changed()
                self.bell()
                for alert in fired:
                    message = f"{alert} · now {fmt_price(tick.price)}"
                    self.notify(message, title="🔔 alert", severity="error", timeout=15)
                    desktop_notify("crypto-terminal alert", message)

    def handle_feed_status(self, status: str):
        self.feed_status = status
        self.refresh_status()

    def refresh_status(self):
        now = time.monotonic()
        state = self.feed_status.upper()
        if self.feed and self.feed_status == "live" and now - self.feed.last_msg > STALE_AFTER:
            state = "STALE"
        age = f"{now - self.last_tick:.0f}s ago" if self.last_tick else "—"
        clock = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        self.query_one("#status", Static).update(f"● {state}  │  last tick {age}  │  {clock}")

    @work(exclusive=True, group="products")
    async def load_products(self):
        try:
            self.products = await fetch_products(self.http)
        except (httpx.HTTPError, ValueError):
            pass  # `add` falls back to a per-pair lookup; search stays unavailable

    # ── chart ──

    def on_data_table_row_highlighted(self, event: PriceTable.RowHighlighted):
        self.selected = event.row_key.value
        self.refresh_bar()
        self.load_chart()
        self.restart_book()

    def action_timeframe(self, key: str):
        self.timeframe = key
        self.refresh_bar()
        self.load_chart()

    def action_step_timeframe(self, step: int):
        keys = list(GRANULARITIES)
        key = keys[min(max(keys.index(self.timeframe) + step, 0), len(keys) - 1)]
        if key != self.timeframe:  # clamped at 1m and 1d
            self.action_timeframe(key)

    def action_zoom(self, step: int):
        # redraws the candles already fetched: no request, so never more than history.MAX_CANDLES
        chart = self.query_one(ChartPane)
        chart.slot = ZOOMS[min(max(ZOOMS.index(chart.slot) + step, 0), len(ZOOMS) - 1)]
        chart.refresh()
        self.refresh_bar()

    def action_cycle_view(self):
        """split -> chart only -> watchlist only -> split; whatever stays visible takes the full width."""
        self.view = VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)]
        table = self.query_one(PriceTable)
        table.display = self.view != "chart"
        table.set_class(self.view == "watchlist", "alone")
        self.query_one("#chart-col").display = self.view != "watchlist"
        if self.query_one(DepthPane).display != self.book_wanted:  # split <-> chart keeps the open connection
            self.restart_book()

    # ── order book + trades ──

    def action_group_book(self):
        self.query_one(DepthPane).cycle_group()

    def action_toggle_book(self):
        self.show_book = not self.show_book
        self.restart_book()

    @property
    def book_wanted(self) -> bool:
        return self.show_book and self.view != "watchlist"

    def restart_book(self):
        """One book connection, for the selected pair, only while the pane is visible."""
        pane = self.query_one(DepthPane)
        pane.display = self.book_wanted
        if pane.symbol != self.selected:
            pane.group = None  # each pair opens at its own default grouping
        pane.symbol, pane.feed = self.selected, None  # never show the previous pair's book, even for a moment
        if pane.display and self.selected in self.symbols:
            self.run_book(self.selected)
        else:
            self.workers.cancel_group(self, "book")

    @work(exclusive=True, group="book")
    async def run_book(self, symbol: str):
        await asyncio.sleep(BOOK_DELAY)  # scrolling through the watchlist cancels this before anything is fetched
        feed = self.query_one(DepthPane).feed = BookFeed(symbol)
        await feed.run()

    def refresh_bar(self):
        bar = Text(f" {self.selected or '—'} │ ")
        for key, (label, _) in GRANULARITIES.items():
            bar.append(f" {label} ", style=f"bold black on {AMBER}" if key == self.timeframe else "")
        bar.append(f" │ zoom ×{self.query_one(ChartPane).slot} ")
        self.query_one("#bar", Static).update(bar)

    @work(exclusive=True, group="chart")
    async def load_chart(self):
        # exclusive: a newer selection cancels this fetch, so a slow reply can't draw the wrong pair
        chart = self.query_one(ChartPane)
        if self.selected not in self.symbols:
            chart.show_message("watchlist empty: press / then `add BTC-USD`")
            return
        symbol, (label, granularity) = self.selected, GRANULARITIES[self.timeframe]
        chart.show_message(f"loading {symbol} {label}…")
        try:
            candles = await fetch_candles(self.http, symbol, granularity)
        except httpx.HTTPError as e:
            chart.show_message(f"{symbol} {label}: history unavailable ({type(e).__name__})")
            return
        chart.show(symbol, label, granularity, candles)

    # ── command bar ──

    def action_command(self):
        cmd = self.query_one("#cmd", Input)
        cmd.display = True
        cmd.focus()

    def action_close_command(self):
        cmd = self.query_one("#cmd", Input)
        cmd.value, cmd.display = "", False
        self.query_one(PriceTable).focus()

    def on_input_submitted(self, event: Input.Submitted):
        text = event.value
        self.action_close_command()
        try:
            cmd, arg = parse_command(text)
        except ValueError as e:
            self.notify(str(e), severity="error")
            return
        if cmd == "quit":
            self.exit()
        elif cmd == "add":
            self.add_symbol(arg)
        elif cmd == "find":
            self.find_pair(arg)
        elif cmd == "ind":
            chart = self.query_one(ChartPane)
            chart.set_indicators(chart.indicators ^ set(arg) if arg else ())  # toggle
            save_indicators(chart.indicators)
        elif cmd == "alert":
            self.add_alert(arg)
        elif cmd == "alerts":
            self.notify("\n".join(f"{i}. {a}" for i, a in enumerate(self.alerts, 1)) or "no alerts", markup=False)
        elif cmd == "unalert":
            if arg > len(self.alerts):
                self.notify(f"no alert {arg} (see `alerts`)", severity="error")
            else:
                self.notify(f"removed: {self.alerts.pop(arg - 1)}", markup=False)
                self.alerts_changed()
        elif arg not in self.symbols:
            self.notify(f"{arg} is not on the watchlist", severity="error")
        else:
            self.symbols.remove(arg)
            self.watchlist_changed()

    @work(group="commands")
    async def add_symbol(self, symbol: str):
        if symbol in self.symbols:
            self.notify(f"{symbol} is already on the watchlist")
            return
        try:
            exists = (any(p.id == symbol for p in self.products) if self.products
                      else await product_exists(self.http, symbol))
        except httpx.HTTPError as e:
            self.notify(f"could not check {symbol} with Coinbase ({type(e).__name__})", severity="error")
            return
        if not exists:
            self.notify(f"unknown pair: {symbol}", severity="error")
        elif symbol not in self.symbols:  # re-check: a second `add` may have landed while we awaited
            self.symbols.append(symbol)
            self.watchlist_changed()

    def find_pair(self, query: str):
        if not self.products:
            self.notify("coin search unavailable (Coinbase product list not loaded): add an exact pair, e.g. add SOL-USD",
                        severity="error")
            return
        matches = search_products(self.products, query)
        if not matches:
            self.notify(f"no matches for {query}", severity="error")
        else:
            self.push_screen(PairPicker(query, matches), lambda symbol: symbol and self.add_symbol(symbol))

    def add_alert(self, alert: Alert):
        # ticks only arrive for watchlist pairs, so an alert anywhere else could never fire
        if alert.symbol not in self.symbols:
            self.notify(f"{alert.symbol} is not on the watchlist: add it first", severity="error")
            return
        if alert.kind == "move":
            ref = self.query_one(PriceTable).last_price(alert.symbol)
            if ref is None:
                self.notify(f"no price for {alert.symbol} yet: try again in a moment", severity="error")
                return
            alert = Alert(alert.symbol, alert.kind, alert.level, ref)
        self.alerts.append(alert)
        self.alerts_changed()
        self.notify(f"alert set: {alert}", markup=False)

    def alerts_changed(self):
        save_alerts(self.alerts)
        self.query_one(PriceTable).set_alerts({a.symbol for a in self.alerts})

    def watchlist_changed(self):
        save_watchlist(self.symbols)
        self.query_one(PriceTable).set_symbols(self.symbols)
        self.query_one(PriceTable).set_alerts({a.symbol for a in self.alerts})
        self.restart_feed()
        if not self.symbols:
            self.load_chart()
            self.restart_book()
