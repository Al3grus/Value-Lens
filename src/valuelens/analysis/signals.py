"""Per-framework signal rules: quality/defensiveness score combined with margin of safety."""

from __future__ import annotations

from typing import Any

from ..models import Signal


def framework_signal(score: float | None, mos: float | None, rules: dict[str, Any]) -> Signal:
    if score is None:
        return Signal.HOLD
    if score < rules["sell_score"]:
        return Signal.SELL
    if mos is None:
        return Signal.HOLD  # never recommend buying without a valuation
    if mos <= rules["sell_mos"]:
        return Signal.SELL
    if score >= rules["strong_buy_score"] and mos >= rules["strong_buy_mos"]:
        return Signal.STRONG_BUY
    if score >= rules["buy_score"] and mos >= rules["buy_mos"]:
        return Signal.BUY
    return Signal.HOLD
