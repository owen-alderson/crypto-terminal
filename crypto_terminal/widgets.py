import time
from collections import deque
from itertools import accumulate, islice

from rich.text import Text
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import DataTable, OptionList
from textual.widgets.option_list import Option

from . import chart
from .chart import fmt_price
from .book import BookFeed, big_threshold, default_step, flow, group_steps, prints, step_decimals
from .feed import STALE_AFTER, Tick
from .history import Candle, Product, apply_tick
from .indicators import ema, sma, vwap
from .theme import AMBER, AXIS, GREEN, RED, VOL_GREEN, VOL_RED

SPARK = "▁▂▃▄▅▆▇█"
SPARK_LEN = 20  # ticks shown beside the chart; the watchlist-only view stretches the sparkline to fill the width
SPARK_HISTORY = 500
FLASH_SECONDS = 0.5
UP, DOWN = GREEN, RED
REDRAW_EVERY = 0.25  # live candle redraws at most four times a second
SIZE_DP = 8  # Coinbase quotes sizes to at most 8 decimals

# name -> (legend, colour, series over all candles); RSI gets its own panel
OVERLAYS = {
    "sma20": ("SMA 20", "#FFD600", lambda candles: sma([c.c for c in candles], 20)),
    "ema50": ("EMA 50", "#00B8D4", lambda candles: ema([c.c for c in candles], 50)),
    "vwap": ("VWAP", "#D500F9", vwap),
}
INDICATORS = (*OVERLAYS, "rsi")


