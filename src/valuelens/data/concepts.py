"""Mapping from canonical field names to US-GAAP XBRL concepts (SEC companyfacts).

Companies switch concepts over time (e.g. ``SalesRevenueNet`` -> ``RevenueFromContractWith
CustomerExcludingAssessedTax`` after ASC 606), so every field lists candidates in priority
order. ``merge`` decides how candidates are combined per fiscal year:

* ``first`` - take the highest-priority concept that has a value for that year
* ``max``   - take the largest value (used for revenue, where sub-totals can appear under
              other concepts but the total is always the largest)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    concepts: tuple[str, ...]
    kind: str  # "flow" (duration) or "instant"
    unit: str = "money"  # "money", "per_share" or "shares"
    merge: str = "first"
    taxonomy: str = "us-gaap"


FIELDS: dict[str, Field] = {
    # ---- income statement (flows) ----
    "revenue": Field(
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueGoodsNet",
            "SalesRevenueServicesNet",
            "RevenuesNetOfInterestExpense",
        ),
        "flow",
        merge="max",
    ),
    "cogs": Field(
        (
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
            "CostOfGoodsSold",
            "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
            "CostOfServices",
        ),
        "flow",
    ),
    "gross_profit": Field(("GrossProfit",), "flow"),
    "operating_income": Field(("OperatingIncomeLoss",), "flow"),
    "net_income": Field(
        (
            "NetIncomeLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            "ProfitLoss",
        ),
        "flow",
    ),
    "eps_diluted": Field(
        ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"),
        "flow",
        unit="per_share",
    ),
    "shares_diluted": Field(
        (
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
            "WeightedAverageNumberOfSharesOutstandingBasic",
        ),
        "flow",
        unit="shares",
    ),
    "interest_expense": Field(
        (
            "InterestExpense",
            "InterestExpenseNonoperating",
            "InterestExpenseDebt",
            "InterestAndDebtExpense",
            "InterestExpenseOperating",
            "InterestPaidNet",
        ),
        "flow",
    ),
    "dep_amort": Field(
        (
            "DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet",
            "DepreciationAndAmortization",
            "DepreciationAmortizationAndOther",
        ),
        "flow",
    ),
    "depreciation": Field(
        (
            "Depreciation",
            "DepreciationNonproduction",
            "DepreciationAndAmortizationOfPropertyPlantAndEquipment",
        ),
        "flow",
    ),
    "amortization": Field(
        ("AmortizationOfIntangibleAssets", "AmortizationOfAcquiredIntangibleAssets"),
        "flow",
    ),
    "pretax_income": Field(
        (
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
        ),
        "flow",
    ),
    "income_tax": Field(("IncomeTaxExpenseBenefit",), "flow"),
    "sga": Field(("SellingGeneralAndAdministrativeExpense",), "flow"),
    # Many tech companies report these separately instead of a combined SG&A line.
    "general_admin": Field(("GeneralAndAdministrativeExpense",), "flow"),
    "selling_marketing": Field(
        ("SellingAndMarketingExpense", "SellingExpense", "MarketingAndAdvertisingExpense"), "flow"
    ),
    # ---- cash flow statement (flows) ----
    "cfo": Field(
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
        "flow",
    ),
    "capex": Field(
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "PaymentsForCapitalImprovements",
            "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        ),
        "flow",
    ),
    "sbc": Field(("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"), "flow"),
    "dividends_paid": Field(
        ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock", "PaymentsOfOrdinaryDividends"),
        "flow",
    ),
    "buybacks": Field(
        ("PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"),
        "flow",
    ),
    "stock_issued": Field(
        (
            "ProceedsFromIssuanceOfCommonStock",
            "ProceedsFromStockOptionsExercised",
            "ProceedsFromIssuanceOrSaleOfEquity",
        ),
        "flow",
    ),
    # ---- balance sheet (instants) ----
    "total_assets": Field(("Assets",), "instant"),
    "current_assets": Field(("AssetsCurrent",), "instant"),
    "total_liabilities": Field(("Liabilities",), "instant"),
    "current_liabilities": Field(("LiabilitiesCurrent",), "instant"),
    "liabilities_and_equity": Field(("LiabilitiesAndStockholdersEquity",), "instant"),
    "equity": Field(
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
        "instant",
    ),
    "equity_incl_nci": Field(
        (
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "StockholdersEquity",
        ),
        "instant",
    ),
    "cash": Field(
        (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "CashAndDueFromBanks",
            "Cash",
        ),
        "instant",
    ),
    "st_investments": Field(
        (
            "ShortTermInvestments",
            "MarketableSecuritiesCurrent",
            "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
            "AvailableForSaleSecuritiesCurrent",
            "HeldToMaturitySecuritiesCurrent",
            "OtherShortTermInvestments",
        ),
        "instant",
    ),
    "lt_debt_noncurrent": Field(
        (
            "LongTermDebtNoncurrent",
            "LongTermDebtAndCapitalLeaseObligations",
            "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
            "LongTermNotesPayable",
            "SeniorLongTermNotes",
            "ConvertibleNotesPayableNoncurrent",
        ),
        "instant",
    ),
    "lt_debt_total": Field(("LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"), "instant"),
    "lt_debt_current": Field(
        (
            "LongTermDebtCurrent",
            "LongTermDebtAndCapitalLeaseObligationsCurrent",
            "LongTermDebtAndFinanceLeaseObligationsCurrent",
        ),
        "instant",
    ),
    "debt_current_total": Field(("DebtCurrent",), "instant"),
    "short_term_borrowings": Field(("ShortTermBorrowings", "OtherShortTermBorrowings"), "instant"),
    "commercial_paper": Field(("CommercialPaper",), "instant"),
    "retained_earnings": Field(("RetainedEarningsAccumulatedDeficit",), "instant"),
    "receivables": Field(
        (
            "AccountsReceivableNetCurrent",
            "ReceivablesNetCurrent",
            "AccountsNotesAndLoansReceivableNetCurrent",
        ),
        "instant",
    ),
    "ppe_net": Field(
        (
            "PropertyPlantAndEquipmentNet",
            "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
        ),
        "instant",
    ),
    "shares_outstanding": Field(
        ("EntityCommonStockSharesOutstanding",), "instant", unit="shares", taxonomy="dei"
    ),
}

ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "20-F", "20-F/A", "40-F", "40-F/A"})
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})

# Canonical columns the analysis layer relies on (after normalisation).
CANONICAL_COLUMNS = (
    "revenue", "cogs", "gross_profit", "operating_income", "net_income", "eps", "shares",
    "interest_expense", "dep_amort", "ebitda", "pretax_income", "income_tax", "sga",
    "cfo", "capex", "fcf", "sbc", "owner_fcf", "dividends_paid", "buybacks", "stock_issued",
    "total_assets", "current_assets", "total_liabilities", "current_liabilities", "equity",
    "cash", "st_investments", "cash_total", "lt_debt", "current_debt", "total_debt",
    "net_debt", "retained_earnings", "receivables", "ppe_net", "shares_outstanding",
    "core_net_income", "core_eps", "maint_capex",
)  # fmt: skip

# Columns that are counts or per-share amounts (never currency-converted as totals).
SHARE_COLUMNS = ("eps", "core_eps", "shares", "shares_outstanding")
