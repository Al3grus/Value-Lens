import math

import pandas as pd
import pytest

from valuelens.analysis.metrics import cagr, clamp, fmt_money, point_cagr, smoothed_cagr
from valuelens.analysis.valuation import (
    compute_valuation,
    dcf_enterprise,
    dcf_per_share,
    discount_rate,
    graham_formula,
    graham_number,
    reverse_dcf,
)


def test_cagr():
    assert cagr(100, 200, 1) == pytest.approx(1.0)
    assert cagr(100, 121, 2) == pytest.approx(0.10)
    assert cagr(-1, 100, 3) is None
    assert cagr(100, 0, 3) is None


def test_smoothed_and_point_cagr():
    s = pd.Series([100 * 1.1**i for i in range(10)])
    g, span = smoothed_cagr(s, 5)
    assert span == 5 and g == pytest.approx(0.10)
    g, span = point_cagr(s, 5)
    assert span == 5 and g == pytest.approx(0.10)
    g, span = point_cagr(s.iloc[:3], 5)
    assert span == 2


def test_fmt_money():
    assert fmt_money(63.95e9) == "$63.95B"
    assert fmt_money(-1.5e12) == "-$1.50T"
    assert fmt_money(None) == "n/a"


def test_dcf_zero_growth_is_perpetuity():
    # With g1 = gt = 0 the value equals a perpetuity: CF / r.
    v = dcf_enterprise(100.0, 0.0, 0.10, 0.0, 5, 5)
    assert v == pytest.approx(1000.0)


def test_reverse_dcf_round_trip(cfg):
    vc = cfg["valuation"]
    price = dcf_per_share(1e9, 0.07, 0.09, vc, 5e8, 1e8)
    g, note = reverse_dcf(price, 1e9, 0.09, vc, 5e8, 1e8)
    assert note == "" and g == pytest.approx(0.07, abs=1e-6)


def test_discount_rate_floor_and_beta_clamp(cfg):
    vc = cfg["valuation"]
    assert discount_rate(0.2, 0.01, vc) == pytest.approx(vc["min_discount_rate"])
    assert discount_rate(3.0, 0.04, vc) == pytest.approx(0.04 + vc["beta_cap"] * vc["equity_risk_premium"])
    assert discount_rate(None, 0.042, vc) == pytest.approx(0.092)


def test_graham_formula_and_number():
    # EPS 5, g 10% -> 5 * (8.5 + 20) * 4.4 / 4.4 = 142.5
    assert graham_formula(5.0, 0.10, 4.4) == pytest.approx(142.5)
    assert graham_formula(-1.0, 0.10, 4.4) is None
    assert graham_number(4.0, 10.0) == pytest.approx(math.sqrt(900))
    assert graham_number(4.0, -1.0) is None


def test_valuation_on_fixture(bundle, cfg):
    v = compute_valuation(bundle, cfg)
    names = {m.name: m for m in v.methods}
    assert names["DCF (owner earnings)"].value and names["Graham formula"].value
    assert v.fair_value and v.bear < names["DCF (owner earnings)"].value < v.bull
    assert v.dcf_growth == pytest.approx(0.08, abs=0.005)
    assert -1 < v.margin_of_safety < 1
    assert clamp(v.discount_rate, 0.08, 0.2) == v.discount_rate


def test_financials_skip_dcf(cfg):
    from conftest import build_bundle

    b = build_bundle(sic=6022)  # state commercial bank
    v = compute_valuation(b, cfg)
    names = {m.name: m for m in v.methods}
    assert names["DCF (owner earnings)"].value is None
    assert names["Graham number"].weight > 0
