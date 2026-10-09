"""Benjamin Graham's seven criteria for the defensive investor (The Intelligent Investor, ch. 14)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from ..data.bundle import DataBundle
from ..models import Criterion, Scorecard, Valuation
from .metrics import div, finite, fmt_money, pct, tail
from .signals import framework_signal


def _fy(ts: pd.Timestamp) -> str:
    return f"FY{ts.year}"


def dividend_streak(b: DataBundle, today: date | None = None) -> tuple[int, str]:
    """Consecutive calendar years with a dividend, ending with the last complete year."""
    today = today or date.today()
    last_full = today.year - 1
    if b.dividends is not None and not b.dividends.empty:
        paid_years = set(b.dividends[b.dividends > 0].index.year)
        source = "dividend history"
    else:
        paid = b.annual["dividends_paid"]
        paid_years = {ts.year for ts, v in paid.items() if finite(v) and v > 0}
        source = "cash-flow statements"
    streak, year = 0, last_full
    while year in paid_years:
        streak += 1
        year -= 1
    return streak, source


def graham_scorecard(b: DataBundle, val: Valuation, cfg: dict[str, Any]) -> Scorecard:
    g = cfg["graham"]
    a, ttm = b.annual, b.ttm
    crit: list[Criterion] = []
    min_years = int(g["min_history_years"])

    # 1. Adequate size
    rev = ttm.get("revenue")
    label = f"Revenue >= {fmt_money(g['min_revenue_usd'])}"
    if finite(rev) and b.usd_fx:
        threshold = g["min_revenue_usd"] * b.usd_fx
        detail = fmt_money(rev, b.currency)
        if b.currency != "USD":
            detail += f" (≈ {fmt_money(rev / b.usd_fx)})"
        crit.append(Criterion.check("size", label, rev >= threshold, detail, rev, "safety"))
    elif finite(rev):
        crit.append(Criterion.na("size", label, f"No USD/{b.currency} rate", "safety"))
    else:
        crit.append(Criterion.na("size", label, "No revenue data", "safety"))

    # 2 & 3. Strong financial condition
    cr = div(ttm.get("current_assets"), ttm.get("current_liabilities"))
    cr_label = f"Current ratio >= {g['min_current_ratio']:.1f}"
    if b.is_financial:
        crit.append(Criterion.na("current_ratio", cr_label, "n/m for financials", "safety"))
        crit.append(Criterion.na("ltd_nca", "LT debt <= net current assets", "n/m for financials", "safety"))
    elif b.is_utility:
        crit.append(Criterion.na("current_ratio", cr_label, "n/m for utilities", "safety"))
        de = div(ttm.get("total_debt"), ttm.get("equity"))
        if de is None or ttm.get("equity", 0) <= 0:
            crit.append(Criterion.na("ltd_nca", "Debt <= 2x equity (utility)", "No equity data", "safety"))
        else:
            crit.append(
                Criterion.check("ltd_nca", "Debt <= 2x equity (utility)", de <= 2, f"{de:.2f}x", de, "safety")
            )
    else:
        if cr is None:
            crit.append(Criterion.na("current_ratio", cr_label, "No current assets/liabilities", "safety"))
        else:
            crit.append(
                Criterion.check(
                    "current_ratio", cr_label, cr >= g["min_current_ratio"], f"{cr:.2f}", cr, "safety"
                )
            )
        nca = (ttm.get("current_assets") or float("nan")) - (ttm.get("current_liabilities") or float("nan"))
        ltd = ttm.get("lt_debt")
        if b.debt_unknown:
            crit.append(Criterion.na("ltd_nca", "LT debt <= net current assets", "Interest paid but no debt found", "safety"))  # fmt: skip
        elif not finite(nca) or not finite(ltd):
            crit.append(Criterion.na("ltd_nca", "LT debt <= net current assets", "No data", "safety"))
        else:
            detail = f"LTD={fmt_money(ltd, b.currency)} NCA={fmt_money(nca, b.currency)}"
            if not b.debt_data_found:
                detail += " (no debt reported)"
            crit.append(Criterion.check("ltd_nca", "LT debt <= net current assets", ltd <= nca, detail, ltd - nca, "safety"))  # fmt: skip

    # 4. Earnings stability
    n_req = int(g["earnings_years"])
    ni = tail(a["net_income"], n_req)
    label = f"No losses ({n_req}y)"
    if len(ni) < min_years:
        crit.append(Criterion.na("stability", label, f"{len(ni)}y of data only", "safety"))
    else:
        losses = int((ni <= 0).sum())
        detail = f"{losses} losses in {len(ni)}y"
        if len(ni) < n_req:
            detail += f" (only {len(ni)}y available)"
        crit.append(Criterion.check("stability", label, losses == 0, detail, losses, "safety"))

    # 5. Dividend record
    d_req = int(g["dividend_years"])
    streak, source = dividend_streak(b)
    label = f"Dividends uninterrupted ({d_req}y)"
    detail = "No dividends" if streak == 0 else f"{streak}/{d_req}y"
    if source != "dividend history" and streak:
        detail += " (from statements)"
    crit.append(Criterion.check("dividends", label, streak >= d_req, detail, streak, "safety"))

    # 6. Earnings growth (3-year averages at both ends of ~10 years)
    eps = tail(a["core_eps"], 10)
    label = f"EPS growth >= {g['min_eps_growth']:.0%} (10y, 3y avgs)"
    if len(eps) < 6:
        crit.append(Criterion.na("eps_growth", label, f"{len(eps)}y of data only"))
    else:
        start, end = eps.iloc[:3].mean(), eps.iloc[-3:].mean()
        span = f"{_fy(eps.index[0])}-{eps.index[2].year % 100:02d} -> {_fy(eps.index[-3])}-{eps.index[-1].year % 100:02d}"
        if start <= 0:
            crit.append(Criterion.na("eps_growth", label, f"negative starting EPS ({span})"))
        else:
            growth = end / start - 1
            crit.append(Criterion.check("eps_growth", label, growth >= g["min_eps_growth"], f"{pct(growth)} ({span})", growth))  # fmt: skip

    # 7. Moderate P/E on 3-year average earnings
    eps3 = tail(a["core_eps"], 3)
    label = f"P/E <= {g['max_pe_3y']:.0f} (3y avg EPS)"
    pe3 = None
    if len(eps3) < 3:
        crit.append(Criterion.na("pe3", label, "Insufficient EPS history", "valuation"))
    elif eps3.mean() <= 0:
        crit.append(Criterion.check("pe3", label, False, "Negative average EPS", None, "valuation"))
    else:
        pe3 = b.price / eps3.mean()
        crit.append(Criterion.check("pe3", label, pe3 <= g["max_pe_3y"], f"{pe3:.1f}", pe3, "valuation"))

    # 8. Moderate price-to-assets
    label = f"P/B <= {g['max_pb']} or P/E x P/B <= {g['max_pe_x_pb']}"
    bvps = div(ttm.get("equity"), b.shares)
    if bvps is None:
        crit.append(Criterion.na("pb", label, "No book value", "valuation"))
    elif bvps <= 0:
        crit.append(Criterion.check("pb", label, False, "Negative book value", None, "valuation"))
    else:
        pb = b.price / bvps
        product = pe3 * pb if pe3 else None
        ok = pb <= g["max_pb"] or (product is not None and product <= g["max_pe_x_pb"])
        detail = f"P/B={pb:.2f}" + (f" P/E×P/B={product:.1f}" if product else "")
        crit.append(Criterion.check("pb", label, ok, detail, pb, "valuation"))

    card = Scorecard("GRAHAM DEFENSIVE INVESTOR CRITERIA", crit)
    gf = next((m.value for m in val.methods if m.name == "Graham formula"), None)
    gn = next((m.value for m in val.methods if m.name == "Graham number"), None)
    mos = (gf - b.price) / gf if gf else None
    card.extras = {"intrinsic_value": gf, "graham_number": gn, "margin_of_safety": mos}
    card.signal = framework_signal(card.ratio(), mos, cfg["signals"]["graham"])
    return card
