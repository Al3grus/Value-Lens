"""Regression tests for the maths audit: not-applicable vs missing checks, banks and insurers,
short records, core-earnings consistency, unknown capex, the equity bridge, the sensitivity grid,
data age, wording that quotes thresholds, and property tests for the DCF."""

import copy
from datetime import date

import numpy as np
import pandas as pd
import pytest
from conftest import build_bundle

from valuelens.analysis.buffett import buffett_scorecard
from valuelens.analysis.decision import decide
from valuelens.analysis.graham import dividend_streak, graham_scorecard
from valuelens.analysis.health import altman, piotroski
from valuelens.analysis.returns import roe_series, roic_series, rotce_series
from valuelens.analysis.valuation import (
    compute_valuation,
    dcf_enterprise,
    dcf_per_share,
    effective_tax_rate,
    excess_return_value,
    reverse_dcf,
    unlevered,
)
from valuelens.data.concepts import CANONICAL_COLUMNS
from valuelens.data.normalize import apply_owner_earnings, has_capex_data, normalize
from valuelens.engine import analyze_bundle
from valuelens.models import Criterion, Scorecard, Signal, Status
from valuelens.report.content import score_summary, sensitivity_table, valuation_rows
from valuelens.report.glossary import pillar_word, plain_point, summarize
from valuelens.report.text import render

TODAY = date(2026, 10, 12)


def _by_key(card):
    return {c.key: c for c in card.criteria}


# ---- 1 & 2. "not meaningful" leaves the scores; "no data" counts half ----------------------
def test_not_meaningful_checks_leave_scores_and_coverage():
    card = Scorecard(
        "t",
        [
            Criterion.check("a", "A", True, "", 1.0),
            Criterion.check("b", "B", False, "", 1.0),
            Criterion.nm("c", "C", "n/m for financials"),
        ],
    )
    assert card.applicable() == 2 and card.missing() == 0
    assert card.score() == 0.5 and card.ratio() == 0.5


def test_missing_data_is_not_passed_and_never_lifts_a_score():
    passed = Criterion.check("a", "A", True, "", 1.0)
    failed = Criterion.check("b", "B", False, "", 1.0)
    missing = Criterion.na("b", "B", "No data")
    assert Scorecard("t", [passed, failed]).score() == 0.5
    assert Scorecard("t", [passed, missing]).score() == 0.5  # not 1.0, as passed / evaluated gave
    assert Scorecard("t", [passed, missing]).ratio() == 1.0  # the table still shows 1/1 passed


def test_bank_can_be_a_buy_when_cheap(cfg):
    r = analyze_bundle(build_bundle(sic=6022, price=20.0), cfg, today=TODAY)
    assert r.decision.coverage == 1.0  # n/m checks are not "missing"
    assert r.decision.signal in (Signal.BUY, Signal.STRONG_BUY)
    assert r.decision.confidence == "HIGH"
    names = {m.name: m for m in r.valuation.methods}
    assert names["Excess returns (financials)"].value and names["Excess returns (financials)"].weight == 0.5
    assert names["DCF (owner earnings)"].value is None
    assert not any("could be evaluated" in w for w in r.decision.warnings)


def test_bank_scorecards_use_balance_sheet_strength(cfg):
    b = build_bundle(sic=6022)
    g = _by_key(graham_scorecard(b, compute_valuation(b, cfg), cfg))
    assert "current_ratio" not in g and g["equity_assets"].status is Status.PASS
    assert g["equity_assets"].value == pytest.approx(b.ttm["equity"] / b.ttm["total_assets"])
    assert g["tang_equity_assets"].value == pytest.approx(g["equity_assets"].value)  # no intangibles here


def test_missing_data_never_upgrades_the_verdict(cfg):
    """Blank each canonical column in turn: the quality, safety and factor pillars may fall, never
    rise (a check without data is not passed), and the verdict never improves."""
    base = analyze_bundle(build_bundle(price=48.0), cfg, today=TODAY)
    for col in CANONICAL_COLUMNS:
        b = build_bundle(price=48.0)
        b.annual[col] = np.nan
        b.ttm[col] = np.nan
        r = analyze_bundle(b, cfg, today=TODAY)
        for pillar in ("quality", "safety", "factors"):
            before, after = base.decision.components[pillar], r.decision.components[pillar]
            assert after is None or after <= before + 1e-9, (col, pillar, before, after)
        assert r.decision.signal.rank >= base.decision.signal.rank, col


