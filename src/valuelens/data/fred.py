"""Latest value of a FRED series via the public CSV download (no API key needed).

Series used:
  AAA   - Moody's Seasoned Aaa Corporate Bond Yield (Graham's "Y")
  DGS10 - 10-Year Treasury Constant Maturity (risk-free rate for the DCF)
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from .cache import FileCache
from .http import HttpxTransport, Transport, TransportError

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_API = "https://api.stlouisfed.org/fred/series/observations"
KEY_RE = re.compile(r"^[a-z0-9]{32}$")  # "a 32 character lower-cased alpha-numeric string"


def key_problem(key: str) -> str | None:
    if not KEY_RE.match(key or ""):
        return "A FRED API key is 32 lower-case letters and digits."
    return None


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


@dataclass
class KeyCheck:
    ok: bool
    message: str
    status: int | None = None
    observation: tuple[float, str] | None = None


def check_key(api_key: str, transport: Transport) -> KeyCheck:
    """Validate a key with one live request (latest AAA yield, limit 1)."""
    problem = key_problem(api_key)
    if problem:
        return KeyCheck(False, problem)
    obs, status, error = _from_api("AAA", api_key, transport, limit=1)
    if status is None:
        return KeyCheck(False, f"Could not reach FRED ({error}). Try again in a moment.")
    if status == 429:
        return KeyCheck(
            False, "FRED says this key has used its 120 requests for this minute. Wait a minute.", status
        )
    if status == 400 and "api_key" in error:
        return KeyCheck(False, "FRED does not recognise this key. Copy it again from fredaccount.stlouisfed.org/apikeys.", status)  # fmt: skip
    if status != 200 or obs is None:
        return KeyCheck(False, f"FRED answered with an error: {error or status}.", status)
    return KeyCheck(True, "Key accepted by FRED.", status, obs)


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
