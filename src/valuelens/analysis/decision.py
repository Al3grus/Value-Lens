"""Combined verdict: weighted composite of quality, safety, factors and valuation, with
guardrails (margin of safety, accounting red flags, data coverage and age) and a technical
timing overlay."""

from __future__ import annotations

from typing import Any

import numpy as np

from ..models import Decision, HealthScore, Scorecard, Signal, Status, Technicals, Valuation
from .metrics import clamp, finite, pct

ALTMAN_SCORE = {"safe": 100.0, "grey": 50.0, "distress": 0.0}


def _label(score: float, d: dict[str, Any]) -> Signal:
    if score >= d["strong_buy"]:
        return Signal.STRONG_BUY
    if score >= d["buy"]:
        return Signal.BUY
    if score >= d["hold"]:
        return Signal.HOLD
    if score >= d["sell"]:
        return Signal.SELL
    return Signal.STRONG_SELL


def components(
    graham: Scorecard,
    buffett: Scorecard,
    factors: Scorecard,
    val: Valuation,
    health: list[HealthScore],
    cfg: dict[str, Any],
) -> dict[str, float | None]:
    """Pillar scores 0..100. Scorecard pillars use ``Scorecard.score``: checks that are not
    meaningful for the company drop out, checks without data are not passed. A health score that
    could not be computed counts as neutral (50) rather than dropping out."""
    q = buffett.score(("quality",))
    safety_parts: list[float] = []
    g_safety = graham.score(("safety",))
    if g_safety is not None:
        safety_parts.append(g_safety * 100)
    for h in health:  # a score that could not be computed counts as neutral, never drops out
        if h.name.startswith("Piotroski"):
            if finite(h.value):
                safety_parts.append(h.value / 9 * 100)
            elif h.applicable:
                safety_parts.append(50.0)
        if h.name.startswith("Altman"):
            if h.zone in ALTMAN_SCORE:
                safety_parts.append(ALTMAN_SCORE[h.zone])
            elif h.applicable:
                safety_parts.append(50.0)
    f = factors.score()
    mos = val.margin_of_safety
    full = cfg["decision"]["mos_full_scale"]
    return {
        "quality": q * 100 if q is not None else None,
        "safety": float(np.mean(safety_parts)) if safety_parts else None,
        "factors": f * 100 if f is not None else None,
        "valuation": clamp(50 + mos / full * 50, 0, 100) if finite(mos) else None,
    }


