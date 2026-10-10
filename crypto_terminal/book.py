"""Order book and trade tape for one pair, from the same public feed (no API key).

`level2_batch` sends a full snapshot, then changes every 50 ms; `matches` sends every trade.
A malformed book message raises, so the feed reconnects and starts again from a fresh snapshot:
a book with a level silently missing would be wrong without looking wrong.
"""

import heapq
import math
import re
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Callable

from .feed import Feed

TAPE_LEN = 5000  # enough for a busy minute of BTC-USD, which the buy/sell flow covers
MAX_GROUP = 0.005  # coarsest price grouping offered: 0.5% of the price
DEFAULT_GROUP_BP = 1  # a pair opens grouped at the step nearest 1 basis point of its price ($10 on BTC at ~$82k)
TIME_RE = re.compile(r"\d{4}-\d\d-\d\dT(\d\d:\d\d:\d\d)")


def decimals(text: str) -> int:
    return len(text.partition(".")[2])


def parse_number(text, allow_zero: bool = False) -> float:
    value = float(text)  # TypeError / ValueError for anything that isn't a number
    if not isinstance(text, str) or not (0 <= value < math.inf if allow_zero else 0 < value < math.inf):
        raise ValueError(f"bad number: {text!r}")
    return value


@dataclass(frozen=True)
class Trade:
    id: int
    time: str  # HH:MM:SS UTC
    price: float
    size: float
    buy: bool  # the taker bought (lifted the ask)
    dp: int  # decimals Coinbase quoted the price with
    missed: int = 0  # trades missing just before this one (trade ids are sequential per pair)
    at: float = 0.0  # monotonic time received
    count: int = 1  # trades combined into this print (see `prints`)


