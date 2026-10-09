"""Exercise the yfinance adapter with a fake module (no network)."""

import sys
import types

import fixtures
import pandas as pd
import pytest


class FakeTicker:
    def __init__(self, symbol):
        self.symbol = symbol
        dates = pd.to_datetime(["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"])
        rows = {
            "Total Revenue": [400, 350, 300, 280],
            "Cost Of Revenue": [160, 140, 120, 112],
            "Operating Income": [120, 105, 90, 84],
            "Net Income Common Stockholders": [90, 80, 70, 60],
            "Diluted EPS": [9.0, 8.0, 7.0, 6.0],
            "Diluted Average Shares": [10, 10, 10, 10],
        }
        self.income_stmt = pd.DataFrame(rows, index=dates).T
        self.balance_sheet = pd.DataFrame(
            {"Total Assets": [800, 700, 650, 600], "Stockholders Equity": [400, 350, 320, 300]},
            index=dates,
        ).T
        self.cashflow = pd.DataFrame(
            {"Operating Cash Flow": [130, 110, 95, 90], "Capital Expenditure": [-30, -25, -20, -20]},
            index=dates,
        ).T
        q = pd.to_datetime(["2026-06-30"])
        self.ttm_income_stmt = pd.DataFrame({"Total Revenue": [420], "Net Income Common Stockholders": [95]}, index=q).T  # fmt: skip
        self.ttm_cashflow = pd.DataFrame({"Operating Cash Flow": [135]}, index=q).T
        self.quarterly_balance_sheet = pd.DataFrame({"Total Assets": [820]}, index=q).T
        hist = fixtures.price_history()
        hist.index = hist.index.tz_localize("America/New_York")
        self._hist = hist
        self.splits = fixtures.splits().tz_localize("America/New_York")
        self.dividends = fixtures.dividends()
        self.info = {"currency": "USD", "financialCurrency": "USD", "longName": "Fake Corp",
                     "sector": "Technology", "industry": "Software", "beta": 1.1}  # fmt: skip
        self.fast_info = {"last_price": 123.45, "currency": "USD"}
        self.insider_purchases = pd.DataFrame(
            {
                "Insider Purchases Last 6m": ["Purchases", "Sales", "Net Shares Purchased (Sold)"],
                "Shares": [1000, 5000, -4000],
                "Trans": [2, 9, 11],
            }
        )

    def history(self, **kwargs):
        return self._hist


@pytest.fixture
def fake_yf(monkeypatch):
    mod = types.ModuleType("yfinance")
    mod.Ticker = FakeTicker
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    return mod


def test_fetch_market(fake_yf):
    from valuelens.data.yahoo import fetch_market

    md = fetch_market("FAKE")
    assert md.price == pytest.approx(123.45)
    assert md.history.index.tz is None
    assert md.splits.index.tz is None
    assert md.insider["buy_trans"] == 2 and md.insider["sell_trans"] == 9
    assert md.name == "Fake Corp"


def test_fetch_statements(fake_yf):
    from valuelens.data.normalize import normalize
    from valuelens.data.yahoo import fetch_statements

    annual, ttm = fetch_statements("FAKE")
    assert list(annual.index.year) == [2022, 2023, 2024, 2025]
    norm = normalize(annual)
    assert norm["capex"].iloc[-1] == 30  # sign flipped
    assert norm["fcf"].iloc[-1] == 100
    assert norm["gross_profit"].iloc[-1] == 240
    assert ttm.iloc[0]["revenue"] == 420 and ttm.iloc[0]["total_assets"] == 820
