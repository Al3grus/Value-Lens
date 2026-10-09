"""What the reports say, as plain data. Shared by the terminal views (simple.py, detailed.py) and
the website (web/render.py), so each renderer only decides how things look.

``tone`` is one of "good", "bad", "warn", "muted" or "" (normal text).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..analysis.metrics import finite, fmt_money, pct
from ..models import Decision, HealthScore, Report, Scorecard, Status, Valuation
from .style import money

DISCLAIMER = "Educational only. Not investment advice. Do your own research and consult a professional."
SIMPLE_FOOTER = "detailed view has every number {dot} not investment advice"


@dataclass(frozen=True)
class Row:
    """One labelled figure: label, value, explanation."""

    label: str
    value: str
    note: str = ""
    tone: str = ""
    strong: bool = False


@dataclass(frozen=True)
class Scale:
    """Value gauge: the visible span, the fair-value range, the fair value and the price."""

    left: float
    right: float
    low: float
    high: float
    fair: float
    price: float

    def at(self, v: float) -> float:
        """Position of ``v`` on the scale, 0..1."""
        span = self.right - self.left or 1.0
        return max(0.0, min(1.0, (v - self.left) / span))


def meta(r: Report) -> list[str]:
    return [m for m in (r.sector, r.industry) if m]


def value_range(v: Valuation) -> tuple[float, float] | None:
    """Fair-value range: DCF bear to bull case, always including the fair value itself."""
    if not v.fair_value:
        return None
    lo = min(x for x in (v.bear, v.fair_value) if finite(x))
    hi = max(x for x in (v.bull, v.fair_value) if finite(x))
    return lo, hi


def scale(v: Valuation, price: float) -> Scale | None:
    rng = value_range(v)
    if rng is None:
        return None
    lo, hi = rng
    return Scale(min(lo, price) * 0.92, max(hi, price) * 1.05, lo, hi, v.fair_value, price)


def price_tone(price: float, fair: float) -> str:
    return "good" if price <= fair else "bad"


def data_line(r: Report, dot: str) -> str:
    """Where the numbers come from: price, size, source and the years covered."""
    return (
        f"Price {money(r.price, r.currency)}  {dot}  Market value {fmt_money(r.market_cap, r.currency)}  {dot}  "
        f"{r.data_source}, FY{r.fiscal_years[0][:4]}-FY{r.fiscal_years[-1][:4]} ({len(r.fiscal_years)}y), "
        + (f"TTM to {r.ttm_end}" if r.ttm_end > r.fiscal_years[-1] else "latest = annual report")
    )


def score_summary(card: Scorecard, kinds: tuple[str, ...] | None = None) -> tuple[str, str, str] | None:
    """("7/9", " passed (78%)", tone), or None when nothing could be evaluated."""
    n, p = card.evaluated(kinds), card.passed(kinds)
    if not n:
        return None
    ratio = p / n
    tone = "good" if ratio >= 0.7 else "warn" if ratio >= 0.45 else "bad"
    return f"{p}/{n}", f" passed ({ratio:.0%})", tone


def margin_of_safety(mos: float | None) -> tuple[str, str]:
    if mos is None:
        return "n/a", "muted"
    if mos >= 0:
        return f"{mos:.0%} below value (margin of safety)", "good"
    return f"{-mos:.0%} above value (no margin of safety)", "bad"


def verdict_reasons(d: Decision) -> list[str]:
    """Reasons the score bars and tables do not already show (guardrail explanations stay)."""
    return [r for r in d.reasons if not r.startswith(("Business quality", "Blended fair value"))]


def health_status(h: HealthScore) -> tuple[Status, str]:
    """(status, zone tone) for one health score."""
    status = Status.NA if h.value is None else Status.FAIL if h.red_flag else Status.PASS
    return status, "bad" if h.red_flag else ("muted" if h.value is None else "good")


def valuation_rows(r: Report, dot: str) -> list[Row]:
    v = r.valuation
    rows: list[Row] = []
    if finite(v.implied_growth):
        hist = f"vs {pct(v.historical_growth)}/yr delivered (5y)" if finite(v.historical_growth) else ""
        note = f" [{v.implied_growth_note}]" if v.implied_growth_note else ""
        rows.append(Row("Market-implied growth", f"{pct(v.implied_growth)}/yr for 5y{note}",
                        f"{hist} {dot} growth the price already assumes", strong=True))  # fmt: skip
    if finite(v.discount_rate):
        ke = f"cost of equity {pct(v.cost_of_equity)}" if finite(v.cost_of_equity) else ""
        rows.append(Row("Discount rate (WACC)", pct(v.discount_rate), f"{ke} {dot} return investors require"))
    if finite(v.fcf_yield):
        rows.append(Row("Owner FCF yield", pct(v.fcf_yield), "cash for owners ÷ market value"))
    if finite(v.maintenance_capex_share) and v.maintenance_capex_share < 0.999:
        rows.append(Row("Maintenance capex", f"{v.maintenance_capex_share:.0%} of capex",
                        "rest treated as growth investment (Greenwald)"))  # fmt: skip
    if finite(v.earnings_yield):
        rows.append(
            Row("Earnings yield (EBIT/EV)", pct(v.earnings_yield), "operating profit ÷ company value")
        )
    if finite(v.pe_now):
        med = f"10y median {v.pe_median_10y:.1f}" if finite(v.pe_median_10y) else ""
        rows.append(Row("P/E (core EPS)", f"{v.pe_now:.1f}", med))
    return rows


def trend_rows(r: Report) -> list[Row]:
    t = r.technicals
    tone = {"UPTREND": "good", "DOWNTREND": "bad"}.get(t.trend, "warn")
    rows = [Row("Trend", t.trend, "price vs averages and 12-month momentum", tone, strong=True)]
    if t.trend != "N/A":
        averages = f"{money(t.sma50, r.currency)} / {money(t.sma200, r.currency)}"
        rows += [
            Row("50 / 200-day average", averages, "short vs long-term average price"),
            Row(
                "12-1 month momentum", pct(t.momentum_12_1), "return over the past year, excluding last month"
            ),
            Row(
                "RSI (14 days)",
                f"{t.rsi14:.0f}" if finite(t.rsi14) else "n/a",
                "70+ overbought, 30- oversold",
            ),
            Row("From 52-week high", pct(t.from_52w_high), "how far below the year's peak"),
            Row("Volatility (1y)", pct(t.volatility_1y), "typical yearly price swing"),
        ]
    return rows


def sentiment_rows(r: Report, dot: str) -> list[Row]:
    s = r.sentiment or {}
    rows: list[Row] = []
    if "analyst_target" in s:
        rows.append(Row(
            "Analyst mean target",
            f"{money(s['analyst_target'], r.currency)} ({pct(s['analyst_upside'])})",
            f"{s.get('analyst_count') or '?'} analysts {dot} rating: {s.get('analyst_rating') or 'n/a'}",
        ))  # fmt: skip
    if "short_pct_float" in s:
        rows.append(Row("Short interest", pct(s["short_pct_float"]), "share of tradable stock bet against"))
    if s.get("insider_buy_trans") is not None or s.get("insider_sell_trans") is not None:
        rows.append(Row(
            "Insider trades (6m)",
            f"{int(s.get('insider_buy_trans') or 0)} buys / {int(s.get('insider_sell_trans') or 0)} sells",
            "includes stock awards; context only",
        ))  # fmt: skip
    if "beta" in s:
        rows.append(Row("Beta", f"{s['beta']:.2f}", "1.0 = moves with the market"))
    return rows
