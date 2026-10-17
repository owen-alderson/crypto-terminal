"""`crypto-terminal price | book | venues | perps | mcp`, with the snapshots stubbed out."""

import builtins
import json
import sys

import pytest

from crypto_terminal import __main__ as cli
from crypto_terminal import snapshot

PRICE = {"symbol": "BTC-USD", "price": 81266.22, "change_24h_pct": 0.218, "open_24h": 81089.2, "bid": 81266.21,
         "ask": 81266.22, "spread_bp": 0.00123, "last_trade": "2026-10-17T17:54:48Z", "exchange": "Coinbase"}


def run(monkeypatch, capsys, *args) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["crypto-terminal", *args])
    code = 0
    try:
        cli.main()
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def stubbed(monkeypatch):
    async def price(client, symbol):
        if symbol == "ZZQ-USD":
            raise LookupError("unknown pair: ZZQ-USD")
        return {**PRICE, "symbol": symbol}

    async def book(client, symbol, depth, group):
        return {"symbol": symbol, "group": group or 10, "bid": 81266.21, "ask": 81266.22, "mid": 81266.215,
                "spread_bp": 0.00123, "asks": [[81270.0, 0.5], [81280.0, 1.25]][:depth],
                "bids": [[81260.0, 2.0], [81250.0, 0.1]][:depth], "time": "t", "exchange": "Coinbase"}

    async def venues(client, symbol):
        return {"symbol": symbol, "coinbase": {"bid": 81266.21, "ask": 81266.22, "mid": 81266.215}, "usdt_rate": 0.99875,
                "venues": [{"exchange": "Binance", "pair": "BTCUSDT", "quote": "USDT", "bid": 81370.0, "ask": 81370.2,
                            "vs_coinbase_bp": 0.71, "status": "live"},
                           {"exchange": "OKX", "pair": "BTC-USDT", "quote": "USDT", "bid": None, "ask": None,
                            "vs_coinbase_bp": None, "status": "no data within 10s"}], "note": ""}

    async def perps(symbol):
        return {"symbol": symbol, "perp": "BTCUSDT", "note": "", "venues": [
            {"exchange": "Binance", "funding_pct": 0.01, "next_funding": "2026-10-18T00:00:00+00:00", "interval_h": 8,
             "mark": 1.0, "index": 1.0, "premium_bp": -4.4, "open_interest": 94138.6, "status": "live"},
            {"exchange": "Bybit", "funding_pct": None, "next_funding": None, "interval_h": None, "mark": None,
             "index": None, "premium_bp": None, "open_interest": None, "status": "not listed (BTCUSDT)"}]}

    for name, fn in (("price", price), ("book", book), ("venues", venues), ("perps", perps)):
        monkeypatch.setattr(snapshot, name, fn)


def test_price_json_is_a_list_per_pair(monkeypatch, capsys, stubbed):
    code, out, _ = run(monkeypatch, capsys, "price", "BTC-USD", "ETH-USD", "--json")
    assert code == 0 and [row["symbol"] for row in json.loads(out)] == ["BTC-USD", "ETH-USD"]
    code, out, _ = run(monkeypatch, capsys, "price", "BTC-USD")
    assert "81,266.22" in out and "+0.22% 24h" in out and "spread 0.0012 bp" in out


def test_errors_exit_1_with_the_reason(monkeypatch, capsys, stubbed):
    code, out, err = run(monkeypatch, capsys, "price", "BTC-USD", "ZZQ-USD")
    assert code == 1 and out == "" and err == "crypto-terminal: unknown pair: ZZQ-USD\n"
    code, _, err = run(monkeypatch, capsys, "price")
    assert code == 2 and "PAIR" in err


def test_book(monkeypatch, capsys, stubbed):
    code, out, _ = run(monkeypatch, capsys, "book", "BTC-USD", "--depth", "2", "--group", "10")
    lines = out.splitlines()
    assert code == 0 and lines[0] == "BTC-USD book on Coinbase · grouped by 10 · spread 0.0012 bp"
    assert lines[1:] == ["  ask 81,280.00  1.25", "  ask 81,270.00  0.5", "  ─── best bid 81,266.21 · ask 81,266.22",
                         "  bid 81,260.00  2", "  bid 81,250.00  0.1"]  # best ask nearest the middle
    code, out, _ = run(monkeypatch, capsys, "book", "BTC-USD", "--json")
    assert json.loads(out)["group"] == 10


def test_venues_and_perps_say_what_is_missing(monkeypatch, capsys, stubbed):
    _, out, _ = run(monkeypatch, capsys, "venues", "BTC-USD")
    assert "Binance  bid 81,370.00 · ask 81,370.20 USDT  +0.71 bp vs Coinbase" in out
    assert "OKX      no data within 10s" in out and "converted at 0.99875 USD" in out
    _, out, _ = run(monkeypatch, capsys, "perps", "BTC-USD")
    assert "(BTCUSDT, not spot)" in out and "funding +0.0100%" in out and "OI 94,138.6 BTC" in out
    assert "Bybit    not listed (BTCUSDT)" in out


def test_mcp_without_package_explains(monkeypatch, capsys):
    real_import = builtins.__import__

    def no_mcp(name, *args, **kwargs):
        if name.endswith("mcp_server") or (args and args[2] and "mcp_server" in args[2]):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_mcp)
    code, _, err = run(monkeypatch, capsys, "mcp")
    assert code == 1 and "crypto-terminal[mcp]" in err


def test_no_command_opens_the_terminal_and_watch_still_works(monkeypatch, capsys):
    from crypto_terminal import app

    opened = []
    monkeypatch.setattr(app.TerminalApp, "run", lambda self: opened.append("tui"))
    monkeypatch.setattr(cli, "run_watch", lambda: (opened.append("watch"), sys.exit(0)))
    assert run(monkeypatch, capsys)[0] == 0 and run(monkeypatch, capsys, "watch")[0] == 0
    assert opened == ["tui", "watch"]
