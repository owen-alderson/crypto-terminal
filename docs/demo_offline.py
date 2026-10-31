"""Run the terminal on made-up data, for recording the README demo (`vhs docs/demo.tape`).

Exchange data terms don't allow showing their market data to other people, so the public GIF and screenshots use
this instead: a seeded random walk per pair, fed through the app's real parsers in each exchange's message format.
Nothing here touches the network (sockets refuse to connect). Not part of the package.
"""

import asyncio
import random
import socket
import time
from datetime import datetime, timezone

from textual.widgets import Static


BLOCKED = []  # connection attempts refused (there should be none)


def no_network(*args, **kwargs):
    BLOCKED.append(args[1:] or kwargs)
    raise RuntimeError("demo: network is blocked")


socket.socket.connect = socket.socket.connect_ex = no_network  # before anything can open a connection
socket.create_connection = no_network

from crypto_terminal import app as app_module  # noqa: E402
from crypto_terminal.book import BookFeed  # noqa: E402
from crypto_terminal.derivs import OI_WINDOW, BinancePerp, BybitPerp  # noqa: E402
from crypto_terminal.feed import Tick  # noqa: E402
from crypto_terminal.history import MAX_CANDLES, Candle, Product  # noqa: E402
from crypto_terminal.venues import OKX, Binance, Bybit, Kraken  # noqa: E402

rng = random.Random(7)
# base: (made-up USD price, price step, coin name)
COINS = {"BTC": (50_000.0, 0.01, "Bitcoin"), "ETH": (2_600.0, 0.01, "Ethereum"), "XRP": (0.62, 0.0001, "XRP"),
         "SOL": (120.0, 0.01, "Solana"), "ADA": (0.45, 0.0001, "Cardano"), "DOGE": (0.12, 0.00001, "Dogecoin"),
         "USDT": (1.0003, 0.00001, "Tether")}
EUR, USDT = 0.92, 1.0003
PRODUCTS = [Product(f"{base}-{quote}", base, quote, name) for base, (_, _, name) in COINS.items()
            for quote in ("USD", "EUR", "BTC") if base != quote and not (base == "USDT" and quote != "USD")]


class Pair:
    def __init__(self, symbol):
        base, _, quote = symbol.partition("-")
        usd, self.step, _ = COINS.get(base, (1.0, 0.0001, base))
        scale = {"USD": 1, "EUR": EUR, "BTC": 1 / COINS["BTC"][0]}[quote]
        self.step = self.step if quote != "BTC" else 1e-8
        self.mid = usd * scale
        self.vol = 0.000005 if base == "USDT" else 0.0001  # per step
        self.open_24h = self.mid * (1 + rng.uniform(-0.04, 0.03))

    def move(self):
        self.mid *= 1 + rng.gauss(0, self.vol)
        return self.mid

    def round(self, price):
        return round(round(price / self.step) * self.step, 10)

    def text(self, price):
        decimals = max(0, len(f"{self.step:.10f}".rstrip("0").split(".")[1]))
        return f"{self.round(price):.{decimals}f}"


PAIRS: dict[str, Pair] = {}


def pair(symbol) -> Pair:
    if symbol not in PAIRS:
        PAIRS[symbol] = Pair(symbol)
    return PAIRS[symbol]


def usd_mid(symbol) -> float:
    """Mid in USD (for venues/perps), from the USD pair's walk."""
    return pair(symbol.split("-")[0] + "-USD").mid


async def tick_forever():
    while True:
        for p in list(PAIRS.values()):
            p.move()
        await asyncio.sleep(0.3)


class DemoFeed:
    def __init__(self, symbols, on_tick, on_status, trade_symbols=(), on_trade=None, **kwargs):
        self.symbols, self.on_tick, self.on_status, self.last_msg = list(symbols), on_tick, on_status, 0.0

    async def run(self):
        self.on_status("live")
        while True:
            self.last_msg = time.monotonic()
            for symbol in self.symbols:
                p = pair(symbol)
                half = max(p.step, p.mid * 0.00004)
                self.on_tick(Tick(symbol, p.round(p.mid), p.open_24h, p.round(p.mid - half), p.round(p.mid + half)))
            await asyncio.sleep(0.3)


