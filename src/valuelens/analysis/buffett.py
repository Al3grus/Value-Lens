"""Buffett-style business quality checks plus two price-discipline checks.

Sources for thresholds: Buffett's shareholder letters (owner earnings 1986, the "$1 retained
-> $1 of market value" test 1984), and Mary Buffett & David Clark, "Warren Buffett and the
Interpretation of Financial Statements" (gross margin >= 40%, capex < 50% of earnings).

Banks and insurers skip the cash-flow, margin and debt tests (not meaningful) and are judged on
return on tangible equity, book-value growth and price to tangible book instead.
"""

from __future__ import annotations

from typing import Any

from ..data.bundle import DataBundle
from ..models import Criterion, Scorecard, Valuation
from .metrics import cagr, div, finite, fmt_money, pct, point_cagr, smoothed_endpoints, tail
from .returns import ratio_series, roe_series, roic_series, rotce_series
from .signals import framework_signal

NM_FIN = "n/m for financials"
NO_CAPEX = "Capex not found in filings"


def buffett_scorecard(b: DataBundle, val: Valuation, cfg: dict[str, Any]) -> Scorecard:
    c = cfg["buffett"]
    vcfg = cfg["valuation"]
    a, ttm = b.annual, b.ttm
    fin = b.is_financial
    crit: list[Criterion] = []
    a10 = a.iloc[-10:]
    min_hist, min_med = int(c["min_history_years"]), int(c["min_median_years"])

    # 1. Consistent free cash flow
    label = f"Positive FCF in >= {c['fcf_positive_ratio']:.0%} of years (10y)"
    fcf = a10["fcf"].dropna()
    if fin:
        crit.append(Criterion.nm("fcf_positive", label, NM_FIN))
    elif not b.capex_data_found:
        crit.append(Criterion.na("fcf_positive", label, NO_CAPEX))
    elif len(fcf) < min_hist:
        crit.append(Criterion.na("fcf_positive", label, f"{len(fcf)}y of data only"))
    else:
        pos = int((fcf > 0).sum())
        ok = pos / len(fcf) >= c["fcf_positive_ratio"]
        crit.append(Criterion.check("fcf_positive", label, ok, f"{pos}/{len(fcf)}y", pos, threshold=c["fcf_positive_ratio"]))  # fmt: skip

    # 2. Return on equity
    label = f"ROE >= {c['min_roe']:.0%} (10y median)"
    roe = tail(roe_series(a), 10)
    if len(roe) < min_med:
        neg = bool((a10["equity"] <= 0).any())
        if neg:  # buybacks beyond book value: ROE is undefined, ROIC still judges the returns
            crit.append(Criterion.nm("roe", label, "negative equity (buybacks)"))
        else:
            crit.append(Criterion.na("roe", label, "Insufficient data"))
    else:
        m = float(roe.median())
        detail = f"{pct(m)} (latest {pct(roe.iloc[-1])})"
        crit.append(Criterion.check("roe", label, m >= c["min_roe"], detail, m, threshold=c["min_roe"]))

    # 3. Return on invested capital (banks: on tangible common equity)
    if fin:
        label = f"ROTCE >= {c['min_rotce']:.0%} (10y median)"
        rotce = tail(rotce_series(a), 10)
        if len(rotce) < min_med:
            crit.append(Criterion.na("rotce", label, "Insufficient data"))
        else:
            m = float(rotce.median())
            detail = f"{pct(m)} (latest {pct(rotce.iloc[-1])})"
            crit.append(
                Criterion.check("rotce", label, m >= c["min_rotce"], detail, m, threshold=c["min_rotce"])
            )
    else:
        label = f"ROIC >= {c['min_roic']:.0%} (10y median)"
        roic = tail(
            roic_series(a, vcfg["default_tax_rate"], vcfg["tax_rate_floor"], vcfg["tax_rate_cap"]), 10
        )
        if len(roic) < min_med:
            crit.append(Criterion.na("roic", label, "Insufficient data"))
        else:
            m = float(roic.median())
            detail = f"{pct(m)} (latest {pct(roic.iloc[-1])})"
            crit.append(
                Criterion.check("roic", label, m >= c["min_roic"], detail, m, threshold=c["min_roic"])
            )

    # 4. Interest coverage
    label = f"Interest coverage >= {c['min_interest_coverage']:.0f}x"
    ebit, interest, debt = ttm.get("operating_income"), ttm.get("interest_expense"), ttm.get("total_debt")
    if fin:
        crit.append(Criterion.nm("interest_cov", label, NM_FIN))
    elif not finite(ebit):
        crit.append(Criterion.na("interest_cov", label, "No EBIT"))
    elif finite(interest) and interest > 0:
        cov = ebit / interest
        crit.append(Criterion.check("interest_cov", label, cov >= c["min_interest_coverage"], f"{cov:.1f}x", cov, threshold=c["min_interest_coverage"]))  # fmt: skip
    elif not b.debt_data_found:
        # Neither a debt nor an interest concept matched: unknown, not debt-free.
        crit.append(Criterion.na("interest_cov", label, "No debt or interest found in filings"))
    elif not finite(debt) or debt <= c["negligible_debt_to_ebit"] * max(abs(ebit), 1):
        crit.append(Criterion.check("interest_cov", label, ebit > 0, "No meaningful debt", None))
    else:
        crit.append(Criterion.na("interest_cov", label, "Interest expense not reported"))

    # 5. Net debt / EBITDA
    label = f"Net debt/EBITDA <= {c['max_net_debt_ebitda']:.0f}"
    net_debt, ebitda = ttm.get("net_debt"), ttm.get("ebitda")
    if fin:
        crit.append(Criterion.nm("nd_ebitda", label, NM_FIN))
    elif b.debt_unknown:
        crit.append(Criterion.na("nd_ebitda", label, "Interest paid but no debt found in filings"))
    elif finite(net_debt) and net_debt <= 0:
        crit.append(Criterion.check("nd_ebitda", label, True, f"Net cash {fmt_money(-net_debt, b.currency)}", net_debt))  # fmt: skip
    else:
        proxy = ""
        if not finite(ebitda) and finite(ebit):
            ebitda, proxy = ebit, " (EBIT proxy)"
        if not finite(ebitda) or not finite(net_debt):
            crit.append(Criterion.na("nd_ebitda", label, "No data"))
        elif ebitda <= 0:
            crit.append(Criterion.check("nd_ebitda", label, False, "Negative EBITDA", None))
        else:
            x = net_debt / ebitda
            crit.append(Criterion.check("nd_ebitda", label, x <= c["max_net_debt_ebitda"], f"{x:.2f}x{proxy}", x, threshold=c["max_net_debt_ebitda"]))  # fmt: skip

    # 6. Gross margin (pricing power)
    label = f"Gross margin >= {c['min_gross_margin']:.0%} (10y median)"
    gm = ratio_series(a10["gross_profit"], a10["revenue"]).dropna()
    gm = gm[(gm > -1) & (gm <= 1)]
    if fin:
        crit.append(Criterion.nm("gross_margin", label, NM_FIN))
    elif len(gm) < min_med:
        crit.append(Criterion.na("gross_margin", label, "Gross profit not reported"))
    else:
        m = float(gm.median())
        crit.append(Criterion.check("gross_margin", label, m >= c["min_gross_margin"], pct(m), m, threshold=c["min_gross_margin"]))  # fmt: skip

    # 7. Operating margin
    label = f"Operating margin >= {c['min_op_margin']:.0%} (10y median)"
    om = ratio_series(a10["operating_income"], a10["revenue"]).dropna()
    om = om[(om > -2) & (om <= 1)]
    if fin:
        crit.append(Criterion.nm("op_margin", label, NM_FIN))
    elif len(om) < min_med:
        crit.append(Criterion.na("op_margin", label, "Insufficient data"))
    else:
        m = float(om.median())
        crit.append(Criterion.check("op_margin", label, m >= c["min_op_margin"], pct(m), m, threshold=c["min_op_margin"]))  # fmt: skip

    # 8. Revenue growth
    label = f"Revenue CAGR >= {c['min_revenue_cagr']:.0%} (5y)"
    g, span = point_cagr(a["revenue"], 5)
    if g is None or span < 3:
        crit.append(Criterion.na("rev_cagr", label, "Insufficient data"))
    else:
        detail = pct(g) + (f" ({span}y)" if span < 5 else "")
        crit.append(Criterion.check("rev_cagr", label, g >= c["min_revenue_cagr"], detail, g, threshold=c["min_revenue_cagr"]))  # fmt: skip

    # 9. Owner-earnings growth
    label = f"Owner FCF CAGR >= {c['min_fcf_cagr']:.0%} (5y, 3y avgs)"
    start, end, span = smoothed_endpoints(a["owner_fcf"], 5)
    if fin:
        crit.append(Criterion.nm("fcf_cagr", label, NM_FIN))
    elif not b.capex_data_found:
        crit.append(Criterion.na("fcf_cagr", label, NO_CAPEX))
    elif span < 3:
        crit.append(Criterion.na("fcf_cagr", label, "Insufficient data"))
    elif end <= 0:
        detail = f"Owner FCF negative (3y avg {fmt_money(end, b.currency)})"
        crit.append(Criterion.check("fcf_cagr", label, False, detail, None, threshold=c["min_fcf_cagr"]))
    elif start <= 0:
        crit.append(Criterion.na("fcf_cagr", label, "Turned positive from a negative base"))
    else:
        g = cagr(start, end, span)
        detail = pct(g) + (f" ({span}y)" if span < 5 else "")
        crit.append(Criterion.check("fcf_cagr", label, g >= c["min_fcf_cagr"], detail, g, threshold=c["min_fcf_cagr"]))  # fmt: skip

    # 10. No dilution
    label = f"Share count not rising (5y, <= +{c['max_dilution_5y']:.0%})"
    sh = a["shares"].dropna()
    if len(sh) < int(c["min_share_years"]):
        crit.append(Criterion.na("dilution", label, f"{len(sh)}y of data only"))
    else:
        span = min(5, len(sh) - 1)
        change = sh.iloc[-1] / sh.iloc[-1 - span] - 1
        detail = f"{change:+.1%} over {span}y"
        crit.append(Criterion.check("dilution", label, change <= c["max_dilution_5y"], detail, change, threshold=c["max_dilution_5y"]))  # fmt: skip

    # 11. The one-dollar test (Buffett 1984 letter)
    crit.append(_one_dollar_test(b, int(c["one_dollar_years"])))

    # 12. Capital intensity
    label = f"Capex <= {c['max_capex_to_net_income']:.0%} of net income (10y)"
    if fin:
        crit.append(Criterion.nm("capex_ni", label, NM_FIN))
    elif not b.capex_data_found:
        crit.append(Criterion.na("capex_ni", label, NO_CAPEX))
    else:
        both = a10[["capex", "net_income"]].dropna()  # same years on both sides of the ratio
        capex, ni = both["capex"].sum(min_count=3), both["net_income"].sum(min_count=3)
        if not finite(capex) or not finite(ni):
            crit.append(Criterion.na("capex_ni", label, "Insufficient data"))
        elif ni <= 0:
            crit.append(Criterion.check("capex_ni", label, False, "Cumulative losses", None))
        else:
            x = capex / ni
            crit.append(Criterion.check("capex_ni", label, x <= c["max_capex_to_net_income"], pct(x, 0), x, threshold=c["max_capex_to_net_income"]))  # fmt: skip

    # 13. EV / FCF
    label = f"EV/FCF <= {c['max_ev_fcf']:.0f}"
    mcap = b.market_cap
    fcf_ttm = ttm.get("fcf")
    if fin:
        crit.append(Criterion.nm("ev_fcf", label, NM_FIN, "valuation"))
    elif not b.capex_data_found:
        crit.append(Criterion.na("ev_fcf", label, NO_CAPEX, "valuation"))
    elif not mcap or not finite(fcf_ttm) or not finite(ttm.get("net_debt")):
        crit.append(Criterion.na("ev_fcf", label, "No data", "valuation"))
    elif fcf_ttm <= 0:
        crit.append(Criterion.check("ev_fcf", label, False, "Negative FCF", None, "valuation", threshold=c["max_ev_fcf"]))  # fmt: skip
    else:
        x = (mcap + ttm["net_debt"]) / fcf_ttm
        crit.append(Criterion.check("ev_fcf", label, x <= c["max_ev_fcf"], f"{x:.1f}", x, "valuation", threshold=c["max_ev_fcf"]))  # fmt: skip

    # 14. P/E
    label = f"P/E <= {c['max_pe']:.0f} (TTM, core EPS)"
    eps = ttm.get("core_eps")
    if not finite(eps):
        crit.append(Criterion.na("pe", label, "No EPS", "valuation"))
    elif eps <= 0:
        crit.append(Criterion.check("pe", label, False, "Negative earnings", None, "valuation", threshold=c["max_pe"]))  # fmt: skip
    else:
        pe = b.price / eps
        crit.append(Criterion.check("pe", label, pe <= c["max_pe"], f"{pe:.1f}", pe, "valuation", threshold=c["max_pe"]))  # fmt: skip

    if fin:
        # 15. Book value per share growth: for a bank, the equivalent of owner-earnings growth.
        label = f"Book value/share CAGR >= {c['min_bvps_cagr']:.0%} (5y)"
        bvps = (a["common_equity"] / a["shares"]).where(a["shares"] > 0)
        g, span = point_cagr(bvps, 5)
        if g is None or span < 3:
            crit.append(Criterion.na("bvps_growth", label, "Insufficient data"))
        else:
            detail = pct(g) + (f" ({span}y)" if span < 5 else "")
            crit.append(Criterion.check("bvps_growth", label, g >= c["min_bvps_cagr"], detail, g, threshold=c["min_bvps_cagr"]))  # fmt: skip

        # 16. Price to tangible book value
        label = f"P/TBV <= {c['max_p_tbv']:.1f}"
        tbvps = div(ttm.get("tangible_equity"), b.shares)
        if tbvps is None:
            crit.append(Criterion.na("p_tbv", label, "No tangible book value", "valuation"))
        elif tbvps <= 0:
            crit.append(Criterion.check("p_tbv", label, False, "Negative tangible book value", None, "valuation", threshold=c["max_p_tbv"]))  # fmt: skip
        else:
            x = b.price / tbvps
            crit.append(Criterion.check("p_tbv", label, x <= c["max_p_tbv"], f"{x:.2f}", x, "valuation", threshold=c["max_p_tbv"]))  # fmt: skip

    card = Scorecard("BUFFETT QUALITY CRITERIA", crit)
    dcf = next((m.value for m in val.methods if m.name.startswith("DCF")), None)
    iv = dcf or val.fair_value
    mos = (iv - b.price) / iv if iv else None
    card.extras = {
        "intrinsic_value": iv,
        "intrinsic_method": "DCF (owner earnings)" if dcf else "blended",
        "margin_of_safety": mos,
        "quality_ratio": card.score(("quality",)),
    }
    card.signal = framework_signal(card.score(("quality",)), mos, cfg["signals"]["buffett"])
    return card


