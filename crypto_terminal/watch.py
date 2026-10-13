"""`crypto-terminal watch`: checks your alerts with no screen, on a laptop left open or a small server.

It reads the same alerts.json as the terminal and picks up changes within seconds, so you set alerts in the terminal
and let this watch them. While it runs, the terminal leaves alert checking to it, so nothing fires twice.
Fired alerts print to stdout (one line each, so `watch >> alerts.log` keeps a history); connection news goes to stderr.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

import httpx

from . import app as config  # read paths at call time: tests point them elsewhere
from .alerts import Watcher, desktop_notify, push
from .feed import Feed

RELOAD_EVERY = 2  # seconds between checks for changed alerts or push settings


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def summary(watcher: Watcher, push_url: str | None) -> str:
    pairs = len({a.symbol for a in watcher.alerts})
    return (f"watching {len(watcher.alerts)} alert{'s' * (len(watcher.alerts) != 1)} on {pairs} "
            f"pair{'s' * (pairs != 1)} · push {push_url or 'off'}")


async def watch(out=lambda line: print(line, flush=True),  # flushed: `>> alerts.log` loses nothing
                err=lambda line: print(line, file=sys.stderr, flush=True)) -> int:
    if pid := config.watch_pid():
        err(f"already running (pid {pid})")
        return 1
    pid_file = config.WATCH_PID_FILE
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    watcher = Watcher(config.load_alerts())
    seen, push_url = config.alerts_mtime(), config.load_push()
    feed_task, feed_key, sends = None, None, set()
    err(f"{summary(watcher, push_url)} · Ctrl-C stops")
    if not watcher.alerts:
        err("no alerts yet: set some in the terminal (crypto-terminal, then / alert ...)")

    async with httpx.AsyncClient() as client:
        async def send(message: str):
            if error := await push(client, push_url, message):
                err(error)

        def deliver(fired):
            nonlocal seen
            if any(not f.alert.repeats for f in fired):  # one-shot alerts are gone: write that back
                config.save_alerts(watcher.alerts)
                seen = config.alerts_mtime()
            for f in fired:
                out(f"{stamp()}  {f.message}")
                desktop_notify("crypto-terminal alert", f.message)
                if push_url:
                    task = asyncio.create_task(send(f.message))
                    sends.add(task)  # held until done, or it could be garbage-collected mid-send
                    task.add_done_callback(sends.discard)

        try:
            while True:
                if config.alerts_mtime() != seen or config.load_push() != push_url:
                    watcher.set_alerts(config.load_alerts())
                    seen, push_url = config.alerts_mtime(), config.load_push()
                    err(f"{stamp()}  changed: {summary(watcher, push_url)}")
                key = (sorted({a.symbol for a in watcher.alerts}), watcher.trade_symbols)
                if key != feed_key:  # only the pairs with alerts, trades only for trade alerts
                    if feed_task:
                        feed_task.cancel()
                    feed_key, feed_task = key, None
                    if key[0]:
                        feed = Feed(key[0], lambda tick: deliver(watcher.on_tick(tick)),
                                    lambda status: err(f"{stamp()}  feed {status}"), trade_symbols=key[1],
                                    on_trade=lambda symbol, trade: deliver(watcher.on_trade(symbol, trade)))
                        feed_task = asyncio.create_task(feed.run())
                await asyncio.sleep(RELOAD_EVERY)
        finally:
            if feed_task:
                feed_task.cancel()
            if config.watch_pid() == os.getpid():
                pid_file.unlink(missing_ok=True)
