"""Latest value of a FRED series via the public CSV download (no API key needed), or FRED's
official API when the command-line config has a key. Both give the same figures.

Series used:
  AAA   - Moody's Seasoned Aaa Corporate Bond Yield (Graham's "Y")
  DGS10 - 10-Year Treasury Constant Maturity (risk-free rate for the DCF)
"""

from __future__ import annotations

import csv
import io

from .cache import FileCache
from .http import HttpxTransport, Transport, TransportError

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_API = "https://api.stlouisfed.org/fred/series/observations"


def latest_value(
    series: str,
    cache: FileCache,
    ttl_hours: float,
    api_key: str = "",
    transport: Transport | None = None,
) -> tuple[float, str] | None:
    """Latest observation. Uses the official API when a key is configured, otherwise (or if
    the API fails) the public CSV download."""
    hit = cache.get("fred", series, ttl_hours)
    if hit:
        return float(hit[0]), str(hit[1])
    owned = transport is None
    transport = transport or HttpxTransport(timeout=20.0)
    try:
        value = _from_api(series, api_key, transport)[0] if api_key else None
        if value is None:
            value = _from_csv(series, transport)
    finally:
        if owned:
            transport.close()
    if value:
        cache.set("fred", series, list(value))
    return value


def _from_api(series: str, api_key: str, transport: Transport, limit: int = 10):
    """(observation or None, HTTP status or None, FRED error message)."""
    params = {"series_id": series, "api_key": api_key, "file_type": "json",
              "sort_order": "desc", "limit": limit}  # fmt: skip
    try:
        resp = transport.request("GET", FRED_API, params=params)
    except TransportError as exc:
        return None, None, str(exc)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not resp.ok:
        return None, resp.status, str(data.get("error_message") or f"HTTP {resp.status}")
    return parse_api(data), resp.status, ""


def _from_csv(series: str, transport: Transport) -> tuple[float, str] | None:
    try:
        resp = transport.request("GET", FRED_CSV, params={"id": series})
    except TransportError:
        return None
    return parse_latest(resp.text) if resp.ok else None


def parse_api(data: dict) -> tuple[float, str] | None:
    """Newest non-missing observation (FRED marks missing values with '.')."""
    for obs in data.get("observations", []):
        try:
            return float(obs["value"]), str(obs["date"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def parse_latest(text: str) -> tuple[float, str] | None:
    rows = list(csv.reader(io.StringIO(text)))
    for row in reversed(rows[1:]):
        if len(row) >= 2 and row[1] not in ("", "."):
            try:
                return float(row[1]), row[0]
            except ValueError:
                continue
    return None
