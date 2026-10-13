"""Intrinsic value: owner-earnings DCF, Graham formula, Earnings Power Value, Graham number and,
for banks and insurers, an excess-return (residual income) value; plus a reverse DCF (what growth
does today's price imply?) and a DCF sensitivity grid."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ..data.bundle import DataBundle
from ..models import Valuation, ValuationMethod
from .metrics import clamp, div, finite, fmt_money, point_cagr, smoothed_cagr, tail
from .returns import roe_series


# --------------------------------------------------------------------------------------
# Building blocks (pure functions, unit-tested)
# --------------------------------------------------------------------------------------
def rate_floor(vcfg: dict[str, Any]) -> float:
    """No discount rate goes below the configured minimum or too close to terminal growth."""
    return max(vcfg["min_discount_rate"], vcfg["terminal_growth"] + vcfg["min_rate_over_terminal"])


def discount_rate(beta: float | None, rf: float, vcfg: dict[str, Any]) -> float:
    b = beta if finite(beta) else 1.0
    b = clamp(b, vcfg["beta_floor"], vcfg["beta_cap"])
    return max(rf + b * vcfg["equity_risk_premium"], rate_floor(vcfg))


def wacc(
    cost_of_equity: float,
    market_cap: float | None,
    debt: float | None,
    interest: float | None,
    tax_rate: float,
    rf: float,
    vcfg: dict[str, Any],
) -> float:
    """Weighted average cost of capital at market weights. Cost of debt = interest / debt, kept
    within the configured spreads over the risk-free rate so stale low-coupon debt does not
    flatter it."""
    floor = rate_floor(vcfg)
    if not finite(market_cap) or market_cap <= 0 or not finite(debt) or debt <= 0:
        return max(cost_of_equity, floor)
    kd = interest / debt if finite(interest) and interest > 0 else rf + vcfg["cost_of_debt_default_spread"]
    kd = clamp(kd, rf + vcfg["cost_of_debt_min_spread"], rf + vcfg["cost_of_debt_max_spread"])
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


def sensitivity_grid(
    base_cf: float, g1: float, r: float, vcfg: dict[str, Any], net_cash: float, shares: float
) -> tuple[list[float], list[float], list[list[float | None]]]:
    """DCF value per share for discount rates (rows) and growth rates (columns) around the base case."""
    floor = rate_floor(vcfg)
    rates: list[float] = []
    for d in vcfg["sensitivity_rate_steps"]:  # the floor can make two steps coincide: show one row
        rate = max(r + d, floor)
        if rate not in rates:
            rates.append(rate)
    growths = [g1 + d for d in vcfg["sensitivity_growth_steps"]]
    grid = [[_pos(dcf_per_share(base_cf, g, rr, vcfg, net_cash, shares)) for g in growths] for rr in rates]
    return rates, growths, grid


def graham_formula(eps: float | None, growth: float | None, aaa_pct: float) -> float | None:
    """Graham's revised formula (1974): V = EPS x (8.5 + 2g) x 4.4 / Y, g and Y in percent."""
    if not finite(eps) or eps <= 0 or not finite(growth) or aaa_pct <= 0:
        return None
    return eps * (8.5 + 2 * growth * 100) * 4.4 / aaa_pct


def graham_number(eps: float | None, bvps: float | None) -> float | None:
    if not finite(eps) or not finite(bvps) or eps <= 0 or bvps <= 0:
        return None
    return math.sqrt(22.5 * eps * bvps)


def excess_return_value(bvps: float | None, roe: float | None, ke: float, g: float) -> float | None:
    """Residual-income value for a bank or insurer, where cash flow is not meaningful: book value
    per share plus the capitalised excess of ROE over the cost of equity,
    V = B x (1 + (ROE - ke) / (ke - g))."""
    if not finite(bvps) or bvps <= 0 or not finite(roe) or not finite(ke) or ke <= g:
        return None
    return bvps * (1 + (roe - ke) / (ke - g))


