from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))

import fixtures

from valuelens.config import load_defaults
from valuelens.data.bundle import DataBundle, _sec_frames
from valuelens.data.normalize import apply_owner_earnings, normalize
from valuelens.data.sec import parse_company_facts


@pytest.fixture
def cfg():
    return load_defaults()


@pytest.fixture
def parsed():
    return parse_company_facts(fixtures.company_facts())


def build_bundle(price: float = 60.0, **overrides) -> DataBundle:
    parsed = parse_company_facts(fixtures.company_facts())
    raw, ttm_raw = _sec_frames(parsed, fixtures.splits())
    annual = normalize(raw)
    ttm = normalize(ttm_raw).iloc[-1].fillna(annual.iloc[-1])
    maint_share = apply_owner_earnings(annual, ttm)
    kwargs = dict(
        ticker="TEST",
        maint_capex_share=maint_share,
        price=price,
        currency="USD",
        annual=annual,
        ttm=ttm,
        ttm_end=pd.Timestamp(parsed.ttm_end),
        history=fixtures.price_history(),
        dividends=fixtures.dividends(),
        splits=fixtures.splits(),
        name="TESTCO INC",
        sector="Technology",
        industry="Software - Application",
        sic=7372,
        info={"beta": 1.0},
        aaa_yield=5.3,
        treasury_10y=4.2,
    )
    kwargs.update(overrides)
    return DataBundle(**kwargs)


@pytest.fixture
def bundle():
    return build_bundle()
