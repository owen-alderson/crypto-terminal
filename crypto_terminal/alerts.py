"""Price alerts.

One-shot alerts fire once and are then removed: a price level (`>` / `<`), or a percentage move from where the alert
was set. Watch alerts stay armed and fire each time their condition becomes true, at most once a minute each:

    move 2% 15m   the price is 2% above the lowest or below the highest of the last 15 minutes
    spread 5bp    best ask minus best bid is wider than 5 basis points of the mid price
    print 500k    one taker order (all of its fills together) is worth 500,000 or more in the quote currency
    volume 3x     the last minute traded 3x the average minute of the 15 before it
"""

import math
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass

import httpx

from .feed import Tick
from .history import SYMBOL_RE
from .widgets import fmt_bp, fmt_price

KINDS = (">", "<", "move", "spread", "print", "volume")
TRADE_KINDS = ("print", "volume")  # need every trade (the `matches` channel), not just the ticker
COOLDOWN = 60  # seconds: a watch alert fires at most once a minute
MAX_WINDOW = 240  # minutes, for `move N% in a window`
BASELINE = 15  # minutes of volume a `volume` alert compares the last minute against
USAGE = ("usage: alert BTC-USD > 90000 · < 80000 · move 5% · move 2% 15m · spread 5bp · print 500k · volume 3x")
SUFFIXES = {"k": 1e3, "m": 1e6}
WINDOW_RE = re.compile(r"(\d+)(m|h)")
NTFY_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class Alert:
    symbol: str
    kind: str  # one of KINDS
    level: float  # price for > and <, percent for move, basis points for spread, quote amount for print, multiple for volume
    ref: float | None = None  # one-shot move only: the price when the alert was set
    window: int | None = None  # minutes: makes `move` a watch alert over that window

    @property
    def repeats(self) -> bool:
        return self.kind in ("spread", "print", "volume") or self.window is not None

    def __str__(self):
        if self.kind == "move" and self.window:
            return f"{self.symbol} moves ±{self.level:g}% within {self.window}m"
        if self.kind == "move":
            return f"{self.symbol} moves ±{self.level:g}% from {fmt_price(self.ref)}"
        if self.kind == "spread":
            return f"{self.symbol} spread > {self.level:g} bp"
        if self.kind == "print":
            return f"{self.symbol} order ≥ {fmt_price(self.level)} {self.symbol.partition('-')[2]}"
        if self.kind == "volume":
            return f"{self.symbol} volume ≥ {self.level:g}× normal"
        return f"{self.symbol} {self.kind} {fmt_price(self.level)}"


def valid_level(kind: str, level: float) -> bool:
    # chained comparisons reject NaN, inf, zero and negatives
    if kind == "move":
        return 0 < level < 100
    if kind == "spread":
        return 0 < level < 10_000
    if kind == "volume":
        return 1 < level < math.inf
    return 0 < level < math.inf


def valid_alert(alert: Alert) -> bool:
    """Full check for alerts read back from disk (the file is user-editable)."""
    if not (isinstance(alert.symbol, str) and SYMBOL_RE.fullmatch(alert.symbol) and alert.kind in KINDS
            and valid_level(alert.kind, alert.level)):
        return False
    if alert.window is not None:
        return (alert.kind == "move" and type(alert.window) is int and 1 <= alert.window <= MAX_WINDOW
                and alert.ref is None)
    return alert.kind != "move" or (alert.ref is not None and 0 < alert.ref < math.inf)


def parse_alert(args: list[str]) -> Alert:
    """`BTC-USD move 2% 15m` -> Alert. Raises ValueError with a user-facing message."""
    if len(args) not in (3, 4) or not SYMBOL_RE.fullmatch(args[0].upper()) or args[1].lower() not in KINDS:
        raise ValueError(USAGE)
    symbol, kind, raw = args[0].upper(), args[1].lower(), args[2].lower()
    window = None
    if len(args) == 4:
        match = WINDOW_RE.fullmatch(args[3].lower())
        if kind != "move" or not match:
            raise ValueError(USAGE)
        window = int(match[1]) * (60 if match[2] == "h" else 1)
        if not 1 <= window <= MAX_WINDOW:
            raise ValueError(f"window must be 1m to {MAX_WINDOW // 60}h")
    unit = {"move": "%", "spread": "bp", "volume": "x"}.get(kind, "")
    scale = 1.0
    if kind == "print" and raw[-1:] in SUFFIXES:
        raw, scale = raw[:-1], SUFFIXES[raw[-1]]
    try:
        level = float(raw.removesuffix(unit) if unit else raw) * scale
    except ValueError:
        level = math.nan
    if not valid_level(kind, level):
        raise ValueError({"move": "move must be between 0 and 100%", "spread": "spread must be between 0 and 10000 bp",
                          "volume": "volume must be more than 1x"}.get(kind, "price must be a positive number"))
    return Alert(symbol, kind, level, window=window)