# ---- 3. the maintenance-capex rule is what the config says ---------------------------------
def test_default_maintenance_capex_is_min_of_capex_and_depreciation(bundle):
    a = bundle.annual
    assert np.allclose(a["maint_capex"], np.minimum(a["dep_amort"], a["capex"]))


def test_greenwald_estimate_acts_above_the_depreciation_cap():
    idx = pd.date_range("2021-12-31", periods=6, freq="YE")
    rev = np.array([100.0, 110, 121, 133, 146, 161])
    a = pd.DataFrame(
        {"revenue": rev, "ppe_net": rev * 0.5, "capex": rev * 0.12, "dep_amort": rev * 0.05, "cfo": rev * 0.3,
         "sbc": 0.0, "fcf": rev * 0.18, "owner_fcf": np.nan, "maint_capex": np.nan},
        index=idx,
    )  # fmt: skip
    apply_owner_earnings(a, a.iloc[-1].copy(), max_da_multiple=3.0)
    growth_capex = 0.5 * (161 - 146)  # PP&E / sales x increase in sales
    assert a["maint_capex"].iloc[-1] == pytest.approx(161 * 0.12 - growth_capex)


# ---- 4. the EPS growth bar scales with the record --------------------------------------------
def test_short_eps_record_gets_the_same_yearly_bar(cfg):
    b = build_bundle()
    b.annual = b.annual.iloc[-6:]  # 3-year averages 3 years apart instead of 7
    c = _by_key(graham_scorecard(b, compute_valuation(b, cfg), cfg))["eps_growth"]
    assert c.status is Status.PASS  # ~9%/yr clears a bar of 33% over 7 years (4.2%/yr)
    assert c.threshold == pytest.approx(1.33 ** (3 / 7) - 1)
    assert "bar" in c.detail


def test_full_eps_record_keeps_grahams_bar(bundle, cfg):
    c = _by_key(graham_scorecard(bundle, compute_valuation(bundle, cfg), cfg))["eps_growth"]
    assert c.threshold == pytest.approx(cfg["graham"]["min_eps_growth"])
    assert "bar" not in c.detail


# ---- 5. core earnings are decided once per company ----------------------------------------
def _income_rows(fractions):
    rows = []
    for frac in fractions:
        op, interest = 100.0, 2.0
        pretax = (op - interest) * (1 + frac)
        rows.append(
            {"revenue": 400.0, "operating_income": op, "interest_expense": interest, "pretax_income": pretax,
             "income_tax": pretax * 0.2, "net_income": pretax * 0.8, "eps_diluted": pretax * 0.8 / 10,
             "shares_diluted": 10.0}
        )  # fmt: skip
    return pd.DataFrame(rows)


def test_core_eps_series_does_not_flip_year_to_year():
    n = normalize(_income_rows([0.13, 0.16, 0.14, 0.17]))
    assert n.attrs["core_earnings"] is True
    assert np.allclose(n["core_eps"], 7.84)  # the same non-operating items stripped every year
    assert (n["core_eps"] < n["eps"]).all()
    n = normalize(_income_rows([0.16, 0.05, 0.05, 0.05]))  # material only in an old year
    assert n.attrs["core_earnings"] is False and (n["core_eps"] == n["eps"]).all()


def test_equity_method_income_counts_as_core():
    raw = _income_rows([0.0])
    raw["equity_method_income"] = [30.0]  # affiliates' profits are recurring
    raw["pretax_income"] += 30.0
    raw["net_income"] = raw["pretax_income"] * 0.8
    raw["income_tax"] = raw["pretax_income"] * 0.2
    assert normalize(raw).attrs["core_earnings"] is False


def test_ttm_follows_annual_core_decision():
    annual = normalize(_income_rows([0.16, 0.16, 0.16]))
    ttm = normalize(_income_rows([0.01]), use_core=bool(annual.attrs["core_earnings"]))
    assert ttm.attrs["core_earnings"] is True and ttm["core_eps"].iloc[0] < ttm["eps"].iloc[0]


