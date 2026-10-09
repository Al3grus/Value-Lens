"""Glue between the web page's worker (engine.mjs) and ``WebSession``, used under Pyodide.

Requests go through the relay: SEC, FRED and Yahoo do not send CORS headers, so a page on
github.io cannot call them directly. Browsers also refuse to set a User-Agent header, so the
SEC identity travels as ``X-SEC-Identity`` and the relay turns it back into the User-Agent.
Requests are synchronous XMLHttpRequests, which browsers allow inside a web worker; that keeps
the analysis code identical to the command-line version.
"""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from ..data.http import HttpResponse, TransportError, with_params

ROUTES = {
    "https://www.sec.gov/": "sec/www/",
    "https://data.sec.gov/": "sec/data/",
    "https://fred.stlouisfed.org/": "fred/web/",
    "https://query1.finance.yahoo.com/": "yahoo/q1/",
    "https://query2.finance.yahoo.com/": "yahoo/q2/",
    "https://cloudflare-dns.com/": "dns/",
}
FORWARDED_HEADERS = {"accept": "Accept", "user-agent": "X-SEC-Identity"}
RELAY_LIMIT_MESSAGE = "ValueLens relay limit reached (120 requests per minute); wait a minute"


def relay_url(relay_base: str, url: str, params: dict[str, Any] | None = None) -> str:
    full = with_params(url, params)
    parts = urlsplit(full)
    origin = f"{parts.scheme}://{parts.netloc}/"
    prefix = ROUTES.get(origin)
    if prefix is None:
        raise TransportError(f"No relay route for {origin}")
    rest = full[len(origin) :]
    return f"{relay_base.rstrip('/')}/{prefix}{rest}"


def relay_headers(headers: dict[str, str] | None) -> dict[str, str]:
    out = {}
    for name, value in (headers or {}).items():
        mapped = FORWARDED_HEADERS.get(name.lower())
        if mapped:
            out[mapped] = value
    return out


class XhrTransport:
    def __init__(self, relay_base: str, xhr_factory: Callable[[], Any] | None = None) -> None:
        if xhr_factory is None:
            from js import XMLHttpRequest  # only exists under Pyodide

            xhr_factory = XMLHttpRequest.new
        self.relay_base = relay_base
        self._new = xhr_factory

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        target = relay_url(self.relay_base, url, params)
        xhr = self._new()
        try:
            xhr.open(method, target, False)
            for name, value in relay_headers(headers).items():
                xhr.setRequestHeader(name, value)
            xhr.send()
        except Exception as exc:  # JsException: network error, CORS refusal, relay down
            # Never include the browser's message: it contains the request URL.
            raise TransportError("relay unreachable") from exc
        if not xhr.status:
            raise TransportError("relay unreachable")
        if int(xhr.status) == 429 and xhr.getResponseHeader("X-Relay-Limit"):
            raise TransportError(RELAY_LIMIT_MESSAGE)
        return HttpResponse(int(xhr.status), str(xhr.responseText or ""), url)

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------------------
# Entry points called from engine.mjs
# --------------------------------------------------------------------------------------
_session = None


def start(relay_base: str, cache_root: str, transport=None) -> str:
    global _session
    from ..config import load_config
    from .session import WebSession

    cfg = load_config()
    cfg.setdefault("cache", {})["dir"] = cache_root
    cfg["sec"]["user_agent"] = ""
    cfg.setdefault("fred", {})["api_key"] = ""
    _session = WebSession(transport or XhrTransport(relay_base), cfg)
    return json.dumps({"ok": True, "result": {"usage": _session.usage()}})


def call(cmd: str, arg: str = "", progress: Callable[[str], None] | None = None) -> str:
    """Run one command and return JSON: {"ok": true, "result": ...} or {"ok": false, "error": ...}."""
    from .session import SessionError

    if _session is None:
        return json.dumps({"ok": False, "error": "Engine not started."})
    try:
        if cmd == "identity":
            result = _session.set_identity(arg)
        elif cmd == "ticker":
            result = _session.check_ticker(arg)
        elif cmd == "analyze":
            result = _session.analyze(arg, progress)
        elif cmd == "usage":
            result = _session.usage()
        elif cmd == "forget":
            _session.forget()
            result = {"usage": _session.usage()}
        else:
            return json.dumps({"ok": False, "error": f"Unknown command {cmd!r}."})
    except SessionError as exc:
        return json.dumps({"ok": False, "error": str(exc), "usage": _session.usage()})
    except Exception as exc:  # report, never crash the worker
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return json.dumps({"ok": False, "error": f"Unexpected error: {detail}", "usage": _session.usage()})
    return json.dumps({"ok": True, "result": result}, default=str)
