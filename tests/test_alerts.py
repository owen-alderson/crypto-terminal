import json

import pytest

from crypto_terminal import alerts as alerts_module
from crypto_terminal import app as app_module
from crypto_terminal.alerts import Alert, check, desktop_notify
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


def test_cross_fires_once_and_is_removed():
    above, below = Alert("BTC-USD", ">", 100), Alert("BTC-USD", "<", 90)
    other = Alert("ETH-USD", ">", 1)
    alerts = [above, below, other]
    assert check(alerts, "BTC-USD", 99.99) == ([], alerts)
    fired, alerts = check(alerts, "BTC-USD", 100)
    assert fired == [above] and alerts == [below, other]
    assert check(alerts, "BTC-USD", 101) == ([], alerts)  # gone: does not fire again
    fired, alerts = check(alerts, "BTC-USD", 89)
    assert fired == [below] and alerts == [other]


def test_move_fires_either_direction():
    move = Alert("BTC-USD", "move", 5, ref=100)
    assert check([move], "BTC-USD", 104.9)[0] == []
    assert check([move], "BTC-USD", 105)[0] == [move]
    assert check([move], "BTC-USD", 95)[0] == [move]


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
