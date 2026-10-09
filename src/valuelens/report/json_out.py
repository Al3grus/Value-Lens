"""JSON serialisation (the future website/API will consume this)."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from enum import Enum
from typing import Any

from ..models import Report
from .glossary import HEALTH_MEANING, MEANING, METHOD_MEANING, PILLARS, summarize


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if hasattr(obj, "item"):  # numpy scalar
        return _clean(obj.item())
    return obj


def report_dict(r: Report) -> dict[str, Any]:
    """Full report plus the plain-English summary and per-check explanations, so a front-end
    can show the same wording as the terminal views."""
    data = r.to_dict()
    data["summary"] = asdict(summarize(r))
    keys = {c.key for card in (r.graham, r.buffett, r.factors) for c in card.criteria}
    data["glossary"] = {
        "criteria": {k: MEANING[k] for k in sorted(keys) if k in MEANING},
        "health": HEALTH_MEANING,
        "methods": METHOD_MEANING,
        "pillars": {k: {"name": n, "covers": h} for k, (n, h) in PILLARS.items()},
    }
    return _clean(data)


def to_json(reports: list[Report], indent: int = 2) -> str:
    payload = [report_dict(r) for r in reports]
    return json.dumps(payload[0] if len(payload) == 1 else payload, indent=indent)
