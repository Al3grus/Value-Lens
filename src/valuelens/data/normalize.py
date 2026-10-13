"""Turn raw statement fields (from SEC or Yahoo) into the canonical columns used by analysis."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from .concepts import CANONICAL_COLUMNS, SHARE_COLUMNS

MONEY_COLUMNS = tuple(c for c in CANONICAL_COLUMNS if c not in SHARE_COLUMNS)
CORE_RECENT_YEARS = 3  # core earnings are used when non-operating items were material in any of these


def split_factor(splits: pd.Series | None, after: date | pd.Timestamp | None) -> float:
    """Cumulative split ratio for splits that happened strictly after ``after``.

    A value filed on date F already reflects every split before F (ASC 260 requires
    retroactive restatement), so only later splits need to be applied.
    """
    if splits is None or splits.empty or after is None:
        return 1.0
    after_ts = pd.Timestamp(after)
    later = splits[splits.index > after_ts]
    later = later[later > 0]
    return float(np.prod(later.values)) if not later.empty else 1.0


def _col(raw: pd.DataFrame, name: str) -> pd.Series:
    if name in raw.columns:
        return pd.to_numeric(raw[name], errors="coerce").astype(float)
    return pd.Series(np.nan, index=raw.index, dtype=float)


def normalize(
    raw: pd.DataFrame,
    core_threshold: float = 0.15,
    default_tax: float = 0.21,
    tax_floor: float = 0.10,
    tax_cap: float = 0.35,
    use_core: bool | None = None,
    count_lt_investments: bool = False,
    leases_as_debt: bool = False,
) -> pd.DataFrame:
    """Derive canonical columns. ``raw`` uses the field names from ``concepts.FIELDS`` (plus
    optional ``total_debt_reported`` / ``current_debt_reported`` / ``fcf_reported`` from Yahoo).

    ``use_core`` forces the core-earnings choice (the TTM row follows the annual frame's decision);
    None decides from the frame itself. The decision is kept in ``frame.attrs["core_earnings"]``."""
    c = pd.DataFrame(index=raw.index)
    g = lambda k: _col(raw, k)

    c["revenue"] = g("revenue")
    c["cogs"] = g("cogs").abs()
    c["gross_profit"] = g("gross_profit").fillna(c["revenue"] - c["cogs"])
    c["interest_expense"] = g("interest_expense").abs()
    c["interest_income"] = g("interest_income").abs()
    c["equity_method_income"] = g("equity_method_income")
    c["pretax_income"] = g("pretax_income")
    c["operating_income"] = g("operating_income").fillna(c["pretax_income"] + c["interest_expense"].fillna(0))
    c["net_income"] = g("net_income")

    eps, shares = g("eps_diluted"), g("shares_diluted")
    shares = shares.where(shares > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        implied_shares = (c["net_income"] / eps).where(eps.abs() > 1e-9)
    c["shares"] = shares.fillna(implied_shares.where(implied_shares > 0))
    c["eps"] = eps.fillna(c["net_income"] / c["shares"])
    so = g("shares_outstanding")
    c["shares_outstanding"] = so.where(so > 0)

    da = g("dep_amort")
    da_parts = g("depreciation") + g("amortization").fillna(0)
    c["dep_amort"] = da.fillna(da_parts).abs()
    c["ebitda"] = c["operating_income"] + c["dep_amort"]
    c["income_tax"] = g("income_tax")
    c["sga"] = g("sga").fillna(g("general_admin") + g("selling_marketing").fillna(0))

    # Core earnings: operating profit (plus equity-method income, a recurring share of affiliates'
    # profits) after interest and tax. Reported net income also carries investment gains/losses,
    # FX and one-offs; when those were material in any recent year, core earnings are used for
    # every year so the EPS series stays comparable (a per-year switch makes a company hovering
    # around the threshold look jagged) and a mark-to-market gain cannot make a stock look cheap.
    tax_rate = (c["income_tax"] / c["pretax_income"]).where(c["pretax_income"] > 0)
    tax_rate = tax_rate.clip(tax_floor, tax_cap).fillna(default_tax)
    core_pretax = (
        c["operating_income"] - c["interest_expense"].fillna(0) + c["equity_method_income"].fillna(0)
    )
    non_operating = c["pretax_income"] - core_pretax
    material = (non_operating.abs() > core_threshold * core_pretax.abs()) & c["net_income"].notna()
    if use_core is None:
        use_core = bool(material.iloc[-CORE_RECENT_YEARS:].any())
    core_ni = core_pretax * (1 - tax_rate)
    c["core_net_income"] = core_ni.where(core_ni.notna(), c["net_income"]) if use_core else c["net_income"]
    scale = (c["core_net_income"] / c["net_income"]).where(c["net_income"] > 0)
    adjusted_eps = (c["eps"] * scale).fillna(c["core_net_income"] / c["shares"])
    c["core_eps"] = adjusted_eps.where(adjusted_eps.notna(), c["eps"]) if use_core else c["eps"]

    c["cfo"] = g("cfo")
    c["capex"] = g("capex").abs()
    # No capex figure means free cash flow is unknown, not that nothing was spent (has_capex_data()).
    c["fcf"] = (c["cfo"] - c["capex"]).fillna(g("fcf_reported"))
    c["sbc"] = g("sbc").abs()
    c["owner_fcf"] = c["fcf"] - c["sbc"].fillna(0)  # refined by apply_owner_earnings()
    c["maint_capex"] = c["capex"]
    c["dividends_paid"] = g("dividends_paid").abs().fillna(0)
    c["buybacks"] = g("buybacks").abs().fillna(0)
    c["stock_issued"] = g("stock_issued").abs().fillna(0)

    c["total_assets"] = g("total_assets")
    c["current_assets"] = g("current_assets")
    c["current_liabilities"] = g("current_liabilities")
    c["equity"] = g("equity")
    c["total_liabilities"] = g("total_liabilities").fillna(
        g("liabilities_and_equity") - g("equity_incl_nci").fillna(c["equity"])
    )
    # Claims ranking ahead of common shareholders, and what is left for them after intangibles.
    c["preferred_stock"] = g("preferred_stock")
    c["minority_interest"] = g("minority_interest").fillna(g("equity_incl_nci") - g("equity"))
    c["common_equity"] = c["equity"] - c["preferred_stock"].fillna(0)
    intangibles = g("goodwill_and_intangibles").fillna(g("goodwill").fillna(0) + g("intangibles").fillna(0))
    c["tangible_equity"] = c["common_equity"] - intangibles
    c["cash"] = g("cash")
    c["st_investments"] = g("st_investments").fillna(0)
    c["lt_investments"] = g("lt_investments")
    c["cash_total"] = c["cash"].fillna(0) + c["st_investments"]
    if count_lt_investments:
        c["cash_total"] = c["cash_total"] + c["lt_investments"].fillna(0)

    current_ltd = g("lt_debt_current").fillna(g("debt_current_total")).fillna(0)
    lt_nc = g("lt_debt_noncurrent").fillna(g("lt_debt_total") - current_ltd)
    current_parts = (
        g("lt_debt_current").fillna(0)
        + g("short_term_borrowings").fillna(0)
        + g("commercial_paper").fillna(0)
    )
    current_debt = g("debt_current_total").fillna(g("current_debt_reported")).fillna(current_parts)
    c["lt_debt"] = lt_nc.fillna(0)
    c["current_debt"] = current_debt.fillna(0)
    total = c["lt_debt"] + c["current_debt"]
    c["total_debt"] = g("total_debt_reported").fillna(total)
    c["lease_liabilities"] = g("operating_leases").fillna(
        g("operating_leases_noncurrent") + g("operating_leases_current").fillna(0)
    )
    if leases_as_debt:
        c["total_debt"] = c["total_debt"] + c["lease_liabilities"].fillna(0)
    c["net_debt"] = c["total_debt"] - c["cash_total"]

    c["retained_earnings"] = g("retained_earnings")
    c["receivables"] = g("receivables")
    c["ppe_net"] = g("ppe_net")
    out = c[list(CANONICAL_COLUMNS)]
    out.attrs["core_earnings"] = use_core
    return out


def apply_owner_earnings(
    annual: pd.DataFrame,
    ttm: pd.Series,
    lookback: int = 5,
    maintenance: bool = True,
    subtract_sbc: bool = True,
    max_da_multiple: float | None = 1.0,
) -> float | None:
    """Buffett's owner earnings (1986 letter) deduct only the capex needed to *maintain* the
    business, not capex spent on growth. Maintenance capex is Bruce Greenwald's estimate (growth
    capex = average PP&E / sales over prior years x increase in sales; maintenance = total capex -
    growth capex) kept between the smaller of depreciation and capex (the floor) and
    ``max_da_multiple`` x depreciation (the cap; None = no cap). With the default multiple of 1.0
    floor and cap coincide, so maintenance capex is simply min(capex, depreciation): during a
    build-out, capex far ahead of sales counts as growth investment rather than upkeep.

    Updates ``maint_capex`` and ``owner_fcf`` in place (NaN where capex is unknown) and returns
    the latest maintenance share of total capex (None if unknown)."""
    capex = annual["capex"]
    ppe_to_sales = (annual["ppe_net"] / annual["revenue"]).where(annual["revenue"] > 0)
    avg_ratio = ppe_to_sales.shift(1).rolling(lookback, min_periods=1).mean()
    growth_capex = (avg_ratio * annual["revenue"].diff().clip(lower=0)).fillna(0)
    maint = (capex - growth_capex).clip(lower=0)
    known = maint.notna()
    floor = np.minimum(annual["dep_amort"], capex)
    maint = maint.where(~known | floor.isna() | (maint >= floor), floor)
    if max_da_multiple is not None:
        cap = annual["dep_amort"] * max_da_multiple
        maint = maint.where(~known | cap.isna() | (maint <= cap), cap)
    maint = maint.clip(upper=capex)
    if not maintenance:
        maint = capex.copy()
    annual["maint_capex"] = maint
    sbc_a = annual["sbc"].fillna(0) if subtract_sbc else 0.0
    owner = annual["cfo"] - maint - sbc_a
    fallback = annual["fcf"] - sbc_a  # e.g. Yahoo data without operating cash flow
    annual["owner_fcf"] = owner.fillna(fallback)

    share = (maint / capex).where(capex > 0).dropna()
    latest_share = float(share.iloc[-1]) if not share.empty else None
    ttm_capex = ttm.get("capex")
    sbc = ttm.get("sbc")
    sbc_t = float(sbc) if subtract_sbc and sbc is not None and np.isfinite(sbc) else 0.0
    if ttm_capex is not None and np.isfinite(ttm_capex):
        ttm["maint_capex"] = ttm_capex * (latest_share if latest_share is not None and maintenance else 1.0)
        cfo = ttm.get("cfo")
        if cfo is not None and np.isfinite(cfo):
            ttm["owner_fcf"] = cfo - ttm["maint_capex"] - sbc_t
    elif ttm.get("fcf") is not None and np.isfinite(ttm.get("fcf")):
        ttm["owner_fcf"] = ttm["fcf"] - sbc_t
    return latest_share if maintenance else 1.0


def has_debt_data(raw: pd.DataFrame) -> bool:
    keys = (
        "lt_debt_noncurrent", "lt_debt_total", "lt_debt_current", "debt_current_total",
        "short_term_borrowings", "commercial_paper", "total_debt_reported",
    )  # fmt: skip
    return any(k in raw.columns and raw[k].notna().any() for k in keys)


def has_capex_data(raw: pd.DataFrame) -> bool:
    """Whether any capital-expenditure (or reported free-cash-flow) figure was found."""
    return any(k in raw.columns and raw[k].notna().any() for k in ("capex", "fcf_reported"))
