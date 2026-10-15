import json

import httpx
import pytest

from crypto_terminal import alerts as alerts_module
from crypto_terminal import app as app_module
from crypto_terminal.alerts import Alert, Watcher, desktop_notify, push, push_url, valid_alert
from crypto_terminal.book import Trade
from crypto_terminal.feed import Tick
from crypto_terminal.app import load_alerts, parse_command, save_alerts


@pytest.mark.parametrize("text,expected", [
    ("alert BTC-USD > 90000", Alert("BTC-USD", ">", 90000)),
    ("alert btc-usd < 80000.5", Alert("BTC-USD", "<", 80000.5)),
    ("alert BTC-USD move 5%", Alert("BTC-USD", "move", 5)),
    ("ALERT BTC-USD MOVE 0.5", Alert("BTC-USD", "move", 0.5)),
])
def test_parse_alert(text, expected):
    assert parse_command(text) == ("alert", expected)


@pytest.mark.parametrize("text", [
    "alert", "alert BTC-USD", "alert BTC-USD > ", "alert BTC-USD >= 5", "alert BTC > 5", "alert BTC-USD > abc",
    "alert BTC-USD > 0", "alert BTC-USD > -5", "alert BTC-USD > nan", "alert BTC-USD > inf",
    "alert BTC-USD move 0", "alert BTC-USD move 100", "alert BTC-USD move 150%", "alert BTC-USD move nan%",
    "alert BTC-USD > 5%", "alerts now", "unalert", "unalert 0", "unalert -1", "unalert x", "unalert 1 2",
])
def test_parse_alert_invalid(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_parse_alerts_and_unalert():
    assert parse_command("alerts") == ("alerts", None)
    assert parse_command("unalert 2") == ("unalert", 2)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fired(results):
    return [f.alert for f in results]


def tick(price, bid=None, ask=None, symbol="BTC-USD"):
    return Tick(symbol, price, price, bid, ask)


def trade(id, size, price=100.0, taker="a", buy=True):
    return Trade(id, "12:00:00", price, size, buy, 2, taker=taker)


def test_cross_fires_once_and_is_removed():
    above, below = Alert("BTC-USD", ">", 100), Alert("BTC-USD", "<", 90)
    other = Alert("ETH-USD", ">", 1)
    w = Watcher([above, below, other])
    assert w.on_tick(tick(99.99)) == []
    results = w.on_tick(tick(100))
    assert fired(results) == [above] and w.alerts == [below, other]
    assert results[0].message == "BTC-USD > 100.00 · now 100.00"
    assert w.on_tick(tick(101)) == []  # gone: does not fire again
    assert fired(w.on_tick(tick(89))) == [below] and w.alerts == [other]


def test_move_fires_either_direction():
    for price in (105, 95):
        move = Alert("BTC-USD", "move", 5, ref=100)
        w = Watcher([move])
        assert w.on_tick(tick(104.9 if price > 100 else 95.1)) == []
        assert fired(w.on_tick(tick(price))) == [move]


@pytest.mark.parametrize("text,expected", [
    ("alert BTC-USD move 2% 15m", Alert("BTC-USD", "move", 2, window=15)),
    ("alert BTC-USD move 1 4h", Alert("BTC-USD", "move", 1, window=240)),
    ("alert BTC-USD spread 5bp", Alert("BTC-USD", "spread", 5)),
    ("alert BTC-USD spread 0.5", Alert("BTC-USD", "spread", 0.5)),
    ("alert BTC-USD print 500k", Alert("BTC-USD", "print", 500_000)),
    ("alert ETH-BTC print 1.5", Alert("ETH-BTC", "print", 1.5)),
    ("alert BTC-USD print 2M", Alert("BTC-USD", "print", 2_000_000)),
    ("alert BTC-USD volume 3x", Alert("BTC-USD", "volume", 3)),
])
def test_parse_watch_alerts(text, expected):
    assert parse_command(text) == ("alert", expected)
    assert expected.repeats and valid_alert(expected)


@pytest.mark.parametrize("text", [
    "alert BTC-USD move 2% 0m", "alert BTC-USD move 2% 5h", "alert BTC-USD move 2% 15s", "alert BTC-USD > 5 15m",
    "alert BTC-USD spread 0", "alert BTC-USD spread 10000", "alert BTC-USD print 0k", "alert BTC-USD print 5g",
    "alert BTC-USD print -1m", "alert BTC-USD volume 1x", "alert BTC-USD volume 0.5x", "alert BTC-USD volume nanx",
])
def test_parse_watch_alerts_invalid(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_move_within_a_window_fires_on_the_way_in_once_a_minute():
    clock = Clock()
    alert = Alert("BTC-USD", "move", 2, window=15)
    w = Watcher([alert], clock)
    assert w.on_tick(tick(100)) == []
    clock.now += 60
    assert w.on_tick(tick(101.9)) == []
    clock.now += 60
    (f,) = w.on_tick(tick(102))
    assert f.message == "BTC-USD +2.00% within 15m (100.00 → 102.00)"
    assert w.alerts == [alert]  # stays armed
    clock.now += 1
    assert w.on_tick(tick(102.5)) == []  # still true: not a new event
    clock.now += 20 * 60  # the 100 has left the window: false again
    assert w.on_tick(tick(102.5)) == []
    clock.now += 60
    (f,) = w.on_tick(tick(100.4))  # 2.05% below the window's high
    assert f.message == "BTC-USD -2.05% within 15m (102.50 → 100.40)"


def test_cooldown_counts_what_it_held_back():
    clock = Clock()
    alert = Alert("BTC-USD", "spread", 5)
    w = Watcher([alert], clock)
    assert w.on_tick(tick(100, 99.99, 100.01)) == []  # 2 bp
    (f,) = w.on_tick(tick(100, 99.9, 100.1))
    assert f.message == "BTC-USD spread 20 bp (bid 99.900 · ask 100.10)"
    for _ in range(3):  # flapping inside the minute: held back, counted
        clock.now += 5
        w.on_tick(tick(100, 99.99, 100.01))
        assert w.on_tick(tick(100, 99.9, 100.1)) == []
    clock.now += 60
    w.on_tick(tick(100, 99.99, 100.01))
    (f,) = w.on_tick(tick(100, 99.9, 100.1))
    assert f.message.endswith(" · 3 more since the last alert")
    assert w.on_tick(tick(100)) == []  # no bid/ask: no spread rather than a wrong one


def test_print_adds_up_one_orders_fills_and_fires_once_per_order():
    clock = Clock()
    alert = Alert("BTC-USD", "print", 1000)
    w = Watcher([alert], clock)
    assert w.trade_symbols == ["BTC-USD"]
    assert w.on_trade("BTC-USD", trade(1, 4, taker="a")) == []
    (f,) = w.on_trade("BTC-USD", trade(2, 6, taker="a"))  # 400 + 600 from the same order
    assert f.message == "BTC-USD buy order ≥ 1,000.00 USD (1,000.00 so far, at 100.00)"
    assert w.on_trade("BTC-USD", trade(3, 50, taker="a")) == []  # same order keeps filling: no second alert
    clock.now += 61
    assert w.on_trade("BTC-USD", trade(4, 9, taker="b")) == []  # a new, small order
    (f,) = w.on_trade("BTC-USD", trade(5, 20, taker="c", buy=False))
    assert f.message.startswith("BTC-USD sell order ≥ 1,000.00 USD (2,000.00 so far")
    assert w.on_trade("ETH-USD", trade(1, 1e6)) == []


def test_volume_warms_up_then_fires_on_a_spike_and_restarts_after_a_gap():
    clock = Clock()
    alert = Alert("BTC-USD", "volume", 3)
    w = Watcher([alert], clock)
    trade_id = 0

    def minute(total, n=6):  # `total` traded over one minute, in n trades
        nonlocal trade_id
        out = []
        for _ in range(n):
            clock.now += 60 / n
            trade_id += 1
            out += w.on_trade("BTC-USD", trade(trade_id, total / n))
        return out

    for _ in range(17):  # warm-up: 16 minutes from the first trade before anything can fire
        assert minute(10) == []
    assert w.warming(alert) <= 0
    assert minute(25) == []  # 2.5x normal
    (f,) = minute(60)  # fires part-way through the minute, as the last 60s pass 3x the baseline
    assert f.message.startswith("BTC-USD volume 3.")
    assert "on average over the 15 before" in f.message

    trade_id += 5  # trades missed (reconnect): the baseline would be understated, so warm up again
    assert minute(1000) == []
    assert w.warming(alert) > 14 * 60

def test_alerts_roundtrip_and_bad_files(tmp_path, monkeypatch):
    path = tmp_path / "crypto-terminal" / "alerts.json"
    monkeypatch.setattr(app_module, "ALERTS_FILE", path)
    assert load_alerts() == []
    saved = [Alert("BTC-USD", ">", 90000), Alert("ETH-USD", "move", 5, ref=2500)]
    save_alerts(saved)
    assert load_alerts() == saved
    assert not path.with_name("alerts.json.tmp").exists()  # saved via a temp file, renamed into place
    for bad in ("{not json", '{"symbol": "BTC-USD"}'):
        path.write_text(bad)
        assert load_alerts() == []
    path.write_text(json.dumps([
        {"symbol": "BTC-USD", "kind": ">", "level": 1},
        {"symbol": "../etc", "kind": ">", "level": 1},
        {"symbol": "BTC-USD", "kind": "=", "level": 1},
        {"symbol": "BTC-USD", "kind": ">", "level": "NaN"},
        {"symbol": "BTC-USD", "kind": ">", "level": -1},
        {"symbol": "BTC-USD", "kind": "move", "level": 5},  # no ref
        {"symbol": "BTC-USD", "kind": "move", "level": 500, "ref": 1},
        {"symbol": 5, "kind": ">", "level": 1},
        ["BTC-USD", ">", 1], "garbage",
    ]))
    assert load_alerts() == [Alert("BTC-USD", ">", 1)]


@pytest.mark.parametrize("platform,which,expected", [
    ("darwin", {"osascript"}, "osascript"),
    ("linux", {"notify-send"}, "notify-send"),
    ("linux", set(), None),
])
def test_desktop_notify_uses_argv_never_a_shell(monkeypatch, platform, which, expected):
    calls = []
    monkeypatch.setattr(alerts_module.sys, "platform", platform)
    monkeypatch.setattr(alerts_module.shutil, "which", lambda name: f"/usr/bin/{name}" if name in which else None)
    monkeypatch.setattr(alerts_module.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    message = 'BTC-USD > 1" & do shell script "rm -rf ~'
    desktop_notify("title", message)
    if expected is None:
        assert calls == []
        return
    (args, kw), = calls
    assert isinstance(args, list) and args[0] == expected and not kw.get("shell")
    assert args[-1] == message and all(message not in a for a in args[:-1])  # passed as data, not code


@pytest.mark.parametrize("text,expected", [
    ("my-topic_123", "https://ntfy.sh/my-topic_123"),
    ("https://ntfy.example.com/alerts", "https://ntfy.example.com/alerts"),
])
def test_push_url(text, expected):
    assert push_url(text) == expected


@pytest.mark.parametrize("text", ["http://ntfy.sh/topic", "https://ntfy.sh/a/b", "file:///etc/passwd", "a b", "x" * 65,
                                  "https://ntfy.sh/", "javascript:alert(1)"])
def test_push_url_rejects(text):
    with pytest.raises(ValueError):
        push_url(text)


async def test_push_posts_the_message_and_reports_failures():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200 if request.url.path == "/ok" else 500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await push(client, "https://ntfy.sh/ok", "BTC-USD > 90,000.00 · now 90,001.00") is None
        assert sent[0].content.decode() == "BTC-USD > 90,000.00 · now 90,001.00"
        assert sent[0].headers["Title"] == "crypto-terminal alert"
        assert await push(client, "https://ntfy.sh/down", "x") == "push to https://ntfy.sh/down failed (HTTPStatusError)"


@pytest.mark.parametrize("text,expected", [
    ("alert BTC-USD funding 0.05%", Alert("BTC-USD", "funding", 0.05)),
    ("alert eth-usd FUNDING 0.1", Alert("ETH-USD", "funding", 0.1)),
    ("alert BTC-USD liq 1m", Alert("BTC-USD", "liq", 1_000_000)),
    ("alert BTC-USD liq 250K", Alert("BTC-USD", "liq", 250_000)),
])
def test_parse_perp_alerts(text, expected):
    assert parse_command(text) == ("alert", expected)
    assert expected.repeats and valid_alert(expected)


@pytest.mark.parametrize("text", [
    "alert BTC-USD funding 0", "alert BTC-USD funding 10%", "alert BTC-USD funding -0.05%", "alert BTC-USD funding x",
    "alert BTC-USD funding 0.05% 15m", "alert BTC-USD liq 0", "alert BTC-USD liq 1b", "alert BTC-USD liq nan",
])
def test_parse_perp_alerts_invalid(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_funding_fires_on_either_venue_either_sign_once_a_minute():
    now = [0.0]
    funding, other = Alert("BTC-USD", "funding", 0.05), Alert("ETH-USD", "funding", 0.05)
    w = Watcher([funding, other], clock=lambda: now[0])
    assert w.perp_symbols == ["BTC-USD", "ETH-USD"] and w.trade_symbols == []
    assert w.on_tick(Tick("BTC-USD", 100.0, 90.0)) == []  # perp alerts never read spot ticks
    assert w.on_funding("BTC-USD", "Binance", 0.0001) == []
    fired = w.on_funding("BTC-USD", "Bybit", -0.0006)
    assert [f.message for f in fired] == ["BTC-USD perp funding -0.0600% on Bybit (Binance +0.0100%)"]
    assert w.on_funding("BTC-USD", "Bybit", -0.0007) == []  # still true: edge-triggered
    assert w.on_funding("BTC-USD", "Bybit", None) == []  # Bybit dropped: nothing to compare, state kept
    assert w.on_funding("BTC-USD", "Binance", 0.0001) == []
    now[0] = 30
    assert w.on_funding("BTC-USD", "Binance", 0.0005) == []  # true again, within the cooldown: held back
    now[0] = 100
    assert w.on_funding("BTC-USD", "Binance", 0.0001) == []
    fired = w.on_funding("BTC-USD", "Binance", 0.0008)
    assert [f.message for f in fired] == ["BTC-USD perp funding +0.0800% on Binance · 1 more since the last alert"]


def test_liq_fires_for_each_big_liquidation_never_summed():
    from crypto_terminal.derivs import Liquidation

    now = [0.0]
    w = Watcher([Alert("BTC-USD", "liq", 100_000)], clock=lambda: now[0])
    assert w.on_liquidation("BTC-USD", Liquidation("Binance", 0, True, 1.0, 80_000.0)) == []
    assert w.on_liquidation("BTC-USD", Liquidation("Binance", 0, True, 1.0, 80_000.0)) == []  # 160k together: no
    assert w.on_liquidation("ETH-USD", Liquidation("Bybit", 0, False, 100.0, 2_000.0)) == []  # another pair
    fired = w.on_liquidation("BTC-USD", Liquidation("Bybit", 0, False, 1.5, 80_000.0))
    assert [f.message for f in fired] == ["BTC-USD short liquidated on Bybit: 120,000.00 USDT (1.5000 BTC at 80,000.00)"]
    now[0] = 61
    fired = w.on_liquidation("BTC-USD", Liquidation("Binance", 0, True, 2.0, 80_000.0))
    assert fired[0].message.startswith("BTC-USD long liquidated on Binance: 160,000.00 USDT")


def test_perp_alerts_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "ALERTS_FILE", tmp_path / "alerts.json")
    alerts = [Alert("BTC-USD", "funding", 0.05), Alert("BTC-USD", "liq", 1e6)]
    save_alerts(alerts)
    assert load_alerts() == alerts
    assert [str(a) for a in alerts] == ["BTC-USD perp funding ≥ ±0.05%", "BTC-USD liquidation ≥ 1,000,000.00 USDT"]
