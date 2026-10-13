import json
from decimal import Decimal

import pytest

from crypto_terminal import app as app_module
from crypto_terminal.app import load_portfolio, parse_command, save_portfolio
from crypto_terminal.portfolio import Holding, fmt_total, parse_amount, totals

D = Decimal


@pytest.mark.parametrize("text, expected", [("0.5", D("0.5")), ("62000", D("62000")), ("1e-3", D("0.001"))])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["0", "-1", "nan", "inf", "abc", "1,000", "", None, 5])
def test_parse_amount_rejects(text):
    with pytest.raises(ValueError):
        parse_amount(text)


@pytest.mark.parametrize("text, expected", [
    ("hold BTC-USD 0.5 @ 62000", ("hold", Holding("BTC-USD", D("0.5"), D("62000")))),
    ("hold eth-usd 2", ("hold", Holding("ETH-USD", D(2)))),
    ("unhold btc-usd", ("unhold", "BTC-USD")),
])
def test_parse_hold(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize("text", ["hold", "hold BTC-USD", "hold BTC-USD 1 62000", "hold BTC-USD 1 @",
                                  "hold BTC-USD 0", "hold BTC-USD 1 @ -5", "hold ../x 1", "unhold", "unhold x y"])
def test_parse_hold_invalid(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_value_and_pnl_are_exact():
    h = Holding("BTC-USD", D("0.1"), D("60000.10"))
    assert h.value(0.3) == D("0.03")  # 0.1 * 0.3 in floats is 0.030000000000000002
    assert h.pnl(62000.01) == D("199.991")
    assert Holding("BTC-USD", D(1)).pnl(5.0) is None


def test_totals_per_quote_never_mixed():
    prices = {"BTC-USD": 62000.0, "ETH-USD": 2500.0, "ETH-BTC": 0.04}
    holdings = [Holding("BTC-USD", D("0.5"), D(60000)), Holding("ETH-USD", D(2), D(3000)),
                Holding("ETH-BTC", D(1), D("0.05"))]
    usd, btc = totals(holdings, prices.get)
    assert (usd.quote, usd.value, usd.cost, usd.pnl) == ("USD", D(36000), D(36000), D(0))
    assert (btc.quote, btc.value, btc.pnl) == ("BTC", D("0.04"), D("-0.01"))
    assert fmt_total(usd).plain == "USD 36,000.00 +0.00 +0.00%"
    assert fmt_total(btc).plain == "BTC 0.040000 -0.010000 -20.00%"


def test_total_hides_pnl_unless_every_holding_counts():
    priced = Holding("BTC-USD", D(1), D(50000))
    unpriced = totals([priced, Holding("SOL-USD", D(10), D(100))], {"BTC-USD": 60000.0}.get)[0]
    assert unpriced.pnl is None and fmt_total(unpriced).plain == "USD 60,000.00 + 1 unpriced"
    no_cost = totals([priced, Holding("ETH-USD", D(1))], {"BTC-USD": 60000.0, "ETH-USD": 2000.0}.get)[0]
    assert no_cost.pnl is None and fmt_total(no_cost).plain == "USD 62,000.00 · 1 without cost"


def test_portfolio_roundtrip_and_bad_files(tmp_path, monkeypatch):
    path = tmp_path / "crypto-terminal" / "portfolio.json"
    monkeypatch.setattr(app_module, "PORTFOLIO_FILE", path)
    assert load_portfolio() == {}
    saved = {"BTC-USD": Holding("BTC-USD", D("0.10"), D("62000.5")), "ETH-USD": Holding("ETH-USD", D(3))}
    save_portfolio(saved)
    assert load_portfolio() == saved
    assert str(load_portfolio()["BTC-USD"].qty) == "0.10"  # exactly as typed
    assert not path.with_name("portfolio.json.tmp").exists()
    for bad in ("{not json", "[1, 2]"):
        path.write_text(bad)
        assert load_portfolio() == {}
    path.write_text(json.dumps({
        "BTC-USD": {"qty": 0.5, "cost": 60000},  # hand-edited numbers are fine
        "../etc": {"qty": "1"},
        "ETH-USD": {"qty": "-1"},
        "SOL-USD": {"qty": "1", "cost": "NaN"},
        "ADA-USD": {"cost": "1"},
        "XRP-USD": {"qty": True},
        "DOGE-USD": "garbage",
    }))
    assert load_portfolio() == {"BTC-USD": Holding("BTC-USD", D("0.5"), D(60000))}


def test_value_and_pnl_round_the_same_way():
    # 0.25 x 83,453.94 = 20,863.485 exactly: as a float it could round to .49 while the P&L showed .48
    h = Holding("BTC-USD", D("0.25"), D(60000))
    t = totals([h], {"BTC-USD": 83453.94}.get)[0]
    assert fmt_total(t).plain == "USD 20,863.48 +5,863.48 +39.09%"
