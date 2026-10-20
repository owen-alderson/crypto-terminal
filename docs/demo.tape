# Re-record the README demo: `vhs docs/demo.tape` from the repo root (live Coinbase data, a throwaway config dir).
# Then `gifsicle -O3 --lossy=10 -b docs/demo.gif`: GitHub's image proxy won't show a README image over 5 MB.
Output docs/demo.gif

Set Shell zsh
Set FontSize 12
Set Width 1180
Set Height 760
Set Padding 10
Set Framerate 15
Set TypingSpeed 60ms
Set Theme { "background": "#000000", "foreground": "#e0e0e0" }

Hide
Type "export XDG_CONFIG_HOME=$(mktemp -d) && clear && .venv/bin/python -m crypto_terminal"
Enter
Sleep 8s
Show

# live watchlist, candles, order book and trades
Sleep 4s
Down
Sleep 3s
Up
Sleep 3s

# the same pair on Kraken, Binance, Bybit and OKX, then perps: funding, open interest, liquidations
Type "x"
Sleep 4s
Type "d"
Sleep 5s
Type "x"
Type "d"
Sleep 1s

# group the book more coarsely so walls stand out
Type "g"
Sleep 3s

# hourly candles with indicators
Type "4"
Sleep 2s
Type "/"
Type "ind ema50 vwap"
Enter
Sleep 3s

# coin search
Type "/"
Type "add dogecoin"
Enter
Sleep 2s
Enter
Sleep 2s

# a watch alert that stays armed (bell, toast, desktop notification, optional phone push)
Type "/"
Type "alert BTC-USD move 2% 15m"
Enter
Sleep 3s

# views: chart only, watchlist only, watchlist + book, back to split
Type "f"
Sleep 3s
Type "f"
Sleep 2s
Type "b"
Sleep 4s
Type "b"
Type "f"
Sleep 4s

Ctrl+Q
Sleep 1s
