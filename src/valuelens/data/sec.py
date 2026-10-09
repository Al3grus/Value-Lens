"""SEC EDGAR client and XBRL "companyfacts" parser.

Endpoints (no API key; SEC asks for a descriptive User-Agent and <= 10 requests/second):
  https://www.sec.gov/files/company_tickers.json
  https://data.sec.gov/submissions/CIK##########.json
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .cache import FileCache
from .concepts import ANNUAL_FORMS, FIELDS, QUARTERLY_FORMS, Field
from .http import HttpxTransport, Transport, TransportError

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

ANNUAL_MIN_DAYS, ANNUAL_MAX_DAYS = 340, 390
MAX_YEARS = 12


class SecError(RuntimeError):
    pass


class SecIdentityError(SecError):
    """Missing/malformed identity or SEC refused it (403): the run cannot continue."""


# --------------------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------------------
IDENTITY_RE = re.compile(r"^\S.{0,120}\s[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def ascii_identity(user_agent: str) -> str:
    """HTTP headers are Latin-1 at best and browsers reject anything else, so accents are
    folded (Ćorić -> Coric) and other letters spelled out where Unicode does not decompose them."""
    import unicodedata

    special = {"Ł": "L", "ł": "l", "Ø": "O", "ø": "o", "Đ": "D", "đ": "d", "ß": "ss", "Æ": "AE",
               "æ": "ae", "Œ": "OE", "œ": "oe", "Þ": "Th", "þ": "th", "ı": "i"}  # fmt: skip
    text = "".join(special.get(ch, ch) for ch in " ".join((user_agent or "").split()))
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def identity_problem(user_agent: str) -> str | None:
    """Why ``user_agent`` is not a valid SEC identity ("Name e-mail"), or None."""
    text = (user_agent or "").strip()
    if not text:
        return 'Enter your name and e-mail, e.g. "Jane Doe jane@example.com".'
    if "@" not in text:
        return "Add your e-mail address after your name."
    if not IDENTITY_RE.match(text):
        return 'Use the form "First Last name@domain.com" (name first, then e-mail).'
    return None


class SecClient:
    def __init__(
        self,
        user_agent: str,
        cache: FileCache,
        cfg_cache: dict[str, Any],
        transport: Transport | None = None,
    ) -> None:
        problem = identity_problem(user_agent)
        if problem:
            raise SecIdentityError(
                "SEC requires a User-Agent with your name and e-mail. "
                f"{problem} Set VALUELENS_SEC_USER_AGENT or [sec] user_agent in valuelens.toml."
            )
        self._headers = {"User-Agent": user_agent.strip()}
        self._owns_transport = transport is None
        self._transport: Transport = transport or HttpxTransport()
        self._cache = cache
        self._ttl_facts = float(cfg_cache.get("fundamentals_ttl_hours", 24))
        self._ttl_map = float(cfg_cache.get("ticker_map_ttl_hours", 168))

    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()

    def _request(self, method: str, url: str):
        try:
            resp = self._transport.request(method, url, headers=self._headers)
        except TransportError as exc:
            raise SecError(f"SEC request failed: {url}: {exc}") from exc
        if resp.status == 403:
            raise SecIdentityError(
                "The SEC refused the request (403). This is usually temporary: wait a minute and try "
                "again. If it keeps happening, check that the name and e-mail are your own."
            )
        if resp.status == 429:
            raise SecError("SEC rate limit reached (more than 10 requests per second). Wait a minute.")
        return resp

    def verify(self) -> int:
        """One uncached HEAD request with this identity; returns the HTTP status (200 = accepted)."""
        return self._request("HEAD", TICKERS_URL).status

    def _get_json(self, url: str) -> Any:
        resp = self._request("GET", url)
        if resp.status == 404:
            return None
        if not resp.ok:
            raise SecError(f"SEC returned HTTP {resp.status} for {url}.")
        try:
            return resp.json()
        except ValueError as exc:
            raise SecError(f"SEC sent an unreadable response for {url}.") from exc

    def _cached(self, namespace: str, url: str, ttl: float, transform=None) -> Any:
        hit = self._cache.get(namespace, url, ttl)
        if hit is not None:
            return hit
        data = self._get_json(url)
        if data is not None and transform:
            data = transform(data)
        if data is not None:
            self._cache.set(namespace, url, data)
        return data

    def lookup_cik(self, ticker: str) -> tuple[int, str] | None:
        def to_map(raw: dict[str, Any]) -> dict[str, list[Any]]:
            return {v["ticker"].upper(): [int(v["cik_str"]), v["title"]] for v in raw.values()}

        mapping = self._cached("sec-map", TICKERS_URL, self._ttl_map, to_map) or {}
        key = ticker.upper().replace(".", "-")
        hit = mapping.get(key) or mapping.get(key.replace("-", "."))
        return (hit[0], hit[1]) if hit else None

    def submissions(self, cik: int) -> dict[str, Any]:
        def slim(raw: dict[str, Any]) -> dict[str, Any]:
            keep = ("name", "sic", "sicDescription", "fiscalYearEnd", "tickers", "exchanges")
            return {k: raw.get(k) for k in keep}

        return self._cached("sec-sub", SUBMISSIONS_URL.format(cik=cik), self._ttl_facts, slim) or {}

    def company_facts(self, cik: int) -> dict[str, Any] | None:
        return self._cached("sec-facts", FACTS_URL.format(cik=cik), self._ttl_facts)


# --------------------------------------------------------------------------------------
# companyfacts parsing
# --------------------------------------------------------------------------------------
@dataclass
class Value:
    val: float
    end: date
    filed: date
    # For TTM values built as FY + YTD - prior YTD: each signed component with its own filing
    # date, so a stock split between the 10-K and the 10-Q is adjusted part by part.
    parts: tuple[tuple[float, date], ...] = ()

    def split_adjusted(self, factor_for) -> float:
        """``factor_for(filed) -> float`` is the split ratio after a filing date; per-share
        values divide by it, share counts multiply (callers pass the right direction)."""
        if not self.parts:
            return self.val * factor_for(self.filed)
        return sum(v * factor_for(f) for v, f in self.parts)


@dataclass
class ConceptSeries:
    annual: dict[date, Value] = field(default_factory=dict)
    ttm: Value | None = None


@dataclass
class ParsedFacts:
    currency: str
    fiscal_year_ends: list[date]
    annual: dict[str, dict[date, Value]]  # field -> fy_end -> Value
    ttm: dict[str, Value]  # field -> Value (end = ttm_end, or latest FY if no newer quarter)
    ttm_end: date
    notes: list[str]


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _duration(f: dict[str, Any]) -> int | None:
    if not f.get("start"):
        return None
    return (_d(f["end"]) - _d(f["start"])).days


def detect_currency(facts: dict[str, Any]) -> str:
    gaap = facts.get("facts", {}).get("us-gaap", {})
    counts: Counter[str] = Counter()
    for concept in FIELDS["revenue"].concepts + FIELDS["net_income"].concepts + ("Assets",):
        for unit, rows in gaap.get(concept, {}).get("units", {}).items():
            if re.fullmatch(r"[A-Z]{3}", unit):
                counts[unit] += len(rows)
    if not counts:
        return "USD"
    top = counts.most_common()
    if "USD" in counts and counts["USD"] == top[0][1]:
        return "USD"
    return top[0][0]


def _unit_key(f: Field, currency: str) -> str:
    return {"money": currency, "per_share": f"{currency}/shares", "shares": "shares"}[f.unit]


def _rows(facts: dict[str, Any], f: Field, concept: str, currency: str) -> list[dict[str, Any]]:
    node = facts.get("facts", {}).get(f.taxonomy, {}).get(concept)
    if not node:
        return []
    return node.get("units", {}).get(_unit_key(f, currency), [])


def _latest_by_end(rows: list[dict[str, Any]]) -> dict[date, Value]:
    out: dict[date, Value] = {}
    for r in rows:
        end, filed = _d(r["end"]), _d(r["filed"])
        cur = out.get(end)
        if cur is None or filed > cur.filed:
            out[end] = Value(float(r["val"]), end, filed)
    return out


def parse_concept(rows: list[dict[str, Any]], kind: str, unit: str = "money") -> ConceptSeries:
    """Annual values keyed by period end (latest filing wins, so restatements and split
    adjustments made in later 10-Ks take precedence) plus a trailing-twelve-month value."""
    series = ConceptSeries()
    if kind == "flow":
        annual = [
            r for r in rows
            if r.get("form") in ANNUAL_FORMS
            and (d := _duration(r)) is not None
            and ANNUAL_MIN_DAYS <= d <= ANNUAL_MAX_DAYS
        ]  # fmt: skip
        series.annual = _latest_by_end(annual)
        if not series.annual:
            return series
        fy_end = max(series.annual)
        # Latest year-to-date figure from a 10-Q that starts right after the last fiscal year.
        ytd = [
            r for r in rows
            if r.get("form") in QUARTERLY_FORMS
            and r.get("start")
            and _d(r["end"]) > fy_end
            and abs((_d(r["start"]) - (fy_end + timedelta(days=1))).days) <= 10
        ]  # fmt: skip
        if ytd:
            cur = max(ytd, key=lambda r: (r["end"], r["filed"]))
            cur_dur = _duration(cur) or 0
            cur_end = _d(cur["end"])
            prior = [
                r for r in rows
                if r.get("start")
                and abs((_d(r["end"]) - (cur_end - timedelta(days=364))).days) <= 10
                and abs((_duration(r) or 0) - cur_dur) <= 10
            ]  # fmt: skip
            if unit == "shares":
                # Weighted-average share counts are not additive: use the latest YTD average.
                series.ttm = Value(float(cur["val"]), cur_end, _d(cur["filed"]))
            elif prior:
                p = max(prior, key=lambda r: r["filed"])
                fy = series.annual[fy_end]
                parts = (
                    (fy.val, fy.filed),
                    (float(cur["val"]), _d(cur["filed"])),
                    (-float(p["val"]), _d(p["filed"])),
                )
                series.ttm = Value(sum(v for v, _ in parts), cur_end, _d(cur["filed"]), parts)
    else:
        annual = [r for r in rows if r.get("form") in ANNUAL_FORMS and not r.get("start")]
        series.annual = _latest_by_end(annual)
        quarterly = [r for r in rows if r.get("form") in QUARTERLY_FORMS and not r.get("start")]
        if quarterly and series.annual:
            q = _latest_by_end(quarterly)
            last = max(q)
            if last > max(series.annual):
                series.ttm = q[last]
    return series


def _align(values: dict[date, Value], fy_ends: list[date], tol_days: int = 10) -> dict[date, Value]:
    out: dict[date, Value] = {}
    for fy in fy_ends:
        best = min(values, key=lambda e: abs((e - fy).days), default=None)
        if best is not None and abs((best - fy).days) <= tol_days:
            out[fy] = values[best]
    return out


def parse_company_facts(facts: dict[str, Any]) -> ParsedFacts:
    currency = detect_currency(facts)
    notes: list[str] = []
    per_concept: dict[str, list[ConceptSeries]] = {}
    for name, f in FIELDS.items():
        per_concept[name] = [parse_concept(_rows(facts, f, c, currency), f.kind, f.unit) for c in f.concepts]

    # Fiscal-year ends: union of annual period ends for net income and revenue.
    ends: set[date] = set()
    for name in ("net_income", "revenue"):
        for cs in per_concept[name]:
            ends.update(cs.annual)
    if not ends:
        raise SecError("No annual (10-K/20-F) US-GAAP data found for this company.")
    fy_ends = _dedupe_close_dates(sorted(ends))[-MAX_YEARS:]

    # TTM end: latest quarter end seen for net income / revenue.
    # TTM end: latest quarter after the latest fiscal year. A concept the company stopped
    # using (e.g. SalesRevenueNet before ASC 606) can carry a stale "TTM" from years ago.
    latest_fy = fy_ends[-1]
    ttm_candidates = [
        cs.ttm.end
        for n in ("net_income", "revenue")
        for cs in per_concept[n]
        if cs.ttm and cs.ttm.end > latest_fy
    ]
    ttm_end = max(ttm_candidates) if ttm_candidates else latest_fy

    annual: dict[str, dict[date, Value]] = {}
    ttm: dict[str, Value] = {}
    for name, f in FIELDS.items():
        if f.taxonomy == "dei":
            # Cover-page share count: take the most recently filed value from any form.
            rows = [r for c in f.concepts for r in _rows(facts, f, c, currency)]
            if rows:
                latest = max(rows, key=lambda r: (r["filed"], r["end"]))
                # Multi-class issuers report one row per class on the same filing.
                same = [r for r in rows if r["accn"] == latest["accn"]]
                total = sum(float(r["val"]) for r in same)
                ttm[name] = Value(total, _d(latest["end"]), _d(latest["filed"]))
            continue
        series_list = per_concept[name]
        merged: dict[date, Value] = {}
        for fy in fy_ends:
            candidates = [v for cs in series_list if (v := _align(cs.annual, [fy]).get(fy))]
            if not candidates:
                continue
            merged[fy] = max(candidates, key=lambda v: v.val) if f.merge == "max" else candidates[0]
        if merged:
            annual[name] = merged
        ttm_vals = [
            cs.ttm
            for cs in series_list
            if cs.ttm and cs.ttm.end > latest_fy and abs((cs.ttm.end - ttm_end).days) <= 7
        ]
        if ttm_vals:
            ttm[name] = max(ttm_vals, key=lambda v: v.val) if f.merge == "max" else ttm_vals[0]
        elif merged and fy_ends[-1] in merged:
            ttm[name] = merged[fy_ends[-1]]
            if ttm_end != fy_ends[-1] and f.kind == "flow" and name in ("revenue", "net_income"):
                notes.append(f"TTM {name} unavailable; using last fiscal year.")
    return ParsedFacts(currency, fy_ends, annual, ttm, ttm_end, notes)


def _dedupe_close_dates(ends: list[date], min_gap_days: int = 300) -> list[date]:
    """Keep one fiscal-year end per ~year. Collapses 52/53-week dates a few days apart and,
    after a fiscal-year change (10-KT + recast 12-month comparative), drops the earlier of two
    overlapping year-ends so every row is a distinct, non-overlapping year."""
    out: list[date] = []
    for e in ends:
        if out and (e - out[-1]).days < min_gap_days:
            out[-1] = e
        else:
            out.append(e)
    return out
