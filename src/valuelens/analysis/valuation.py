"""Intrinsic value: owner-earnings DCF, Graham formula, Earnings Power Value, Graham number,
plus a reverse DCF (what growth does today's price imply?)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ..data.bundle import DataBundle
from ..models import Valuation, ValuationMethod
from .metrics import clamp, div, finite, point_cagr, smoothed_cagr, tail


# --------------------------------------------------------------------------------------
# Building blocks (pure functions, unit-tested)
# --------------------------------------------------------------------------------------
def discount_rate(beta: float | None, rf: float, vcfg: dict[str, Any]) -> float:
    b = beta if finite(beta) else 1.0
    b = clamp(b, vcfg["beta_floor"], vcfg["beta_cap"])
    r = rf + b * vcfg["equity_risk_premium"]
    return max(r, vcfg["min_discount_rate"], vcfg["terminal_growth"] + 0.02)


def wacc(
    cost_of_equity: float,
    market_cap: float | None,
    debt: float | None,
    interest: float | None,
    tax_rate: float,
    rf: float,
    vcfg: dict[str, Any],
) -> float:
    """Weighted average cost of capital at market weights. Cost of debt = interest / debt,
    bounded to [rf + 0.5%, rf + 6%] so stale low-coupon debt does not flatter it."""
    floor = max(vcfg["min_discount_rate"], vcfg["terminal_growth"] + 0.02)
    if not finite(market_cap) or market_cap <= 0 or not finite(debt) or debt <= 0:
        return max(cost_of_equity, floor)
    kd = interest / debt if finite(interest) and interest > 0 else rf + 0.015
    kd = clamp(kd, rf + 0.005, rf + 0.06)
    e, d = market_cap, debt
    w = (e * cost_of_equity + d * kd * (1 - tax_rate)) / (e + d)
    return max(w, floor)


def dcf_enterprise(base_cf: float, g1: float, r: float, gt: float, n_high: int, n_fade: int) -> float:
    """Two-stage DCF: ``n_high`` years at g1, ``n_fade`` years fading linearly to gt,
    then a Gordon-growth terminal value."""
    cf, pv, t = base_cf, 0.0, 0
    for _ in range(n_high):
        t += 1
        cf *= 1 + g1
        pv += cf / (1 + r) ** t
    for k in range(1, n_fade + 1):
        t += 1
        g = g1 + (gt - g1) * k / n_fade
        cf *= 1 + g
        pv += cf / (1 + r) ** t
    terminal = cf * (1 + gt) / (r - gt)
    return pv + terminal / (1 + r) ** t


def dcf_per_share(
    base_cf: float, g1: float, r: float, vcfg: dict[str, Any], net_cash: float, shares: float
) -> float:
    ev = dcf_enterprise(
        base_cf, g1, r, vcfg["terminal_growth"], vcfg["high_growth_years"], vcfg["fade_years"]
    )
    return (ev + net_cash) / shares


def reverse_dcf(
    price: float, base_cf: float, r: float, vcfg: dict[str, Any], net_cash: float, shares: float
) -> tuple[float | None, str]:
    lo, hi = -0.5, 1.0
    f = lambda g: dcf_per_share(base_cf, g, r, vcfg, net_cash, shares) - price
    if f(lo) > 0:
        return lo, "below -50%/yr"
    if f(hi) < 0:
        return hi, "above 100%/yr"
    for _ in range(100):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2, ""


def graham_formula(eps: float | None, growth: float | None, aaa_pct: float) -> float | None:
    """Graham's revised formula (1974): V = EPS x (8.5 + 2g) x 4.4 / Y, g and Y in percent."""
    if not finite(eps) or eps <= 0 or not finite(growth) or aaa_pct <= 0:
        return None
    return eps * (8.5 + 2 * growth * 100) * 4.4 / aaa_pct


def graham_number(eps: float | None, bvps: float | None) -> float | None:
    if not finite(eps) or not finite(bvps) or eps <= 0 or bvps <= 0:
        return None
    return math.sqrt(22.5 * eps * bvps)