# ---- 6. unknown capex is unknown, not zero -------------------------------------------------
def test_missing_capex_leaves_fcf_unknown():
    n = normalize(pd.DataFrame({"cfo": [100.0], "net_income": [80.0]}))
    assert np.isnan(n["fcf"].iloc[0]) and np.isnan(n["owner_fcf"].iloc[0])
    assert not has_capex_data(pd.DataFrame({"cfo": [100.0]}))
    assert has_capex_data(pd.DataFrame({"fcf_reported": [5.0]}))


def test_unknown_capex_skips_cash_flow_tests_and_dcf(cfg):
    b = build_bundle(capex_data_found=False)
    for col in ("capex", "fcf", "owner_fcf", "maint_capex"):
        b.annual[col] = np.nan
        b.ttm[col] = np.nan
    val = compute_valuation(b, cfg)
    assert val.methods[0].value is None and any("capital expenditure" in n for n in val.notes)
    c = _by_key(buffett_scorecard(b, val, cfg))
    for key in ("fcf_positive", "fcf_cagr", "capex_ni", "ev_fcf"):
        assert c[key].status is Status.NA and c[key].applicable


# ---- 7. interest coverage needs evidence ---------------------------------------------------
def test_interest_coverage_unknown_when_no_debt_or_interest_found(cfg):
    b = build_bundle(debt_data_found=False)
    b.ttm["interest_expense"] = np.nan
    b.ttm["total_debt"] = 0.0
    c = _by_key(buffett_scorecard(b, compute_valuation(b, cfg), cfg))["interest_cov"]
    assert c.status is Status.NA and "No debt or interest" in c.detail


def test_debt_free_company_still_passes_interest_coverage(cfg):
    b = build_bundle()  # debt concepts found and nil
    b.ttm["interest_expense"] = np.nan
    b.ttm["total_debt"] = 0.0
    c = _by_key(buffett_scorecard(b, compute_valuation(b, cfg), cfg))["interest_cov"]
    assert c.status is Status.PASS and c.detail == "No meaningful debt"


# ---- 8. a dividend record as long as the statements is unknown, not a FAIL -------------------
def test_dividend_streak_as_long_as_the_statements_is_unknown(cfg):
    b = build_bundle(dividends=pd.Series(dtype=float))
    assert dividend_streak(b, today=TODAY) == (12, "cash-flow statements", 12)
    c = _by_key(graham_scorecard(b, compute_valuation(b, cfg), cfg, today=TODAY))["dividends"]
    assert c.status is Status.NA and c.detail.startswith("at least 12y")


def test_dividend_gap_inside_the_statements_still_fails(cfg):
    b = build_bundle(dividends=pd.Series(dtype=float))
    b.annual.loc[b.annual.index[-4], "dividends_paid"] = 0.0
    c = _by_key(graham_scorecard(b, compute_valuation(b, cfg), cfg, today=TODAY))["dividends"]
    assert c.status is Status.FAIL and c.detail.startswith("3/20y")


# ---- 9. the growth comparison names its basis ---------------------------------------------
def test_growth_comparison_names_revenue_when_owner_fcf_growth_is_unavailable(cfg):
    b = build_bundle()
    b.annual.loc[b.annual.index[:-2], "owner_fcf"] = -1.0
    r = analyze_bundle(b, cfg, today=TODAY)
    assert r.valuation.historical_growth_basis == "revenue"
    assert any("revenue growth delivered" in x for x in r.decision.reasons)
    assert "sales grew" in summarize(r).expectation
    r = analyze_bundle(build_bundle(), cfg, today=TODAY)
    assert r.valuation.historical_growth_basis == "owner earnings"
    assert "it grew" in summarize(r).expectation


# ---- 10. the equity bridge ----------------------------------------------------------------
def test_unlevered_takes_out_after_tax_interest_income():
    row = pd.Series({"owner_fcf": 100.0, "interest_expense": 10.0, "interest_income": 4.0})
    assert unlevered(row, "owner_fcf", 0.25) == pytest.approx(100 + 6 * 0.75)
    frame = pd.DataFrame({"owner_fcf": [100.0], "interest_expense": [np.nan], "interest_income": [4.0]})
    assert unlevered(frame, "owner_fcf", 0.25).iloc[0] == pytest.approx(97.0)


