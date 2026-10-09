"""Financial health and forensic accounting scores.

* Piotroski (2000) F-score - nine binary signals of improving/deteriorating fundamentals.
* Altman (1968) Z-score for manufacturers; Altman Z'' (1995) for non-manufacturers.
* Beneish (1999) eight-variable M-score - probability of earnings manipulation.
None of these apply to banks/insurers.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..data.bundle import DataBundle
from ..models import HealthScore
from .metrics import div, finite


def _get(row: pd.Series, key: str) -> float | None:
    v = row.get(key)
    return float(v) if finite(v) else None


# --------------------------------------------------------------------------------------
# Piotroski F-score
# --------------------------------------------------------------------------------------
def piotroski(a: pd.DataFrame) -> tuple[int | None, int, list[str]]:
    """Returns (score, signals_evaluated, failed_signal_names) using the last two fiscal years."""
    if len(a) < 3:
        return None, 0, []
    t, p, pp = a.iloc[-1], a.iloc[-2], a.iloc[-3]
    checks: dict[str, bool | None] = {}

    def roa(cur, prev_assets):
        return div(_get(cur, "net_income"), _get(prev_assets, "total_assets"))

    roa_t, roa_p = roa(t, p), roa(p, pp)
    cfo_a = div(_get(t, "cfo"), _get(p, "total_assets"))
    checks["ROA > 0"] = None if roa_t is None else roa_t > 0
    checks["CFO > 0"] = None if _get(t, "cfo") is None else _get(t, "cfo") > 0
    checks["ROA improving"] = None if roa_t is None or roa_p is None else roa_t > roa_p
    checks["CFO > net income"] = None if cfo_a is None or roa_t is None else cfo_a > roa_t

    lev_t = div(_get(t, "lt_debt"), _avg(t, p, "total_assets"))
    lev_p = div(_get(p, "lt_debt"), _avg(p, pp, "total_assets"))
    checks["Leverage falling"] = None if lev_t is None or lev_p is None else lev_t <= lev_p

    cr_t = div(_get(t, "current_assets"), _get(t, "current_liabilities"))
    cr_p = div(_get(p, "current_assets"), _get(p, "current_liabilities"))
    checks["Liquidity improving"] = None if cr_t is None or cr_p is None else cr_t > cr_p

    sh_t, sh_p = _get(t, "shares"), _get(p, "shares")
    checks["No dilution"] = None if sh_t is None or sh_p is None else sh_t <= sh_p * 1.005

    gm_t = div(_get(t, "gross_profit"), _get(t, "revenue"))
    gm_p = div(_get(p, "gross_profit"), _get(p, "revenue"))
    checks["Gross margin improving"] = None if gm_t is None or gm_p is None else gm_t > gm_p

    at_t = div(_get(t, "revenue"), _get(p, "total_assets"))
    at_p = div(_get(p, "revenue"), _get(pp, "total_assets"))
    checks["Asset turnover improving"] = None if at_t is None or at_p is None else at_t > at_p

    evaluated = [k for k, v in checks.items() if v is not None]
    score = sum(bool(checks[k]) for k in evaluated)
    failed = [k for k in evaluated if not checks[k]]
    return (score if evaluated else None), len(evaluated), failed


def _avg(cur: pd.Series, prev: pd.Series, key: str) -> float | None:
    a, b = _get(cur, key), _get(prev, key)
    if a is None:
        return None
    return (a + b) / 2 if b is not None else a


# --------------------------------------------------------------------------------------
# Altman Z
# --------------------------------------------------------------------------------------
def altman(b: DataBundle) -> tuple[float | None, str, str]:
    t = b.ttm
    ta, tl = _get(t, "total_assets"), _get(t, "total_liabilities")
    wc = None
    if _get(t, "current_assets") is not None and _get(t, "current_liabilities") is not None:
        wc = t["current_assets"] - t["current_liabilities"]
    re_, ebit, sales = _get(t, "retained_earnings"), _get(t, "operating_income"), _get(t, "revenue")
    if None in (ta, tl, wc, re_, ebit) or not ta or not tl:
        return None, "n/a", "Missing balance-sheet data"
    x1, x2, x3 = wc / ta, re_ / ta, ebit / ta
    if b.is_manufacturer and b.market_cap and sales is not None:
        x4, x5 = b.market_cap / tl, sales / ta
        z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
        zone = "safe" if z > 2.99 else "distress" if z < 1.81 else "grey"
        return z, zone, "original Z (manufacturer)"
    equity = _get(t, "equity")
    if equity is None:
        return None, "n/a", "Missing equity"
    z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * (equity / tl)
    zone = "safe" if z > 2.60 else "distress" if z < 1.10 else "grey"
    return z, zone, "Z'' (non-manufacturer)"


# --------------------------------------------------------------------------------------
# Beneish M
# --------------------------------------------------------------------------------------
BENEISH_COEF = {
    "DSRI": 0.920, "GMI": 0.528, "AQI": 0.404, "SGI": 0.892,
    "DEPI": 0.115, "SGAI": -0.172, "TATA": 4.679, "LVGI": -0.327,
}  # fmt: skip


def beneish(a: pd.DataFrame) -> tuple[float | None, dict[str, float], list[str]]:
    if len(a) < 2:
        return None, {}, []
    t, p = a.iloc[-1], a.iloc[-2]
    g = _get

    def ratio(x, y):
        return div(x, y)

    idx: dict[str, float | None] = {}
    idx["DSRI"] = ratio(ratio(g(t, "receivables"), g(t, "revenue")), ratio(g(p, "receivables"), g(p, "revenue")))  # fmt: skip
    gm_t = ratio(g(t, "gross_profit"), g(t, "revenue"))
    gm_p = ratio(g(p, "gross_profit"), g(p, "revenue"))
    idx["GMI"] = ratio(gm_p, gm_t) if gm_t and gm_t > 0 else None

    def soft_assets(r):
        ta, ca, ppe = g(r, "total_assets"), g(r, "current_assets"), g(r, "ppe_net")
        if None in (ta, ca, ppe) or not ta:
            return None
        return 1 - (ca + ppe) / ta

    idx["AQI"] = ratio(soft_assets(t), soft_assets(p))
    idx["SGI"] = ratio(g(t, "revenue"), g(p, "revenue"))

    def dep_rate(r):
        d, ppe = g(r, "dep_amort"), g(r, "ppe_net")
        return ratio(d, (d or 0) + (ppe or 0)) if d is not None and ppe is not None else None

    idx["DEPI"] = ratio(dep_rate(p), dep_rate(t))
    idx["SGAI"] = ratio(ratio(g(t, "sga"), g(t, "revenue")), ratio(g(p, "sga"), g(p, "revenue")))
    ni, cfo, ta = g(t, "net_income"), g(t, "cfo"), g(t, "total_assets")
    idx["TATA"] = (ni - cfo) / ta if None not in (ni, cfo, ta) and ta else None

    def lev(r):
        ta = g(r, "total_assets")
        cl, ltd = g(r, "current_liabilities"), g(r, "lt_debt")
        return (cl + (ltd or 0)) / ta if cl is not None and ta else None

    idx["LVGI"] = ratio(lev(t), lev(p))

    if idx["TATA"] is None or idx["SGI"] is None:
        return None, {}, list(idx)
    missing = [k for k, v in idx.items() if v is None]
    if len(missing) > 3:
        return None, {}, missing
    filled = {k: (1.0 if v is None else v) for k, v in idx.items()}
    filled["TATA"] = idx["TATA"]  # neutral value for TATA would be 0, but it is required
    m = -4.84 + sum(BENEISH_COEF[k] * filled[k] for k in BENEISH_COEF)
    return m, filled, missing


# --------------------------------------------------------------------------------------
def health_scores(b: DataBundle, cfg: dict[str, Any]) -> list[HealthScore]:
    h = cfg["health"]
    out: list[HealthScore] = []

    f, n, failed = piotroski(b.annual)
    if f is None:
        out.append(HealthScore("Piotroski F-score", None, "n/a", "Needs 3 fiscal years"))
    else:
        scaled = f * 9 / n if n else 0
        zone = "strong" if scaled >= h["piotroski_strong"] else "weak" if scaled <= h["piotroski_weak"] else "neutral"  # fmt: skip
        detail = f"{f}/{n}" + (f" (scaled {scaled:.1f}/9)" if n < 9 else "")
        if failed:
            detail += " | missed: " + ", ".join(failed)
        out.append(HealthScore("Piotroski F-score", scaled, zone, detail, red_flag=zone == "weak"))

    if b.is_financial:
        out.append(HealthScore("Altman Z-score", None, "n/a", "n/m for financials"))
        out.append(HealthScore("Beneish M-score", None, "n/a", "n/m for financials"))
        return out

    z, zone, variant = altman(b)
    out.append(HealthScore("Altman Z-score", z, zone, variant, red_flag=zone == "distress"))

    m, _idx, missing = beneish(b.annual)
    if m is None:
        out.append(HealthScore("Beneish M-score", None, "n/a", "Insufficient data"))
    else:
        above = m > h["beneish_threshold"]
        # Very fast sales growth alone pushes M above the threshold (SGI has a large weight);
        # treat that case as a caution rather than a red flag.
        growth_driven = above and _idx.get("SGI", 1.0) > 1.5
        flag = above and not growth_driven
        zone = "possible manipulation" if flag else "elevated (growth-driven)" if growth_driven else "clean"  # fmt: skip
        detail = f"threshold {h['beneish_threshold']}"
        if missing:
            detail += f" | neutral 1.0 used for {', '.join(missing)}"
        out.append(HealthScore("Beneish M-score", m, zone, detail, red_flag=flag))
    return out