def _one_dollar_test(b: DataBundle, years: int) -> Criterion:
    """Over ``years`` years, did each dollar of retained earnings create >= $1 of market value?"""
    label = f"$1 retained -> >= $1 market value ({years}y)"
    a = b.annual
    if len(a) < years + 1:
        return Criterion.na("one_dollar", label, f"{len(a)}y of data only")
    end, start = a.index[-1], a.index[-years - 1]
    p_end, p_start = b.price_on(end), b.price_on(start)
    sh_end, sh_start = a["shares"].iloc[-1], a["shares"].iloc[-years - 1]
    if not all(finite(x) and x > 0 for x in (p_end, p_start, sh_end, sh_start)):
        return Criterion.na("one_dollar", label, "No price/share history")
    # Money actually kept in the business: earnings minus dividends and net buybacks.
    payout = a["dividends_paid"] + a["buybacks"] - a["stock_issued"]
    retained = (a["net_income"] - payout).iloc[-years:].sum()
    if not finite(retained):
        return Criterion.na("one_dollar", label, "Insufficient data")
    if retained <= 0:  # nothing kept, nothing to test
        return Criterion.nm("one_dollar", label, "All earnings returned to shareholders")
    created = p_end * sh_end - p_start * sh_start
    ratio = div(created, retained)
    return Criterion.check(
        "one_dollar", label, created >= retained, f"$1 -> ${ratio:.2f}", ratio, threshold=1.0
    )