def test_minority_interest_and_preferred_reduce_per_share_value(cfg):
    base = compute_valuation(build_bundle(), cfg)
    b = build_bundle()
    b.ttm["minority_interest"] = 2e9
    b.ttm["preferred_stock"] = 1e9
    v = compute_valuation(b, cfg)
    drop = 3e9 / b.shares
    assert v.methods[0].value == pytest.approx(base.methods[0].value - drop)  # DCF
    assert v.methods[2].value == pytest.approx(base.methods[2].value - drop)  # earnings power
    assert any("rank ahead" in n for n in v.notes)


def test_common_and_tangible_equity():
    n = normalize(
        pd.DataFrame({"equity": [100.0], "preferred_stock": [20.0], "goodwill": [30.0], "intangibles": [5.0]})
    )
    assert n["common_equity"].iloc[0] == 80.0 and n["tangible_equity"].iloc[0] == 45.0
    n = normalize(pd.DataFrame({"equity": [100.0], "goodwill_and_intangibles": [40.0]}))
    assert n["tangible_equity"].iloc[0] == 60.0
    n = normalize(pd.DataFrame({"equity": [100.0], "equity_incl_nci": [112.0]}))
    assert n["minority_interest"].iloc[0] == 12.0


def test_lt_investments_and_leases_switches():
    raw = pd.DataFrame(
        {"cash": [10.0], "lt_investments": [50.0], "lt_debt_noncurrent": [30.0], "operating_leases": [20.0]}
    )
    plain = normalize(raw)
    assert plain["net_debt"].iloc[0] == 20.0 and plain["lease_liabilities"].iloc[0] == 20.0
    both = normalize(raw, count_lt_investments=True, leases_as_debt=True)
    assert both["cash_total"].iloc[0] == 60.0 and both["total_debt"].iloc[0] == 50.0
    assert both["net_debt"].iloc[0] == -10.0


# ---- wording ------------------------------------------------------------------------------
def test_utility_debt_check_has_its_own_wording(cfg):
    b = build_bundle(sic=4911)
    c = _by_key(graham_scorecard(b, compute_valuation(b, cfg), cfg))
    assert "ltd_nca" not in c and c["ltd_equity"].status is Status.PASS
    assert "utility" in plain_point(c["ltd_equity"], "USD")
    assert not c["current_ratio"].applicable


def test_plain_sentences_quote_the_configured_threshold(cfg):
    cfg = copy.deepcopy(cfg)
    cfg["buffett"]["max_pe"] = 12.0
    cfg["graham"]["max_pe_3y"] = 9.0
    b = build_bundle(price=95.0)
    val = compute_valuation(b, cfg)
    assert "limit: 12x" in plain_point(_by_key(buffett_scorecard(b, val, cfg))["pe"], "USD")
    assert "limit: 9x" in plain_point(_by_key(graham_scorecard(b, val, cfg))["pe3"], "USD")


def test_fairly_priced_band_straddles_fair_value():
    assert pillar_word("valuation", 50) == pillar_word("valuation", 49) == "Fairly priced"
    assert pillar_word("valuation", 44) == "Fairly priced"
    assert pillar_word("valuation", 43) == "Somewhat expensive"


def test_score_summary_counts_checks_without_data():
    card = Scorecard(
        "t",
        [
            Criterion.check("a", "A", True, "", 1.0),
            Criterion.na("b", "B", "No data"),
            Criterion.nm("c", "C", "n/m"),
        ],
    )
    assert score_summary(card) == ("1/1", " passed (100%), 1 without data", "good")


