from datetime import date

import numpy as np
import pandas as pd
import pytest
from conftest import build_bundle

from valuelens.analysis.buffett import buffett_scorecard
from valuelens.analysis.factors import factor_scorecard
from valuelens.analysis.graham import dividend_streak, graham_scorecard
from valuelens.analysis.health import beneish, health_scores, piotroski
from valuelens.analysis.valuation import compute_valuation
from valuelens.models import Status


def _by_key(card):
    return {c.key: c for c in card.criteria}


def test_graham_fixture(bundle, cfg):
    card = graham_scorecard(bundle, compute_valuation(bundle, cfg), cfg)
    c = _by_key(card)
    assert len(card.criteria) == 8
    assert c["size"].status is Status.PASS
    assert c["stability"].status is Status.PASS
    assert c["dividends"].status is Status.PASS
    assert c["eps_growth"].status is Status.PASS
    assert c["eps_growth"].value == pytest.approx(0.839, abs=0.01)
    assert c["pe3"].status is Status.FAIL  # price 60 / avg EPS ~2.63 = 22.8
    assert card.extras["intrinsic_value"] > 0


def test_short_dividend_record_fails_not_na(cfg):
    divs = pd.Series(0.2, index=pd.DatetimeIndex(["2024-03-15", "2025-03-15", "2026-03-15"]))
    b = build_bundle(dividends=divs)
    c = _by_key(graham_scorecard(b, compute_valuation(b, cfg), cfg))
    assert c["dividends"].status is Status.FAIL
    assert c["dividends"].detail.startswith("2/20y")


def test_dividend_streak_needs_consecutive_years():
    divs = pd.Series(
        1.0, index=pd.DatetimeIndex([f"{y}-06-01" for y in (2018, 2019, 2021, 2022, 2023, 2024, 2025)])
    )
    b = build_bundle(dividends=divs)
    assert dividend_streak(b, today=date(2026, 10, 1))[0] == 5


def test_buffett_fixture(bundle, cfg):
    card = buffett_scorecard(bundle, compute_valuation(bundle, cfg), cfg)
    c = _by_key(card)
    assert len(card.criteria) == 14
    assert c["gross_margin"].value == pytest.approx(0.60)
    assert c["op_margin"].value == pytest.approx(0.30)
    assert c["rev_cagr"].value == pytest.approx(0.08)
    assert c["interest_cov"].status is Status.PASS  # bug in old script: always "No data"
    assert c["nd_ebitda"].status is Status.PASS
    assert c["dilution"].status is Status.PASS
    assert card.ratio(("quality",)) == 1.0


def test_buffett_financial_company_na(cfg):
    b = build_bundle(sic=6022)
    c = _by_key(buffett_scorecard(b, compute_valuation(b, cfg), cfg))
    for key in ("fcf_positive", "roic", "gross_margin", "nd_ebitda", "ev_fcf"):
        assert c[key].status is Status.NA
    assert c["roe"].status is not Status.NA


def test_negative_equity_roe_is_na(cfg):
    b = build_bundle()
    b.annual["equity"] = -abs(b.annual["equity"])
    c = _by_key(buffett_scorecard(b, compute_valuation(b, cfg), cfg))
    assert c["roe"].status is Status.NA
    assert "negative equity" in c["roe"].detail


def test_factors(bundle, cfg):
    card = factor_scorecard(bundle, cfg)
    c = _by_key(card)
    assert c["gp_assets"].value == pytest.approx(0.40)
    assert c["asset_growth"].value == pytest.approx(0.08)


def _frame(**cols):
    n = len(next(iter(cols.values())))
    base = {k: np.full(n, np.nan) for k in (
        "net_income", "total_assets", "cfo", "lt_debt", "current_assets", "current_liabilities",
        "shares", "gross_profit", "revenue", "receivables", "ppe_net", "dep_amort", "sga",
    )}  # fmt: skip
    base.update({k: np.asarray(v, dtype=float) for k, v in cols.items()})
    return pd.DataFrame(base, index=pd.date_range("2022-12-31", periods=n, freq="YE"))


def test_piotroski_perfect_score():
    a = _frame(
        net_income=[5, 8, 12], total_assets=[100, 100, 100], cfo=[10, 12, 15],
        lt_debt=[30, 25, 20], current_assets=[40, 45, 50], current_liabilities=[30, 30, 30],
        shares=[100, 99, 98], gross_profit=[40, 42, 46], revenue=[100, 100, 105],
    )  # fmt: skip
    score, n, failed = piotroski(a)
    assert (score, n, failed) == (9, 9, [])


def test_beneish_neutral_company():
    a = _frame(
        net_income=[10, 10], cfo=[10, 10], total_assets=[100, 100], current_assets=[40, 40],
        current_liabilities=[20, 20], lt_debt=[10, 10], gross_profit=[50, 50], revenue=[100, 100],
        receivables=[10, 10], ppe_net=[30, 30], dep_amort=[5, 5], sga=[20, 20],
    )  # fmt: skip
    m, _idx, missing = beneish(a)
    assert missing == []
    assert m == pytest.approx(-4.84 + 0.920 + 0.528 + 0.404 + 0.892 + 0.115 - 0.172 - 0.327)


def test_health_on_fixture(bundle, cfg):
    names = {h.name: h for h in health_scores(bundle, cfg)}
    assert names["Altman Z-score"].zone == "safe"
    assert names["Beneish M-score"].zone == "clean"
    assert not any(h.red_flag for h in names.values())
