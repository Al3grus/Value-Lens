"""Local relay: the same allow-listed proxy as the Cloudflare Worker (relay/src/index.js), for
running the website on your own computer with ``valuelens-dashboard``.

Both read ``relay_routes.json``. Only the listed paths and query parameters are forwarded; the
SEC identity arrives as ``X-SEC-Identity`` and leaves as the User-Agent the SEC requires.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from importlib import resources
from typing import Any
from urllib.parse import urlencode

YAHOO_COOKIE_URL = "https://fc.yahoo.com"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
CRUMB_TTL_S = 3600


def load_routes() -> dict[str, Any]:
    text = resources.files("valuelens.web").joinpath("relay_routes.json").read_text(encoding="utf-8")
    return json.loads(text)


@dataclass
class RelayResponse:
    status: int
    body: bytes
    content_type: str = "application/json"


def _error(status: int, message: str) -> RelayResponse:
    return RelayResponse(status, json.dumps({"error": message}).encode())


class Relay:
    def __init__(self, client=None, routes: dict[str, Any] | None = None) -> None:
        import httpx2

        self.spec = routes or load_routes()
        self.identity_re = re.compile(self.spec["identity_pattern"])
        self.routes = {
            prefix: {**r, "_paths": [re.compile(p) for p in r["paths"]]}
            for prefix, r in self.spec["routes"].items()
        }
        self.client = client or httpx2.Client(timeout=httpx2.Timeout(30.0), follow_redirects=True)
        self._crumb: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def match(self, path: str) -> tuple[str, dict[str, Any], str] | None:
        for prefix, route in self.routes.items():
            if path.startswith(prefix):
                rest = path[len(prefix) :]
                if any(p.fullmatch(rest) for p in route["_paths"]):
                    return prefix, route, rest
                return None
        return None

    def handle(
        self, method: str, path: str, query: list[tuple[str, str]], headers: dict[str, str]
    ) -> RelayResponse:
        hit = self.match(path.lstrip("/"))
        if not hit:
            return _error(404, "This relay only forwards ValueLens data requests.")
        _prefix, route, rest = hit
        if method not in route["methods"]:
            return _error(405, "Method not allowed.")
        allowed = set(route["params"])
        params = [(k, v) for k, v in query if k in allowed]
        out_headers = {"User-Agent": self.spec["relay_agent"]}
        lower = {k.lower(): v for k, v in headers.items()}
        if route.get("identity"):
            ident = " ".join((lower.get("x-sec-identity") or "").split())
            if not self.identity_re.match(ident):
                return _error(400, "Missing or malformed SEC identity (name and e-mail).")
            out_headers["User-Agent"] = ident
        if route.get("browser_agent"):
            out_headers["User-Agent"] = self.spec["browser_agent"]
        if route.get("accept"):
            out_headers["Accept"] = route["accept"]
        url = route["upstream"] + rest
        if route.get("yahoo_crumb"):
            return self._yahoo(url, params, out_headers)
        return self._forward(method, url, params, out_headers, route.get("retry_denied", 0))

    def _forward(self, method: str, url: str, params, headers, retries: int = 0) -> RelayResponse:
        import httpx2

        target = f"{url}?{urlencode(params)}" if params else url
        try:
            resp = self.client.request(method, target, headers=headers)
            for _ in range(retries):  # relay_routes.json "retry_denied"
                if resp.status_code != 403:
                    break
                resp = self.client.request(method, target, headers=headers)
        except httpx2.HTTPError as exc:
            return _error(502, f"Upstream unreachable: {type(exc).__name__}")
        ctype = resp.headers.get("content-type", "application/octet-stream")
        return RelayResponse(resp.status_code, b"" if method == "HEAD" else resp.content, ctype)

    def _crumb_value(self, headers, refresh: bool = False) -> str | None:
        import httpx2

        with self._lock:
            if self._crumb and not refresh and time.time() - self._crumb[1] < CRUMB_TTL_S:
                return self._crumb[0]
            try:
                self.client.get(YAHOO_COOKIE_URL, headers=headers)  # sets Yahoo's session cookie
                resp = self.client.get(YAHOO_CRUMB_URL, headers=headers)
            except httpx2.HTTPError:
                return None
            crumb = resp.text.strip()
            if resp.status_code != 200 or not crumb or "<" in crumb:
                return None
            self._crumb = (crumb, time.time())
            return crumb

    def _yahoo(self, url: str, params, headers) -> RelayResponse:
        for attempt in (0, 1):
            crumb = self._crumb_value(headers, refresh=attempt == 1)
            if not crumb:
                return _error(502, "Yahoo session unavailable.")
            resp = self._forward("GET", url, [*params, ("crumb", crumb)], headers)
            if resp.status not in (401, 403):
                return resp
        return resp
