"""Plain-English wording shared by the simple view, the detailed view and the JSON output.

* ``MEANING``      - one-line "what this number means" for every check (detailed view).
* ``plain_point``  - turns a PASS/FAIL check into a sentence a non-specialist understands.
* ``summarize``    - headline, strengths, concerns and valuation sentences (simple view).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..analysis.metrics import finite, fmt_money
from ..models import Criterion, Report, Status

# --------------------------------------------------------------------------------------
# What each number means (short enough for a table column)
# --------------------------------------------------------------------------------------
MEANING: dict[str, str] = {
    # Graham
    "size": "Larger, established firms are more resilient",
    "current_ratio": "Short-term assets ÷ short-term bills; 2+ is a solid cushion",
    "ltd_nca": "Long-term debt should be covered by working capital",
    "stability": "Profitable every year = a durable business",
    "dividends": "A long, unbroken dividend record signals reliability",
    "eps_growth": "Earnings per share should rise meaningfully over a decade",
    "pe3": "Price ÷ 3-year average earnings; 15 or less is Graham-cheap",
    "pb": "Price vs net assets (book value); caps what you pay for assets",
    # Buffett
    "fcf_positive": "Cash left over after investing, year after year",
    "roe": "Profit per $1 of shareholders' money; 15%+ is excellent",
    "roic": "Profit per $1 of all capital (debt + equity) in the business",
    "interest_cov": "How many times profit covers interest; higher = safer",
    "nd_ebitda": "Years of cash profit needed to repay all net debt",
    "gross_margin": "Share of sales kept after production costs: pricing power",
    "op_margin": "Share of sales kept after all operating costs",
    "rev_cagr": "Average yearly sales growth",
    "fcf_cagr": "Average yearly growth of owner earnings (cash for owners)",
    "dilution": "A rising share count shrinks your slice of the company",
    "one_dollar": "Does each $1 of kept profit create $1+ of market value?",
    "capex_ni": "Share of profit that must be reinvested in equipment",
    "ev_fcf": "Whole-company price ÷ free cash flow; lower = cheaper",
    "pe": "Price ÷ core earnings per share; lower = cheaper",
    # Factors
    "gp_assets": "Gross profit per $1 of assets; predicts future returns",
    "roc": "Operating profit per $1 of tangible capital used",
    "earnings_yield": "Operating profit ÷ company value, like a bond's yield",
    "sh_yield": "Dividends + net buybacks as % of market value",
    "asset_growth": "Very fast balance-sheet growth tends to precede weak returns",
}

HEALTH_MEANING: dict[str, str] = {
    "Piotroski F-score": "9 yes/no checks of improving finances; 7+ is strong, 2- is weak",
    "Altman Z-score": "Bankruptcy-risk score; 'safe' zone is what you want",
    "Beneish M-score": "Flags possible earnings manipulation; below -1.78 is clean",
}

METHOD_MEANING: dict[str, str] = {
    "DCF (owner earnings)": "Future cash for owners, discounted to today",
    "Graham formula": "Graham's growth formula, adjusted for today's bond yields",
    "Earnings power (no growth)": "Value if the company never grew again",
    "Graham number": "Most a defensive investor should pay (earnings + assets)",
}

PILLARS: dict[str, tuple[str, str]] = {
    "quality": ("Business quality", "profits, margins, growth"),
    "safety": ("Financial strength", "debt, stability, distress risk"),
    "factors": ("Research signals", "research-backed return predictors"),
    "valuation": ("Price vs value", "price against fair value"),
}


def pillar_word(key: str, score: float | None) -> str:
    if score is None:
        return "n/a"
    if key == "valuation":
        scale = ((83, "Deep discount"), (65, "Undervalued"), (50, "Fairly priced"),
                 (38, "Somewhat expensive"), (15, "Expensive"))  # fmt: skip
        return next((w for t, w in scale if score >= t), "Very expensive")
    scale = ((80, "Excellent"), (65, "Good"), (45, "Average"), (30, "Weak"))
    return next((w for t, w in scale if score >= t), "Poor")


# --------------------------------------------------------------------------------------
# Plain-English sentences per check
# --------------------------------------------------------------------------------------
Fn = Callable[[Criterion, str], str | None]


def _pct(v: float | None) -> str:
    return f"{v:.0%}" if finite(v) else "?"


def _x(v: float | None, digits: int = 0) -> str:
    return f"{v:.{digits}f}x" if finite(v) else "?"


PLAIN: dict[str, tuple[Fn, Fn]] = {
    "size": (
        lambda c, cur: f"Large, established business ({fmt_money(c.value, cur)} of yearly sales)",
        lambda c, cur: f"Small company ({fmt_money(c.value, cur)} of sales); Graham preferred large ones",
    ),
    "current_ratio": (
        lambda c, cur: f"Comfortable short-term cushion (assets cover bills {_x(c.value, 1)})",
        lambda c, cur: f"Thin short-term cushion: assets cover short-term bills only {_x(c.value, 1)}",
    ),
    "ltd_nca": (
        lambda c, cur: "Long-term debt is covered by working capital",
        lambda c, cur: "Long-term debt is larger than working capital",
    ),
    "stability": (
        lambda c, cur: "Profitable every single year of the last decade",
        lambda c, cur: f"Lost money in {int(c.value or 0)} of the last 10 years",
    ),
    "dividends": (
        lambda c, cur: f"Has paid a dividend {int(c.value or 0)} years in a row",
        lambda c, cur: "Pays no dividend" if not c.value
        else f"Dividend record is only {int(c.value)} years (Graham wanted 20)",
    ),
    "eps_growth": (
        lambda c, cur: f"Earnings per share up {_pct(c.value)} over about 10 years",
        lambda c, cur: f"Earnings per share up only {_pct(c.value)} over about 10 years",
    ),
    "pe3": (
        lambda c, cur: f"Cheap by Graham's standard: {_x(c.value)} average earnings",
        lambda c, cur: f"Pricey by Graham's standard: {_x(c.value)} average earnings (his limit: 15x)"
        if finite(c.value) else "Has not earned a profit on average recently",
    ),
    "pb": (
        lambda c, cur: f"Priced close to its net assets ({_x(c.value, 1)} book value)",
        lambda c, cur: f"Priced at {_x(c.value, 1)} its net assets (book value)"
        if finite(c.value) else "Has negative net assets (book value)",
    ),
    "fcf_positive": (
        lambda c, cur: f"Generated spare cash in {int(c.value or 0)} of the last 10 years",
        lambda c, cur: f"Generated spare cash in only {int(c.value or 0)} of the last 10 years",
    ),
    "roe": (
        lambda c, cur: f"Earns {_pct(c.value)} a year on shareholders' money (15%+ is excellent)",
        lambda c, cur: f"Earns only {_pct(c.value)} a year on shareholders' money (15%+ is excellent)",
    ),
    "roic": (
        lambda c, cur: f"Earns {_pct(c.value)} on all the capital invested in the business",
        lambda c, cur: f"Earns only {_pct(c.value)} on the capital invested in the business",
    ),
    "interest_cov": (
        lambda c, cur: f"Profits cover interest payments {_x(c.value)} over"
        if finite(c.value) else "Carries no meaningful debt",
        lambda c, cur: f"Profits cover interest only {_x(c.value, 1)}; little room for a bad year",
    ),
    "nd_ebitda": (
        lambda c, cur: f"Has more cash than debt ({c.detail.removeprefix('Net cash ')} net cash)"
        if c.detail.startswith("Net cash") else f"Low debt: about {_x(c.value, 1)} yearly cash profit",
        lambda c, cur: f"Heavy debt: about {_x(c.value, 1)} yearly cash profit to repay"
        if finite(c.value) else "Has debt but no cash profit to service it",
    ),
    "gross_margin": (
        lambda c, cur: f"Keeps {_pct(c.value)} of each sale after production costs: pricing power",
        lambda c, cur: f"Keeps only {_pct(c.value)} of each sale after production costs",
    ),
    "op_margin": (
        lambda c, cur: f"Turns {_pct(c.value)} of sales into operating profit",
        lambda c, cur: f"Turns only {_pct(c.value)} of sales into operating profit",
    ),
    "rev_cagr": (
        lambda c, cur: f"Sales growing {_pct(c.value)} a year",
        lambda c, cur: f"Sales growing only {_pct(c.value)} a year",
    ),
    "fcf_cagr": (
        lambda c, cur: f"Cash for owners growing {_pct(c.value)} a year",
        lambda c, cur: f"Cash for owners growing only {_pct(c.value)} a year"
        if finite(c.value) else "Cash for owners is negative",
    ),
    "dilution": (
        lambda c, cur: f"Buying back stock: share count down {_pct(-(c.value or 0))} in 5 years"
        if (c.value or 0) < -0.005 else "Share count is stable: owners are not diluted",
        lambda c, cur: f"Issuing shares: share count up {_pct(c.value)} in 5 years (dilutes owners)",
    ),
    "one_dollar": (
        lambda c, cur: f"Each $1 kept in the business turned into ${c.value:.2f} of market value",
        lambda c, cur: f"Each $1 kept in the business turned into only ${c.value:.2f} of market value",
    ),
    "capex_ni": (
        lambda c, cur: f"Needs little reinvestment: equipment spending is {_pct(c.value)} of profit",
        lambda c, cur: f"Capital-hungry: equipment spending is {_pct(c.value)} of profit"
        if finite(c.value) else "Has lost money overall in the last decade",
    ),
    "ev_fcf": (
        lambda c, cur: f"Reasonably priced against its cash flow ({_x(c.value)} free cash flow)",
        lambda c, cur: f"Expensive against its cash flow: {_x(c.value)} free cash flow (limit: 25x)"
        if finite(c.value) else "Burns cash, so cash-flow valuation is not possible",
    ),
    "pe": (
        lambda c, cur: f"Reasonably priced against earnings ({_x(c.value)} core profit)",
        lambda c, cur: f"Expensive: {_x(c.value)} core earnings (Buffett-style limit: 25x)"
        if finite(c.value) else "Not profitable, so no earnings multiple",
    ),
    "gp_assets": (
        lambda c, cur: f"Very productive assets: gross profit is {_pct(c.value)} of assets each year",
        lambda c, cur: f"Gross profit is only {_pct(c.value)} of assets each year",
    ),
    "roc": (
        lambda c, cur: f"Earns {_pct(c.value)} on the tangible capital it actually uses",
        lambda c, cur: f"Earns only {_pct(c.value)} on the tangible capital it uses",
    ),
    "earnings_yield": (
        lambda c, cur: f"Operating profit is {c.value:.1%} of the whole company's price"
        if finite(c.value) else "Holds more cash than its market value",
        lambda c, cur: f"Operating profit is only {c.value:.1%} of the company's price (want 7%+)",
    ),
    "sh_yield": (
        lambda c, cur: f"Returns {c.value:.1%} of its value to shareholders yearly",
        lambda c, cur: f"Returns only {c.value:.1%} of its value to shareholders yearly",
    ),
    "asset_growth": (
        lambda c, cur: f"Growing its balance sheet at a measured pace ({_pct(c.value)} last year)",
        lambda c, cur: f"Balance sheet grew {_pct(c.value)} last year; fast expansion often precedes weaker returns",
    ),
}  # fmt: skip


def plain_point(c: Criterion, currency: str) -> str | None:
    fns = PLAIN.get(c.key)
    if not fns or c.status is Status.NA:
        return None
    try:
        return fns[0 if c.status is Status.PASS else 1](c, currency)
    except (TypeError, ValueError):
        return None


# Related checks share a theme; the simple view shows at most one sentence per theme.
THEME: dict[str, str] = {
    "pe": "earn_multiple", "pe3": "earn_multiple", "ev_fcf": "cash_multiple",
    "earnings_yield": "cash_multiple", "pb": "book", "roe": "returns", "roic": "returns",
    "roc": "returns", "gp_assets": "returns", "gross_margin": "margins", "op_margin": "margins",
    "rev_cagr": "growth", "eps_growth": "growth", "fcf_cagr": "cash_growth",
    "nd_ebitda": "debt", "interest_cov": "debt", "ltd_nca": "debt", "current_ratio": "liquidity",
    "dilution": "shares", "sh_yield": "payout", "dividends": "payout", "one_dollar": "allocation",
    "capex_ni": "capital", "asset_growth": "capital", "stability": "stability",
    "fcf_positive": "stability", "size": "size",
}  # fmt: skip
KEY_PRIORITY = [
    "roe", "roic", "gp_assets", "roc", "gross_margin", "op_margin", "rev_cagr", "eps_growth",
    "fcf_cagr", "nd_ebitda", "interest_cov", "ltd_nca", "current_ratio", "pe", "pe3", "ev_fcf",
    "earnings_yield", "stability", "fcf_positive", "one_dollar", "dilution", "sh_yield",
    "dividends", "capex_ni", "asset_growth", "pb", "size",
]  # fmt: skip
STRENGTH_THEMES = ["returns", "margins", "growth", "cash_growth", "debt", "stability",
                   "allocation", "shares", "cash_multiple", "earn_multiple", "payout",
                   "capital", "liquidity", "book", "size"]  # fmt: skip
CONCERN_THEMES = ["earn_multiple", "cash_multiple", "debt", "stability", "returns", "margins",
                  "growth", "cash_growth", "shares", "capital", "allocation", "liquidity",
                  "payout", "book", "size"]  # fmt: skip


@dataclass
class Summary:
    headline: str
    strengths: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    value: str = ""
    expectation: str = ""
    confidence: str = ""


def _pick(criteria: list[Criterion], status: Status, themes: list[str], cur: str, n: int) -> list[str]:
    by_theme: dict[str, Criterion] = {}
    for c in sorted(criteria, key=lambda c: KEY_PRIORITY.index(c.key) if c.key in KEY_PRIORITY else 99):
        if c.status is status and c.key in THEME:
            by_theme.setdefault(THEME[c.key], c)
    out = []
    for theme in themes:
        c = by_theme.get(theme)
        text = plain_point(c, cur) if c else None
        if text:
            out.append(text)
        if len(out) == n:
            break
    return out


def headline(r: Report) -> str:
    q = r.decision.components.get("quality")
    mos = r.valuation.margin_of_safety
    if q is None:
        kind = "A business we could only partly assess"
    elif q >= 75:
        kind = "A high-quality business"
    elif q >= 55:
        kind = "A solid business"
    else:
        kind = "A business with weak fundamentals"
    good_business = q is not None and q >= 55
    if mos is None:
        value, cheap = "but we could not estimate its value reliably", None
    elif mos >= 0.2:
        value, cheap = "trading well below our value estimate", True
    elif mos >= 0:
        value, cheap = "trading close to our value estimate", True
    elif mos >= -0.25:
        value, cheap = "priced somewhat above our value estimate", False
    else:
        value, cheap = "priced far above our value estimate", False
    if cheap is None:
        joiner = ","
    elif cheap == good_business:
        joiner = "," if cheap else " and"
    else:
        joiner = ", but" if good_business else ", though"
    text = f"{kind}{joiner} {value}."
    if any(h.red_flag for h in r.health):
        text += " Accounting or distress red flags were found."
    return text


def value_sentence(r: Report) -> str:
    v = r.valuation
    if not v.fair_value:
        return "Not enough reliable data to estimate a fair value."
    ratio = r.price / v.fair_value
    if ratio >= 1.05:
        return f"{ratio:.1f}x our estimate" if ratio >= 1.95 else f"{ratio - 1:.0%} above our estimate"
    if ratio <= 0.95:
        return f"{1 - ratio:.0%} below our estimate"
    return "about equal to our estimate"


def expectation_sentence(r: Report) -> str:
    v = r.valuation
    if not finite(v.implied_growth):
        return ""
    implied = (
        "more than 100%"
        if v.implied_growth_note.startswith("above")
        else (
            "a decline of over 50%"
            if v.implied_growth_note.startswith("below")
            else f"about {v.implied_growth:.0%}"
        )
    )
    text = f"To justify today's price, cash for owners would need to grow {implied} a year for 5 years"
    if finite(v.historical_growth):
        text += f"; over the past 5 years it grew {v.historical_growth:.0%} a year"
    return text + "."


def summarize(r: Report, n: int = 4) -> Summary:
    criteria = [*r.graham.criteria, *r.buffett.criteria, *r.factors.criteria]
    strengths = _pick(criteria, Status.PASS, STRENGTH_THEMES, r.currency, n)
    concerns = _pick(criteria, Status.FAIL, CONCERN_THEMES, r.currency, n)

    flags = []
    for h in r.health:
        if not h.red_flag:
            continue
        if h.name.startswith("Beneish"):
            flags.append("Accounting red flag: numbers show patterns linked to earnings manipulation")
        elif h.name.startswith("Altman"):
            flags.append("Financial distress risk: bankruptcy-risk score is in the danger zone")
        elif h.name.startswith("Piotroski"):
            flags.append("Finances are deteriorating on most yearly health checks")
    concerns = (flags + concerns)[:n]
    healthy = [h for h in r.health if h.value is not None]
    if healthy and not any(h.red_flag for h in healthy) and len(strengths) < n + 1:
        strengths.append("No signs of financial distress or earnings manipulation")

    conf = {
        "HIGH": "High: complete data and valuation methods broadly agree",
        "MEDIUM": "Medium: valuation methods disagree or some data is missing",
        "LOW": "Low: limited history or missing data; treat the verdict as a rough guide",
    }.get(r.decision.confidence, r.decision.confidence)
    return Summary(headline(r), strengths, concerns, value_sentence(r), expectation_sentence(r), conf)
