"""Simple and detailed views, plain-English summary, CLI and loading animation."""

import io
import json

import pytest
from conftest import build_bundle
from rich.console import Console

from valuelens import cli
from valuelens.engine import analyze_bundle
from valuelens.models import HealthScore, Signal
from valuelens.report.glossary import MEANING, PLAIN, THEME, headline, pillar_word, summarize
from valuelens.report.json_out import to_json
from valuelens.report.progress import LoadingDisplay
from valuelens.report.text import render


@pytest.fixture
def cheap(cfg):
    return analyze_bundle(build_bundle(price=48.0), cfg)


@pytest.fixture
def pricey(cfg):
    return analyze_bundle(build_bundle(price=95.0), cfg)


# ---- simple view -------------------------------------------------------------------------
def test_simple_view_is_plain_english(cheap):
    text = render(cheap, "simple")
    for part in ("Verdict", "What is it worth?", "Fair value", "Scorecard", "Business quality",
                 "Strengths", "Timing", "not investment advice"):  # fmt: skip
        assert part in text
    assert "ROIC" not in text and "EBITDA" not in text  # no jargon in the simple view
    assert "trading well below our value estimate" in text


def test_simple_view_concerns_for_expensive_stock(pricey):
    text = render(pricey, "simple")
    assert "Concerns" in text and "Expensive" in text
    assert "priced far above our value estimate" in text
    assert "wait for a better price" in text  # HOLD timing advice


@pytest.mark.parametrize("view", ["simple", "detailed"])
@pytest.mark.parametrize("width", [60, 80, 120, 160])
def test_ascii_output_is_pure_ascii_at_any_width(cheap, view, width):
    render(cheap, view, ascii_only=True, width=width).encode("ascii")


@pytest.mark.parametrize("view", ["simple", "detailed"])
def test_unicode_output_renders_at_narrow_width(pricey, view):
    assert "Verdict" in render(pricey, view, width=60)


# ---- detailed view -----------------------------------------------------------------------
def test_detailed_view_explains_every_check(cheap):
    text = render(cheap, "detailed", width=200)
    for card in (cheap.graham, cheap.buffett, cheap.factors):
        for c in card.criteria:
            assert " ".join(MEANING[c.key].split()[:3]) in text, c.key  # long notes wrap
    assert "What it means" in text and "Strengths" in text and "Concerns" in text


# ---- summary wording ---------------------------------------------------------------------
def test_every_check_has_wording():
    keys = set(THEME)
    assert keys <= set(MEANING) and keys <= set(PLAIN)


def test_summary_one_point_per_theme(pricey):
    s = summarize(pricey)
    assert 1 <= len(s.concerns) <= 4
    # P/E and Graham's 3-year P/E share a theme: never both
    assert not (
        any("core earnings" in c for c in s.concerns) and any("Graham's standard" in c for c in s.concerns)
    )


def test_red_flags_lead_concerns(cheap):
    cheap.health = [HealthScore("Beneish M-score", -1.0, "possible manipulation", "", red_flag=True)]
    s = summarize(cheap)
    assert s.concerns[0].startswith("Accounting red flag")
    assert "red flags" in s.headline
    assert "No signs of financial distress" not in " ".join(s.strengths)


def test_headline_contrasts_quality_and_price(cheap, pricey):
    assert headline(cheap).startswith("A high-quality business, trading")
    assert headline(pricey) == "A high-quality business, but priced far above our value estimate."


def test_pillar_words():
    assert pillar_word("quality", 92) == "Excellent"
    assert pillar_word("quality", 20) == "Poor"
    assert pillar_word("valuation", 0) == "Very expensive"
    assert pillar_word("valuation", 90) == "Deep discount"
    assert pillar_word("safety", None) == "n/a"


def test_json_has_summary_and_glossary(cheap):
    data = json.loads(to_json([cheap]))
    assert data["summary"]["headline"]
    assert data["summary"]["strengths"]
    assert data["glossary"]["criteria"]["roe"] == MEANING["roe"]
    assert "pillars" in data["glossary"]


# ---- CLI -----------------------------------------------------------------------------------
@pytest.fixture
def fake_analyze(monkeypatch, cfg):
    def _analyze(ticker, _cfg, progress=None):
        for step in ("market", "filings", "rates", "analysis"):
            if progress:
                progress(step)
        return analyze_bundle(build_bundle(price=48.0, ticker=ticker.upper()), cfg)

    monkeypatch.setattr(cli, "analyze", _analyze)


def test_cli_simple_default(fake_analyze, capsys):
    assert cli.main(["test"]) == 0
    out = capsys.readouterr().out
    assert "What is it worth?" in out and "Side by side" not in out


def test_cli_detailed_and_comparison(fake_analyze, capsys):
    assert cli.main(["aaa", "bbb", "-d"]) == 0
    out = capsys.readouterr().out
    assert "INTRINSIC VALUE" in out and "Side by side" in out and "AAA" in out and "BBB" in out


def test_cli_json(fake_analyze, capsys):
    assert cli.main(["aaa", "bbb", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["ticker"] for d in data] == ["AAA", "BBB"]


def test_cli_ascii(fake_analyze, capsys):
    assert cli.main(["test", "--ascii", "-d"]) == 0
    capsys.readouterr().out.encode("ascii")


# ---- loading animation ------------------------------------------------------------------
def test_loading_display_disabled_when_not_a_terminal():
    console = Console(file=io.StringIO(), force_terminal=False)
    with LoadingDisplay(console, "MSFT") as loader:
        assert not loader.enabled
        loader.step("market")
        loader.step("filings")
    assert console.file.getvalue() == ""


def test_loading_display_animates_and_cleans_up():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=80)
    with LoadingDisplay(console, "MSFT", "(1/2)") as loader:
        assert loader.enabled
        for step in ("market", "filings", "rates", "analysis"):
            loader.step(step)
    assert [k for k, _ in loader._done] == ["market", "filings", "rates"]
    assert "MSFT" in buf.getvalue()  # rendered while running (transient afterwards)


def test_signal_enum_rendering(cheap):
    assert cheap.decision.signal in Signal


# ---- box alignment: only glyphs every monospace font has --------------------------------
BOX_CHARS = set("─│┌┐└┘├┤┬┴┼")


@pytest.mark.parametrize("view", ["simple", "detailed"])
def test_reports_use_only_wgl4_safe_glyphs(cheap, pricey, view):
    from valuelens.report.style import WGL4_SAFE

    for r in (cheap, pricey):
        text = render(r, view, width=120)
        odd = {ch for ch in text if ord(ch) > 127 and ch not in WGL4_SAFE and ch not in BOX_CHARS}
        assert not odd, odd


@pytest.mark.parametrize("view", ["simple", "detailed"])
def test_box_lines_have_equal_width(cheap, view):
    for line in render(cheap, view, width=120).splitlines():
        if line.startswith(("│", "┌", "└")):
            assert len(line) == len(line.rstrip()) and line.rstrip()[-1] in "│┐┘", repr(line)
