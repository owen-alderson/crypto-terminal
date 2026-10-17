"""The MCP server, through an in-process MCP client, with the network stubbed out."""

import json
import os

import anyio
import pytest
from mcp import Client

from crypto_terminal import app as app_module
from crypto_terminal import mcp_server, snapshot
from crypto_terminal.mcp_server import build


@pytest.fixture
def server(tmp_path, monkeypatch):
    for name, file in (("WATCHLIST_FILE", "watchlist.json"), ("CONFIG_FILE", "config.json"),
                       ("ALERTS_FILE", "alerts.json"), ("WATCH_PID_FILE", "watch.pid")):
        monkeypatch.setattr(app_module, name, tmp_path / file)

    async def exists(client, symbol):
        return symbol != "ZZQ-USD"

    async def price(client, symbol):
        return {"symbol": symbol, "price": 81266.22}

    async def book(client, symbol, depth, group):
        raise ValueError("group for BTC-USD must be one of: 0.01, 10")

    monkeypatch.setattr(mcp_server, "product_exists", exists)
    monkeypatch.setattr(snapshot, "price", price)
    monkeypatch.setattr(snapshot, "book", book)
    return build()


def call(server, tool, **args):
    """The tool's result, or ("error", text) when the server refused the call."""
    async def go():
        async with Client(server) as client:
            if tool is None:
                return [t.name for t in (await client.list_tools()).tools]
            res = await client.call_tool(tool, args)
            if res.is_error:
                return "error", res.content[0].text.removeprefix(f"Error executing tool {tool}: ")
            if res.structured_content:
                return res.structured_content.get("result", res.structured_content)
            return json.loads(res.content[0].text)
    return anyio.run(go)


def test_lists_tools(server):
    assert sorted(call(server, None)) == ["book", "list_alerts", "perps", "price", "remove_alert", "set_alert",
                                          "venues"]


def test_snapshot_tools_and_their_refusals(server):
    assert call(server, "price", symbols=["BTC-USD", "ETH-USD"]) == [{"symbol": "BTC-USD", "price": 81266.22},
                                                                      {"symbol": "ETH-USD", "price": 81266.22}]
    kind, text = call(server, "book", symbol="BTC-USD", group=7)
    assert kind == "error" and "must be one of: 0.01, 10" in text  # the model sees why


def test_alerts_round_trip_through_the_terminals_file(server):
    got = call(server, "set_alert", alert="alert btc-usd > 90000")
    assert got["set"] == "BTC-USD > 90,000.00" and got["number"] == 1
    move = call(server, "set_alert", alert="ETH-USD move 5%")  # one-shot move: counts from the price now
    assert move["set"] == "ETH-USD moves ±5% from 81,266.22"
    assert [a.kind for a in app_module.load_alerts()] == [">", "move"]  # what the terminal and watch read
    listed = call(server, "list_alerts")
    assert [(a["number"], a["repeats"]) for a in listed] == [(1, False), (2, False)] and listed[1]["ref"] == 81266.22
    assert call(server, "remove_alert", number=1, alert=move["set"]) == (
        "error", f"alert 1 is now BTC-USD > 90,000.00, not {move['set']}: call list_alerts again")
    gone = call(server, "remove_alert", number=1, alert="BTC-USD > 90,000.00")
    assert gone["removed"] == "BTC-USD > 90,000.00" and [a["alert"] for a in gone["alerts"]] == [move["set"]]
    assert call(server, "remove_alert", number=3, alert="x") == ("error", "no alert 3: there is 1")


def test_set_alert_refusals(server):
    kind, text = call(server, "set_alert", alert="BTC-USD bogus 5")
    assert kind == "error" and text.startswith("usage: alert BTC-USD")
    assert call(server, "set_alert", alert="ZZQ-USD > 1") == ("error", "unknown pair: ZZQ-USD")
    assert call(server, "set_alert", alert="BTC-EUR funding 0.05%") == (
        "error", "funding alerts need a USD or USDT pair: there's no USDT perp for BTC-EUR")
    assert app_module.load_alerts() == []


def test_set_alert_says_what_will_check_it(server):
    app_module.save_watchlist(["BTC-USD"])
    assert "terminal is open" in call(server, "set_alert", alert="BTC-USD spread 5bp")["fires"]
    assert "won't fire yet" in call(server, "set_alert", alert="SOL-USD liq 1m")["fires"]
    app_module.WATCH_PID_FILE.write_text(str(os.getpid()))  # a live pid: watch is running
    assert call(server, "set_alert", alert="SOL-USD print 500k")["fires"] == \
        f"checked now by crypto-terminal watch (pid {os.getpid()})"
