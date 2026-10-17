import argparse
import asyncio
import json
import signal
import sys

import httpx

from .chart import fmt_price


def signed_bp(bp) -> str:
    return "—" if bp is None else f"{bp:+.2f} bp"


def size(value: float) -> str:
    return f"{value:,.8g}"


def show_price(rows: list[dict]) -> str:
    return "\n".join(
        f"{r['symbol']:<10} {fmt_price(r['price']):>14}  {r['change_24h_pct']:+.2f}% 24h  "
        + (f"bid {fmt_price(r['bid'])} · ask {fmt_price(r['ask'])} · spread {r['spread_bp']:.2g} bp"
           if r["bid"] else "no valid bid/ask") for r in rows)


def show_book(b: dict) -> str:
    width = max(len(fmt_price(p)) for p, _ in b["asks"] + b["bids"])
    lines = [f"{b['symbol']} book on Coinbase · grouped by {b['group']:g} · spread {b['spread_bp']:.2g} bp"
             if b["spread_bp"] is not None else f"{b['symbol']} book on Coinbase · grouped by {b['group']:g}"]
    lines += [f"  ask {fmt_price(p):>{width}}  {size(s)}" for p, s in reversed(b["asks"])]
    # the real best prices (grouping hides them), not the mid: it needs a decimal more than Coinbase quotes
    lines.append(f"  ─── best bid {fmt_price(b['bid'])} · ask {fmt_price(b['ask'])}" if b["mid"] else "  ───")
    lines += [f"  bid {fmt_price(p):>{width}}  {size(s)}" for p, s in b["bids"]]
    return "\n".join(lines)


def show_venues(v: dict) -> str:
    cb = v["coinbase"]
    lines = [f"{v['symbol']} on other exchanges · Coinbase bid {fmt_price(cb['bid'])} · ask {fmt_price(cb['ask'])}"
             if cb["mid"] else f"{v['symbol']} · no Coinbase bid/ask to compare with"]
    for row in v["venues"]:
        if row["status"] == "live":
            lines.append(f"  {row['exchange']:<8} bid {fmt_price(row['bid'])} · ask {fmt_price(row['ask'])} {row['quote']}"
                         f"  {signed_bp(row['vs_coinbase_bp'])} vs Coinbase")
        else:
            lines.append(f"  {row['exchange']:<8} {row['status']}")
    if v["usdt_rate"]:
        lines.append(f"  USDT prices converted at {v['usdt_rate']:.5f} USD (Coinbase USDT-USD)")
    return "\n".join(lines)


def show_perps(p: dict) -> str:
    lines = [f"{p['symbol']} perpetual futures ({p['perp']}, not spot)" if p["perp"] else f"{p['symbol']}: no USDT perp"]
    for row in p["venues"]:
        if row["status"] != "live":
            lines.append(f"  {row['exchange']:<8} {row['status']}")
            continue
        interval = f" every {row['interval_h']}h" if row["interval_h"] else ""
        coin = p["symbol"].partition("-")[0]
        oi = f"OI {size(row['open_interest'])} {coin}" if row["open_interest"] is not None else "OI unknown"
        lines.append(f"  {row['exchange']:<8} funding {row['funding_pct']:+.4f}% (next {row['next_funding']}{interval})"
                     f" · premium {signed_bp(row['premium_bp'])} · {oi}")
    return "\n".join(lines)


async def snapshot(args) -> tuple[object, str]:
    from . import snapshot as snap

    async with httpx.AsyncClient() as client:
        if args.command == "price":
            rows = await asyncio.gather(*(snap.price(client, s) for s in args.symbols))
            return rows, show_price(rows)
        if args.command == "book":
            data = await snap.book(client, args.symbol, args.depth, args.group)
            return data, show_book(data)
        if args.command == "venues":
            data = await snap.venues(client, args.symbol)
            return data, show_venues(data)
    data = await snap.perps(args.symbol)
    return data, show_perps(data)


def run_watch():
    from .watch import watch

    async def run():
        # SIGTERM (kill, systemd, launchd) stops it like Ctrl-C does: cleanly, removing its pid file
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
        return await watch()

    try:
        sys.exit(asyncio.run(run()))
    except (KeyboardInterrupt, asyncio.CancelledError):
        sys.exit(0)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="crypto-terminal", description="With no command: the terminal.")
    sub = top.add_subparsers(dest="command", metavar="command")
    sub.add_parser("watch", help="check alerts with no screen")
    as_json = argparse.ArgumentParser(add_help=False)
    as_json.add_argument("--json", action="store_true", help="print JSON (for scripts and pipes)")
    price = sub.add_parser("price", parents=[as_json], help="last price, 24h change and spread on Coinbase")
    price.add_argument("symbols", nargs="+", metavar="PAIR", help="e.g. BTC-USD")
    book = sub.add_parser("book", parents=[as_json], help="the order book on Coinbase")
    book.add_argument("symbol", metavar="PAIR")
    book.add_argument("--depth", type=int, default=10, help="levels a side (default 10)")
    book.add_argument("--group", type=float, help="price step, e.g. 10 for $10 (default: about 1 bp of the price)")
    for name, text in (("venues", "the pair on Kraken, Binance, Bybit and OKX vs Coinbase"),
                       ("perps", "perp funding, premium and open interest on Binance and Bybit")):
        sub.add_parser(name, parents=[as_json], help=text).add_argument("symbol", metavar="PAIR")
    sub.add_parser("mcp", help="run the MCP server (stdio) for Claude and other clients")
    return top


def main():
    args = parser().parse_args()
    if args.command == "watch":
        run_watch()
    if args.command == "mcp":
        try:
            from .mcp_server import main as serve
        except ImportError:
            print("the MCP server needs the mcp package:  pipx install 'crypto-terminal[mcp]'  "
                  "(or: pipx inject crypto-terminal mcp)", file=sys.stderr)
            sys.exit(1)
        serve()
        return
    if args.command:
        try:
            data, text = asyncio.run(snapshot(args))
        except (LookupError, ValueError, httpx.HTTPError) as e:
            print(f"crypto-terminal: {str(e) or type(e).__name__}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(data, indent=2) if args.json else text)
        return
    from .app import TerminalApp
    TerminalApp().run()


if __name__ == "__main__":
    main()
