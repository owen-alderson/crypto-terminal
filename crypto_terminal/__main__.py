import asyncio
import signal
import sys

USAGE = "usage: crypto-terminal          the terminal\n       crypto-terminal watch    check alerts with no screen"


def main():
    args = sys.argv[1:]
    if args == ["watch"]:
        from .watch import watch

        async def run():
            # SIGTERM (kill, systemd, launchd) stops it like Ctrl-C does: cleanly, removing its pid file
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
            return await watch()

        try:
            sys.exit(asyncio.run(run()))
        except (KeyboardInterrupt, asyncio.CancelledError):
            sys.exit(0)
    if args:
        print(USAGE, file=sys.stderr)
        sys.exit(0 if args[0] in ("-h", "--help") else 2)
    from .app import TerminalApp
    TerminalApp().run()


if __name__ == "__main__":
    main()
