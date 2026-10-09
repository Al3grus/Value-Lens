"""Regression tests for issues found in the first live run (MSFT, GOOG, META, ADBE)."""

from datetime import date

import fixtures
import numpy as np
import pandas as pd
import pytest
from conftest import build_bundle

from valuelens.analysis.technicals import classify_trend
from valuelens.analysis.valuation import compute_valuation
from valuelens.data.normalize import apply_owner_earnings, normalize
from valuelens.data.sec import parse_company_facts
from valuelens.engine import analyze_bundle
from valuelens.report.text import render


# ---- MSFT: a discontinued concept must not define the "latest quarter" ------------------
def test_stale_concept_does_not_set_ttm():
    facts = fixtures.company_facts()
    gaap = facts["facts"]["us-gaap"]
    # Drop the current 10-Q: the latest data is the FY2025 10-K (like MSFT after its June 10-K).
    for concept in gaap.values():
        for unit, rows in concept["units"].items():
            concept["units"][unit] = [r for r in rows if r["form"] != "10-Q"]
    # The pre-2018 revenue concept still has an old 10-Q after its last annual value.
    gaap["SalesRevenueNet"]["units"]["USD"] += [
        {"start": "2018-01-01", "end": "2018-03-31", "val": 3.0e9, "form": "10-Q", "filed": "2018-04-26", "accn": "q1"},
        {"start": "2017-01-01", "end": "2017-03-31", "val": 2.8e9, "form": "10-Q", "filed": "2018-04-26", "accn": "q1"},
    ]  # fmt: skip
    parsed = parse_company_facts(facts)
    assert parsed.ttm_end == date(2025, 12, 31)
    assert parsed.ttm["revenue"].val == pytest.approx(fixtures.fundamentals(2025)["rev"])
    assert not parsed.notes


# ---- GOOG: material non-operating gains are excluded from earnings multiples ------------
def _income(pretax: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "revenue": [446.0], "operating_income": [147.6], "interest_expense": [0.7],
            "pretax_income": [pretax], "income_tax": [pretax * 0.17], "net_income": [pretax * 0.83],
            "eps_diluted": [pretax * 0.83 / 12.27], "shares_diluted": [12.27],
        }
    )  # fmt: skip


def test_core_eps_excludes_large_investment_gains():
    row = normalize(_income(pretax=299.3)).iloc[0]  # ~$152B of non-operating gains
    assert row["core_net_income"] == pytest.approx((147.6 - 0.7) * 0.83)
    assert row["core_eps"] == pytest.approx(row["core_net_income"] / 12.27, rel=1e-3)
    assert row["core_eps"] < 0.55 * row["eps"]


def test_core_eps_equals_reported_when_immaterial():
    row = normalize(_income(pretax=150.0)).iloc[0]
    assert row["core_eps"] == pytest.approx(row["eps"])
    assert row["core_net_income"] == pytest.approx(row["net_income"])


def test_core_eps_used_in_valuation(cfg):
    b = build_bundle()
    b.ttm["core_eps"] = b.ttm["eps"] / 2
    v = compute_valuation(b, cfg)
    assert v.pe_now == pytest.approx(b.price / b.ttm["core_eps"])


# ---- MSFT/META: growth capex is not deducted from owner earnings -----------------------
def _capex_frame(capex_last: float) -> pd.DataFrame:
    idx = pd.date_range("2021-06-30", periods=6, freq="YE-JUN")
    rev = np.array([168.1, 198.3, 211.9, 245.1, 281.7, 331.8])
    return pd.DataFrame(
        {
            "revenue": rev, "ppe_net": [59.7, 74.4, 95.6, 135.6, 205.0, 313.1],
            "capex": [20.6, 23.9, 28.1, 44.5, 64.6, capex_last], "dep_amort": [11.7, 14.5, 13.9, 22.3, 34.2, 34.3],
            "cfo": [76.7, 89.0, 87.6, 118.5, 136.2, 182.9], "sbc": [6.1, 7.5, 9.6, 10.7, 12.0, 12.4],
            "fcf": [56.1, 65.1, 59.5, 74.0, 71.6, 182.9 - capex_last], "owner_fcf": np.nan, "maint_capex": np.nan,
        },
        index=idx,
    )  # fmt: skip


def test_greenwald_maintenance_capex():
    a = _capex_frame(115.95)
    ttm = a.iloc[-1].copy()
    share = apply_owner_earnings(a, ttm, max_da_multiple=None)  # Greenwald's estimate, uncapped
    ratio = (a["ppe_net"] / a["revenue"]).iloc[:-1].mean()
    expected = 115.95 - ratio * (331.8 - 281.7)
    assert a["maint_capex"].iloc[-1] == pytest.approx(expected)
    assert a["owner_fcf"].iloc[-1] == pytest.approx(182.9 - expected - 12.4)
    assert share == pytest.approx(expected / 115.95)
    assert ttm["owner_fcf"] == pytest.approx(182.9 - 115.95 * share - 12.4)


def test_maintenance_capex_capped_at_depreciation():
    # MSFT FY2026: $116B capex during the AI build-out. Greenwald's estimate calls ~$90B of it
    # maintenance; capped at depreciation ($34.3B) the rest counts as growth investment.
    a = _capex_frame(115.95)
    ttm = a.iloc[-1].copy()
    share = apply_owner_earnings(a, ttm)
    assert a["maint_capex"].iloc[-1] == pytest.approx(34.3)
    assert a["owner_fcf"].iloc[-1] == pytest.approx(182.9 - 34.3 - 12.4)
    assert share == pytest.approx(34.3 / 115.95)
    apply_owner_earnings(a, ttm, max_da_multiple=1.5)
    assert a["maint_capex"].iloc[-1] == pytest.approx(34.3 * 1.5)


def test_maintenance_capex_floored_at_depreciation():
    a = _capex_frame(36.0)  # little capex: growth estimate would push maintenance below D&A
    apply_owner_earnings(a, a.iloc[-1].copy())
    assert a["maint_capex"].iloc[-1] == pytest.approx(34.3)


def test_maintenance_capex_can_be_disabled():
    a = _capex_frame(115.95)
    share = apply_owner_earnings(a, a.iloc[-1].copy(), maintenance=False)
    assert share == 1.0
    assert a["owner_fcf"].iloc[-1] == pytest.approx(182.9 - 115.95 - 12.4)


# ---- ADBE: below the 200-day with negative momentum is a downtrend ---------------------
@pytest.mark.parametrize(
    ("price", "sma50", "sma200", "mom", "expected"),
    [
        (241.05, 257.99, 257.55, -0.286, "DOWNTREND"),  # ADBE
        (522.61, 498.49, 432.44, -0.052, "UPTREND"),  # MSFT
        (344.86, 342.99, 337.84, 0.340, "UPTREND"),  # GOOG
        (110.0, 95.0, 100.0, -0.05, "SIDEWAYS"),
        (90.0, 95.0, 100.0, -0.20, "DOWNTREND"),
    ],
)
def test_classify_trend(price, sma50, sma200, mom, expected):
    assert classify_trend(price, sma50, sma200, mom) == expected


# ---- report wording --------------------------------------------------------------------
def test_header_wording(cfg):
    text = render(analyze_bundle(build_bundle(), cfg), "detailed", width=140)
    assert "TTM to 2026-06-30" in text
    assert "Maintenance capex" in text and "growth investment" in text