def triggered(alert: Alert, price: float) -> bool:
    """One-shot alerts only."""
    if alert.kind == ">":
        return price >= alert.level
    if alert.kind == "<":
        return price <= alert.level
    return abs(price - alert.ref) / alert.ref * 100 >= alert.level


@dataclass(frozen=True)
class Fired:
    alert: Alert
    message: str


class _State:
    """What one watch alert remembers between ticks."""

    def __init__(self):
        self.was_true = False
        self.last = -math.inf  # when it last fired
        self.suppressed = 0  # times it would have fired during the cooldown
        self.highs: deque = deque()  # move: (time, price), prices falling, so [0] is the window's high
        self.lows: deque = deque()  # move: prices rising, so [0] is the window's low
        self.taker, self.notional, self.done = None, 0.0, False  # print: the order being filled
        self.restart_volume(None)

    def restart_volume(self, since: float | None):
        self.since = since  # volume: when the unbroken run of trades began (warm-up)
        self.recent: deque = deque()  # volume: (time, size) of the last minute
        self.base: deque = deque()  # volume: the BASELINE minutes before that
        self.recent_sum = self.base_sum = 0.0


class Watcher:
    """Checks alerts against ticks and trades. One-shot alerts that fire are removed from `alerts`."""

    def __init__(self, alerts=(), clock=time.monotonic):
        self.clock = clock
        self.alerts: list[Alert] = []
        self._state: dict[Alert, _State] = {}
        self._last_trade: dict[str, int] = {}
        self.set_alerts(alerts)

    def set_alerts(self, alerts):
        """Alerts kept from before keep their state (cooldown, window, warm-up)."""
        self.alerts = list(alerts)
        self._state = {a: self._state.get(a) or _State() for a in self.alerts if a.repeats}

    @property
    def trade_symbols(self) -> list[str]:
        return sorted({a.symbol for a in self.alerts if a.kind in TRADE_KINDS})

    def warming(self, alert: Alert) -> float:
        """Volume alerts: seconds of unbroken trade data still needed before they can fire."""
        s = self._state.get(alert)
        if alert.kind != "volume" or s is None:
            return 0.0
        return 60 * (BASELINE + 1) - (self.clock() - s.since if s.since is not None else 0)

    def on_tick(self, tick: Tick) -> list[Fired]:
        now, fired = self.clock(), []
        for a in list(self.alerts):
            if a.symbol != tick.symbol or a.kind in TRADE_KINDS:
                continue
            if not a.repeats:
                if triggered(a, tick.price):
                    self.alerts.remove(a)
                    fired.append(Fired(a, f"{a} · now {fmt_price(tick.price)}"))
                continue
            s = self._state[a]
            if a.kind == "spread":
                if tick.bid is None or tick.ask is None:
                    continue
                bp = (tick.ask - tick.bid) / ((tick.ask + tick.bid) / 2) * 10_000
                message = self._edge(s, now, bp > a.level, f"{a.symbol} spread {fmt_bp(bp)} bp "
                                     f"(bid {fmt_price(tick.bid)} · ask {fmt_price(tick.ask)})")
            else:
                message = self._move(a, s, now, tick.price)
            if message:
                fired.append(Fired(a, message))
        return fired

    def on_trade(self, symbol: str, trade) -> list[Fired]:
        """`trade` is a book.Trade from the `matches` channel."""
        now, fired = self.clock(), []
        last = self._last_trade.get(symbol)
        self._last_trade[symbol] = trade.id
        gap = last is not None and trade.id != last + 1  # trade ids are sequential per pair
        for a in self.alerts:
            if a.symbol != symbol or a.kind not in TRADE_KINDS:
                continue
            s = self._state[a]
            if a.kind == "print":
                if trade.taker != s.taker:
                    s.taker, s.notional, s.done = trade.taker, 0.0, False
                s.notional += trade.price * trade.size
                if s.done or s.notional < a.level:
                    continue
                s.done = True  # once per order, however many more fills follow
                message = self._event(s, now, f"{symbol} {'buy' if trade.buy else 'sell'} order "
                                      f"≥ {fmt_price(a.level)} {symbol.partition('-')[2]} "
                                      f"({fmt_price(s.notional)} so far, at {fmt_price(trade.price)})")
            else:
                message = self._volume(a, s, now, trade.size, gap)
            if message:
                fired.append(Fired(a, message))
        return fired

    def _move(self, a: Alert, s: _State, now: float, price: float) -> str | None:
        start = now - a.window * 60
        for q in (s.highs, s.lows):
            while q and q[0][0] < start:
                q.popleft()
        while s.highs and s.highs[-1][1] <= price:
            s.highs.pop()
        while s.lows and s.lows[-1][1] >= price:
            s.lows.pop()
        s.highs.append((now, price))
        s.lows.append((now, price))
        high, low = s.highs[0][1], s.lows[0][1]
        up, down = (price - low) / low * 100, (high - price) / high * 100
        pct, origin = (up, low) if up >= down else (-down, high)
        return self._edge(s, now, max(up, down) >= a.level,
                          f"{a.symbol} {pct:+.2f}% within {a.window}m ({fmt_price(origin)} → {fmt_price(price)})")

    def _volume(self, a: Alert, s: _State, now: float, size: float, gap: bool) -> str | None:
        if gap or s.since is None:  # a missed trade would understate the baseline: start the warm-up again
            s.restart_volume(now)
        s.recent.append((now, size))
        s.recent_sum += size
        while s.recent[0][0] <= now - 60:
            t, v = s.recent.popleft()
            s.recent_sum -= v
            s.base.append((t, v))
            s.base_sum += v
        while s.base and s.base[0][0] <= now - 60 * (BASELINE + 1):
            s.base_sum -= s.base.popleft()[1]
        if now - s.since < 60 * (BASELINE + 1):
            return None
        average = s.base_sum / BASELINE
        base = a.symbol.partition("-")[0]
        return self._edge(s, now, average > 0 and s.recent_sum >= a.level * average,
                          f"{a.symbol} volume {s.recent_sum / average if average > 0 else 0:.1f}× normal: "
                          f"{fmt_price(s.recent_sum)} {base} in the last minute, {fmt_price(average)} a minute "
                          f"on average over the {BASELINE} before")

    def _edge(self, s: _State, now: float, true: bool, message: str) -> str | None:
        """Fire when the condition becomes true, not on every tick while it stays true."""
        became_true = true and not s.was_true
        s.was_true = true
        return self._event(s, now, message) if became_true else None

    def _event(self, s: _State, now: float, message: str) -> str | None:
        if now - s.last < COOLDOWN:
            s.suppressed += 1
            return None
        if s.suppressed:
            message += f" · {s.suppressed} more since the last alert"
        s.last, s.suppressed = now, 0
        return message


