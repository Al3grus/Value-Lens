"""Return-on-capital series shared by the Buffett scorecard and the valuation."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .metrics import average_balance


def ratio_series(num: pd.Series, den: pd.Series) -> pd.Series:
    """num / den for the years where the denominator is positive."""
    r = (num / den).replace([np.inf, -np.inf], np.nan)
    return r[den > 0]


def roe_series(a: pd.DataFrame) -> pd.Series:
    """Net income over average shareholders' equity."""
    return ratio_series(a["net_income"], average_balance(a["equity"]).fillna(a["equity"]))


def rotce_series(a: pd.DataFrame) -> pd.Series:
    """Return on tangible common equity: net income over average common equity less goodwill and
    intangibles (the measure banks are judged on)."""
    tangible = a["tangible_equity"]
    return ratio_series(a["net_income"], average_balance(tangible).fillna(tangible))


def roic_series(
    a: pd.DataFrame, default_tax: float, tax_floor: float = 0.10, tax_cap: float = 0.35
) -> pd.Series:
    """After-tax operating profit over average invested capital (equity + debt - cash)."""
    tax = (a["income_tax"] / a["pretax_income"]).where(a["pretax_income"] > 0)
    tax = tax.clip(tax_floor, tax_cap).fillna(default_tax)
    nopat = a["operating_income"] * (1 - tax)
    invested = a["equity"] + a["total_debt"] - a["cash_total"]
    return ratio_series(nopat, average_balance(invested).fillna(invested))