def effective_tax_rate(annual: pd.DataFrame, default: float, floor: float = 0.10, cap: float = 0.35) -> float:
    rates = (annual["income_tax"] / annual["pretax_income"]).replace([np.inf, -np.inf], np.nan)
    rates = tail(rates[(annual["pretax_income"] > 0)], 5)
    rates = rates[(rates > 0) & (rates < 0.6)]
    return clamp(float(rates.median()), floor, cap) if not rates.empty else default


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
    """Cash flow to all capital providers. Operating cash flow under US GAAP is after interest
    paid and includes interest earned on cash, so add back after-tax interest expense and take
    out after-tax interest income before discounting at WACC and adding net cash: the cash itself
    is counted at face value, so its yield must not be counted a second time."""

    def part(key: str):
        if key not in frame:
            return 0.0
        v = frame[key]
        if isinstance(frame, pd.DataFrame):
            return v.fillna(0)
        return float(v) if finite(v) else 0.0

    return frame[col] + (part("interest_expense") - part("interest_income")) * (1 - tax_rate)


def base_cash_flow(
    annual: pd.Series, ttm_value: float | None, has_newer_quarter: bool, periods: int = 1
) -> float | None:
    """Starting cash flow: average of the latest ``periods`` twelve-month figures, the trailing
    twelve months first when a newer quarter exists, then the annual figures."""
    vals = list(tail(annual, periods).values)
    if has_newer_quarter and finite(ttm_value):
        vals = [*vals, float(ttm_value)][-periods:]
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
    tax = effective_tax_rate(b.annual, vcfg["default_tax_rate"], vcfg["tax_rate_floor"], vcfg["tax_rate_cap"])
    r = wacc(ke, b.market_cap, ttm.get("total_debt"), ttm.get("interest_expense"), tax, rf, vcfg)
    # Equity bridge: enterprise value plus net cash, less the claims that rank ahead of common
    # shareholders (minority interest, preferred stock).
    claims = sum(float(ttm[k]) for k in ("minority_interest", "preferred_stock") if _pos(ttm.get(k)))
    net_cash = (-float(ttm["net_debt"]) if finite(ttm.get("net_debt")) else 0.0) - claims
    if claims and b.market_cap and claims > 0.01 * b.market_cap:
        notes.append(
            f"Minority interest and preferred stock ({fmt_money(claims, b.currency)}) rank ahead of common "
            "shareholders and are deducted from the DCF and earnings-power values."
        )
    if b.debt_unknown:
        notes.append("Interest is paid but no debt was found in the filings: fair value may be overstated.")
    growth = growth_inputs(b, cfg)
    cash_col = "owner_fcf"

    # ---- DCF on owner earnings ----
    dcf = bear = bull = implied = None
    implied_note = ""
    rates: list[float] = []
    growths: list[float] = []
    grid: list[list[float | None]] = []
    g_parts = [g for g in (growth["revenue_5y"], growth["fcf_5y"]) if finite(g)]
    g1 = clamp(float(np.mean(g_parts)), vcfg["growth_floor"], vcfg["growth_cap"]) if g_parts else None
    newer_q = b.ttm_end > b.annual.index[-1]
    base = base_cash_flow(
        unlevered(b.annual, cash_col, tax),
        unlevered(ttm, cash_col, tax),
        newer_q,
        int(vcfg["dcf_base_periods"]),
    )
    if b.is_financial:
        notes.append("Financial company: cash-flow DCF not meaningful.")
    elif not shares:
        notes.append("Share count unavailable: per-share values not computed.")
    elif not finite(base):
        why = " (capital expenditure not found in the filings)" if not b.capex_data_found else ""
        notes.append(f"Owner earnings unavailable{why}: DCF not computed.")
    elif base <= 0:
        notes.append("Owner earnings are negative: DCF not meaningful.")
    elif g1 is None:
        notes.append("Not enough history to estimate growth for the DCF.")
    else:
        dcf = dcf_per_share(base, g1, r, vcfg, net_cash, shares)
        dg, dr = vcfg["scenario_growth_delta"], vcfg["scenario_rate_delta"]
        bear = dcf_per_share(base, g1 - dg, r + dr, vcfg, net_cash, shares)
        r_bull = max(r - dr, rate_floor(vcfg))
        bull = dcf_per_share(base, min(g1 + dg, vcfg["growth_cap"] + dg), r_bull, vcfg, net_cash, shares)
        implied, implied_note = reverse_dcf(price, base, r, vcfg, net_cash, shares)
        rates, growths, grid = sensitivity_grid(base, g1, r, vcfg, net_cash, shares)

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
    bvps = div(ttm.get("common_equity"), shares)
    gn = graham_number(eps, bvps)

    # ---- Excess returns (banks and insurers) ----
    er, er_note = None, ""
    if b.is_financial and shares:
        roe = tail(roe_series(b.annual), 10)
        if len(roe) >= int(cfg["buffett"]["min_median_years"]):
            roe_med = min(float(roe.median()), vcfg["excess_return_roe_cap"])
            er = excess_return_value(bvps, roe_med, ke, vcfg["terminal_growth"])
            er_note = f"ROE={roe_med:.1%}, ke={ke:.1%}, g={vcfg['terminal_growth']:.1%}"

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
    if b.is_financial:
        methods.append(
            ValuationMethod("Excess returns (financials)", _pos(er), weights["excess_return"], er_note)
        )
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
    ebit, owner, cfo = ttm.get("operating_income"), ttm.get(cash_col), ttm.get("cfo")
    dividends = ttm.get("dividends_paid")
    if b.is_financial:  # deposits are not debt and cash flow is not owner earnings: no EV or FCF multiples
        ev = ebit = owner = cfo = None
    pe_hist = _multiple_history(b, "pe")
    ev_ebit_hist = _multiple_history(b, "ev_ebit") if not b.is_financial else pd.Series(dtype=float)
    p_fcf_hist = _multiple_history(b, "p_fcf") if not b.is_financial else pd.Series(dtype=float)
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
        historical_growth_basis="owner earnings" if finite(growth["fcf_5y"]) else "revenue",
        fcf_yield=div(owner, mcap),
        earnings_yield=div(ebit, ev),
        pe_now=div(price, eps) if finite(eps) and eps > 0 else None,
        pe_median_10y=_median(pe_hist),
        ev_ebit_now=div(ev, ebit) if finite(ebit) and ebit > 0 else None,
        ev_ebit_median_10y=_median(ev_ebit_hist),
        p_fcf_now=div(mcap, owner) if finite(owner) and owner > 0 else None,
        p_fcf_median_10y=_median(p_fcf_hist),
        dividend_cover=div(owner, dividends) if finite(dividends) and dividends > 0 else None,
        sbc_to_cfo=div(ttm.get("sbc"), cfo) if finite(cfo) and cfo > 0 else None,
        maintenance_capex_share=b.maint_capex_share,
        sensitivity_rates=rates,
        sensitivity_growths=growths,
        sensitivity=grid,
        notes=notes,
    )


def _pos(x: float | None) -> float | None:
    return float(x) if finite(x) and x > 0 else None


def _median(s: pd.Series) -> float | None:
    return float(s.median()) if not s.empty else None


def _multiple_history(b: DataBundle, kind: str) -> pd.Series:
    """Year-end multiples over the last 10 fiscal years: ``pe`` (price / core EPS), ``ev_ebit``
    (enterprise value / operating income) or ``p_fcf`` (market value / owner earnings)."""
    out = {}
    for fy, row in b.annual.iterrows():
        p = b.price_on(fy)
        if not p:
            continue
        if kind == "pe":
            num, den = p, row["core_eps"]
        else:
            sh = row["shares"]
            if not finite(sh) or sh <= 0:
                continue
            mcap = p * sh
            if kind == "ev_ebit":
                nd = row["net_debt"] if finite(row["net_debt"]) else 0.0
                num, den = mcap + nd, row["operating_income"]
            else:
                num, den = mcap, row["owner_fcf"]
        if finite(num) and finite(den) and den > 0:
            out[fy] = num / den
    return pd.Series(out, dtype=float).iloc[-10:]