class DemoBookFeed(BookFeed):
    async def run(self):
        p, trade_id = pair(self.symbol), 1_000_000
        unit = 50_000 / usd_mid(self.symbol)  # sizes: about the same dollar amounts on every pair
        gap = max(p.step, p.round(p.mid * 0.00002))  # about 0.2 bp between levels
        offsets = [0]
        for _ in range(59):
            offsets.append(offsets[-1] + gap * rng.choice((1, 1, 1, 2, 3)))
        sizes = {side: [rng.expovariate(1 / 1.2) * unit for _ in offsets] for side in ("bids", "asks")}
        self.last_msg = time.monotonic()
        self._status("live")
        while True:
            self.last_msg = time.monotonic()
            best_bid = p.round(p.mid - p.step / 2)
            for side in sizes.values():
                for i in range(len(side)):
                    if rng.random() < 0.15:
                        side[i] = rng.expovariate(1 / 1.2) * unit
            self.handle({"type": "snapshot", "product_id": self.symbol,
                         "bids": [[p.text(best_bid - o), f"{s:.8f}"] for o, s in zip(offsets, sizes["bids"])],
                         "asks": [[p.text(best_bid + p.step + o), f"{s:.8f}"] for o, s in zip(offsets, sizes["asks"])]})
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            for _ in range(rng.randint(0, 3)):
                trade_id += 1
                buy = rng.random() < 0.55
                self.handle({"type": "match", "product_id": self.symbol, "trade_id": trade_id, "time": now,
                             "side": "sell" if buy else "buy", "price": p.text(best_bid + (p.step if buy else 0)),
                             "size": f"{rng.expovariate(1 / 0.08) * unit:.8f}", "taker_order_id": f"t{trade_id}"})
            await asyncio.sleep(0.4)


def demo_venue(cls, offset_bp, message):
    class Demo(cls):
        def __init__(self, symbol, url=None):
            self.coinbase_symbol = symbol
            super().__init__(symbol, url)

        async def run(self):
            if not self.pair:
                self._status("not listed")
                return
            self.last_msg = time.monotonic()
            self._status("live")
            while True:
                self.last_msg = time.monotonic()
                mid = usd_mid(self.coinbase_symbol)
                mid = mid * (1 + (offset_bp + rng.gauss(0, 0.4)) / 10_000) / (USDT if self.USD_IN_STABLE else 1)
                spread = mid * 0.00001
                self.handle(message(self.pair, f"{mid - spread:.2f}", f"{mid + spread:.2f}"))
                await asyncio.sleep(0.5)
    Demo.NAME = cls.NAME
    return Demo


VENUES = (
    demo_venue(Kraken, 0.6, lambda s, b, a: {"channel": "ticker",
                                            "data": [{"symbol": s, "bid": float(b), "ask": float(a)}]}),
    demo_venue(Binance, 0.8, lambda s, b, a: {"s": s, "b": b, "a": a}),
    demo_venue(Bybit, 0.3, lambda s, b, a: {"topic": f"orderbook.1.{s}", "type": "snapshot",
                                           "data": {"b": [[b, "1"]], "a": [[a, "1"]]}}),
    demo_venue(OKX, 0.2, lambda s, b, a: {"arg": {"instId": s}, "data": [{"bids": [[b, "1"]], "asks": [[a, "1"]]}]}),
)


