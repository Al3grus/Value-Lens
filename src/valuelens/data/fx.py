"""Currency helpers shared by the Yahoo sources (no pandas, so the browser can import it early)."""

from __future__ import annotations

from collections.abc import Callable

# Yahoo quotes some exchanges in minor units: pence (GBp), cents (ZAc), agorot (ILA).
MINOR_UNITS = {"GBp": ("GBP", 100.0), "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}


def convert_rate(from_ccy: str, to_ccy: str, quote: Callable[[str, str], float | None]) -> float | None:
    """``quote(a, b)`` returns the major-unit rate a->b; this adds minor-unit handling."""
    mult = 1.0
    if to_ccy in MINOR_UNITS:
        to_ccy, mult = MINOR_UNITS[to_ccy]
    if from_ccy == to_ccy:
        return mult
    rate = quote(from_ccy, to_ccy)
    return rate * mult if rate else None
