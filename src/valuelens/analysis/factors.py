"""Evidence-based factor checks, expressed as absolute rules of thumb for a single stock.

* Gross profitability (Novy-Marx 2013): gross profit / total assets.
* Greenblatt "Magic Formula" (2005): return on capital and earnings yield.
* Shareholder yield (Boudoukh et al. 2007; Faber): dividends + net buybacks over market cap.
* Asset growth (Cooper, Gulen & Schill 2008): fast balance-sheet expansion predicts lower returns.
"""

from __future__ import annotations

from typing import Any

from ..data.bundle import DataBundle
from ..models import Criterion, Scorecard
from .metrics import div, finite, pct

NM_FIN = "n/m for financials"


def factor_scorecard(b: DataBundle, cfg: dict[str, Any]) -> Scorecard:
    f = cfg["factors"]
    a, ttm = b.annual, b.ttm
    crit: list[Criterion] = []
    fin = b.is_financial

    # Gross profitability (latest fiscal year, Novy-Marx definition)
    label = f"Gross profit / assets >= {f['min_gross_profitability']:.2f}"
    gpa = div(a["gross_profit"].iloc[-1], a["total_assets"].iloc[-1])
    if fin:
        crit.append(Criterion.na("gp_assets", label, NM_FIN, "factor"))
    elif gpa is None:
        crit.append(Criterion.na("gp_assets", label, "Gross profit not reported", "factor"))
    else:
        crit.append(Criterion.check("gp_assets", label, gpa >= f["min_gross_profitability"], f"{gpa:.2f}", gpa, "factor"))  # fmt: skip

    # Greenblatt return on capital: EBIT / (net working capital + net fixed assets)
    label = f"Return on capital (Greenblatt) >= {f['min_greenblatt_roc']:.0%}"
    ebit = ttm.get("operating_income")
    ca, cl, ppe = ttm.get("current_assets"), ttm.get("current_liabilities"), ttm.get("ppe_net")
    if fin:
        crit.append(Criterion.na("roc", label, NM_FIN, "factor"))
    elif not all(finite(x) for x in (ebit, ca, cl, ppe)):
        crit.append(Criterion.na("roc", label, "No data", "factor"))
    else:
        excess_cash = ttm.get("cash_total") or 0.0
        nwc = max(ca - excess_cash - (cl - (ttm.get("current_debt") or 0.0)), 0.0)
        capital = nwc + ppe
        if capital <= 0:
            crit.append(Criterion.na("roc", label, "No tangible capital", "factor"))
        else:
            roc = ebit / capital
            crit.append(Criterion.check("roc", label, roc >= f["min_greenblatt_roc"], pct(roc), roc, "factor"))  # fmt: skip

    # Greenblatt earnings yield: EBIT / EV
    label = f"Earnings yield (EBIT/EV) >= {f['min_earnings_yield']:.0%}"
    mcap = b.market_cap
    if fin:
        crit.append(Criterion.na("earnings_yield", label, NM_FIN, "factor"))
    elif not mcap or not finite(ebit) or not finite(ttm.get("net_debt")):
        crit.append(Criterion.na("earnings_yield", label, "No data", "factor"))
    else:
        ev = mcap + ttm["net_debt"]
        ey = ebit / ev if ev > 0 else None
        if ey is None:
            crit.append(Criterion.check("earnings_yield", label, True, "EV <= 0 (cash exceeds market cap)", None, "factor"))  # fmt: skip
        else:
            crit.append(Criterion.check("earnings_yield", label, ey >= f["min_earnings_yield"], pct(ey), ey, "factor"))  # fmt: skip

    # Shareholder yield
    label = f"Shareholder yield >= {f['min_shareholder_yield']:.0%}"
    if not mcap:
        crit.append(Criterion.na("sh_yield", label, "No market cap", "factor"))
    else:
        div_, bb, iss = (ttm.get(k) or 0.0 for k in ("dividends_paid", "buybacks", "stock_issued"))
        sy = (div_ + bb - iss) / mcap
        detail = f"{pct(sy)} (div {pct(div_ / mcap)}, net buyback {pct((bb - iss) / mcap)})"
        crit.append(Criterion.check("sh_yield", label, sy >= f["min_shareholder_yield"], detail, sy, "factor"))  # fmt: skip

    # Asset growth
    label = f"Asset growth <= {f['max_asset_growth']:.0%} (1y)"
    ta = a["total_assets"].dropna()
    if len(ta) < 2:
        crit.append(Criterion.na("asset_growth", label, "Insufficient data", "factor"))
    else:
        ag = ta.iloc[-1] / ta.iloc[-2] - 1
        crit.append(Criterion.check("asset_growth", label, ag <= f["max_asset_growth"], pct(ag), ag, "factor"))  # fmt: skip

    return Scorecard("EVIDENCE-BASED FACTORS", crit)