def effective_tax_rate(annual: pd.DataFrame, default: float) -> float:
    rates = (annual["income_tax"] / annual["pretax_income"]).replace([np.inf, -np.inf], np.nan)
    rates = tail(rates[(annual["pretax_income"] > 0)], 5)
    rates = rates[(rates > 0) & (rates < 0.6)]
    return clamp(float(rates.median()), 0.10, 0.35) if not rates.empty else default


# --------------------------------------------------------------------------------------
# Growth inputs
# --------------------------------------------------------------------------------------
def growth_inputs(b: DataBundle, cfg: dict[str, Any]) -> dict[str, float | None]:
    a = b.annual
    cash_col = "owner_fcf"
    eps_g, _ = smoothed_cagr(a["core_eps"], 9)
    rev_g, _ = point_cagr(a["revenue"], 5)
    fcf_g, _ = smoothed_cagr(a[cash_col], 5)
    return {"eps_10y": eps_g, "revenue_5y": rev_g, "fcf_5y": fcf_g}


def unlevered(frame: pd.DataFrame | pd.Series, col: str, tax_rate: float):
    """Cash flow to all capital providers: operating cash flow under US GAAP is after interest
    paid, so add back after-tax interest before discounting at WACC and subtracting debt."""
    interest = frame["interest_expense"]
    if isinstance(frame, pd.DataFrame):
        interest = interest.fillna(0)
    elif not finite(interest):
        interest = 0.0
    return frame[col] + interest * (1 - tax_rate)


def base_cash_flow(annual: pd.Series, ttm_value: float | None, has_newer_quarter: bool) -> float | None:
    """Normalised starting cash flow: average of the last three annual figures, using the
    trailing twelve months in place of the oldest year when a newer quarter exists."""
    vals = list(tail(annual, 3).values)
    if has_newer_quarter and finite(ttm_value):
        vals = [*vals[-2:], float(ttm_value)]
    vals = [v for v in vals if finite(v)]
    return float(np.mean(vals)) if vals else None


