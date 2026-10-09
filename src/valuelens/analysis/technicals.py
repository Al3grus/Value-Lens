"""Price-trend overlay. Used only to time entries/exits, never to judge the business.

* Trend: price vs 50/200-day moving averages.
* 12-1 momentum (Jegadeesh & Titman 1993): return over the past 12 months excluding the last.
* RSI(14), Wilder smoothing; distance from 52-week high; 1-year annualised volatility.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..models import Technicals

TRADING_DAYS = 252


def rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) <= period:
        return None
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    g, lo = gain.iloc[-1], loss.iloc[-1]
    if not np.isfinite(g) or not np.isfinite(lo):
        return None
    if lo == 0:
        return 100.0
    return float(100 - 100 / (1 + g / lo))


def classify_trend(price: float, sma50: float, sma200: float, mom: float | None) -> str:
    """Vote of three signals: price above the 200-day average, 50-day above 200-day, positive
    12-1 momentum. Three up, or two up with price above the 200-day -> UPTREND; at most one up
    with price below the 200-day -> DOWNTREND; anything else -> SIDEWAYS."""
    votes = [price > sma200, sma50 > sma200]
    if mom is not None:
        votes.append(mom > 0)
    up = sum(votes)
    if up == len(votes) or (up >= 2 and price > sma200):
        return "UPTREND"
    if up <= 1 and price < sma200:
        return "DOWNTREND"
    return "SIDEWAYS"


def compute_technicals(history: pd.DataFrame) -> Technicals:
    if history is None or history.empty:
        return Technicals("N/A", notes=["No price history"])
    col = "Adj Close" if "Adj Close" in history else "Close"
    close = history[col].dropna()
    if len(close) < 60:
        return Technicals("N/A", notes=["Less than 60 days of price history"])

    price = float(close.iloc[-1])
    sma50 = float(close.iloc[-50:].mean())
    sma200 = float(close.iloc[-200:].mean()) if len(close) >= 200 else None
    mom = None
    if len(close) > TRADING_DAYS:
        mom = float(close.iloc[-21] / close.iloc[-TRADING_DAYS - 1] - 1)
    year = close.iloc[-TRADING_DAYS:]
    from_high = float(price / year.max() - 1)
    vol = float(np.log(year).diff().std() * np.sqrt(TRADING_DAYS))
    r = rsi(close)

    trend = "SIDEWAYS"
    notes: list[str] = []
    if sma200 is not None:
        trend = classify_trend(price, sma50, sma200, mom)
        notes.append("50d above 200d" if sma50 > sma200 else "50d below 200d")
    else:
        notes.append("Less than 200 days of history")
    if r is not None:
        if r >= 70:
            notes.append("RSI overbought (>= 70)")
        elif r <= 30:
            notes.append("RSI oversold (<= 30)")
    if from_high <= -0.30:
        notes.append(f"{from_high:.0%} below 52-week high")
    return Technicals(trend, price, sma50, sma200, mom, r, from_high, vol, notes)