def parse_trade(msg: dict) -> Trade | None:
    """Validate a `match` / `last_match` message. Returns None for anything malformed."""
    if msg.get("type") not in ("match", "last_match"):
        return None
    try:
        trade_id, side, raw_price = msg["trade_id"], msg["side"], msg["price"]
        price, size = parse_number(raw_price), parse_number(msg["size"])
        clock = TIME_RE.match(msg["time"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(trade_id, int) or side not in ("buy", "sell") or not clock:
        return None
    # `side` is the resting (maker) order's side: a resting sell was hit by a buyer
    return Trade(trade_id, clock[1], price, size, side == "sell", decimals(raw_price))


class OrderBook:
    def __init__(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.ready = False  # no snapshot yet: an empty book must never look like a real one
        self.dp = 0  # decimals Coinbase quotes prices with

    def load(self, msg: dict):
        """Replace the book with a `snapshot`. Raises ValueError if any level is malformed."""
        sides = []
        for key in ("bids", "asks"):
            if not isinstance(msg.get(key), list):
                raise ValueError(f"snapshot without {key}")
            levels = {}
            for row in msg[key]:
                try:
                    raw_price, raw_size = row
                    levels[parse_number(raw_price)] = parse_number(raw_size)
                except (TypeError, ValueError) as e:
                    raise ValueError(f"malformed {key} level in snapshot: {row!r}") from e
                self.dp = max(self.dp, decimals(raw_price))
            sides.append(levels)
        self.bids, self.asks = sides
        self.ready = True

    def update(self, changes):
        """Apply an `l2update`'s changes; size 0 removes the level. Raises ValueError if any change is malformed."""
        if not isinstance(changes, list):
            raise ValueError("malformed l2update")
        parsed = []
        for row in changes:  # validate everything first, so a bad message changes nothing
            try:
                side, raw_price, raw_size = row
                parsed.append(({"buy": self.bids, "sell": self.asks}[side], parse_number(raw_price),
                               parse_number(raw_size, allow_zero=True)))
            except (TypeError, ValueError, KeyError) as e:
                raise ValueError(f"malformed l2update change: {row!r}") from e
            self.dp = max(self.dp, decimals(raw_price))
        for levels, price, size in parsed:
            if size:
                levels[price] = size
            else:
                levels.pop(price, None)

    def top(self, n: int, step: int = 1) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """Best `n` asks (lowest first) and best `n` bids (highest first) as (price, size), grouped into buckets of
        `step` price ticks (a tick is 10**-dp)."""
        if step == 1:
            return ([(p, self.asks[p]) for p in heapq.nsmallest(n, self.asks)],
                    [(p, self.bids[p]) for p in heapq.nlargest(n, self.bids)])
        return grouped(self.asks, n, step, 10 ** self.dp, ask=True), grouped(self.bids, n, step, 10 ** self.dp, ask=False)

    def best(self) -> tuple[float | None, float | None]:
        return max(self.bids, default=None), min(self.asks, default=None)


def grouped(levels: dict[float, float], n: int, step: int, scale: int, ask: bool) -> list[tuple[float, float]]:
    """Best `n` non-empty buckets of `step` ticks. Asks round up and bids round down, so a grouped level never shows
    a better price than is really on offer (a bid at 82,617.99 counts toward 82,610 when grouped by 10)."""
    def bucket(price):
        ticks = round(price * scale)  # whole ticks: no float drift at bucket edges
        return -(-ticks // step) * step if ask else ticks // step * step

    def group(items):
        out = {}
        for price, size in items:
            key = bucket(price)
            out[key] = out.get(key, 0.0) + size
        return out

    if not levels:
        return []
    # only levels that can land in the first n buckets: one pass of comparisons instead of grouping ~20k levels
    first = bucket(min(levels) if ask else max(levels))
    edge = (first + (n - 1) * step + 0.5) / scale if ask else (first - (n - 1) * step - 0.5) / scale
    buckets = group((p, s) for p, s in levels.items() if (p <= edge if ask else p >= edge))
    if len(buckets) < n:  # sparse book: some of those buckets are empty, so the best n reach further out
        buckets = group(levels.items())
    keys = heapq.nsmallest(n, buckets) if ask else heapq.nlargest(n, buckets)
    return [(key / scale, buckets[key]) for key in keys]


def group_steps(mid: float, dp: int) -> list[int]:
    """Groupings offered, in ticks: 1, 5, 10, 50, 100… up to 0.5% of the price. BTC: $0.01 to $100."""
    steps, e = [], 0
    while True:
        for k in (1, 5):
            step = k * 10 ** e
            if step > 1 and step / 10 ** dp > mid * MAX_GROUP:
                return steps
            steps.append(step)
        e += 1


def default_step(steps: list[int], mid: float, dp: int) -> int:
    target = mid * DEFAULT_GROUP_BP / 1e4
    return min(steps, key=lambda step: abs(math.log(step / 10 ** dp / target)))


def step_decimals(step: int, dp: int) -> int:
    """Decimals a grouped price needs: 2 for $0.05 steps, 0 for $10."""
    zeros = len(str(step)) - len(str(step).rstrip("0"))
    return max(0, dp - zeros)


def prints(trades) -> list[Trade]:
    """Combine consecutive trades with the same second, side and price into one line (count = how many).
    Never across a gap, so a "missed" marker stays exactly where the gap was."""
    out: list[Trade] = []
    for trade in trades:  # newest first
        last = out[-1] if out else None
        if last and not last.missed and (last.time, last.buy, last.price) == (trade.time, trade.buy, trade.price):
            out[-1] = replace(last, size=last.size + trade.size, count=last.count + trade.count,
                              missed=trade.missed, dp=max(last.dp, trade.dp))
        else:
            out.append(trade)
    return out


def flow(trades, now: float, window: float = 60) -> tuple[float, float]:
    """Base-currency volume bought and sold by takers over the last `window` seconds."""
    bought = sold = 0.0
    for trade in trades:  # newest first
        if now - trade.at > window:
            break
        if trade.buy:
            bought += trade.size
        else:
            sold += trade.size
    return bought, sold


def big_threshold(lines: list[Trade], quantile: float = 0.98, minimum: int = 50) -> float:
    """Notional a print must exceed to count as big (bigger than 98% of recent prints). Strictly exceed: when most
    prints are the same size, `>=` would light up nearly all of them. Infinite until there are enough to judge."""
    if len(lines) < minimum:
        return math.inf
    values = sorted(t.price * t.size for t in lines)
    return values[int(quantile * (len(values) - 1))]


class BookFeed(Feed):
    """Order book + recent trades for one pair. `status` mirrors the connection; the book resets whenever it isn't
    live, so data from a dropped connection is never shown as current."""

    CHANNELS = ("level2_batch", "matches", "heartbeat")

    def __init__(self, symbol: str, on_status: Callable[[str], None] = lambda s: None, **kwargs):
        super().__init__([symbol], lambda tick: None, self._status, **kwargs)
        self.symbol, self.forward_status = symbol, on_status
        self.status = "idle"
        self.book = OrderBook()
        self.trades: deque[Trade] = deque(maxlen=TAPE_LEN)  # newest first

    def _status(self, status: str):
        if status != "live":
            self.book = OrderBook()
        self.status = status
        self.forward_status(status)

    def handle(self, msg: dict) -> bool:
        kind = msg.get("type")
        if msg.get("product_id") != self.symbol:
            return False
        if kind == "snapshot":
            self.book.load(msg)
        elif kind == "l2update":
            if self.book.ready:  # changes before the snapshot can't be applied to anything
                self.book.update(msg.get("changes"))
        elif kind in ("match", "last_match"):
            self.add_trade(parse_trade(msg))
        else:
            return False
        return True

    def add_trade(self, trade: Trade | None):
        if trade is None:  # malformed: the next trade's id shows it as missed
            return
        last = self.trades[0].id if self.trades else None
        if last is not None and trade.id <= last:  # `last_match` repeats a trade after a reconnect
            return
        missed = trade.id - last - 1 if last is not None else 0
        self.trades.appendleft(replace(trade, missed=missed, at=time.monotonic()))
