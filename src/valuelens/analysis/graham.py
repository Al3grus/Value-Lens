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


def dividend_streak(b: DataBundle, today: date | None = None) -> tuple[int, str, int | None]:
    """Consecutive calendar years with a dividend, ending with the last complete year.

    Returns (streak, source, window): ``window`` is how many years the source covers up to that
    year when the source is the cash-flow statements (a streak as long as the window may well
    continue further back), or None for a full dividend history."""
    today = today or date.today()
    last_full = today.year - 1
    window = None
    if b.dividends is not None and not b.dividends.empty:
        paid_years = set(b.dividends[b.dividends > 0].index.year)
        source = "dividend history"
    else:
        paid = b.annual["dividends_paid"]
        paid_years = {ts.year for ts, v in paid.items() if finite(v) and v > 0}
        window = sum(ts.year <= last_full for ts in paid.index)
        source = "cash-flow statements"
    streak, year = 0, last_full
    while year in paid_years:
        streak += 1
        year -= 1
    return streak, source, window


def _leverage(key: str, label: str, num: float | None, den: float | None, bar: float) -> Criterion:
    ratio = div(num, den)
    if ratio is None or den <= 0:
        return Criterion.na(key, label, "No balance-sheet data", "safety")
    return Criterion.check(key, label, ratio >= bar, pct(ratio), ratio, "safety", threshold=bar)


