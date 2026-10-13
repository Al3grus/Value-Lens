"""Small, well-tested numeric helpers."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def finite(x: float | None) -> bool:
    return x is not None and isinstance(x, (int, float, np.floating)) and math.isfinite(x)


def div(a: float | None, b: float | None) -> float | None:
    if not finite(a) or not finite(b) or b == 0:
        return None
    return float(a) / float(b)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def cagr(start: float | None, end: float | None, years: float) -> float | None:
    if not finite(start) or not finite(end) or years <= 0 or start <= 0 or end <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def tail(series: pd.Series, n: int) -> pd.Series:
    return series.dropna().iloc[-n:]


def smoothed_endpoints(
    series: pd.Series, span: int, window: int = 3
) -> tuple[float | None, float | None, int]:
    """(start average, end average, years between them) using ``window``-year averages."""
    s = series.dropna()
    if len(s) < window + 1:
        return None, None, 0
    span = min(span, len(s) - window)
    if span <= 0:
        return None, None, 0
    end_avg = float(s.iloc[-window:].mean())
    start_avg = float(s.iloc[-window - span : len(s) - span].mean())
    return start_avg, end_avg, span


def smoothed_cagr(series: pd.Series, span: int, window: int = 3) -> tuple[float | None, int]:
    """CAGR between the average of the last ``window`` values and the average of the
    ``window`` values ending ``span`` years earlier. Returns (cagr, years_used)."""
    start, end, span = smoothed_endpoints(series, span, window)
    if span == 0:
        return None, 0
    return cagr(start, end, span), span


def point_cagr(series: pd.Series, span: int) -> tuple[float | None, int]:
    s = series.dropna()
    if len(s) < 2:
        return None, 0
    span = min(span, len(s) - 1)
    return cagr(s.iloc[-span - 1], s.iloc[-1], span), span


def average_balance(series: pd.Series) -> pd.Series:
    """Average of opening and closing balance for each year (first year uses closing)."""
    return (series + series.shift(1)) / 2


def fmt_money(x: float | None, currency: str = "$") -> str:
    if not finite(x):
        return "n/a"
    sign = "-" if x < 0 else ""
    x = abs(x)
    sym = "$" if currency in ("$", "USD") else f"{currency} "
    if x >= 1e12:
        return f"{sign}{sym}{x / 1e12:.2f}T"
    if x >= 1e9:
        return f"{sign}{sym}{x / 1e9:.2f}B"
    if x >= 1e6:
        return f"{sign}{sym}{x / 1e6:.1f}M"
    return f"{sign}{sym}{x:,.0f}"


def pct(x: float | None, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%" if finite(x) else "n/a"
