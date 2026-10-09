"""Yahoo Finance over plain HTTP, for the browser build (yfinance cannot run in Pyodide).

Two public endpoints:
  chart         query1.finance.yahoo.com/v8/finance/chart/{symbol}      prices, splits, dividends
  quoteSummary  query2.finance.yahoo.com/v10/finance/quoteSummary/{s}   profile, beta, analysts...

quoteSummary needs Yahoo's cookie + "crumb"; the relay Worker obtains and attaches them. If the
profile is unavailable the analysis still runs: beta is then computed from five years of monthly
returns against the S&P 500, and the informational sentiment section is left empty.
"""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import quote

from .fx import convert_rate
from .http import Transport, TransportError

# numpy/pandas are imported inside the functions that need them: the browser loads them in the
# background, and the credential and ticker checks must work before they arrive.

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SUMMARY = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
SUMMARY_MODULES = (
    "price,summaryProfile,summaryDetail,defaultKeyStatistics,financialData,quoteType,netSharePurchaseActivity"
)
EARLIEST = -2208994789  # 1900-01-01, the same "max" start yfinance uses
MARKET_INDEX = "^GSPC"


class YahooError(RuntimeError):
    pass


def _url(template: str, symbol: str) -> str:
    return template.format(symbol=quote(symbol, safe=""))


def _result(data: dict[str, Any]) -> dict[str, Any]:
    chart = data.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        err = chart.get("error") or {}
        raise YahooError(err.get("description") or "No data for this symbol.")
    return results[0]