def graham_scorecard(
    b: DataBundle, val: Valuation, cfg: dict[str, Any], today: date | None = None
) -> Scorecard:
    """``today`` (default: the real date) fixes the last complete year for the dividend record."""
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
        crit.append(
            Criterion.check("size", label, rev >= threshold, detail, rev, "safety", threshold=threshold)
        )
    elif finite(rev):
        crit.append(Criterion.na("size", label, f"No USD/{b.currency} rate", "safety"))
    else:
        crit.append(Criterion.na("size", label, "No revenue data", "safety"))

    # 2 & 3. Strong financial condition
    cr = div(ttm.get("current_assets"), ttm.get("current_liabilities"))
    cr_label = f"Current ratio >= {g['min_current_ratio']:.1f}"
    if b.is_financial:
        # A bank's balance sheet has no current ratio or working capital: "strong financial
        # condition" becomes equity, and tangible equity, against total assets.
        bar = g["min_equity_to_assets"]
        crit.append(_leverage("equity_assets", f"Equity/assets >= {bar:.0%}", ttm.get("equity"), ttm.get("total_assets"), bar))  # fmt: skip
        bar = g["min_tangible_equity_to_assets"]
        crit.append(_leverage("tang_equity_assets", f"Tangible equity/assets >= {bar:.0%}", ttm.get("tangible_equity"), ttm.get("total_assets"), bar))  # fmt: skip
    elif b.is_utility:
        crit.append(Criterion.nm("current_ratio", cr_label, "n/m for utilities", "safety"))
        de = div(ttm.get("total_debt"), ttm.get("equity"))
        label = "Debt <= 2x equity (utility)"
        if de is None or ttm.get("equity", 0) <= 0:
            crit.append(Criterion.na("ltd_equity", label, "No equity data", "safety"))
        else:
            crit.append(
                Criterion.check("ltd_equity", label, de <= 2, f"{de:.2f}x", de, "safety", threshold=2.0)
            )
    else:
        if cr is None:
            crit.append(Criterion.na("current_ratio", cr_label, "No current assets/liabilities", "safety"))
        else:
            bar = g["min_current_ratio"]
            crit.append(Criterion.check("current_ratio", cr_label, cr >= bar, f"{cr:.2f}", cr, "safety", threshold=bar))  # fmt: skip
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
    streak, source, window = dividend_streak(b, today)
    label = f"Dividends uninterrupted ({d_req}y)"
    from_statements = " (from statements)" if source != "dividend history" and streak else ""
    if streak >= d_req:
        crit.append(Criterion.check("dividends", label, True, f"{streak}/{d_req}y{from_statements}", streak, "safety", threshold=d_req))  # fmt: skip
    elif window and streak >= window:
        # Paid in every year the statements cover: the record may well be longer than the data.
        detail = f"at least {streak}y: statements cover {window}y only"
        crit.append(Criterion.na("dividends", label, detail, "safety"))
    else:
        detail = "No dividends" if streak == 0 else f"{streak}/{d_req}y{from_statements}"
        crit.append(Criterion.check("dividends", label, False, detail, streak, "safety", threshold=d_req))

    # 6. Earnings growth: 3-year averages at both ends of the window. Graham's +33% is for
    # averages whose midpoints are 7 years apart; a shorter record gets the same yearly bar.
    n_win, n_min = int(g["eps_growth_years"]), int(g["eps_growth_min_years"])
    eps = tail(a["core_eps"], n_win)
    label = f"EPS growth >= {g['min_eps_growth']:.0%} ({n_win}y, 3y avgs)"
    if len(eps) < n_min:
        crit.append(Criterion.na("eps_growth", label, f"{len(eps)}y of data only"))
    else:
        start, end = eps.iloc[:3].mean(), eps.iloc[-3:].mean()
        span = f"{_fy(eps.index[0])}-{eps.index[2].year % 100:02d} -> {_fy(eps.index[-3])}-{eps.index[-1].year % 100:02d}"
        gap, full_gap = len(eps) - 3, n_win - 3
        required = (1 + g["min_eps_growth"]) ** (gap / full_gap) - 1
        if start <= 0:
            crit.append(Criterion.na("eps_growth", label, f"negative starting EPS ({span})"))
        else:
            growth = end / start - 1
            detail = f"{pct(growth)} ({span})"
            if gap < full_gap:
                detail += f"; bar {pct(required)} over {gap}y"
            crit.append(Criterion.check("eps_growth", label, growth >= required, detail, growth, threshold=required))  # fmt: skip

    # 7. Moderate P/E on 3-year average earnings
    eps3 = tail(a["core_eps"], 3)
    label = f"P/E <= {g['max_pe_3y']:.0f} (3y avg EPS)"
    pe3 = None
    if len(eps3) < 3:
        crit.append(Criterion.na("pe3", label, "Insufficient EPS history", "valuation"))
    elif eps3.mean() <= 0:
        crit.append(Criterion.check("pe3", label, False, "Negative average EPS", None, "valuation", threshold=g["max_pe_3y"]))  # fmt: skip
    else:
        pe3 = b.price / eps3.mean()
        crit.append(Criterion.check("pe3", label, pe3 <= g["max_pe_3y"], f"{pe3:.1f}", pe3, "valuation", threshold=g["max_pe_3y"]))  # fmt: skip

    # 8. Moderate price-to-assets
    label = f"P/B <= {g['max_pb']} or P/E x P/B <= {g['max_pe_x_pb']}"
    bvps = div(ttm.get("common_equity"), b.shares)
    if bvps is None:
        crit.append(Criterion.na("pb", label, "No book value", "valuation"))
    elif bvps <= 0:
        crit.append(Criterion.check("pb", label, False, "Negative book value", None, "valuation", threshold=g["max_pb"]))  # fmt: skip
    else:
        pb = b.price / bvps
        product = pe3 * pb if pe3 else None
        ok = pb <= g["max_pb"] or (product is not None and product <= g["max_pe_x_pb"])
        detail = f"P/B={pb:.2f}" + (f" P/E×P/B={product:.1f}" if product else "")
        crit.append(Criterion.check("pb", label, ok, detail, pb, "valuation", threshold=g["max_pb"]))

    card = Scorecard("GRAHAM DEFENSIVE INVESTOR CRITERIA", crit)
    gf = next((m.value for m in val.methods if m.name == "Graham formula"), None)
    gn = next((m.value for m in val.methods if m.name == "Graham number"), None)
    mos = (gf - b.price) / gf if gf else None
    card.extras = {"intrinsic_value": gf, "graham_number": gn, "margin_of_safety": mos}
    card.signal = framework_signal(card.score(), mos, cfg["signals"]["graham"])
    return card
