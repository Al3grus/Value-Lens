"""Regression tests for defects found in the independent review."""

import copy
from datetime import date

import fixtures
import pandas as pd
import pytest
from conftest import build_bundle

from valuelens.analysis.buffett import buffett_scorecard
from valuelens.analysis.decision import decide
from valuelens.analysis.graham import graham_scorecard
from valuelens.analysis.valuation import compute_valuation, unlevered, wacc
from valuelens.data.bundle import (
    _sec_frames,
    looks_like_adr,
    reconcile_share_basis,
    share_basis,
)
from valuelens.data.normalize import normalize
from valuelens.data.sec import _dedupe_close_dates, parse_company_facts
from valuelens.engine import analyze_bundle
from valuelens.models import Signal, Status


def _by_key(card):
    return {c.key: c for c in card.criteria}


# ---- 1. split between the last 10-K and the latest 10-Q -------------------------------
def test_ttm_eps_adjusted_for_split_between_10k_and_10q():
    facts = fixtures.company_facts()
    gaap = facts["facts"]["us-gaap"]
    for row in gaap["EarningsPerShareDiluted"]["units"]["USD/shares"]:
        if row["form"] == "10-Q":
            row["val"] /= 3  # 3-for-1 split on 2026-04-01, before the 10-Q was filed
    for row in gaap["WeightedAverageNumberOfDilutedSharesOutstanding"]["units"]["shares"]:
        if row["form"] == "10-Q":
            row["val"] *= 3
    splits = pd.concat([fixtures.splits(), pd.Series([3.0], index=pd.DatetimeIndex(["2026-04-01"]))])
    parsed = parse_company_facts(facts)
    raw, ttm_raw = _sec_frames(parsed, splits)

    f25, f26 = fixtures.fundamentals(2025), fixtures.fundamentals(2026)
    expected_eps = (f25["eps_post"] + fixtures.H1_SHARE * (f26["eps_post"] - f25["eps_post"])) / 3
    assert ttm_raw["eps_diluted"].iloc[0] == pytest.approx(expected_eps)
    assert ttm_raw["shares_diluted"].iloc[0] == pytest.approx(f26["shares_post"] * 3)
    assert raw["eps_diluted"].iloc[-1] == pytest.approx(f25["eps_post"] / 3)


# ---- 2. TTM share count is the latest weighted average, not FY + YTD - YTD ------------
def test_ttm_shares_is_latest_ytd_average(parsed):
    assert parsed.ttm["shares_diluted"].val == pytest.approx(fixtures.fundamentals(2026)["shares_post"])


def test_share_basis_prefers_consistent_cover_count():
    assert share_basis(pd.Series({"shares": 1.80e9, "shares_outstanding": 1.77e9})) == 1.77e9
    # Berkshire-style sum of A + B class counts is not comparable: use the diluted count.
    assert share_basis(pd.Series({"shares": 2.16e9, "shares_outstanding": 1.44e9})) == 2.16e9
    assert share_basis(pd.Series({"shares": float("nan")}), {"impliedSharesOutstanding": 5e8}) == 5e8


def test_market_cap_uses_cover_page_shares(bundle):
    assert bundle.shares == pytest.approx(1.77e9)


# ---- 3. negative owner FCF must FAIL, not drop out ------------------------------------
def test_negative_owner_fcf_fails(cfg):
    b = build_bundle()
    b.annual.loc[b.annual.index[-3:], "owner_fcf"] = -1e9
    card = buffett_scorecard(b, compute_valuation(b, cfg), cfg)
    c = _by_key(card)
    assert c["fcf_cagr"].status is Status.FAIL
    assert card.ratio(("quality",)) < 1.0


# ---- 4. ADRs / share classes ----------------------------------------------------------
def test_adr_detection_and_rescale():
    assert looks_like_adr(8.0e9, 1.0e9)
    assert not looks_like_adr(1.5e9, 1.0e9)
    assert not looks_like_adr(1.02e9, 1.0e9)

    annual = pd.DataFrame({"eps": [1.0, 1.2], "shares": [8e9, 8e9], "shares_outstanding": [float("nan")] * 2})
    ttm = pd.Series({"eps": 1.3, "shares": 8e9, "shares_outstanding": 8e9})
    ratio = reconcile_share_basis(annual, ttm, traded_shares=1e9)
    assert ratio == pytest.approx(8.0)
    assert ttm["eps"] == pytest.approx(10.4) and ttm["shares"] == pytest.approx(1e9)
    assert list(annual["eps"]) == pytest.approx([8.0, 9.6])


