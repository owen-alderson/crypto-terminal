"""`crypto-terminal watch` with the network stubbed out."""

import asyncio
import os

import pytest

from crypto_terminal import app as app_module
from crypto_terminal import watch as watch_module
from crypto_terminal.alerts import Alert
from crypto_terminal.feed import Tick
from crypto_terminal.watch import watch


class FakeFeed:
    instances = []

    def __init__(self, symbols, on_tick, on_status, trade_symbols=(), on_trade=None):
        self.symbols, self.on_tick, self.trade_symbols = symbols, on_tick, list(trade_symbols)
        FakeFeed.instances.append(self)

    async def run(self):
        await asyncio.Event().wait()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    FakeFeed.instances = []
    for name, file in (("WATCHLIST_FILE", "watchlist.json"), ("CONFIG_FILE", "config.json"),
                       ("ALERTS_FILE", "alerts.json"), ("WATCH_PID_FILE", "watch.pid")):
        monkeypatch.setattr(app_module, name, tmp_path / file)
    monkeypatch.setattr(watch_module, "Feed", FakeFeed)
    monkeypatch.setattr(watch_module, "RELOAD_EVERY", 0.05)
    desktop, pushed = [], []
    monkeypatch.setattr(watch_module, "desktop_notify", lambda title, message: desktop.append(message))

    async def fake_push(client, url, message):
        pushed.append((url, message))
        return "push to x failed (ConnectError)" if message == "fail" else None

    monkeypatch.setattr(watch_module, "push", fake_push)
    return tmp_path, desktop, pushed


async def test_watch_fires_saves_reloads_and_cleans_up(setup):
    tmp_path, desktop, pushed = setup
    app_module.save_alerts([Alert("BTC-USD", ">", 100), Alert("ETH-USD", "print", 1000)])
    app_module.update_config(push="my-topic")
    out, err = [], []
    task = asyncio.create_task(watch(out.append, err.append))
    await asyncio.sleep(0.1)
    assert int((tmp_path / "watch.pid").read_text()) == os.getpid()
    assert err[0] == "watching 2 alerts on 2 pairs · push https://ntfy.sh/my-topic · Ctrl-C stops"
    feed = FakeFeed.instances[-1]
    assert feed.symbols == ["BTC-USD", "ETH-USD"] and feed.trade_symbols == ["ETH-USD"]

    feed.on_tick(Tick("BTC-USD", 100.5, 90.0))
    await asyncio.sleep(0.01)
    assert out[0].endswith("UTC  BTC-USD > 100.00 · now 100.50")
    assert desktop == ["BTC-USD > 100.00 · now 100.50"] and pushed == [("https://ntfy.sh/my-topic", desktop[0])]
    assert app_module.load_alerts() == [Alert("ETH-USD", "print", 1000)]  # one-shot removed on disk too

    app_module.save_alerts([Alert("SOL-USD", "<", 50)])  # changed in the terminal
    await asyncio.sleep(0.2)
    assert FakeFeed.instances[-1].symbols == ["SOL-USD"] and FakeFeed.instances[-1].trade_symbols == []
    assert any("changed: watching 1 alert on 1 pair" in line for line in err)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "watch.pid").exists()


async def test_only_one_watch_at_a_time(setup):
    tmp_path, _, _ = setup
    (tmp_path / "watch.pid").write_text(str(os.getpid()))  # a live process
    err = []
    assert await watch(err=err.append) == 1
    assert err == [f"already running (pid {os.getpid()})"]
    assert (tmp_path / "watch.pid").read_text() == str(os.getpid())  # left alone


async def test_watch_with_no_alerts_says_how_to_add_them(setup):
    err = []
    task = asyncio.create_task(watch(err=err.append))
    await asyncio.sleep(0.1)
    assert FakeFeed.instances == []  # nothing to watch: no connection
    assert err[1].startswith("no alerts yet")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
