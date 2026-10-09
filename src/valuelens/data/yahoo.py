"""Market data (price, history, splits, dividends, profile, sentiment) from Yahoo Finance via
yfinance, plus a fallback for annual statements when a company does not file with the SEC."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import pandas as pd

from .fx import convert_rate

log = logging.getLogger(__name__)

# Yahoo statement row -> raw field name used by normalize()
YAHOO_ROWS: dict[str, tuple[str, ...]] = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "cogs": ("Cost Of Revenue", "Reconciled Cost Of Revenue"),
    "gross_profit": ("Gross Profit",),
    "operating_income": ("Operating Income", "EBIT"),
    "net_income": ("Net Income Common Stockholders", "Net Income"),
    "eps_diluted": ("Diluted EPS",),
    "shares_diluted": ("Diluted Average Shares",),
    "interest_expense": ("Interest Expense", "Interest Expense Non Operating"),
    "dep_amort": ("Reconciled Depreciation", "Depreciation And Amortization"),
    "pretax_income": ("Pretax Income",),
    "income_tax": ("Tax Provision",),
    "sga": ("Selling General And Administration",),
    "cfo": ("Operating Cash Flow",),
    "capex": ("Capital Expenditure",),
    "fcf_reported": ("Free Cash Flow",),
    "sbc": ("Stock Based Compensation",),
    "dividends_paid": ("Cash Dividends Paid", "Common Stock Dividend Paid"),
    "buybacks": ("Repurchase Of Capital Stock", "Common Stock Payments"),
    "stock_issued": ("Issuance Of Capital Stock", "Common Stock Issuance"),
    "total_assets": ("Total Assets",),
    "current_assets": ("Current Assets",),
    "total_liabilities": ("Total Liabilities Net Minority Interest",),
    "current_liabilities": ("Current Liabilities",),
    "equity": ("Stockholders Equity", "Common Stock Equity"),
    "cash": ("Cash And Cash Equivalents",),
    "st_investments": ("Other Short Term Investments",),
    "lt_debt_noncurrent": ("Long Term Debt",),
    "current_debt_reported": ("Current Debt",),
    "retained_earnings": ("Retained Earnings",),
    "receivables": ("Accounts Receivable", "Receivables"),
    "ppe_net": ("Net PPE",),
}


@dataclass
class MarketData:
    price: float | None = None
    currency: str = "USD"
    financial_currency: str | None = None
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    info: dict[str, Any] = field(default_factory=dict)
    history: pd.DataFrame = field(default_factory=pd.DataFrame)
    splits: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    dividends: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    insider: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _naive_index(obj):
    if obj is None or len(obj) == 0:
        return obj
    idx = pd.DatetimeIndex(obj.index)
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    obj = obj.copy()
    obj.index = idx.normalize()
    return obj


def _safe(fn, default=None, note: str | None = None, notes: list[str] | None = None):
    try:
        return fn()
    except Exception as exc:  # yfinance raises many exception types; never fail the run here
        log.debug("yfinance call failed: %s", exc)
        if note and notes is not None:
            notes.append(note)
        return default


def fetch_market(ticker: str) -> MarketData:
    import yfinance as yf

    md = MarketData()
    t = yf.Ticker(ticker)
    md.info = _safe(lambda: t.info, {}, "Yahoo profile unavailable.", md.notes) or {}
    hist = _safe(
        lambda: t.history(period="max", auto_adjust=False, actions=False),
        pd.DataFrame(),
        "Yahoo price history unavailable.",
        md.notes,
    )
    md.history = _naive_index(hist) if hist is not None else pd.DataFrame()
    md.splits = _naive_index(_safe(lambda: t.splits, pd.Series(dtype=float)))
    md.dividends = _naive_index(_safe(lambda: t.dividends, pd.Series(dtype=float)))

    price = _safe(lambda: float(t.fast_info["last_price"]))
    if not price and not md.history.empty:
        price = float(md.history["Close"].dropna().iloc[-1])
    if not price:
        price = md.info.get("currentPrice") or md.info.get("regularMarketPrice")
    md.price = float(price) if price else None

    md.currency = md.info.get("currency") or _safe(lambda: t.fast_info["currency"]) or "USD"
    md.financial_currency = md.info.get("financialCurrency")
    md.name = md.info.get("longName") or md.info.get("shortName")
    md.sector = md.info.get("sector")
    md.industry = md.info.get("industry")
    md.insider = _safe(lambda: _parse_insider(t.insider_purchases), {}) or {}
    return md


def _parse_insider(df: pd.DataFrame | None) -> dict[str, float]:
    if df is None or df.empty:
        return {}
    label_col = df.columns[0]
    out: dict[str, float] = {}
    for _, row in df.iterrows():
        label = str(row[label_col]).strip().lower()
        shares = pd.to_numeric(row.get("Shares"), errors="coerce")
        trans = pd.to_numeric(row.get("Trans"), errors="coerce")
        if label.startswith("purchases"):
            out["buy_shares"], out["buy_trans"] = shares, trans
        elif label.startswith("sales"):
            out["sell_shares"], out["sell_trans"] = shares, trans
        elif label.startswith("net shares"):
            out["net_shares"] = shares
        elif label.startswith("% net"):
            out["net_pct"] = shares
    return {k: float(v) for k, v in out.items() if pd.notna(v)}


def fx_rate(from_ccy: str, to_ccy: str) -> float | None:
    """Rate to convert amounts in ``from_ccy`` into ``to_ccy`` (handles GBp/ZAc/ILA pence)."""
    import yfinance as yf

    return convert_rate(
        from_ccy, to_ccy, lambda a, b: _safe(lambda: float(yf.Ticker(f"{a}{b}=X").fast_info["last_price"]))
    )


def fetch_statements(ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Annual statements (index = fiscal year end, ascending) and a one-row TTM frame."""
    import yfinance as yf

    t = yf.Ticker(ticker)
    frames = [
        _safe(lambda: t.income_stmt),
        _safe(lambda: t.balance_sheet),
        _safe(lambda: t.cashflow),
    ]
    annual = _rows_to_raw([f for f in frames if f is not None and not f.empty])

    ttm_frames = [
        _safe(lambda: t.ttm_income_stmt),
        _safe(lambda: t.ttm_cashflow),
        _safe(lambda: t.quarterly_balance_sheet),
    ]
    ttm = _rows_to_raw([f.iloc[:, :1] for f in ttm_frames if f is not None and not f.empty])
    if not ttm.empty:
        ttm = ttm.sort_index().ffill().iloc[[-1]]  # latest value of every field in one row
    return annual, ttm


