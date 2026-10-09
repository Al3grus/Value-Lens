"""Result data structures shared by analysis, reporting and the JSON API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NA = "N/A"


class Signal(StrEnum):
    STRONG_BUY = "STRONG BUY"
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    STRONG_SELL = "STRONG SELL"

    @property
    def rank(self) -> int:
        return SIGNAL_ORDER.index(self)

    def capped_at(self, ceiling: Signal) -> Signal:
        """Return the weaker (more bearish) of self and ``ceiling``."""
        return self if self.rank >= ceiling.rank else ceiling


SIGNAL_ORDER = [Signal.STRONG_BUY, Signal.BUY, Signal.HOLD, Signal.SELL, Signal.STRONG_SELL]


@dataclass
class Criterion:
    key: str
    label: str
    status: Status
    detail: str
    value: float | None = None
    kind: str = "quality"  # quality | safety | valuation | factor

    @classmethod
    def na(cls, key: str, label: str, detail: str, kind: str = "quality") -> Criterion:
        return cls(key, label, Status.NA, detail, None, kind)

    @classmethod
    def check(
        cls, key: str, label: str, ok: bool, detail: str, value: float | None, kind: str = "quality"
    ) -> Criterion:
        return cls(key, label, Status.PASS if ok else Status.FAIL, detail, value, kind)


@dataclass
class Scorecard:
    title: str
    criteria: list[Criterion]
    signal: Signal | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def _subset(self, kinds: tuple[str, ...] | None) -> list[Criterion]:
        return [c for c in self.criteria if kinds is None or c.kind in kinds]

    def passed(self, kinds: tuple[str, ...] | None = None) -> int:
        return sum(c.status is Status.PASS for c in self._subset(kinds))

    def evaluated(self, kinds: tuple[str, ...] | None = None) -> int:
        return sum(c.status is not Status.NA for c in self._subset(kinds))

    def ratio(self, kinds: tuple[str, ...] | None = None) -> float | None:
        n = self.evaluated(kinds)
        return self.passed(kinds) / n if n else None


@dataclass
class ValuationMethod:
    name: str
    value: float | None  # per share, in price currency
    weight: float
    note: str = ""


@dataclass
class Valuation:
    methods: list[ValuationMethod]
    fair_value: float | None
    margin_of_safety: float | None
    bear: float | None = None
    bull: float | None = None
    discount_rate: float | None = None  # WACC used for DCF and EPV
    cost_of_equity: float | None = None
    dcf_growth: float | None = None
    implied_growth: float | None = None
    implied_growth_note: str = ""
    historical_growth: float | None = None
    fcf_yield: float | None = None
    earnings_yield: float | None = None
    pe_now: float | None = None
    pe_median_10y: float | None = None
    maintenance_capex_share: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class HealthScore:
    name: str
    value: float | None
    zone: str  # e.g. "strong", "safe", "grey", "distress", "likely manipulator", "n/a"
    detail: str
    red_flag: bool = False


@dataclass
class Technicals:
    trend: str  # UPTREND / DOWNTREND / SIDEWAYS / N/A
    price: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    momentum_12_1: float | None = None
    rsi14: float | None = None
    from_52w_high: float | None = None
    volatility_1y: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Decision:
    signal: Signal
    composite: float | None
    components: dict[str, float | None]
    reasons: list[str]
    warnings: list[str]
    timing: str
    confidence: str
    coverage: float


@dataclass
class Report:
    ticker: str
    name: str
    sector: str | None
    industry: str | None
    price: float
    currency: str
    market_cap: float | None
    data_source: str
    fiscal_years: list[str]
    ttm_end: str
    graham: Scorecard
    buffett: Scorecard
    factors: Scorecard
    valuation: Valuation
    health: list[HealthScore]
    technicals: Technicals
    sentiment: dict[str, Any]
    decision: Decision
    notes: list[str]
    elapsed_s: float = 0.0
    as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