def chart_quote(data: dict[str, Any]) -> dict[str, Any]:
    """Name, exchange, currency, price and instrument type from a chart response (no pandas)."""
    res = _result(data)
    meta = res.get("meta") or {}
    price = meta.get("regularMarketPrice")
    if not price:
        closes = (((res.get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
        price = next((c for c in reversed(closes) if c is not None), None)
    return {
        "name": meta.get("longName") or meta.get("shortName"),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "currency": meta.get("currency"),
        "price": float(price) if price else None,
        "type": (meta.get("instrumentType") or "").upper(),
    }


def parse_chart(data: dict[str, Any]):
    """(meta, history, splits, dividends) from a chart response."""
    import numpy as np
    import pandas as pd

    from .yahoo import _naive_index

    res = _result(data)
    meta = res.get("meta") or {}
    stamps = res.get("timestamp") or []
    index = pd.to_datetime(stamps, unit="s", utc=True)
    quote_ = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((res.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")

    def col(values) -> pd.Series:
        return pd.Series(
            pd.to_numeric(values, errors="coerce") if values else np.nan, index=index, dtype=float
        )

    hist = pd.DataFrame(
        {
            "Open": col(quote_.get("open")),
            "High": col(quote_.get("high")),
            "Low": col(quote_.get("low")),
            "Close": col(quote_.get("close")),
            "Adj Close": col(adj),
            "Volume": col(quote_.get("volume")),
        }
    )
    hist = _naive_index(hist.dropna(subset=["Close"])) if len(hist) else pd.DataFrame()
    if isinstance(hist, pd.DataFrame) and len(hist):
        hist = hist[~hist.index.duplicated(keep="last")]

    events = res.get("events") or {}
    splits = _events(events.get("splits"), lambda e: _ratio(e))
    dividends = _events(events.get("dividends"), lambda e: e.get("amount"))
    return meta, hist, splits, dividends


def _ratio(event: dict[str, Any]) -> float | None:
    num, den = event.get("numerator"), event.get("denominator")
    try:
        return float(num) / float(den) if num and den else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _events(raw: dict[str, Any] | None, value):
    import pandas as pd

    from .yahoo import _naive_index

    if not raw:
        return pd.Series(dtype=float)
    rows = {}
    for ev in raw.values():
        v = value(ev)
        if ev.get("date") is not None and v is not None:
            rows[pd.to_datetime(int(ev["date"]), unit="s", utc=True)] = float(v)
    s = pd.Series(rows, dtype=float).sort_index()
    return _naive_index(s)


def flatten_summary(data: dict[str, Any]) -> dict[str, Any]:
    """quoteSummary modules -> one flat dict with yfinance-style keys (``{"raw": x}`` -> x)."""
    results = ((data.get("quoteSummary") or {}).get("result")) or []
    if not results:
        return {}
    info: dict[str, Any] = {}
    for module in results[0].values():
        if not isinstance(module, dict):
            continue
        for key, value in module.items():
            if isinstance(value, dict):
                value = value.get("raw", value.get("fmt"))
            if value is not None and value != {} and key not in info:
                info[key] = value
    return info


def insider_from_summary(info: dict[str, Any]) -> dict[str, float]:
    mapping = {
        "buyInfoShares": "buy_shares", "buyInfoCount": "buy_trans",
        "sellInfoShares": "sell_shares", "sellInfoCount": "sell_trans",
        "netInfoShares": "net_shares", "netPercentInsiderShares": "net_pct",
    }  # fmt: skip
    out = {}
    for src, dst in mapping.items():
        v = info.get(src)
        if isinstance(v, (int, float)) and math.isfinite(v):
            out[dst] = float(v)
    return out


def monthly_beta(stock_close, index_close, months: int = 60) -> float | None:
    """Beta of monthly returns over the last ``months`` (Yahoo's own convention: 5y monthly)."""
    import pandas as pd

    s = stock_close.resample("ME").last().pct_change()
    m = index_close.resample("ME").last().pct_change()
    both = pd.concat([s, m], axis=1, join="inner").dropna().tail(months)
    if len(both) < 24 or both.iloc[:, 1].var() == 0:
        return None
    cov = both.cov().iloc[0, 1]
    return float(cov / both.iloc[:, 1].var())


class YahooHttpSource:
    name = "yahoo-http"
    has_statements = False  # statements come from SEC filings in this build

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def _json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._t.request("GET", url, params=params)
        except TransportError as exc:
            raise YahooError(f"Yahoo Finance unreachable: {exc}") from exc
        if resp.status == 429:
            raise YahooError("Yahoo Finance is throttling requests. Wait a minute and try again.")
        try:
            data = resp.json()
        except ValueError as exc:
            raise YahooError(f"Yahoo Finance sent an unreadable response (HTTP {resp.status}).") from exc
        if resp.status >= 400 and not (data.get("chart") or {}).get("result"):
            err = (data.get("chart") or data.get("quoteSummary") or {}).get("error") or {}
            raise YahooError(err.get("description") or f"Yahoo Finance answered HTTP {resp.status}.")
        return data

    def chart(self, symbol: str, **params: Any):
        return parse_chart(self._json(_url(CHART, symbol), params))

    def quote(self, symbol: str) -> dict[str, Any]:
        """Cheap check used before a run: one 5-day chart request."""
        return chart_quote(self._json(_url(CHART, symbol), {"range": "5d", "interval": "1d"}))

    def summary(self, symbol: str) -> dict[str, Any]:
        return flatten_summary(self._json(_url(SUMMARY, symbol), {"modules": SUMMARY_MODULES}))

    def fetch_market(self, ticker: str):
        import pandas as pd

        from .yahoo import MarketData

        md = MarketData()
        try:
            meta, md.history, md.splits, md.dividends = self.chart(
                ticker, period1=EARLIEST, period2=int(pd.Timestamp.now(tz="UTC").timestamp()),
                interval="1d", events="div,split", includeAdjustedClose="true",
            )  # fmt: skip
        except YahooError as exc:
            md.notes.append(f"Yahoo price history unavailable ({exc}).")
            return md
        try:
            md.info = self.summary(ticker)
        except YahooError:
            md.info = {}
            md.notes.append("Yahoo profile unavailable: beta computed from prices; analyst data not shown.")
        info = md.info
        info.setdefault("currency", meta.get("currency"))
        info.setdefault("longName", meta.get("longName") or meta.get("shortName"))
        info.setdefault("quoteType", meta.get("instrumentType"))
        if "beta" not in info and len(md.history):
            try:
                _, index_hist, _, _ = self.chart(MARKET_INDEX, range="10y", interval="1d")
                beta = monthly_beta(md.history["Close"], index_hist["Close"])
            except YahooError:
                beta = None
            if beta is not None:
                info["beta"] = beta
        price = meta.get("regularMarketPrice") or info.get("regularMarketPrice")
        if not price and len(md.history):
            price = float(md.history["Close"].iloc[-1])
        md.price = float(price) if price else None
        md.currency = info.get("currency") or "USD"
        md.financial_currency = info.get("financialCurrency")
        md.name = info.get("longName") or info.get("shortName")
        md.sector = info.get("sector")
        md.industry = info.get("industry")
        md.insider = insider_from_summary(info)
        return md

    def fetch_statements(self, ticker: str):
        import pandas as pd

        return pd.DataFrame(), pd.DataFrame()

    def fx_rate(self, from_ccy: str, to_ccy: str) -> float | None:
        def quote_rate(a: str, b: str) -> float | None:
            try:
                return self.quote(f"{a}{b}=X")["price"]
            except YahooError:
                return None

        return convert_rate(from_ccy, to_ccy, quote_rate)
