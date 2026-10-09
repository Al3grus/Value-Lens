"""Assemble everything the analysis needs for one ticker into a ``DataBundle``."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..config import cache_dir
from . import fred
from .cache import FileCache
from .http import Transport
from .normalize import MONEY_COLUMNS, apply_owner_earnings, has_debt_data, normalize, split_factor
from .sec import ParsedFacts, SecClient, SecError, SecIdentityError, parse_company_facts
from .yahoo import MarketData, MarketSource, YFinanceSource

log = logging.getLogger(__name__)

FINANCIAL_INDUSTRY_PREFIXES = (
    "Banks", "Insurance -", "Insurance—", "Capital Markets", "Asset Management",
    "Mortgage Finance", "Financial Conglomerates",
)  # fmt: skip


class DataError(RuntimeError):
    pass


@dataclass
class Sources:
    """Where data comes from. Defaults: yfinance for market data, httpx2 for SEC and FRED.
    The browser build passes a Yahoo HTTP source and a relay transport instead."""

    market: MarketSource = field(default_factory=YFinanceSource)
    transport: Transport | None = None


@dataclass
class DataBundle:
    ticker: str
    price: float
    currency: str
    annual: pd.DataFrame  # canonical columns, index = fiscal year end (ascending)
    ttm: pd.Series  # canonical columns, trailing twelve months / latest balance sheet
    ttm_end: pd.Timestamp
    history: pd.DataFrame = field(default_factory=pd.DataFrame)
    dividends: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    splits: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    sic: int | None = None
    sic_description: str | None = None
    financial_currency: str | None = None
    fx_rate: float = 1.0
    usd_fx: float | None = 1.0  # 1 USD expressed in the price currency (for USD thresholds)
    info: dict[str, Any] = field(default_factory=dict)
    insider: dict[str, float] = field(default_factory=dict)
    aaa_yield: float = 6.0  # percent
    aaa_source: str = "fallback"
    treasury_10y: float = 5.3  # percent
    treasury_source: str = "fallback"
    source: str = "SEC EDGAR"
    debt_data_found: bool = True
    maint_capex_share: float | None = None  # maintenance share of total capex (latest year)
    notes: list[str] = field(default_factory=list)

    # ---- derived helpers -------------------------------------------------------------
    @property
    def shares(self) -> float | None:
        """Shares to value the company on, in units of the traded security."""
        return share_basis(self.ttm, self.info)

    @property
    def debt_unknown(self) -> bool:
        """Interest is being paid but no debt concept was found: debt figures are unreliable."""
        interest = self.ttm.get("interest_expense")
        ebit = self.ttm.get("operating_income")
        material = (
            interest is not None
            and np.isfinite(interest)
            and interest > 0.01 * max(abs(ebit) if ebit is not None and np.isfinite(ebit) else 0, 1)
        )
        return not self.debt_data_found and bool(material)

    @property
    def market_cap(self) -> float | None:
        return self.price * self.shares if self.shares else None

    @property
    def years(self) -> int:
        return len(self.annual.index)

    @property
    def is_financial(self) -> bool:
        if self.sic is not None:
            return 6000 <= self.sic <= 6411
        ind = self.industry or ""
        return (self.sector or "") == "Financial Services" and ind.startswith(FINANCIAL_INDUSTRY_PREFIXES)

    @property
    def is_utility(self) -> bool:
        if self.sic is not None:
            return 4900 <= self.sic <= 4999
        return (self.sector or "") == "Utilities"

    @property
    def is_reit(self) -> bool:
        return self.sic == 6798 or (self.industry or "").startswith("REIT")

    @property
    def is_manufacturer(self) -> bool:
        return self.sic is not None and 2000 <= self.sic <= 3999

    def price_on(self, when: pd.Timestamp | date) -> float | None:
        """Split-adjusted (not dividend-adjusted) close on or before ``when``."""
        if self.history.empty or "Close" not in self.history:
            return None
        closes = self.history["Close"].dropna()
        closes = closes[closes.index <= pd.Timestamp(when)]
        return float(closes.iloc[-1]) if not closes.empty else None


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------
def _finite_pos(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) and x > 0 else None


def share_basis(ttm: pd.Series, info: dict[str, Any] | None = None) -> float | None:
    """Point-in-time share count. Prefers the cover-page count from the latest filing when it
    is consistent with the diluted weighted average (a multi-class sum like Berkshire's A+B
    is not), then the diluted average, then Yahoo."""
    cover = _finite_pos(ttm.get("shares_outstanding"))
    diluted = _finite_pos(ttm.get("shares"))
    if cover and diluted and 0.8 <= cover / diluted <= 1.25:
        return cover
    if diluted:
        return diluted
    if cover:
        return cover
    info = info or {}
    return _finite_pos(info.get("impliedSharesOutstanding")) or _finite_pos(info.get("sharesOutstanding"))


def yahoo_share_basis(info: dict[str, Any], price: float | None) -> float | None:
    """Shares in units of the traded security (ADS for ADRs, class-equivalents for multi-class)."""
    v = _finite_pos(info.get("impliedSharesOutstanding")) or _finite_pos(info.get("sharesOutstanding"))
    if v is None and price and _finite_pos(info.get("marketCap")):
        v = float(info["marketCap"]) / price
    return v


def reconcile_share_basis(
    annual: pd.DataFrame, ttm: pd.Series, traded_shares: float | None, tolerance: float = 0.15
) -> float | None:
    """If filings count a different security than the one that trades (ADR ratio, multi-class
    conversion), rescale share counts and EPS to the traded security in place. Returns the
    ratio applied (reported shares per traded share) or None when no change was needed."""
    reported = share_basis(ttm)
    if not reported or not traded_shares:
        return None
    ratio = reported / traded_shares
    if 1 - tolerance <= ratio <= 1 + tolerance:
        return None
    for frame in (annual, ttm):
        keys = frame.columns if isinstance(frame, pd.DataFrame) else frame.index
        for col in ("eps", "core_eps"):
            if col in keys:
                frame[col] = frame[col] * ratio
        for col in ("shares", "shares_outstanding"):
            if col in keys:
                frame[col] = frame[col] / ratio
    return ratio


def looks_like_adr(reported: float | None, traded: float | None) -> bool:
    """An integer ratio >= 2 between reported ordinary shares and traded shares."""
    if not reported or not traded:
        return False
    ratio = reported / traded
    return ratio >= 1.8 and abs(ratio - round(ratio)) / ratio < 0.05


def _adjusted(name: str, v, splits: pd.Series) -> float:
    if name == "eps_diluted":
        return v.split_adjusted(lambda filed: 1 / split_factor(splits, filed))
    if name in ("shares_diluted", "shares_outstanding"):
        return v.split_adjusted(lambda filed: split_factor(splits, filed))
    return v.val


def _sec_frames(parsed: ParsedFacts, splits: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in parsed.fiscal_year_ends])
    raw = pd.DataFrame(index=idx, dtype=float)
    for name, by_year in parsed.annual.items():
        col = pd.Series(np.nan, index=idx, dtype=float)
        for fy, v in by_year.items():
            col[pd.Timestamp(fy)] = _adjusted(name, v, splits)
        raw[name] = col
    ttm_row = {name: _adjusted(name, v, splits) for name, v in parsed.ttm.items()}
    ttm_raw = pd.DataFrame([ttm_row], index=[pd.Timestamp(parsed.ttm_end)])
    return raw, ttm_raw


def _load_sec(ticker: str, cfg: dict[str, Any], cache: FileCache, md: MarketData, transport=None):
    client = SecClient(cfg["sec"].get("user_agent", ""), cache, cfg.get("cache", {}), transport)
    try:
        hit = client.lookup_cik(ticker)
        if not hit:
            return None
        cik, _title = hit
        facts = client.company_facts(cik)
        if not facts or "us-gaap" not in facts.get("facts", {}):
            return None
        sub = client.submissions(cik)
    finally:
        client.close()
    parsed = parse_company_facts(facts)
    if len(parsed.annual.get("net_income", {})) < 3:
        return None
    splits = md.splits
    reported = parsed.ttm.get("shares_outstanding") or parsed.ttm.get("shares_diluted")
    if looks_like_adr(reported.val if reported else None, yahoo_share_basis(md.info, md.price)):
        # Yahoo "splits" on ADRs include ADS-ratio changes that do not affect ordinary shares.
        splits = pd.Series(dtype=float)
        parsed.notes.append("ADR detected: Yahoo split history not applied to ordinary-share data.")
    raw, ttm_raw = _sec_frames(parsed, splits)
    sic = int(sub["sic"]) if sub.get("sic") and str(sub["sic"]).isdigit() else None
    return raw, ttm_raw, parsed.currency, sub.get("name"), sic, sub.get("sicDescription"), parsed


def load_bundle(
    ticker: str,
    cfg: dict[str, Any],
    progress: Callable[[str], None] | None = None,
    sources: Sources | None = None,
) -> DataBundle:
    """Fetch and normalise everything for ``ticker``. ``progress(step)`` is called as each
    stage starts ("market", "filings", "rates") so a UI can show what is happening."""
    step = progress or (lambda _key: None)
    src = sources or Sources()
    ticker = ticker.strip().upper()
    cache = FileCache(cache_dir(cfg), bool(cfg.get("cache", {}).get("enabled", True)))
    step("market")
    md = src.market.fetch_market(ticker)
    if not md.price:
        if not md.info and md.history.empty:
            raise DataError(
                f"Could not get any data for '{ticker}' from Yahoo Finance. Check the symbol "
                "(Yahoo format, e.g. BRK-B, ASML.AS) and your connection; Yahoo also rate-limits "
                "bursts - wait a minute and retry."
            )
        raise DataError(f"No market price found for '{ticker}'. Check the symbol (Yahoo format).")

    notes = list(md.notes)
    sec = None
    step("filings")
    try:
        sec = _load_sec(ticker, cfg, cache, md, src.transport)
    except SecIdentityError:
        raise
    except SecError as exc:
        notes.append(f"SEC data unavailable ({exc}); using Yahoo statements.")

    if sec:
        raw, ttm_raw, fin_ccy, sec_name, sic, sic_desc, parsed = sec
        notes.extend(parsed.notes)
        source = "SEC EDGAR (10-K/10-Q XBRL)"
    else:
        raw, ttm_raw = src.market.fetch_statements(ticker)
        if raw.empty:
            raise DataError(
                f"No financial statements found for '{ticker}'."
                + (
                    ""
                    if getattr(src.market, "has_statements", True)
                    else " This version reads statements from SEC filings only; companies that do not file "
                    "with the SEC need the command-line version."
                )
            )
        fin_ccy = md.financial_currency or md.currency
        sec_name, sic, sic_desc = None, None, None
        source = "Yahoo Finance (limited history)"
        notes.append(f"Only {len(raw)} years of statements available; long-term tests are limited.")

    vcfg = cfg["valuation"]
    norm = {"core_threshold": vcfg["core_earnings_threshold"], "default_tax": vcfg["default_tax_rate"]}
    annual = normalize(raw, **norm)
    ttm_frame = normalize(ttm_raw, **norm) if not ttm_raw.empty else annual.iloc[[-1]]
    ttm = ttm_frame.iloc[-1]
    ttm_end = ttm_frame.index[-1]
    # Fill TTM gaps (e.g. a field only reported annually) with the latest fiscal year.
    ttm = ttm.fillna(annual.iloc[-1])

    # Some filers (e.g. multi-class Berkshire) report no standard EPS/share count: derive it
    # from net income and Yahoo's share count so per-share metrics remain available.
    if not np.isfinite(ttm.get("eps", np.nan)):
        yahoo_shares = md.info.get("impliedSharesOutstanding") or md.info.get("sharesOutstanding")
        if yahoo_shares and np.isfinite(ttm.get("net_income", np.nan)):
            ttm["shares"] = float(yahoo_shares)
            ttm["eps"] = ttm["net_income"] / float(yahoo_shares)
            annual["shares"] = annual["shares"].fillna(float(yahoo_shares))
            annual["eps"] = annual["eps"].fillna(annual["net_income"] / annual["shares"])
            ttm["core_eps"] = ttm["core_net_income"] / float(yahoo_shares)
            annual["core_eps"] = annual["core_eps"].fillna(annual["core_net_income"] / annual["shares"])
            notes.append("EPS not reported in a standard form: derived from net income / current shares.")

    maint_share = apply_owner_earnings(
        annual,
        ttm,
        int(vcfg["maintenance_capex_lookback"]),
        maintenance=bool(vcfg["maintenance_capex"]),
        subtract_sbc=bool(vcfg["subtract_sbc"]),
    )

    fx = 1.0
    if fin_ccy and fin_ccy != md.currency:
        rate = src.market.fx_rate(fin_ccy, md.currency)
        if rate is None:
            raise DataError(f"Cannot convert {fin_ccy} financials into {md.currency} prices.")
        fx = rate
        cols = [*MONEY_COLUMNS, "eps", "core_eps"]
        annual[cols] = annual[cols] * fx
        ttm[cols] = ttm[cols] * fx
        notes.append(f"Financials converted from {fin_ccy} to {md.currency} at {fx:.4f}.")

    ratio = reconcile_share_basis(annual, ttm, yahoo_share_basis(md.info, md.price))
    if ratio:
        notes.append(
            f"Filings count a different share than the one traded (ADR or share class): "
            f"per-share data rescaled at {ratio:.3g} reported shares per traded share."
        )

    usd_fx: float | None = 1.0
    if md.currency != "USD":
        usd_fx = fx if fin_ccy == "USD" else src.market.fx_rate("USD", md.currency)

    bundle = DataBundle(
        ticker=ticker,
        price=float(md.price),
        currency=md.currency,
        annual=annual,
        ttm=ttm,
        ttm_end=pd.Timestamp(ttm_end),
        history=md.history,
        dividends=md.dividends,
        splits=md.splits,
        name=md.name or sec_name or ticker,
        sector=md.sector,
        industry=md.industry,
        sic=sic,
        sic_description=sic_desc,
        financial_currency=fin_ccy,
        fx_rate=fx,
        usd_fx=usd_fx,
        info=md.info,
        insider=md.insider,
        source=source,
        debt_data_found=has_debt_data(raw),
        maint_capex_share=maint_share,
        notes=notes,
    )
    step("rates")
    _load_macro(bundle, cfg, cache, src.transport)
    return bundle


def _load_macro(bundle: DataBundle, cfg: dict[str, Any], cache: FileCache, transport=None) -> None:
    ttl = float(cfg.get("cache", {}).get("macro_ttl_hours", 24))
    macro = cfg.get("macro", {})
    key = str(cfg.get("fred", {}).get("api_key") or "")
    aaa = fred.latest_value("AAA", cache, ttl, key, transport)
    if aaa:
        bundle.aaa_yield, bundle.aaa_source = aaa[0], f"FRED AAA {aaa[1]}"
    else:
        bundle.aaa_yield = float(macro.get("aaa_yield_fallback", 6.0))
        bundle.notes.append("FRED unreachable: using fallback AAA yield.")
    tsy = fred.latest_value("DGS10", cache, ttl, key, transport)
    if tsy:
        bundle.treasury_10y, bundle.treasury_source = tsy[0], f"FRED DGS10 {tsy[1]}"
    else:
        bundle.treasury_10y = float(macro.get("treasury_10y_fallback", 5.3))