def push_url(text: str) -> str:
    """`mytopic` -> https://ntfy.sh/mytopic; an https URL to a self-hosted ntfy topic is kept. Else ValueError."""
    if NTFY_TOPIC_RE.fullmatch(text):
        return f"https://ntfy.sh/{text}"
    try:
        url = httpx.URL(text)
    except httpx.InvalidURL:
        url = None
    if url is None or url.scheme != "https" or not url.host or not NTFY_TOPIC_RE.fullmatch(url.path.strip("/")):
        raise ValueError("usage: push <topic> (ntfy.sh) or push https://your-ntfy-server/<topic> · push off")
    return str(url)


async def push(client: httpx.AsyncClient, url: str, message: str) -> str | None:
    """Send to an ntfy topic (phone push). Returns an error message, or None when it was delivered."""
    try:
        response = await client.post(url, content=message.encode(), timeout=10,
                                     headers={"Title": "crypto-terminal alert", "Tags": "bell"})
        response.raise_for_status()
    except httpx.HTTPError as e:
        return f"push to {url} failed ({type(e).__name__})"
    return None


def desktop_notify(title: str, message: str):
    """Best effort: osascript on macOS, notify-send on Linux, silently skipped if neither exists."""
    if sys.platform == "darwin" and shutil.which("osascript"):
        # text goes in as argv, never spliced into the script, so it can't inject AppleScript
        args = ["osascript", "-e", "on run argv", "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e", "end run", title, message]
    elif shutil.which("notify-send"):
        args = ["notify-send", "--", title, message]
    else:
        return
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
