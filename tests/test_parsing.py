import json

import pytest

from crypto_terminal import app as app_module
from crypto_terminal.app import load_indicators, load_watchlist, parse_command, save_indicators, save_watchlist
from crypto_terminal.feed import parse_tick
from crypto_terminal.widgets import fmt_price, sparkline

TICKER = {
    "type": "ticker", "product_id": "BTC-USD", "price": "85359.13", "open_24h": "84864.78",
    "time": "2026-10-04T12:07:17.878035Z",
}


def test_parse_tick_valid():
    tick = parse_tick(TICKER)
    assert tick.symbol == "BTC-USD"
    assert tick.price == 85359.13
    assert tick.open_24h == 84864.78


@pytest.mark.parametrize("override", [
    {"type": "heartbeat"},
    {"price": None},
    {"price": "abc"},
    {"price": "NaN"},
    {"price": "-1"},
    {"open_24h": "0"},
    {"price": "inf"},
    {"open_24h": "NaN"},
    {"product_id": 7},
])
def test_parse_tick_rejects_malformed(override):
    assert parse_tick({**TICKER, **override}) is None


@pytest.mark.parametrize("bid,ask,expected", [
    ("85359.12", "85359.13", (85359.12, 85359.13)),
    ("85359.14", "85359.13", (None, None)),  # crossed: no spread rather than a negative one
    ("0", "85359.13", (None, None)),
    ("abc", "1", (None, None)),
    (None, None, (None, None)),
])
def test_parse_tick_best_bid_ask(bid, ask, expected):
    tick = parse_tick({**TICKER, "best_bid": bid, "best_ask": ask})
    assert tick.price == 85359.13 and (tick.bid, tick.ask) == expected


def test_parse_tick_rejects_missing_field():
    assert parse_tick({k: v for k, v in TICKER.items() if k != "price"}) is None


@pytest.mark.parametrize("text,expected", [
    ("add SOL-USD", ("add", "SOL-USD")),
    ("  ADD   sol-usd ", ("add", "SOL-USD")),
    ("rm ADA-USD", ("rm", "ADA-USD")),
    ("quit", ("quit", None)),
    ("add sol", ("find", "SOL")),
    ("add Solana", ("find", "SOLANA")),
    ("ind SMA20 rsi", ("ind", ["sma20", "rsi"])),
    ("ind off", ("ind", [])),
    ("push my-topic", ("push", "https://ntfy.sh/my-topic")),
    ("push off", ("push", "")),
    ("push", ("push", None)),
])
def test_parse_command_valid(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "add", "add SOL-USD ETH-USD", "rm SOL", "add sol!", "add ../../etc", "add SOL-USD?x=1",
    "rm", "buy BTC-USD", "quit now", "ind", "ind macd", "ind off rsi", "push a b", "push http://x/y",
])
def test_parse_command_invalid(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_watchlist_roundtrip_and_bad_files(tmp_path, monkeypatch):
    path = tmp_path / "crypto-terminal" / "watchlist.json"  # config dir is created on first save
    monkeypatch.setattr(app_module, "WATCHLIST_FILE", path)
    assert load_watchlist() == app_module.DEFAULT_WATCHLIST  # missing file
    save_watchlist(["BTC-USD", "DOGE-USD"])
    assert load_watchlist() == ["BTC-USD", "DOGE-USD"]
    path.write_text("{not json")
    assert load_watchlist() == app_module.DEFAULT_WATCHLIST
    path.write_text('["BTC-USD"]')
    assert load_watchlist() == app_module.DEFAULT_WATCHLIST
    path.write_text('{"symbols": ["BTC-USD", "BTC-USD", 5, "bad pair", "ETH-USD"]}')
    assert load_watchlist() == ["BTC-USD", "ETH-USD"]


def test_sparkline():
    assert sparkline([5, 5]) == "▁▁"
    assert sparkline([1, 2, 3]) == "▁▅█"


def test_indicators_config_roundtrip_and_bad_files(tmp_path, monkeypatch):
    path = tmp_path / "crypto-terminal" / "config.json"
    monkeypatch.setattr(app_module, "CONFIG_FILE", path)
    assert load_indicators() == []
    save_indicators({"rsi", "sma20"})
    assert load_indicators() == ["sma20", "rsi"]
    for bad in ("{not json", '["rsi"]', '{"indicators": "rsi"}'):
        path.write_text(bad)
        assert load_indicators() == []
    path.write_text('{"indicators": ["rsi", "macd", 5, "rsi"]}')
    assert load_indicators() == ["rsi"]


@pytest.mark.parametrize("value,expected", [
    (85231.194, "85,231.19"),
    (1000, "1,000.00"),
    (121.69, "121.69"),
    (1.5018, "1.5018"),
    (0.2459, "0.24590"),
    (0.00213, "0.0021300"),     # e.g. SOL-BTC: must not round to 0.0000
    (0.0000123456, "0.000012346"),
    (0, "0.00"),
])
def test_fmt_price_significant_figures(value, expected):
    assert fmt_price(value) == expected


def test_a_save_that_dies_midway_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(app_module, "WATCHLIST_FILE", path)
    save_watchlist(["BTC-USD", "ETH-USD"])
    real_write = type(path).write_text

    def dies_midway(self, text, *args, **kwargs):  # disk full / power cut: half the bytes, then an error
        real_write(self, text[: len(text) // 2])
        raise OSError("No space left on device")

    monkeypatch.setattr(type(path), "write_text", dies_midway)
    with pytest.raises(OSError):
        save_watchlist(["SOL-USD"])
    monkeypatch.undo()
    assert json.loads(path.read_text()) == {"symbols": ["BTC-USD", "ETH-USD"]}
