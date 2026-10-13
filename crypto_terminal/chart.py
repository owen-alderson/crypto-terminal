"""Candlestick chart as text: a pure function from candles to Rich lines.

Wick tips are exact to half a row; bodies cover whole rows as solid background colour, so they never break
(even where a terminal's font leaves gaps between rows) and wicks always meet them."""

import math
from datetime import datetime, timezone
from itertools import groupby
from operator import itemgetter

from rich.text import Text

from .indicators import rsi
from .theme import AMBER, AXIS, GREEN, GRID, RED, VOL_GREEN, VOL_RED

SIG_FIGS = 5
VOLUME_ROWS = 7
RSI_ROWS = 8  # 15 half-row steps: RSI 70 and 30 fall exactly on the middle of rows 2 and 5
MIN_PRICE_ROWS = 3
MIN_TICKS = 4
BLOCKS = " ▁▂▃▄▅▆▇█"
DOTS = ("⠛", "⣤")  # a point filling the top / bottom half of a cell
# wick cell -> glyph by which halves the wick fills: both, top only (low ends mid-row), bottom only (high starts mid-row)
WICK = {(True, True): "│", (True, False): "╵", (False, True): "╷"}
TIME_STEPS = (60, 300, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400, 172800, 604800, 1209600, 2592000)
TOO_SMALL = "enlarge the terminal to see the chart"


def fmt_price(value) -> str:  # float, or a Decimal (rounded exactly, half to even)
    """2 decimals from 1,000 up, otherwise enough decimals for SIG_FIGS significant figures (0.0021300)."""
    if value >= 1_000:
        return f"{value:,.2f}"
    decimals = max(2, SIG_FIGS - 1 - math.floor(math.log10(value))) if value > 0 else 2
    return f"{value:.{decimals}f}"


def half_row(price: float, hi: float, lo: float, rows: int) -> int:
    """0 = top half of the first row ... 2*rows-1 = bottom half of the last; hi and lo land exactly on the ends."""
    return rows - 1 if hi == lo else round((hi - price) / (hi - lo) * (2 * rows - 1))


def row_price(row: int, hi: float, lo: float, rows: int) -> float:
    """Price where `row`'s top half meets its bottom half: the level a mid-cell line (┄) marks."""
    return hi if hi == lo else hi - (2 * row + 0.5) * (hi - lo) / (2 * rows - 1)


def nice_ticks(lo: float, hi: float, least: int) -> list[float]:
    """Round prices inside [lo, hi], 1, 2, 2.5 or 5 x 10^n apart: the widest spacing giving at least `least`."""
    if hi == lo:
        return [lo]
    mag = 10 ** math.floor(math.log10(hi - lo))
    for m in (5, 2.5, 2, 1, 0.5, 0.25, 0.2, 0.1):  # 0.1 * mag always gives 10 or more
        ticks = [i * m * mag for i in range(math.ceil(lo / (m * mag)), math.floor(hi / (m * mag)) + 1)]
        if len(ticks) >= least:
            return ticks
    return ticks


