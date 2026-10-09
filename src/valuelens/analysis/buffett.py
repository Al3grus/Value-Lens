"""Buffett-style business quality checks plus two price-discipline checks.

Sources for thresholds: Buffett's shareholder letters (owner earnings 1986, the "$1 retained
-> $1 of market value" test 1984), and Mary Buffett & David Clark, "Warren Buffett and the
Interpretation of Financial Statements" (gross margin >= 40%, capex < 50% of earnings).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..data.bundle import DataBundle
from ..models import Criterion, Scorecard, Valuation
from .metrics import (
    average_balance,
    cagr,
    div,
    finite,
    fmt_money,
    pct,
    point_cagr,
    smoothed_endpoints,
    tail,
)
from .signals import framework_signal

NM_FIN = "n/m for financials"


def _ratio_series(num: pd.Series, den: pd.Series) -> pd.Series:
    r = (num / den).replace([np.inf, -np.inf], np.nan)
    return r[den > 0]


def roe_series(a: pd.DataFrame) -> pd.Series:
    return _ratio_series(a["net_income"], average_balance(a["equity"]).fillna(a["equity"]))


def roic_series(a: pd.DataFrame, default_tax: float) -> pd.Series:
    tax = (a["income_tax"] / a["pretax_income"]).where(a["pretax_income"] > 0)
    tax = tax.clip(0, 0.35).fillna(default_tax)
    nopat = a["operating_income"] * (1 - tax)
    invested = a["equity"] + a["total_debt"] - a["cash_total"]
    return _ratio_series(nopat, average_balance(invested).fillna(invested))


def buffett_scorecard(b: DataBundle, val: Valuation, cfg: dict[str, Any]) -> Scorecard:
    c = cfg["buffett"]
    a, ttm = b.annual, b.ttm
    fin = b.is_financial
    crit: list[Criterion] = []
    a10 = a.iloc[-10:]

    # 1. Consistent free cash flow
    label = "Positive FCF >= 8/10y"
    fcf = a10["fcf"].dropna()
    if fin:
        crit.append(Criterion.na("fcf_positive", label, NM_FIN))
    elif len(fcf) < 5:
        crit.append(Criterion.na("fcf_positive", label, f"{len(fcf)}y of data only"))
    else:
        pos = int((fcf > 0).sum())
        crit.append(Criterion.check("fcf_positive", label, pos / len(fcf) >= c["fcf_positive_ratio"], f"{pos}/{len(fcf)}y", pos))  # fmt: skip

    # 2. Return on equity
    label = f"ROE >= {c['min_roe']:.0%} (10y median)"
    roe = tail(roe_series(a), 10)
    if len(roe) < 3:
        neg = bool((a10["equity"] <= 0).any())
        crit.append(Criterion.na("roe", label, "negative equity (buybacks)" if neg else "Insufficient data"))
    else:
        m = float(roe.median())
        crit.append(
            Criterion.check("roe", label, m >= c["min_roe"], f"{pct(m)} (latest {pct(roe.iloc[-1])})", m)
        )

    # 3. Return on invested capital
    label = f"ROIC >= {c['min_roic']:.0%} (10y median)"
    roic = tail(roic_series(a, cfg["valuation"]["default_tax_rate"]), 10)
    if fin:
        crit.append(Criterion.na("roic", label, NM_FIN))
    elif len(roic) < 3:
        crit.append(Criterion.na("roic", label, "Insufficient data"))
    else:
        m = float(roic.median())
        crit.append(
            Criterion.check("roic", label, m >= c["min_roic"], f"{pct(m)} (latest {pct(roic.iloc[-1])})", m)
        )

    # 4. Interest coverage
    label = f"Interest coverage >= {c['min_interest_coverage']:.0f}x"
    ebit, interest, debt = ttm.get("operating_income"), ttm.get("interest_expense"), ttm.get("total_debt")
    if fin:
        crit.append(Criterion.na("interest_cov", label, NM_FIN))
    elif not finite(ebit):
        crit.append(Criterion.na("interest_cov", label, "No EBIT"))
    elif finite(interest) and interest > 0:
        cov = ebit / interest
        crit.append(
            Criterion.check("interest_cov", label, cov >= c["min_interest_coverage"], f"{cov:.1f}x", cov)
        )
    elif not finite(debt) or debt <= 0.05 * max(abs(ebit), 1):
        crit.append(Criterion.check("interest_cov", label, ebit > 0, "No meaningful debt", None))
    else:
        crit.append(Criterion.na("interest_cov", label, "Interest expense not reported"))

    # 5. Net debt / EBITDA
    label = f"Net debt/EBITDA <= {c['max_net_debt_ebitda']:.0f}"
    net_debt, ebitda = ttm.get("net_debt"), ttm.get("ebitda")
    if fin:
        crit.append(Criterion.na("nd_ebitda", label, NM_FIN))
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
            crit.append(
                Criterion.check("nd_ebitda", label, x <= c["max_net_debt_ebitda"], f"{x:.2f}x{proxy}", x)
            )

    # 6. Gross margin (pricing power)
    label = f"Gross margin >= {c['min_gross_margin']:.0%} (10y median)"
    gm = _ratio_series(a10["gross_profit"], a10["revenue"]).dropna()
    gm = gm[(gm > -1) & (gm <= 1)]
    if fin:
        crit.append(Criterion.na("gross_margin", label, NM_FIN))
    elif len(gm) < 3:
        crit.append(Criterion.na("gross_margin", label, "Gross profit not reported"))
    else:
        m = float(gm.median())
        crit.append(Criterion.check("gross_margin", label, m >= c["min_gross_margin"], pct(m), m))

    # 7. Operating margin
    label = f"Operating margin >= {c['min_op_margin']:.0%} (10y median)"
    om = _ratio_series(a10["operating_income"], a10["revenue"]).dropna()
    om = om[(om > -2) & (om <= 1)]
    if fin:
        crit.append(Criterion.na("op_margin", label, NM_FIN))
    elif len(om) < 3:
        crit.append(Criterion.na("op_margin", label, "Insufficient data"))
    else:
        m = float(om.median())
        crit.append(Criterion.check("op_margin", label, m >= c["min_op_margin"], pct(m), m))

    # 8. Revenue growth
    label = f"Revenue CAGR >= {c['min_revenue_cagr']:.0%} (5y)"
    g, span = point_cagr(a["revenue"], 5)
    if g is None or span < 3:
        crit.append(Criterion.na("rev_cagr", label, "Insufficient data"))
    else:
        detail = pct(g) + (f" ({span}y)" if span < 5 else "")
        crit.append(Criterion.check("rev_cagr", label, g >= c["min_revenue_cagr"], detail, g))

    # 9. Owner-earnings growth
    col = "owner_fcf"
    label = f"Owner FCF CAGR >= {c['min_fcf_cagr']:.0%} (5y, 3y avgs)"
    start, end, span = smoothed_endpoints(a[col], 5)
    if fin:
        crit.append(Criterion.na("fcf_cagr", label, NM_FIN))
    elif span < 3:
        crit.append(Criterion.na("fcf_cagr", label, "Insufficient data"))
    elif end <= 0:
        detail = f"Owner FCF negative (3y avg {fmt_money(end, b.currency)})"
        crit.append(Criterion.check("fcf_cagr", label, False, detail, None))
    elif start <= 0:
        crit.append(Criterion.na("fcf_cagr", label, "Turned positive from a negative base"))
    else:
        g = cagr(start, end, span)
        detail = pct(g) + (f" ({span}y)" if span < 5 else "")
        crit.append(Criterion.check("fcf_cagr", label, g >= c["min_fcf_cagr"], detail, g))

    # 10. No dilution
    label = f"Share count not rising (5y, <= +{c['max_dilution_5y']:.0%})"
    sh = a["shares"].dropna()
    if len(sh) < 4:
        crit.append(Criterion.na("dilution", label, f"{len(sh)}y of data only"))
    else:
        span = min(5, len(sh) - 1)
        change = sh.iloc[-1] / sh.iloc[-1 - span] - 1
        detail = f"{change:+.1%} over {span}y"
        crit.append(Criterion.check("dilution", label, change <= c["max_dilution_5y"], detail, change))

    # 11. The one-dollar test (Buffett 1984 letter)
    crit.append(_one_dollar_test(b))

    # 12. Capital intensity
    label = f"Capex <= {c['max_capex_to_net_income']:.0%} of net income (10y)"
    if fin:
        crit.append(Criterion.na("capex_ni", label, NM_FIN))
    else:
        both = a10[["capex", "net_income"]].dropna()  # same years on both sides of the ratio
        capex, ni = both["capex"].sum(min_count=3), both["net_income"].sum(min_count=3)
        if not finite(capex) or not finite(ni):
            crit.append(Criterion.na("capex_ni", label, "Insufficient data"))
        elif ni <= 0:
            crit.append(Criterion.check("capex_ni", label, False, "Cumulative losses", None))
        else:
            x = capex / ni
            crit.append(Criterion.check("capex_ni", label, x <= c["max_capex_to_net_income"], pct(x, 0), x))

    # 13. EV / FCF
    label = f"EV/FCF <= {c['max_ev_fcf']:.0f}"
    mcap = b.market_cap
    fcf_ttm = ttm.get("fcf")
    if fin:
        crit.append(Criterion.na("ev_fcf", label, NM_FIN, "valuation"))
    elif not mcap or not finite(fcf_ttm) or not finite(ttm.get("net_debt")):
        crit.append(Criterion.na("ev_fcf", label, "No data", "valuation"))
    elif fcf_ttm <= 0:
        crit.append(Criterion.check("ev_fcf", label, False, "Negative FCF", None, "valuation"))
    else:
        x = (mcap + ttm["net_debt"]) / fcf_ttm
        crit.append(Criterion.check("ev_fcf", label, x <= c["max_ev_fcf"], f"{x:.1f}", x, "valuation"))

    # 14. P/E
    label = f"P/E <= {c['max_pe']:.0f} (TTM, core EPS)"
    eps = ttm.get("core_eps")
    if not finite(eps):
        crit.append(Criterion.na("pe", label, "No EPS", "valuation"))
    elif eps <= 0:
        crit.append(Criterion.check("pe", label, False, "Negative earnings", None, "valuation"))
    else:
        pe = b.price / eps
        crit.append(Criterion.check("pe", label, pe <= c["max_pe"], f"{pe:.1f}", pe, "valuation"))

    card = Scorecard("BUFFETT QUALITY CRITERIA", crit)
    dcf = next((m.value for m in val.methods if m.name.startswith("DCF")), None)
    iv = dcf or val.fair_value
    mos = (iv - b.price) / iv if iv else None
    card.extras = {
        "intrinsic_value": iv,
        "intrinsic_method": "DCF (owner earnings)" if dcf else "blended",
        "margin_of_safety": mos,
        "quality_ratio": card.ratio(("quality",)),
    }
    card.signal = framework_signal(card.ratio(("quality",)), mos, cfg["signals"]["buffett"])
    return card


def _one_dollar_test(b: DataBundle) -> Criterion:
    """Over 5 years, did each dollar of retained earnings create >= $1 of market value?"""
    label = "$1 retained -> >= $1 market value (5y)"
    a = b.annual
    if len(a) < 6:
        return Criterion.na("one_dollar", label, f"{len(a)}y of data only")
    end, start = a.index[-1], a.index[-6]
    p_end, p_start = b.price_on(end), b.price_on(start)
    sh_end, sh_start = a["shares"].iloc[-1], a["shares"].iloc[-6]
    if not all(finite(x) and x > 0 for x in (p_end, p_start, sh_end, sh_start)):
        return Criterion.na("one_dollar", label, "No price/share history")
    # Money actually kept in the business: earnings minus dividends and net buybacks.
    payout = a["dividends_paid"] + a["buybacks"] - a["stock_issued"]
    retained = (a["net_income"] - payout).iloc[-5:].sum()
    if not finite(retained) or retained <= 0:
        return Criterion.na("one_dollar", label, "All earnings returned to shareholders")
    created = p_end * sh_end - p_start * sh_start
    ratio = div(created, retained)
    return Criterion.check("one_dollar", label, created >= retained, f"$1 -> ${ratio:.2f}", ratio)
