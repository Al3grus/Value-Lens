"""Request accounting per data service, so users can see what they have used and what is left.

None of the services return a "remaining quota" header, so the numbers are counted here from the
published limits:
  SEC EDGAR   10 requests/second, no daily cap (sec.gov "Accessing EDGAR Data")
  FRED API    120 requests/minute per API key, then HTTP 429 (fred.stlouisfed.org API errors page)
  Yahoo       no published limit; bursts are throttled
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .http import HttpResponse, Transport, TransportError


@dataclass(frozen=True)
class Limit:
    label: str
    limit: int | None  # requests per window; None = no published limit
    window_s: int | None
    note: str


LIMITS: dict[str, Limit] = {
    "sec": Limit("SEC EDGAR", 10, 1, "10 per second, no daily cap"),
    "fred": Limit("FRED API", 120, 60, "120 per minute per key"),
    "fred_public": Limit("FRED public CSV", None, None, "no key, no published limit"),
    "yahoo": Limit("Yahoo Finance", None, None, "no published limit"),
    "dns": Limit("DNS lookup", None, None, "e-mail domain check"),
}

HOSTS = {
    "www.sec.gov": "sec",
    "data.sec.gov": "sec",
    "api.stlouisfed.org": "fred",
    "fred.stlouisfed.org": "fred_public",
    "query1.finance.yahoo.com": "yahoo",
    "query2.finance.yahoo.com": "yahoo",
    "cloudflare-dns.com": "dns",
}

# Network requests one analysis makes when nothing is cached (ticker list, submissions, facts;
# chart + profile; AAA + 10y yields). Repeats within the cache lifetime cost nothing.
PER_ANALYSIS = {"sec": 3, "yahoo": 2, "fred": 2}


def service_of(url: str) -> str:
    return HOSTS.get(urlsplit(url).hostname or "", "other")


class UsageMeter:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._totals: dict[str, int] = {}
        self._errors: dict[str, int] = {}
        self._recent: dict[str, deque[float]] = {}

    def record(self, service: str, status: int | None) -> None:
        now = self._clock()
        with self._lock:
            self._totals[service] = self._totals.get(service, 0) + 1
            if status is None or status >= 400:
                self._errors[service] = self._errors.get(service, 0) + 1
            self._recent.setdefault(service, deque(maxlen=512)).append(now)

    def used_in_window(self, service: str) -> int:
        lim = LIMITS.get(service)
        if not lim or not lim.window_s:
            return 0
        cutoff = self._clock() - lim.window_s
        with self._lock:
            return sum(1 for t in self._recent.get(service, ()) if t > cutoff)

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, lim in LIMITS.items():
            used = self.used_in_window(key)
            out[key] = {
                "label": lim.label,
                "note": lim.note,
                "limit": lim.limit,
                "window_s": lim.window_s,
                "used_in_window": used,
                "remaining": (lim.limit - used) if lim.limit else None,
                "total": self._totals.get(key, 0),
                "errors": self._errors.get(key, 0),
                "per_analysis": PER_ANALYSIS.get(key),
            }
        return out


class MeteredTransport:
    """Wraps a transport and records every request against its service."""

    def __init__(self, inner: Transport, meter: UsageMeter) -> None:
        self.inner = inner
        self.meter = meter

    def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        service = service_of(url)
        try:
            resp = self.inner.request(method, url, **kwargs)
        except TransportError:
            self.meter.record(service, None)
            raise
        self.meter.record(service, resp.status)
        return resp

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if close:
            close()