def test_reconcile_noop_when_consistent():
    ttm = pd.Series({"eps": 2.0, "shares": 1.0e9, "shares_outstanding": 1.0e9})
    annual = pd.DataFrame({"eps": [2.0], "shares": [1e9], "shares_outstanding": [1e9]})
    assert reconcile_share_basis(annual, ttm, traded_shares=1.05e9) is None
    assert ttm["eps"] == 2.0


# ---- 5. Graham size test in local currency --------------------------------------------
def test_graham_size_threshold_converted(cfg):
    b = build_bundle(currency="JPY", usd_fx=150.0)  # revenue 24.2B "JPY" ~ $0.16B
    c = _by_key(graham_scorecard(b, compute_valuation(b, cfg), cfg))
    assert c["size"].status is Status.FAIL
    b = build_bundle(currency="JPY", usd_fx=None)
    c = _by_key(graham_scorecard(b, compute_valuation(b, cfg), cfg))
    assert c["size"].status is Status.NA


# ---- 6. fiscal-year change ------------------------------------------------------------
def test_fiscal_year_change_drops_overlapping_year():
    ends = [date(2022, 6, 30), date(2023, 6, 30), date(2023, 12, 31), date(2024, 12, 31)]
    assert _dedupe_close_dates(ends) == [date(2022, 6, 30), date(2023, 12, 31), date(2024, 12, 31)]
    # 52/53-week years a few days apart are still kept as separate years
    ends = [date(2022, 9, 24), date(2023, 9, 30), date(2024, 9, 28)]
    assert _dedupe_close_dates(ends) == ends


# ---- 7. capex / net income uses the same years -----------------------------------------
def test_capex_ratio_uses_matching_years(cfg):
    b = build_bundle()
    b.annual.loc[b.annual.index[-6:], "capex"] = float("nan")
    c = _by_key(buffett_scorecard(b, compute_valuation(b, cfg), cfg))
    assert c["capex_ni"].value == pytest.approx(0.06 / 0.22)


# ---- 8. missing data cannot upgrade the verdict ---------------------------------------
def test_low_coverage_caps_at_hold(cfg):
    b = build_bundle(price=35.0)
    r = analyze_bundle(b, cfg)
    assert r.decision.signal in (Signal.BUY, Signal.STRONG_BUY)
    sparse = copy.deepcopy(r.buffett)
    for crit in sparse.criteria[:-3]:
        crit.status = Status.NA
    for card in (r.graham, r.factors):
        for crit in card.criteria[1:]:
            crit.status = Status.NA
    d = decide(r.graham, sparse, r.factors, r.valuation, r.health, r.technicals, b.years, cfg)
    assert d.signal is Signal.HOLD
    assert any("could be evaluated" in w for w in d.warnings)


def test_single_valuation_method_caps_at_hold(cfg):
    b = build_bundle(price=35.0)
    r = analyze_bundle(b, cfg)
    val = copy.deepcopy(r.valuation)
    for m in val.methods[1:]:
        m.value = None
    d = decide(r.graham, r.buffett, r.factors, val, r.health, r.technicals, b.years, cfg)
    assert d.signal is Signal.HOLD
    assert any("single valuation method" in w for w in d.warnings)


# ---- 9. DCF on unlevered cash flow at WACC ---------------------------------------------
def test_wacc(cfg):
    vc = cfg["valuation"]
    assert wacc(0.10, 100.0, 0.0, 0.0, 0.2, 0.042, vc) == pytest.approx(0.10)
    assert wacc(0.14, 100.0, 100.0, 5.0, 0.2, 0.042, vc) == pytest.approx(0.09)
    assert wacc(0.09, 100.0, 300.0, 3.0, 0.2, 0.042, vc) == pytest.approx(vc["min_discount_rate"])


def test_unlevered_adds_back_after_tax_interest():
    row = pd.Series({"owner_fcf": 100.0, "interest_expense": 10.0})
    assert unlevered(row, "owner_fcf", 0.25) == pytest.approx(107.5)
    row = pd.Series({"owner_fcf": 100.0, "interest_expense": float("nan")})
    assert unlevered(row, "owner_fcf", 0.25) == pytest.approx(100.0)


# ---- debt not reported while interest is paid ------------------------------------------
def test_unknown_debt_is_not_treated_as_zero(cfg):
    b = build_bundle(debt_data_found=False)
    assert b.debt_unknown
    val = compute_valuation(b, cfg)
    assert any("no debt was found" in n for n in val.notes)
    assert _by_key(graham_scorecard(b, val, cfg))["ltd_nca"].status is Status.NA
    assert _by_key(buffett_scorecard(b, val, cfg))["nd_ebitda"].status is Status.NA


def test_normalize_keeps_cover_shares():
    raw = pd.DataFrame({"net_income": [10.0], "eps_diluted": [1.0], "shares_outstanding": [9.5]})
    assert normalize(raw)["shares_outstanding"].iloc[0] == 9.5