def sparkline(values) -> str:
    lo, hi = min(values), max(values)
    if hi == lo:
        return SPARK[0] * len(values)
    return "".join(SPARK[round((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in values)


class PriceTable(DataTable):
    """Watchlist: one row per pair, price flashes green/red on each up/down tick.
    Columns fit their content, so the table is only as wide as it needs to be."""

    def on_mount(self):
        self.cursor_type = "row"
        for label, key in (("PAIR", "sym"), ("LAST", "last"), ("24H %", "chg"), ("TICKS", "spark")):
            self.add_column(label, key=key)
        self._last: dict[str, Tick] = {}
        self._dir: dict[str, str] = {}
        self._spark: dict[str, deque] = {}
        self._flashing: set[str] = set()

    def set_symbols(self, symbols: list[str]):
        for sym in list(self._spark):
            if sym not in symbols:
                self.remove_row(sym)
                for d in (self._last, self._dir, self._spark):
                    d.pop(sym, None)
        for sym in symbols:
            if sym not in self._spark:
                self._spark[sym] = deque(maxlen=SPARK_HISTORY)
                self.add_row(Text(sym, style="bold"), "—", "—", " " * self.spark_len(), key=sym)

    def set_alerts(self, symbols: set[str]):
        """Mark pairs that have an alert set."""
        for sym in self._spark:
            self.update_cell(sym, "sym", Text(f"{sym} 🔔" if sym in symbols else sym, style="bold"), update_width=True)

    def last_price(self, sym: str) -> float | None:
        return self._last[sym].price if sym in self._last else None

    def update_tick(self, tick: Tick):
        sym = tick.symbol
        if sym not in self._spark:  # late tick for a pair just removed
            return
        prev = self._last.get(sym)
        self._last[sym] = tick
        self._spark[sym].append(tick.price)
        if prev and tick.price != prev.price:
            self._dir[sym] = UP if tick.price > prev.price else DOWN
            if sym not in self._flashing:
                self._flashing.add(sym)
                self.set_timer(FLASH_SECONDS, lambda: self._unflash(sym))
        pct = (tick.price - tick.open_24h) / tick.open_24h * 100
        self.update_cell(sym, "chg", Text(f"{'▲' if pct >= 0 else '▼'} {pct:+.2f}%", style=UP if pct >= 0 else DOWN),
                         update_width=True)
        self._render_spark(sym)
        self._render_price(sym)

    def spark_len(self) -> int:
        """20 ticks beside the chart; alone, whatever width the other columns leave, so no space sits blank."""
        if not self.has_class("alone"):
            return SPARK_LEN
        others = sum(c.get_render_width(self) for key, c in self.columns.items() if key != "spark")
        return max(SPARK_LEN, self.scrollable_content_region.width - others - 2 * self.cell_padding)

    def on_resize(self):
        for sym in self._spark:
            self._render_spark(sym)

    def _render_spark(self, sym: str):
        n = self.spark_len()
        ticks = list(self._spark[sym])[-n:]
        # padded to n so the column (and the table) keeps its width while ticks arrive
        text = f"{sparkline(ticks) if ticks else '':<{n}}"
        self.update_cell(sym, "spark", Text(text, style="dark_orange"), update_width=True)

    def _unflash(self, sym: str):
        self._flashing.discard(sym)
        if sym in self._last:
            self._render_price(sym)

    def _render_price(self, sym: str):
        color = self._dir.get(sym, "white")
        style = f"bold black on {color}" if sym in self._flashing else f"bold {color}"
        self.update_cell(sym, "last", Text(fmt_price(self._last[sym].price), style=style, justify="right"),
                         update_width=True)


class PairPicker(ModalScreen[str | None]):
    """Search results for `add <coin>`: enter adds the highlighted pair, esc cancels."""

    BINDINGS = [("escape", "dismiss", "Cancel")]
    DEFAULT_CSS = """
    PairPicker { align: center middle; }
    PairPicker > OptionList { width: 50; height: auto; max-height: 20; background: black; border: solid #ffb000; }
    """

    def __init__(self, query: str, products: list[Product]):
        super().__init__()
        self.term, self.products = query, products

    def compose(self):
        # Text, not str: coin names are server data and must never be parsed as markup
        picker = OptionList(*(Option(Text(f"{p.id:<14}{p.name}"), id=p.id) for p in self.products))
        picker.border_title = f"add {self.term.lower()} · enter adds · esc cancels"
        yield picker

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(event.option.id)


class ChartPane(Widget):
    """Candles for one pair; the last candle follows live ticks via `update_price`."""

    DEFAULT_CSS = "ChartPane { width: 1fr; height: 1fr; }"

    def on_mount(self):
        self.symbol, self.candles, self.indicators, self._dirty, self.slot = None, [], set(), False, 2
        self.set_interval(REDRAW_EVERY, self._redraw_if_dirty)
        self.show_message("select a pair")

    def show_message(self, text: str):
        self.candles, self.message = [], text  # stop live updates until the next `show`
        self.refresh()

    def show(self, symbol: str, label: str, granularity: int, candles: list[Candle]):
        if not candles:
            return self.show_message(f"{symbol} {label}: no data")
        self.symbol, self.granularity, self.candles = symbol, granularity, candles
        self.refresh()

    def set_indicators(self, names):
        self.indicators = set(names)
        self._dirty = bool(self.candles)

    def update_price(self, symbol: str, price: float):
        if self.candles and symbol == self.symbol:
            apply_tick(self.candles, price, time.time(), self.granularity)
            self._dirty = True

    def _redraw_if_dirty(self):
        if self._dirty:
            self._dirty = False
            self.refresh()

    def render(self):
        if not self.candles:
            return Text(self.message)
        # indicators see the full history, not just the candles in view
        overlays = [(legend, colour, series(self.candles))
                    for name, (legend, colour, series) in OVERLAYS.items() if name in self.indicators]
        return Text("\n").join(chart.render(self.candles, self.granularity, self.size.width, self.size.height,
                                            self.slot, overlays, "rsi" in self.indicators))


def fmt_sizes(sizes: list[float], width: int) -> list[str]:
    """One decimal count for the whole column (as many as fit, up to 8) so the decimal points line up.
    A size too small to show at that precision reads <0.0001, never 0."""
    if not sizes:
        return []
    dp = max(0, min(SIZE_DP, width - len(f"{max(sizes):,.0f}") - 2))
    floor = f"<{10 ** -dp:.{dp}f}" if dp else "<1"
    return [text if float(text.replace(",", "")) else floor for text in (f"{s:,.{dp}f}" for s in sizes)]


class DepthPane(Widget):
    """Order book (top) and trade tape (bottom) for the selected pair, drawn from a BookFeed."""

    DEFAULT_CSS = "DepthPane { width: 35; height: 1fr; border-left: solid #ffb000; }"
    TAPE_SCAN = 500  # newest trades merged into prints for the tape and the big-print threshold

    def on_mount(self):
        self.symbol: str | None = None
        self.feed: BookFeed | None = None
        self.group: int | None = None  # price grouping in ticks; None = the pair's default
        self.set_interval(REDRAW_EVERY, self.refresh)

    def steps(self) -> tuple[list[int], int] | None:
        """Groupings on offer for the current book and the one in use, or None before there's a book."""
        if not (self.feed and self.feed.book.ready):
            return None
        book = self.feed.book
        bid, ask = book.best()
        mid = (bid + ask) / 2 if bid and ask else bid or ask
        if not mid:
            return None
        steps = group_steps(mid, book.dp)
        return steps, self.group if self.group in steps else default_step(steps, mid, book.dp)

    def cycle_group(self):
        """Next coarser grouping, wrapping back to ungrouped after the coarsest."""
        if found := self.steps():
            steps, step = found
            self.group = steps[(steps.index(step) + 1) % len(steps)]
            self.refresh()

    def render(self):
        width, height = self.size.width, self.size.height
        book_rows = max(4, height * 3 // 5)
        lines = self.book_lines(width, book_rows) + self.tape_lines(width, height - book_rows)
        return Text("\n").join(lines[:height])

    def book_lines(self, width: int, rows: int) -> list[Text]:
        found, feed = self.steps(), self.feed
        title = f"{self.symbol or '—'} order book"
        if found:
            title = f"{self.symbol} book · by {found[1] / 10 ** feed.book.dp:,.{step_decimals(found[1], feed.book.dp)}f}"
        lines = [Text(title, style=f"bold {AMBER}")]
        if feed is None:
            return lines + [Text("connecting…")]
        age = time.monotonic() - feed.last_msg
        if feed.status != "live":
            return lines + [Text(feed.status)]  # status carries server text: Text, never markup
        if age > STALE_AFTER:
            return lines + [Text(f"STALE: no data for {age:.0f}s", style=RED)]
        if not found:
            return lines + [Text("loading book…")]
        book, step, n = feed.book, found[1], max(1, (rows - 2) // 2)
        asks, bids = book.top(n, step)
        dp = step_decimals(step, book.dp)
        price_w = (width - 2) // 2
        size_w = width - price_w - 2
        sizes = fmt_sizes([s for _, s in asks + bids], size_w)
        cum_asks, cum_bids = list(accumulate(s for _, s in asks)), list(accumulate(s for _, s in bids))
        deepest = max(cum_asks[-1:] + cum_bids[-1:], default=0)

        def level(price, size_text, cum, colour, bar_colour):
            line = Text(f"{price:>{price_w},.{dp}f}", style=colour)
            line.append(f"  {size_text:>{size_w}}")
            bar = round(cum / deepest * width) if deepest else 0
            line.stylize(f"on {bar_colour}", width - bar, width)  # depth: cumulative size from the best price out
            return line

        ask_lines = [level(p, sz, c, RED, VOL_RED) for (p, _), sz, c in zip(asks, sizes, cum_asks)]
        bid_lines = [level(p, sz, c, GREEN, VOL_GREEN) for (p, _), sz, c in zip(bids, sizes[len(asks):], cum_bids)]
        # best ask sits just above the spread, best bid just below; short books pad away from the middle
        lines += [Text("")] * (n - len(asks)) + ask_lines[::-1]
        bid, ask = book.best()
        if bid and ask:  # the real spread, whatever the grouping
            spread = ask - bid
            bps = spread / ((ask + bid) / 2) * 1e4  # BTC's one-cent spread is ~0.0012 bp: 2 significant figures
            gap = f" spread {spread:,.{book.dp}f} · {f'{bps:.2g}' if bps < 100 else f'{bps:.0f}'} bp "
        else:
            gap = " one-sided book "
        lines.append(Text(f"{gap:─^{width}}", style=AXIS))
        return lines + bid_lines + [Text("")] * (n - len(bids))

    def tape_lines(self, width: int, rows: int) -> list[Text]:
        header = Text("trades", style=f"bold {AMBER}")
        if not (self.feed and self.feed.trades):
            return [header, Text("waiting for trades…" if self.feed else "")]
        trades = list(islice(self.feed.trades, self.TAPE_SCAN))
        bought, sold = flow(self.feed.trades, time.monotonic())
        if bought + sold:
            share = bought / (bought + sold)
            header.append(f"  last 1m: {share:.0%} buys", style=AXIS)
            green = round(share * width)
            bar = Text("━" * green, style=GREEN) + Text("━" * (width - green), style=RED)
        else:
            header.append("  last 1m: no trades", style=AXIS)
            bar = Text("━" * width, style=AXIS)
        lines = [header, bar]

        recent = prints(trades)
        big = big_threshold(recent)
        shown = recent[:max(0, rows - 2)]
        dp = max(t.dp for t in shown)
        count_w = max(3, *(len(f" ×{t.count}") for t in shown))  # ×295 must never be cut to ×29
        price_w = (width - 10 - count_w) // 2
        size_w = width - 10 - count_w - price_w
        for trade, size_text in zip(shown, fmt_sizes([t.size for t in shown], size_w)):
            colour = GREEN if trade.buy else RED
            count = f"×{trade.count}" if trade.count > 1 else ""
            body = f" {trade.price:>{price_w},.{dp}f} {size_text:>{size_w}}{count:>{count_w}}"
            if trade.price * trade.size > big:  # bigger than 98% of recent prints
                line = Text(trade.time + body, style=f"bold black on {colour}")
            else:
                line = Text(trade.time, style=AXIS) + Text(body, style=colour)
            lines.append(line)
            if trade.missed:  # newest first, so the missed (older) trades sit below
                lines.append(Text(f"{f' {trade.missed} missed ':·^{width}}", style=AXIS))
        return lines