# --------------------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------------------
def compute_valuation(b: DataBundle, cfg: dict[str, Any]) -> Valuation:
    vcfg = cfg["valuation"]
    notes: list[str] = []
    shares = b.shares
    price = b.price
    ttm = b.ttm
    rf = b.treasury_10y / 100
    ke = discount_rate(b.info.get("beta"), rf, vcfg)
    tax = effective_tax_rate(b.annual, vcfg["default_tax_rate"])
    r = wacc(ke, b.market_cap, ttm.get("total_debt"), ttm.get("interest_expense"), tax, rf, vcfg)
    net_cash = -float(ttm["net_debt"]) if finite(ttm.get("net_debt")) else 0.0
    if b.debt_unknown:
        notes.append("Interest is paid but no debt was found in the filings: fair value may be overstated.")
    growth = growth_inputs(b, cfg)
    cash_col = "owner_fcf"

    # ---- DCF on owner earnings ----
    dcf = bear = bull = implied = None
    implied_note = ""
    g_parts = [g for g in (growth["revenue_5y"], growth["fcf_5y"]) if finite(g)]
    g1 = clamp(float(np.mean(g_parts)), vcfg["growth_floor"], vcfg["growth_cap"]) if g_parts else None
    newer_q = b.ttm_end > b.annual.index[-1]
    base = base_cash_flow(unlevered(b.annual, cash_col, tax), unlevered(ttm, cash_col, tax), newer_q)
    if b.is_financial:
        notes.append("Financial company: cash-flow DCF not meaningful.")
    elif not shares:
        notes.append("Share count unavailable: per-share values not computed.")
    elif not finite(base) or base <= 0:
        notes.append("Owner earnings are negative: DCF not meaningful.")
    elif g1 is None:
        notes.append("Not enough history to estimate growth for the DCF.")
    else:
        dcf = dcf_per_share(base, g1, r, vcfg, net_cash, shares)
        dg, dr = vcfg["scenario_growth_delta"], vcfg["scenario_rate_delta"]
        bear = dcf_per_share(base, g1 - dg, r + dr, vcfg, net_cash, shares)
        r_bull = max(r - dr, vcfg["terminal_growth"] + 0.02)
        bull = dcf_per_share(base, min(g1 + dg, vcfg["growth_cap"] + dg), r_bull, vcfg, net_cash, shares)
        implied, implied_note = reverse_dcf(price, base, r, vcfg, net_cash, shares)

    # ---- Graham formula ----
    eps = ttm.get("core_eps")
    g_graham = growth["eps_10y"]
    g_graham = clamp(g_graham, 0.0, cfg["graham"]["formula_growth_cap"]) if finite(g_graham) else None
    gf = graham_formula(eps, g_graham, b.aaa_yield)

    # ---- Earnings Power Value (no growth) ----
    epv = None
    op_margin = tail(b.annual["operating_income"] / b.annual["revenue"], 5)
    if shares and not b.is_financial and not op_margin.empty and finite(ttm.get("revenue")):
        ebit_norm = float(op_margin.median()) * float(ttm["revenue"])
        if ebit_norm > 0:
            epv = (ebit_norm * (1 - tax) / r + net_cash) / shares

    # ---- Graham number ----
    bvps = div(ttm.get("equity"), shares)
    gn = graham_number(eps, bvps)

    weights = vcfg["weights_financial"] if b.is_financial else vcfg["weights"]
    methods = [
        ValuationMethod(
            "DCF (owner earnings)", _pos(dcf), weights["dcf"],
            f"g={g1:.1%} fading to {vcfg['terminal_growth']:.1%}, WACC={r:.1%}" if g1 is not None else "",
        ),
        ValuationMethod(
            "Graham formula", _pos(gf), weights["graham_formula"],
            f"g={g_graham:.1%}, AAA={b.aaa_yield:.2f}%" if g_graham is not None else "",
        ),
        ValuationMethod("Earnings power (no growth)", _pos(epv), weights["epv"], f"WACC={r:.1%}"),
        ValuationMethod("Graham number", _pos(gn), weights["graham_number"], "√(22.5×EPS×BVPS)"),
    ]  # fmt: skip
    usable = [m for m in methods if m.value is not None and m.weight > 0]
    total_w = sum(m.weight for m in usable)
    fair = sum(m.value * m.weight for m in usable) / total_w if total_w else None
    if fair is None:
        # Fall back to whatever is available, equally weighted.
        vals = [m.value for m in methods if m.value is not None]
        fair = float(np.mean(vals)) if vals else None
        if fair is not None:
            notes.append("Weighted methods unavailable: fair value is a simple average.")
    mos = (fair - price) / fair if fair else None

    mcap = b.market_cap
    ev = mcap + float(ttm["net_debt"]) if mcap and finite(ttm.get("net_debt")) else mcap
    pe_hist = _pe_history(b)
    return Valuation(
        methods=methods,
        fair_value=fair,
        margin_of_safety=mos,
        bear=_pos(bear),
        bull=_pos(bull),
        discount_rate=r,
        cost_of_equity=ke,
        dcf_growth=g1,
        implied_growth=implied,
        implied_growth_note=implied_note,
        historical_growth=growth["fcf_5y"] if finite(growth["fcf_5y"]) else growth["revenue_5y"],
        fcf_yield=div(ttm.get(cash_col), mcap),
        earnings_yield=div(ttm.get("operating_income"), ev),
        pe_now=div(price, eps) if finite(eps) and eps > 0 else None,
        pe_median_10y=float(pe_hist.median()) if not pe_hist.empty else None,
        maintenance_capex_share=b.maint_capex_share,
        notes=notes,
    )


def _pos(x: float | None) -> float | None:
    return float(x) if finite(x) and x > 0 else None


def _pe_history(b: DataBundle) -> pd.Series:
    out = {}
    for fy, eps in b.annual["core_eps"].dropna().items():
        p = b.price_on(fy)
        if p and eps > 0:
            out[fy] = p / eps
    return pd.Series(out, dtype=float).iloc[-10:]