def render(candles, granularity: int, width: int, height: int, slot: int, overlays=(), show_rsi=False) -> list[Text]:
    """The chart as `height` lines of `width` cells: price panel with a right axis, volume, optional RSI, UTC times.

    Each candle owns `slot` columns, newest on the right; its slot follows from its time, so missing candles leave
    gaps. `overlays` are (legend, colour, series aligned with `candles`); values outside the candles' range are not
    drawn, so they never squash the scale.
    """
    if not candles or width < 1 or height < 1:
        return []
    legend = bool(overlays) or show_rsi
    rows = height - legend - VOLUME_ROWS - 2 - (RSI_ROWS + 1) * show_rsi  # 2: separator + time axis
    # widest label over all candles bounds every label in view (fmt_price width only grows away from 1-1000)
    label_w = max(len(fmt_price(min(min(c.o, c.l, c.c) for c in candles))),
                  len(fmt_price(max(max(c.o, c.h, c.c) for c in candles))))
    plot_w = width - label_w - 3
    if rows < MIN_PRICE_ROWS or plot_w < slot:
        return [Text(TOO_SMALL[:width])]

    n = plot_w // slot
    x0, body = plot_w - n * slot, max(1, slot - 1)
    mid = body // 2  # wick column inside the body
    last = candles[-1]
    shown = []  # (slot, index into candles, candle), newest first
    for i in range(len(candles) - 1, -1, -1):
        s = n - 1 - int((last.t - candles[i].t) // granularity)
        if s < 0:
            break
        shown.append((s, i, candles[i]))
    hi = max(max(c.o, c.h, c.c) for _, _, c in shown)
    lo = min(min(c.o, c.l, c.c) for _, _, c in shown)

    chars = [[" "] * width for _ in range(height)]
    styles = [[""] * width for _ in range(height)]

    def put(r, x, ch, style):
        chars[r][x], styles[r][x] = ch, style

    def write(r, x, text, style):
        for j, ch in enumerate(text[:max(0, width - x)]):
            put(r, x + j, ch, style)

    def hline(r, ch, style):
        for x in range(plot_w):
            put(r, x, ch, style)

    def y(price):
        return half_row(price, hi, lo, rows)

    top = int(legend)
    vol_top = top + rows + 1
    rsi_top = vol_top + VOLUME_ROWS + 1
    axis = plot_w + 2
    panels = [*range(top, top + rows), *range(vol_top, vol_top + VOLUME_ROWS)]
    if show_rsi:
        panels += range(rsi_top, rsi_top + RSI_ROWS)

    # time labels under their candle's wick column; format follows the visible span
    span = n * granularity
    fmt = "%H:%M" if span <= 6 * 3600 else "%b %d %H:%M" if span <= 3 * 86400 else "%b %d"
    label_len = len(datetime.fromtimestamp(0, timezone.utc).strftime(fmt))
    shortest = 86400 if fmt == "%b %d" else granularity  # a date-only label every 12h would repeat itself
    step = next((t for t in TIME_STEPS if t >= shortest and t % granularity == 0
                 and t // granularity * slot > label_len + 2), None)
    labels, end = [], -1
    for s in range(n if step else 0):
        t = last.t - (n - 1 - s) * granularity
        x = x0 + s * slot + mid
        if t % step == 0 and x > end and x + label_len <= width:
            labels.append((x, datetime.fromtimestamp(t, timezone.utc).strftime(fmt)))
            end = x + label_len

    # grid, then overlays, then the last-price line, then candles: each layer overwrites the one before
    for x, _ in labels:
        for r in panels:
            put(r, x, "┆", GRID)
    tick_rows = {y(v) // 2 for v in nice_ticks(lo, hi, max(1, min(MIN_TICKS, rows // 4)))}
    for r in tick_rows:
        hline(top + r, "┄", GRID)
    hline(vol_top - 1, "─", GRID)

    for _, colour, series in overlays:
        for s, i, _ in shown:
            v = series[i]
            if v is not None and lo <= v <= hi:
                k = y(v)
                for x in range(x0 + s * slot, x0 + (s + 1) * slot):
                    put(top + k // 2, x, DOTS[k % 2], colour)

    up = last.c >= last.o
    close_row = top + y(last.c) // 2
    for x in range(plot_w):
        if chars[close_row][x] in " ┄┆":
            put(close_row, x, "╌", VOL_GREEN if up else VOL_RED)

    for s, _, c in shown:
        colour = GREEN if c.c >= c.o else RED
        kh, kl = y(c.h), y(c.l)
        b0, b1 = sorted((y(c.o) // 2, y(c.c) // 2))  # body rows: every row the open-close range touches
        left = x0 + s * slot
        for r in range(kh // 2, kl // 2 + 1):
            if b0 <= r <= b1:
                for x in range(left, left + body):
                    put(top + r, x, " ", f"on {colour}")  # background fills the whole cell, gaps and all
            else:
                put(top + r, left + mid, WICK[(2 * r >= kh, 2 * r + 1 <= kl)], colour)

    # right axis: each grid label is the exact price of its line; the tag is the exact last price
    for r in tick_rows:
        if top + r != close_row:
            write(top + r, axis, fmt_price(row_price(r, hi, lo, rows)).rjust(label_w), AXIS)
    write(close_row, axis - 1, f" {fmt_price(last.c).rjust(label_w)} ", f"black on {GREEN if up else RED}")

    vmax = max(c.v for _, _, c in shown)
    for s, _, c in shown if vmax > 0 else ():
        eighths = round(c.v / vmax * VOLUME_ROWS * 8)
        for j in range(VOLUME_ROWS):
            level = min(8, eighths - 8 * j)
            if level <= 0:
                break
            colour = VOL_GREEN if c.c >= c.o else VOL_RED
            for x in range(x0 + s * slot, x0 + s * slot + body):  # full cells as background, like candle bodies
                put(vol_top + VOLUME_ROWS - 1 - j, x, *((" ", f"on {colour}") if level == 8 else (BLOCKS[level], colour)))
    if vmax > 0 and len(fmt_price(vmax)) <= label_w:
        write(vol_top, axis, fmt_price(vmax).rjust(label_w), AXIS)

    if show_rsi:
        hline(rsi_top - 1, "─", GRID)
        for level in (70, 30):
            r = rsi_top + half_row(level, 100, 0, RSI_ROWS) // 2
            hline(r, "┄", AXIS)
            write(r, axis, str(level).rjust(label_w), AXIS)
        values = rsi([c.c for c in candles])
        for s, i, _ in shown:
            if values[i] is not None:
                k = half_row(values[i], 100, 0, RSI_ROWS)
                for x in range(x0 + s * slot, x0 + (s + 1) * slot):
                    put(rsi_top + k // 2, x, DOTS[k % 2], AMBER)

    if legend:
        x = 1
        for name, colour in [(name, colour) for name, colour, _ in overlays] + [("RSI 14", AMBER)] * show_rsi:
            write(0, x, name, colour)
            x += len(name) + 3
    for x, text in labels:
        write(height - 1, x, text, AXIS)

    return [Text().join(Text("".join(ch for ch, _ in run), style) for style, run in groupby(zip(cs, ss), itemgetter(1)))
            for cs, ss in zip(chars, styles)]
