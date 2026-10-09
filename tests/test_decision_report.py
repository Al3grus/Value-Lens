import json

import numpy as np
import pandas as pd
import pytest
from conftest import build_bundle

from valuelens.analysis.decision import decide
from valuelens.analysis.signals import framework_signal
from valuelens.analysis.technicals import compute_technicals, rsi
from valuelens.config import _deep_merge, load_config
from valuelens.data.fred import parse_latest
from valuelens.engine import analyze_bundle
from valuelens.models import HealthScore, Signal
from valuelens.report.json_out import to_json
from valuelens.report.text import render

RULES = {"strong_buy_score": 0.7, "strong_buy_mos": 0.33, "buy_score": 0.6, "buy_mos": 0.15,
         "sell_mos": -0.30, "sell_score": 0.4}  # fmt: skip


@pytest.mark.parametrize(
    ("score", "mos", "expected"),
    [
        (0.71, -0.156, Signal.HOLD),  # old script said BUY for MSFT here
        (0.50, 0.255, Signal.BUY.capped_at(Signal.HOLD)),
        (0.75, 0.40, Signal.STRONG_BUY),
        (0.65, 0.20, Signal.BUY),
        (0.90, -0.50, Signal.SELL),
        (0.30, 0.50, Signal.SELL),
        (0.90, None, Signal.HOLD),
    ],
)
def test_framework_signal(score, mos, expected):
    assert framework_signal(score, mos, RULES) is expected


def test_signal_capping():
    assert Signal.STRONG_BUY.capped_at(Signal.HOLD) is Signal.HOLD
    assert Signal.SELL.capped_at(Signal.HOLD) is Signal.SELL


def test_overvalued_quality_company_is_hold(cfg):
    r = analyze_bundle(build_bundle(price=90.0), cfg)
    assert r.decision.components["quality"] == 100
    assert r.valuation.margin_of_safety < 0
    assert r.decision.signal is Signal.HOLD


def test_cheap_quality_company_is_buy(cfg):
    r = analyze_bundle(build_bundle(price=35.0), cfg)
    assert r.valuation.margin_of_safety > 0.2
    assert r.decision.signal in (Signal.STRONG_BUY, Signal.BUY)


def test_red_flags_cap_signal(cfg):
    b = build_bundle(price=30.0)
    r = analyze_bundle(b, cfg)
    flags = [
        HealthScore("Altman Z-score", 0.5, "distress", "", red_flag=True),
        HealthScore("Beneish M-score", -1.0, "possible manipulation", "", red_flag=True),
    ]
    d = decide(r.graham, r.buffett, r.factors, r.valuation, flags, r.technicals, b.years, cfg)
    assert d.signal.rank >= Signal.SELL.rank
    assert len(d.warnings) >= 2


def test_rsi_extremes():
    up = pd.Series(np.arange(1, 100, dtype=float))
    assert rsi(up) == 100.0
    assert rsi(pd.Series([1.0, 2.0])) is None


def test_technicals_downtrend():
    idx = pd.bdate_range(end="2026-09-30", periods=400)
    close = pd.Series(np.linspace(200, 100, 400), index=idx)
    t = compute_technicals(pd.DataFrame({"Close": close, "Adj Close": close}))
    assert t.trend == "DOWNTREND"
    assert t.momentum_12_1 < 0


def test_render_and_json(bundle, cfg):
    r = analyze_bundle(bundle, cfg)
    text = render(r, "detailed")
    for section in ("VERDICT", "INTRINSIC VALUE", "GRAHAM: DEFENSIVE INVESTOR", "BUFFETT: BUSINESS QUALITY",
                    "FINANCIAL HEALTH", "RESEARCH-BACKED FACTORS", "PRICE TREND", "NOTES",
                    "Educational only"):  # fmt: skip
        assert section in text
    ascii_text = render(r, "detailed", ascii_only=True)
    ascii_text.encode("ascii")  # must not raise
    data = json.loads(to_json([r]))
    assert data["ticker"] == "TEST"
    assert data["decision"]["signal"] in {s.value for s in Signal}
    assert len(data["buffett"]["criteria"]) == 14


def test_config_merge(tmp_path):
    p = tmp_path / "valuelens.toml"
    p.write_text('[graham]\nmax_pe_3y = 20.0\n[sec]\nuser_agent = "A B a@b.co"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg["graham"]["max_pe_3y"] == 20.0
    assert cfg["graham"]["min_current_ratio"] == 2.0  # untouched default
    assert _deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 3}}) == {"a": {"b": 3, "c": 2}}


def test_fred_parse():
    csv = "observation_date,AAA\n2026-07-01,5.31\n2026-08-01,5.28\n2026-09-01,\n"
    assert parse_latest(csv) == (5.28, "2026-08-01")


def test_fred_api_parse_skips_missing():
    from valuelens.data.fred import parse_api

    data = {"observations": [{"date": "2026-10-08", "value": "."}, {"date": "2026-10-07", "value": "5.28"}]}
    assert parse_api(data) == (5.28, "2026-10-07")
    assert parse_api({"observations": []}) is None


def test_fred_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VALUELENS_FRED_API_KEY", "a" * 32)
    assert load_config()["fred"]["api_key"] == "a" * 32


def test_fred_api_falls_back_to_csv(tmp_path):
    from fakes import FakeTransport

    from valuelens.data import fred
    from valuelens.data.cache import FileCache

    t = FakeTransport(
        {
            fred.FRED_API: (500, {"error_message": "Internal Server Error"}),
            fred.FRED_CSV: (200, "observation_date,AAA\n2026-08-01,6.10\n2026-09-01,6.03\n"),
        }
    )
    assert fred.latest_value("AAA", FileCache(tmp_path), 24, "k" * 32, t) == (6.03, "2026-09-01")
    assert [u.split("?")[0] for u in t.urls()] == [fred.FRED_API, fred.FRED_CSV]
    assert fred.latest_value("AAA", FileCache(tmp_path), 24, "k" * 32, t) == (6.03, "2026-09-01")  # cached
    assert len(t.calls) == 2
