"""Portfolio: what you hold of each pair and at what average cost, valued live in the pair's own quote currency.

Amounts are Decimals parsed from what the user typed, so 0.1 BTC stays exactly 0.1. Totals are kept per quote
currency (USD, EUR, BTC...): there's no FX feed, so they are never converted or added together.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from rich.text import Text

from .chart import fmt_price
from .theme import GREEN, RED


@dataclass(frozen=True)
class Holding:
    symbol: str
    qty: Decimal
    cost: Decimal | None = None  # average cost per unit, in the pair's quote currency

    def value(self, price: float) -> Decimal:
        return self.qty * Decimal(repr(price))  # repr is the shortest exact form of the quoted price

    def pnl(self, price: float) -> Decimal | None:
        return None if self.cost is None else self.value(price) - self.qty * self.cost


def parse_amount(text) -> Decimal:
    """Strict: a positive, finite number as typed (`0.5`, `62000`, `1e-3`), else ValueError."""
    try:
        value = Decimal(text) if isinstance(text, str) else None
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value <= 0:
        raise ValueError(f"bad number: {text!r}")
    return value


@dataclass
class Total:
    """One quote currency's holdings. `pnl` is only given when every holding has a price and a cost:
    a total over some of them would look like the whole portfolio and be wrong."""
    quote: str
    value: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)
    unpriced: int = 0  # no tick yet, or the pair has left the watchlist
    without_cost: int = 0

    @property
    def pnl(self) -> Decimal | None:
        return None if self.unpriced or self.without_cost else self.value - self.cost


def totals(holdings, price_of) -> list[Total]:
    """price_of(symbol) -> last price or None."""
    out: dict[str, Total] = {}
    for h in holdings:
        total = out.setdefault(h.symbol.partition("-")[2], Total(h.symbol.partition("-")[2]))
        price = price_of(h.symbol)
        if price is None:
            total.unpriced += 1
            continue
        total.value += h.value(price)
        if h.cost is None:
            total.without_cost += 1
        else:
            total.cost += h.qty * h.cost
    return list(out.values())


def fmt_signed(value: Decimal) -> str:
    return f"{'+' if value >= 0 else '-'}{fmt_price(abs(value))}"


def fmt_pnl(pnl: Decimal, cost: Decimal) -> Text:
    return Text(f"{fmt_signed(pnl)} {pnl / cost * 100:+.2f}%", style=GREEN if pnl >= 0 else RED)


def fmt_total(total: Total) -> Text:
    text = Text(f"{total.quote} {fmt_price(total.value)}")
    if total.unpriced:
        text.append(f" + {total.unpriced} unpriced")
    elif total.without_cost:
        text.append(f" · {total.without_cost} without cost")
    else:
        text.append(" ").append_text(fmt_pnl(total.pnl, total.cost))
    return text
