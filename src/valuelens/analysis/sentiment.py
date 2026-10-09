"""Informational market-sentiment data. Shown in the report; not used in the verdict."""

from __future__ import annotations

from typing import Any

from ..data.bundle import DataBundle
from .metrics import finite


def sentiment(b: DataBundle) -> dict[str, Any]:
    info = b.info
    out: dict[str, Any] = {}
    target = info.get("targetMeanPrice")
    if finite(target) and target:
        out["analyst_target"] = float(target)
        out["analyst_upside"] = float(target) / b.price - 1
        out["analyst_count"] = info.get("numberOfAnalystOpinions")
        out["analyst_rating"] = info.get("recommendationKey")
    spf = info.get("shortPercentOfFloat")
    if finite(spf):
        out["short_pct_float"] = float(spf)
    if b.insider:
        out["insider_buy_trans"] = b.insider.get("buy_trans")
        out["insider_sell_trans"] = b.insider.get("sell_trans")
        out["insider_net_shares"] = b.insider.get("net_shares")
    beta = info.get("beta")
    if finite(beta):
        out["beta"] = float(beta)
    return out