def decide(
    graham: Scorecard,
    buffett: Scorecard,
    factors: Scorecard,
    val: Valuation,
    health: list[HealthScore],
    tech: Technicals,
    years: int,
    cfg: dict[str, Any],
    data_age_days: int | None = None,
) -> Decision:
    d = cfg["decision"]
    comps = components(graham, buffett, factors, val, health, cfg)
    weights = d["weights"]
    avail = {k: v for k, v in comps.items() if v is not None}
    total_w = sum(weights[k] for k in avail)
    composite = sum(avail[k] * weights[k] for k in avail) / total_w if total_w else None

    reasons: list[str] = []
    warnings: list[str] = []
    signal = _label(composite, d) if composite is not None else Signal.HOLD
    mos = val.margin_of_safety

    # ---- guardrails ------------------------------------------------------------------
    if mos is None:
        signal = signal.capped_at(Signal.HOLD)
        warnings.append("No reliable intrinsic value: verdict capped at HOLD.")
    else:
        if signal in (Signal.STRONG_BUY, Signal.BUY) and mos < d["buy_min_mos"]:
            signal = Signal.HOLD
            reasons.append("Good business, but price is above estimated fair value - wait for a better price")  # fmt: skip
        elif signal is Signal.STRONG_BUY and mos < d["strong_buy_min_mos"]:
            signal = Signal.BUY
            reasons.append(f"Fundamentals are strong, but STRONG BUY needs >= {d['strong_buy_min_mos']:.0%} margin of safety")  # fmt: skip

    # Missing data must never upgrade a verdict: a check without data is not passed in the pillar
    # scores. Too little data is no verdict at all, in either direction: below the coverage floor
    # (over the applicable criteria; checks that are not meaningful for a bank or a utility are
    # not "missing") the verdict stays HOLD, and a BUY also needs two independent valuation methods.
    cards = (graham, buffett, factors)
    applicable = sum(c.applicable() for c in cards)
    coverage = sum(c.evaluated() for c in cards) / applicable if applicable else 0.0
    methods_used = sum(m.value is not None and m.weight > 0 for m in val.methods)
    bullish = signal in (Signal.STRONG_BUY, Signal.BUY)
    if coverage < d["min_coverage"]:
        signal = Signal.HOLD
        warnings.append(f"Only {coverage:.0%} of criteria could be evaluated: verdict held at HOLD.")
    elif bullish and methods_used < 2:
        signal = Signal.HOLD
        warnings.append("Fair value rests on a single valuation method: verdict capped at HOLD.")

    flags = [h for h in health if h.red_flag]
    for h in flags:
        warnings.append(f"Red flag - {h.name}: {h.zone} ({h.value:.2f})")
    if len(flags) >= 2:
        signal = signal.capped_at(Signal.SELL)
    elif flags:
        signal = signal.capped_at(Signal.HOLD)

    # ---- narrative -------------------------------------------------------------------
    q_fail = [c.label for c in buffett.criteria if c.kind == "quality" and c.status is Status.FAIL]
    if comps["quality"] is not None:
        line = f"Business quality {comps['quality']:.0f}/100"
        if q_fail:
            line += " - weak on: " + "; ".join(q_fail[:3])
        reasons.insert(0, line)
    if val.fair_value and mos is not None:
        state = "undervalued" if mos > 0 else "overvalued"
        reasons.insert(1, f"Blended fair value {val.fair_value:,.2f} -> {pct(abs(mos))} {state}")
    if finite(val.implied_growth) and finite(val.historical_growth):
        basis = val.historical_growth_basis or "owner earnings"
        reasons.append(
            f"Price implies {pct(val.implied_growth)}/yr owner-earnings growth vs "
            f"{pct(val.historical_growth)}/yr {basis} growth delivered (5y)"
        )

    # ---- timing overlay --------------------------------------------------------------
    timing = _timing(signal, tech, cfg["technicals"])
    if tech.trend == "DOWNTREND" and signal is Signal.STRONG_BUY:
        signal = Signal.BUY

    # ---- confidence ------------------------------------------------------------------
    values = [m.value for m in val.methods if m.value and m.weight > 0]  # reference-only methods excluded
    dispersion = (max(values) - min(values)) / val.fair_value if len(values) > 1 and val.fair_value else 0  # fmt: skip
    if val.fair_value is None or coverage < d["confidence_low_coverage"] or years < d["confidence_low_years"]:
        confidence = "LOW"
    elif (
        coverage >= d["confidence_high_coverage"]
        and dispersion <= d["max_method_dispersion"]
        and years >= d["confidence_high_years"]
    ):
        confidence = "HIGH"
    else:
        confidence = "MEDIUM"
    if dispersion > d["max_method_dispersion"]:
        warnings.append("Valuation methods disagree widely - treat fair value as a rough range.")
    if data_age_days is not None and data_age_days > d["stale_data_days"]:
        warnings.append(
            f"Latest statements are {data_age_days} days old: the figures may be stale or the company "
            "may have stopped filing; confidence reduced."
        )
        if confidence == "HIGH":
            confidence = "MEDIUM"

    return Decision(signal, composite, comps, reasons, warnings, timing, confidence, coverage)


def _timing(signal: Signal, tech: Technicals, tcfg: dict[str, Any]) -> str:
    bullish = signal in (Signal.STRONG_BUY, Signal.BUY)
    bearish = signal in (Signal.SELL, Signal.STRONG_SELL)
    overbought = tech.rsi14 is not None and tech.rsi14 >= tcfg["rsi_overbought"]
    if tech.trend == "N/A":
        return "No price trend data."
    if bullish and tech.trend == "DOWNTREND":
        return "Downtrend: build the position gradually (e.g. 3 tranches) or wait for price to reclaim the 200-day average."  # fmt: skip
    if bullish and overbought:
        return f"Short-term overbought (RSI >= {tcfg['rsi_overbought']:.0f}): consider waiting for a pullback or scaling in."  # fmt: skip
    if bullish:
        return f"{tech.trend.title()}: price action does not argue against entry."
    if bearish and tech.trend == "UPTREND":
        return "Momentum still positive: consider trimming gradually rather than exiting at once."
    if bearish:
        return f"{tech.trend.title()}: no technical reason to delay."
    return {
        "UPTREND": "Uptrend: the market likes it, but there is no rush; wait for a better price.",
        "DOWNTREND": "Downtrend: no hurry; the price may come closer to fair value.",
    }.get(tech.trend, "Sideways: no strong price signal either way.")