def demo_perp(cls, oi_coins):
    class Demo(cls):
        async def run(self):
            if not self.pair:
                self._status("no USDT perp")
                return
            self.last_msg = time.monotonic()
            self._status("live")
            self.interval = 8
            oi = oi_coins * 50_000 / usd_mid(self.symbol)
            self.oi_samples.append((time.monotonic() - OI_WINDOW - 30, oi * 0.985))  # so OI Δ15m shows
            funding = rng.uniform(0.00002, 0.00009)
            next_ms = (int(time.time()) // 28_800 + 1) * 28_800_000
            while True:
                self.last_msg = time.monotonic()
                index = usd_mid(self.symbol) / USDT
                mark = index * (1 - rng.uniform(2, 6) / 10_000)
                funding += rng.gauss(0, 0.000002)
                oi *= 1 + rng.gauss(0.0002, 0.0005)
                self.feed_numbers(mark, index, funding, next_ms, oi)
                if rng.random() < 0.12:
                    size = round(rng.expovariate(1 / 0.6) * 50_000 / index, 3) or 0.001
                    self.feed_liq(int(time.time() * 1000), rng.random() < 0.6, size, mark)
                await asyncio.sleep(1)
    return Demo


class DemoBinancePerp(demo_perp(BinancePerp, 90_000)):
    def feed_numbers(self, mark, index, funding, next_ms, oi):
        self.handle({"e": "markPriceUpdate", "s": self.pair, "p": f"{mark:.2f}", "i": f"{index:.2f}",
                     "r": f"{funding:.8f}", "T": next_ms})
        self.add_oi(oi)  # Binance's open interest comes from a REST poll in the real feed

    def feed_liq(self, ms, long, size, price):
        self.handle({"e": "forceOrder", "o": {"s": self.pair, "X": "FILLED", "T": ms, "S": "SELL" if long else "BUY",
                                              "z": f"{size}", "ap": f"{price:.2f}"}})


class DemoBybitPerp(demo_perp(BybitPerp, 30_000)):
    def feed_numbers(self, mark, index, funding, next_ms, oi):
        self.handle({"topic": f"tickers.{self.pair}", "type": "snapshot",
                     "data": {"markPrice": f"{mark:.2f}", "indexPrice": f"{index:.2f}", "fundingIntervalHour": "8",
                              "singleOpenInterest": f"{oi:.3f}", "fundingRate": f"{funding:.8f}",
                              "nextFundingTime": str(next_ms)}})

    def feed_liq(self, ms, long, size, price):
        self.handle({"topic": f"allLiquidation.{self.pair}",
                     "data": [{"s": self.pair, "T": ms, "S": "Buy" if long else "Sell", "v": f"{size}",
                               "p": f"{price:.2f}"}]})


async def fake_products(client):
    return PRODUCTS


async def fake_exists(client, symbol):
    return any(p.id == symbol for p in PRODUCTS)


async def fake_candles(client, symbol, granularity):
    """A random walk that ends at the pair's current price, so the live bar carries on from it."""
    p, walk = pair(symbol), random.Random(f"{symbol}{granularity}")
    vol = p.vol * (granularity / 0.3) ** 0.5 * 0.35
    closes = [p.mid]
    for _ in range(MAX_CANDLES - 1):
        closes.append(closes[-1] / (1 + walk.gauss(0, vol)))
    closes.reverse()
    now = time.time()
    start = now - now % granularity - (MAX_CANDLES - 1) * granularity
    candles, prev = [], closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) * (1 + abs(walk.gauss(0, vol / 2)))
        low = min(prev, close) * (1 - abs(walk.gauss(0, vol / 2)))
        volume = walk.uniform(5, 60) * 50_000 / usd_mid(symbol)
        candles.append(Candle(start + i * granularity, prev, high, low, close, volume))
        prev = close
    return candles


async def no_push(client, url, message):
    return None


class DemoApp(app_module.TerminalApp):
    def compose(self):
        for widget in super().compose():
            if getattr(widget, "id", None) == "title":
                widget = Static("CRYPTO TERMINAL · DEMO DATA (made up, not a real market) · [1-6] [ ] timeframe · "
                                "[f] view · [b] book [g] group · [x] exchanges [d] perps · [/] command",
                                id="title", markup=False)
            yield widget

    def on_mount(self):
        asyncio.get_running_loop().create_task(tick_forever())


for name, value in {"Feed": DemoFeed, "BookFeed": DemoBookFeed, "VENUES": VENUES,
                    "PERPS": (DemoBinancePerp, DemoBybitPerp), "fetch_products": fake_products,
                    "product_exists": fake_exists, "fetch_candles": fake_candles, "push": no_push,
                    "desktop_notify": lambda title, message: None}.items():
    setattr(app_module, name, value)

if __name__ == "__main__":
    DemoApp().run()
