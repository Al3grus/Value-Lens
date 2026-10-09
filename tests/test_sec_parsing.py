from datetime import date

import fixtures
import pytest

from valuelens.data.normalize import split_factor
from valuelens.data.sec import SecClient, SecError, parse_company_facts


def test_fiscal_years_detected(parsed):
    assert parsed.fiscal_year_ends[0] == date(2014, 12, 31)
    assert parsed.fiscal_year_ends[-1] == date(2025, 12, 31)
    assert len(parsed.fiscal_year_ends) == 12
    assert parsed.currency == "USD"


def test_revenue_concept_switch_is_merged(parsed):
    rev = parsed.annual["revenue"]
    for y in fixtures.YEARS:
        assert rev[date(y, 12, 31)].val == pytest.approx(fixtures.fundamentals(y)["rev"])


def test_quarter_inside_10k_is_not_annual(parsed):
    # The Q4-only revenue facts (27% of the year) must never be picked.
    assert all(v.val > fixtures.fundamentals(2014)["rev"] * 0.9 for v in parsed.annual["revenue"].values())


def test_latest_filing_wins(parsed):
    # FY2018 EPS appears in the FY2018, FY2019 (pre-split) and FY2020 (post-split) 10-Ks.
    v = parsed.annual["eps_diluted"][date(2018, 12, 31)]
    assert v.filed == date(2021, 2, 1)
    assert v.val == pytest.approx(fixtures.fundamentals(2018)["eps_post"])


def test_ttm_from_10q(parsed):
    assert parsed.ttm_end == date(2026, 6, 30)
    f25, f26 = fixtures.fundamentals(2025), fixtures.fundamentals(2026)
    expected = f25["rev"] + fixtures.H1_SHARE * (f26["rev"] - f25["rev"])
    assert parsed.ttm["revenue"].val == pytest.approx(expected)
    assert parsed.ttm["total_assets"].val == pytest.approx(f26["assets"] * 0.98)


def test_split_factor():
    s = fixtures.splits()
    assert split_factor(s, date(2020, 2, 1)) == 2.0
    assert split_factor(s, date(2021, 2, 1)) == 1.0
    assert split_factor(None, date(2021, 2, 1)) == 1.0


def test_split_adjusted_eps_is_continuous(bundle):
    eps = bundle.annual["eps"]
    growth = eps.pct_change().dropna()
    assert growth.between(0.05, 0.12).all(), growth  # no -50% / +100% jumps around the split


def test_dei_shares(parsed):
    assert parsed.ttm["shares_outstanding"].val == pytest.approx(1.77e9)


def test_no_annual_data_raises():
    with pytest.raises(SecError):
        parse_company_facts({"facts": {"us-gaap": {}}})


def test_user_agent_required(tmp_path):
    from valuelens.data.cache import FileCache

    with pytest.raises(SecError, match="User-Agent"):
        SecClient("", FileCache(tmp_path), {})