# ---- additions -----------------------------------------------------------------------------
def test_sensitivity_grid_brackets_the_base_case(bundle, cfg):
    v = compute_valuation(bundle, cfg)
    vc = cfg["valuation"]
    assert 1 < len(v.sensitivity) <= len(vc["sensitivity_rate_steps"])
    assert all(len(row) == len(vc["sensitivity_growth_steps"]) for row in v.sensitivity)
    grid = sensitivity_table(analyze_bundle(bundle, cfg, today=TODAY))
    i, j = grid.base
    assert v.sensitivity[i][j] == pytest.approx(v.methods[0].value)
    column = [row[j] for row in v.sensitivity]  # value falls as the rate rises, rises with growth
    assert column == sorted(column, reverse=True) and v.sensitivity[i] == sorted(v.sensitivity[i])


def test_valuation_rows_show_cover_sbc_and_multiples(bundle, cfg):
    r = analyze_bundle(bundle, cfg, today=TODAY)
    v, t = r.valuation, bundle.ttm
    assert v.dividend_cover == pytest.approx(t["owner_fcf"] / t["dividends_paid"])
    assert v.sbc_to_cfo == pytest.approx(t["sbc"] / t["cfo"])
    assert v.ev_ebit_now == pytest.approx(1 / v.earnings_yield)
    assert v.p_fcf_now == pytest.approx(1 / v.fcf_yield)
    assert v.ev_ebit_median_10y and v.p_fcf_median_10y and v.pe_median_10y
    labels = [row.label for row in valuation_rows(r, "·")]
    for label in ("Dividend cover", "Stock compensation", "EV/EBIT", "Price / owner FCF"):
        assert label in labels
    text = render(r, "detailed", width=160)
    assert "Sensitivity" in text and "Dividend cover" in text


def test_stale_statements_warn_and_cap_confidence(bundle, cfg):
    fresh = analyze_bundle(bundle, cfg, today=TODAY)
    stale = analyze_bundle(bundle, cfg, today=date(2027, 10, 12))
    assert not any("days old" in w for w in fresh.decision.warnings)
    assert any("days old" in w for w in stale.decision.warnings)
    assert fresh.decision.confidence == "HIGH" and stale.decision.confidence == "MEDIUM"


def test_excess_return_value():
    assert excess_return_value(10.0, 0.15, 0.10, 0.025) == pytest.approx(10 * (1 + 0.05 / 0.075))
    assert excess_return_value(10.0, 0.10, 0.10, 0.025) == pytest.approx(10.0)  # earns its cost: worth book
    assert excess_return_value(-1.0, 0.15, 0.10, 0.025) is None
    assert excess_return_value(10.0, 0.15, 0.02, 0.025) is None


# ---- formulas that had no direct test -----------------------------------------------------
def test_effective_tax_rate_band():
    a = pd.DataFrame({"pretax_income": [100.0] * 5, "income_tax": [5.0, 6.0, 5.0, 7.0, 6.0]})
    assert effective_tax_rate(a, 0.21, 0.10, 0.35) == 0.10  # a 6% median is floored
    a["income_tax"] = [40.0] * 5
    assert effective_tax_rate(a, 0.21, 0.10, 0.35) == 0.35
    a["pretax_income"] = [-1.0] * 5
    assert effective_tax_rate(a, 0.21) == 0.21


def test_return_series_use_average_balances():
    a = pd.DataFrame(
        {"net_income": [10.0, 12.0], "equity": [100.0, 120.0], "tangible_equity": [50.0, 70.0],
         "operating_income": [20.0, 24.0], "income_tax": [2.0, 2.4], "pretax_income": [10.0, 12.0],
         "total_debt": [50.0, 50.0], "cash_total": [10.0, 10.0]}
    )  # fmt: skip
    assert roe_series(a).tolist() == pytest.approx([0.10, 12 / 110])
    assert rotce_series(a).tolist() == pytest.approx([0.20, 12 / 60])
    # tax 20%: NOPAT 16 and 19.2 over invested capital 140 and the average of 140 and 160
    assert roic_series(a, 0.21, 0.10, 0.35).tolist() == pytest.approx([16 / 140, 19.2 / 150])


