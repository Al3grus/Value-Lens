"""Turn raw statement fields (from SEC or Yahoo) into the canonical columns used by analysis."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from .concepts import CANONICAL_COLUMNS, SHARE_COLUMNS

MONEY_COLUMNS = tuple(c for c in CANONICAL_COLUMNS if c not in SHARE_COLUMNS)


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


def normalize(raw: pd.DataFrame, core_threshold: float = 0.15, default_tax: float = 0.21) -> pd.DataFrame:
    """Derive canonical columns. ``raw`` uses the field names from ``concepts.FIELDS``
    (plus optional ``total_debt_reported`` / ``fcf_reported`` from Yahoo)."""
    c = pd.DataFrame(index=raw.index)
    g = lambda k: _col(raw, k)

    c["revenue"] = g("revenue")
    c["cogs"] = g("cogs").abs()
    c["gross_profit"] = g("gross_profit").fillna(c["revenue"] - c["cogs"])
    c["interest_expense"] = g("interest_expense").abs()
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

    # Core earnings: operating profit after interest and tax. Used instead of reported net
    # income only when non-operating items (investment gains/losses, FX, one-offs) are
    # material, so a mark-to-market gain does not make a stock look cheap on P/E.
    tax_rate = (c["income_tax"] / c["pretax_income"]).where(c["pretax_income"] > 0)
    tax_rate = tax_rate.clip(0.0, 0.35).fillna(default_tax)
    core_pretax = c["operating_income"] - c["interest_expense"].fillna(0)
    non_operating = c["pretax_income"] - core_pretax
    use_core = (non_operating.abs() > core_threshold * core_pretax.abs()) & c["net_income"].notna()
    core_ni = core_pretax * (1 - tax_rate)
    c["core_net_income"] = core_ni.where(use_core & core_ni.notna(), c["net_income"])
    scale = (c["core_net_income"] / c["net_income"]).where(c["net_income"] > 0)
    adjusted_eps = (c["eps"] * scale).fillna(c["core_net_income"] / c["shares"])
    c["core_eps"] = adjusted_eps.where(use_core & core_ni.notna(), c["eps"])

    c["cfo"] = g("cfo")
    c["capex"] = g("capex").abs()
    fcf = c["cfo"] - c["capex"].fillna(0)
    c["fcf"] = fcf.fillna(g("fcf_reported"))
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
    c["cash"] = g("cash")
    c["st_investments"] = g("st_investments").fillna(0)
    c["cash_total"] = c["cash"].fillna(0) + c["st_investments"]

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
    c["net_debt"] = c["total_debt"] - c["cash_total"]

    c["retained_earnings"] = g("retained_earnings")
    c["receivables"] = g("receivables")
    c["ppe_net"] = g("ppe_net")
    return c[list(CANONICAL_COLUMNS)]


def apply_owner_earnings(
    annual: pd.DataFrame,
    ttm: pd.Series,
    lookback: int = 5,
    maintenance: bool = True,
    subtract_sbc: bool = True,
) -> float | None:
    """Buffett's owner earnings (1986 letter) deduct only the capex needed to *maintain* the
    business, not capex spent on growth. Maintenance capex is estimated with Bruce Greenwald's
    method: growth capex = (average PP&E / sales over prior years) x increase in sales;
    maintenance = total capex - growth capex, floored at depreciation and capped at total capex.

    Updates ``maint_capex`` and ``owner_fcf`` in place and returns the latest maintenance share
    of total capex (None if unknown)."""
    capex = annual["capex"]
    ppe_to_sales = (annual["ppe_net"] / annual["revenue"]).where(annual["revenue"] > 0)
    avg_ratio = ppe_to_sales.shift(1).rolling(lookback, min_periods=1).mean()
    growth_capex = (avg_ratio * annual["revenue"].diff().clip(lower=0)).fillna(0)
    maint = (capex - growth_capex).clip(lower=0)
    floor = np.minimum(annual["dep_amort"], capex)
    maint = maint.where(floor.isna() | (maint >= floor), floor).clip(upper=capex)
    if not maintenance:
        maint = capex.copy()
    annual["maint_capex"] = maint
    sbc_a = annual["sbc"].fillna(0) if subtract_sbc else 0.0
    owner = annual["cfo"] - maint.fillna(0) - sbc_a
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
