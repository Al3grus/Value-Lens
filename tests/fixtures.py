"""Synthetic but realistic SEC companyfacts payload + market data for an imaginary company.

It deliberately reproduces the traps that broke the original script:
* revenue concept switch (SalesRevenueNet -> RevenueFromContractWithCustomer... in FY2018)
* a 2-for-1 split on 2020-06-15: filings before it report pre-split EPS/share counts
* every 10-K repeats the two prior years, so facts are duplicated across filings
* a 10-Q with year-to-date figures (and prior-year comparatives) for TTM
* quarterly-duration facts that must NOT be mistaken for annual ones
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import numpy as np
import pandas as pd

SPLIT_DATE = date(2020, 6, 15)
YEARS = list(range(2014, 2026))
H1_SHARE = 0.48


def fundamentals(year: int) -> dict[str, float]:
    k = year - 2014
    rev = 10e9 * 1.08**k
    shares_post = 2e9 * 0.99**k
    ni = rev * 0.22
    return {
        "rev": rev,
        "cogs": rev * 0.40,
        "op": rev * 0.30,
        "pretax": rev * 0.28,
        "tax": rev * 0.06,
        "ni": ni,
        "interest": 0.2e9,
        "da": rev * 0.05,
        "sga": rev * 0.15,
        "cfo": rev * 0.30,
        "capex": rev * 0.06,
        "sbc": rev * 0.02,
        "div": ni * 0.30,
        "buyback": ni * 0.20,
        "shares_post": shares_post,
        "eps_post": ni / shares_post,
        "assets": rev * 1.5,
        "ca": rev * 0.6,
        "cl": rev * 0.3,
        "equity": rev * 0.8,
        "liab": rev * 0.7,
        "cash": rev * 0.2,
        "sti": rev * 0.1,
        "ltd": rev * 0.25,
        "ltd_cur": rev * 0.02,
        "re": rev * 0.5,
        "ar": rev * 0.12,
        "ppe": rev * 0.35,
    }


FLOW_MAP = {
    "CostOfRevenue": "cogs",
    "OperatingIncomeLoss": "op",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": "pretax",
    "IncomeTaxExpenseBenefit": "tax",
    "NetIncomeLoss": "ni",
    "InterestExpense": "interest",
    "DepreciationDepletionAndAmortization": "da",
    "SellingGeneralAndAdministrativeExpense": "sga",
    "NetCashProvidedByUsedInOperatingActivities": "cfo",
    "PaymentsToAcquirePropertyPlantAndEquipment": "capex",
    "ShareBasedCompensation": "sbc",
    "PaymentsOfDividends": "div",
    "PaymentsForRepurchaseOfCommonStock": "buyback",
}
INSTANT_MAP = {
    "Assets": "assets",
    "AssetsCurrent": "ca",
    "LiabilitiesCurrent": "cl",
    "StockholdersEquity": "equity",
    "Liabilities": "liab",
    "CashAndCashEquivalentsAtCarryingValue": "cash",
    "ShortTermInvestments": "sti",
    "LongTermDebtNoncurrent": "ltd",
    "LongTermDebtCurrent": "ltd_cur",
    "RetainedEarningsAccumulatedDeficit": "re",
    "AccountsReceivableNetCurrent": "ar",
    "PropertyPlantAndEquipmentNet": "ppe",
}


def company_facts() -> dict:
    gaap: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

    def add(concept, unit, val, end, filed, form, fy, start=None, accn=None):
        row = {"end": end.isoformat(), "val": val, "accn": accn or f"acc-{filed}", "fy": fy,
               "fp": "FY" if form.startswith("10-K") else "Q2", "form": form, "filed": filed.isoformat()}  # fmt: skip
        if start:
            row["start"] = start.isoformat()
        gaap[concept][unit].append(row)

    for fy in YEARS:
        filed = date(fy + 1, 2, 1)
        post_split = filed > SPLIT_DATE
        rev_concept = (
            "RevenueFromContractWithCustomerExcludingAssessedTax" if fy >= 2018 else "SalesRevenueNet"
        )
        for y in (fy - 2, fy - 1, fy):
            if y < YEARS[0]:
                continue
            f = fundamentals(y)
            start, end = date(y, 1, 1), date(y, 12, 31)
            add(rev_concept, "USD", f["rev"], end, filed, "10-K", fy, start)
            for concept, key in FLOW_MAP.items():
                add(concept, "USD", f[key], end, filed, "10-K", fy, start)
            shares = f["shares_post"] if post_split else f["shares_post"] / 2
            eps = f["eps_post"] if post_split else f["eps_post"] * 2
            add("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", shares, end, filed, "10-K", fy, start)  # fmt: skip
            add("EarningsPerShareDiluted", "USD/shares", eps, end, filed, "10-K", fy, start)
            # A Q4-only duration inside the 10-K: must be ignored as "annual".
            add(rev_concept, "USD", f["rev"] * 0.27, end, filed, "10-K", fy, date(y, 10, 1))
        for y in (fy - 1, fy):
            if y < YEARS[0]:
                continue
            f = fundamentals(y)
            for concept, key in INSTANT_MAP.items():
                add(concept, "USD", f[key], date(y, 12, 31), filed, "10-K", fy)

    # 10-Q for H1 2026, with the H1 2025 comparative.
    filed = date(2026, 8, 1)
    f25, f26 = fundamentals(2025), fundamentals(2026)
    for y, f in ((2025, f25), (2026, f26)):
        start, end = date(y, 1, 1), date(y, 6, 30)
        add("RevenueFromContractWithCustomerExcludingAssessedTax", "USD", f["rev"] * H1_SHARE, end, filed, "10-Q", 2026, start)  # fmt: skip
        for concept, key in FLOW_MAP.items():
            add(concept, "USD", f[key] * H1_SHARE, end, filed, "10-Q", 2026, start)
        add("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", f["shares_post"], end, filed, "10-Q", 2026, start)  # fmt: skip
        add("EarningsPerShareDiluted", "USD/shares", f["eps_post"] * H1_SHARE, end, filed, "10-Q", 2026, start)  # fmt: skip
    for concept, key in INSTANT_MAP.items():
        add(concept, "USD", f26[key] * 0.98, date(2026, 6, 30), filed, "10-Q", 2026)

    dei = {
        "EntityCommonStockSharesOutstanding": {
            "units": {
                "shares": [
                    {
                        "end": "2026-07-20",
                        "val": 1.77e9,
                        "accn": "acc-q",
                        "fy": 2026,
                        "fp": "Q2",
                        "form": "10-Q",
                        "filed": "2026-08-01",
                    }
                ]
            }
        }
    }
    return {
        "cik": 9999999,
        "entityName": "TESTCO INC",
        "facts": {
            "dei": dei,
            "us-gaap": {c: {"units": dict(u)} for c, u in gaap.items()},
        },
    }


def splits() -> pd.Series:
    return pd.Series([2.0], index=pd.DatetimeIndex([pd.Timestamp(SPLIT_DATE)]), name="Stock Splits")


def price_history(end: str = "2026-09-30", years: int = 12, start_price: float = 20.0, cagr: float = 0.12):
    idx = pd.bdate_range(end=end, periods=252 * years)
    t = np.arange(len(idx)) / 252
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 0.01, len(idx)).cumsum() * 0.2
    close = start_price * (1 + cagr) ** t * np.exp(noise)
    df = pd.DataFrame({"Close": close, "Adj Close": close * 0.98, "Volume": 1_000_000}, index=idx)
    return df


def dividends(first_year: int = 2000, last_year: int = 2026) -> pd.Series:
    idx = pd.DatetimeIndex([pd.Timestamp(y, m, 15) for y in range(first_year, last_year + 1) for m in (3, 6, 9, 12)])  # fmt: skip
    idx = idx[idx <= pd.Timestamp("2026-09-30")]
    return pd.Series(0.25, index=idx)