def test_altman_formulas():
    b = build_bundle(sic=3571)  # manufacturer: the original Z with market value of equity
    z, _zone, variant = altman(b)
    t = b.ttm
    wc, ta, tl = t["current_assets"] - t["current_liabilities"], t["total_assets"], t["total_liabilities"]
    expected = (1.2 * wc / ta + 1.4 * t["retained_earnings"] / ta + 3.3 * t["operating_income"] / ta
                + 0.6 * b.market_cap / tl + t["revenue"] / ta)  # fmt: skip
    assert z == pytest.approx(expected) and variant.startswith("original")
    z2, _zone, variant2 = altman(build_bundle(sic=7372))
    expected2 = (6.56 * wc / ta + 3.26 * t["retained_earnings"] / ta + 6.72 * t["operating_income"] / ta
                 + 1.05 * t["equity"] / tl)  # fmt: skip
    assert z2 == pytest.approx(expected2) and variant2.startswith("Z''")


def test_piotroski_partial_inputs():
    idx = pd.date_range("2022-12-31", periods=3, freq="YE")
    a = pd.DataFrame({"net_income": [5.0, 8.0, 12.0], "total_assets": [100.0] * 3, "cfo": [10.0, 12.0, 15.0]}, index=idx)  # fmt: skip
    for col in ("lt_debt", "current_assets", "current_liabilities", "shares", "gross_profit", "revenue"):
        a[col] = np.nan
    assert piotroski(a) == (4, 4, [])


def test_dcf_is_monotone_and_reverse_dcf_round_trips(cfg):
    vc = cfg["valuation"]
    rng = np.random.default_rng(0)
    for _ in range(200):
        base, r = rng.uniform(1e8, 1e10), rng.uniform(0.06, 0.15)
        net_cash, shares, g = rng.uniform(-5e9, 5e9), rng.uniform(1e7, 1e10), rng.uniform(-0.4, 0.9)
        assert dcf_enterprise(base, g, r, 0.025, 5, 5) <= dcf_enterprise(base, g + 0.01, r, 0.025, 5, 5)
        assert dcf_enterprise(base, g, r, 0.025, 5, 5) >= dcf_enterprise(base, g, r + 0.01, 0.025, 5, 5)
        price = dcf_per_share(base, g, r, vc, net_cash, shares)
        if price > 0:
            implied, note = reverse_dcf(price, base, r, vc, net_cash, shares)
            assert note == "" and implied == pytest.approx(g, abs=1e-5)


# ---- found on real companies after the fixture tests passed ---------------------------------
def test_health_scores_not_meaningful_for_banks_and_utilities(cfg):
    from valuelens.analysis.health import health_scores

    bank = {h.name: h for h in health_scores(build_bundle(sic=6022), cfg)}
    assert all(h.value is None and not h.applicable for h in bank.values())
    utility = {h.name: h for h in health_scores(build_bundle(sic=4911), cfg)}
    assert utility["Altman Z-score"].value is None and not utility["Altman Z-score"].applicable
    assert utility["Piotroski F-score"].value is not None and utility["Beneish M-score"].value is not None
    r = analyze_bundle(build_bundle(sic=4911, price=35.0), cfg, today=TODAY)
    assert not any("Altman" in w for w in r.decision.warnings)


def test_no_ev_or_fcf_multiples_for_banks(cfg):
    v = compute_valuation(build_bundle(sic=6022), cfg)
    assert v.ev_ebit_now is None and v.ev_ebit_median_10y is None
    assert v.p_fcf_now is None and v.fcf_yield is None and v.earnings_yield is None
    assert v.pe_now and v.pe_median_10y  # earnings multiples still apply


def test_capex_note_not_for_banks(cfg):
    bank = analyze_bundle(build_bundle(sic=6022, capex_data_found=False), cfg, today=TODAY)
    assert not any("Capital expenditure" in n for n in bank.notes)
    firm = analyze_bundle(build_bundle(capex_data_found=False), cfg, today=TODAY)
    assert any("Capital expenditure" in n for n in firm.notes)


def test_dispersion_warning_ignores_reference_only_methods(bundle, cfg):
    r = analyze_bundle(bundle, cfg, today=TODAY)
    val = copy.deepcopy(r.valuation)
    for m in val.methods:
        if m.weight == 0:
            m.value = 1.0  # the Graham number is shown for reference, never blended
    d = decide(r.graham, r.buffett, r.factors, val, r.health, r.technicals, bundle.years, cfg)
    assert not any("disagree" in w for w in d.warnings)
