"""Run the full analysis for a ticker and return a ``Report``."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .analysis.buffett import buffett_scorecard
from .analysis.decision import decide
from .analysis.factors import factor_scorecard
from .analysis.graham import graham_scorecard
from .analysis.health import health_scores
from .analysis.metrics import finite, fmt_money
from .analysis.sentiment import sentiment
from .analysis.technicals import compute_technicals
from .analysis.valuation import compute_valuation
from .data.bundle import DataBundle, Sources, load_bundle
from .models import Report


def analyze_bundle(b: DataBundle, cfg: dict[str, Any], started: float | None = None) -> Report:
    started = started if started is not None else time.perf_counter()
    val = compute_valuation(b, cfg)
    graham = graham_scorecard(b, val, cfg)
    buffett = buffett_scorecard(b, val, cfg)
    factors = factor_scorecard(b, cfg)
    health = health_scores(b, cfg)
    tech = compute_technicals(b.history)
    decision = decide(graham, buffett, factors, val, health, tech, b.years, cfg)

    notes = list(b.notes)
    ni, core = b.ttm.get("net_income"), b.ttm.get("core_net_income")
    if finite(ni) and finite(core) and abs(ni - core) > 1e-6 * max(abs(ni), 1):
        notes.append(
            f"Core earnings used for P/E and Graham values: TTM net income {fmt_money(ni, b.currency)} "
            f"includes {fmt_money(ni - core, b.currency)} of non-operating items (e.g. investment "
            f"gains/losses); core net income is {fmt_money(core, b.currency)}."
        )
    if b.is_financial:
        notes.append("Financial company: industrial metrics (FCF, margins, debt ratios) skipped.")
    if b.is_reit:
        notes.append("REIT: GAAP earnings understate cash generation (depreciation); prefer FFO/AFFO.")
    notes.append(
        f"Graham Y = AAA yield {b.aaa_yield:.2f}% ({b.aaa_source}); "
        f"risk-free = 10y Treasury {b.treasury_10y:.2f}% ({b.treasury_source})."
    )
    return Report(
        ticker=b.ticker,
        name=b.name or b.ticker,
        sector=b.sector,
        industry=b.industry or b.sic_description,
        price=b.price,
        currency=b.currency,
        market_cap=b.market_cap,
        data_source=b.source,
        fiscal_years=[f"{ts:%Y-%m-%d}" for ts in b.annual.index],
        ttm_end=f"{b.ttm_end:%Y-%m-%d}",
        graham=graham,
        buffett=buffett,
        factors=factors,
        valuation=val,
        health=health,
        technicals=tech,
        sentiment=sentiment(b),
        decision=decision,
        notes=notes,
        elapsed_s=time.perf_counter() - started,
        as_of=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )


def analyze(
    ticker: str,
    cfg: dict[str, Any],
    progress: Callable[[str], None] | None = None,
    sources: Sources | None = None,
) -> Report:
    """Full analysis of one ticker. ``progress(step)`` receives "market", "filings", "rates"
    and "analysis" as each stage starts."""
    started = time.perf_counter()
    bundle = load_bundle(ticker, cfg, progress, sources)
    if progress:
        progress("analysis")
    return analyze_bundle(bundle, cfg, started)