def _rows_to_raw(frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame()
    stacked = pd.concat(frames, axis=0)
    stacked = stacked[~stacked.index.duplicated(keep="first")]
    out = {}
    for field_name, labels in YAHOO_ROWS.items():
        for label in labels:
            if label in stacked.index:
                out[field_name] = pd.to_numeric(stacked.loc[label], errors="coerce")
                break
    raw = pd.DataFrame(out)
    raw.index = pd.DatetimeIndex(raw.index).normalize()
    raw = raw.groupby(level=0).last().sort_index()
    return raw.dropna(how="all")


class MarketSource(Protocol):
    """Where prices, profile and (for non-SEC filers) statements come from."""

    name: str

    def fetch_market(self, ticker: str) -> MarketData: ...

    def fetch_statements(self, ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]: ...

    def fx_rate(self, from_ccy: str, to_ccy: str) -> float | None: ...


class YFinanceSource:
    """yfinance on a normal Python install (handles Yahoo's cookies/crumb and statements)."""

    name = "yfinance"

    def fetch_market(self, ticker: str) -> MarketData:
        return fetch_market(ticker)

    def fetch_statements(self, ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        return fetch_statements(ticker)

    def fx_rate(self, from_ccy: str, to_ccy: str) -> float | None:
        return fx_rate(from_ccy, to_ccy)
