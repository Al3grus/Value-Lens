"""Glue between the web page's worker (engine.mjs) and ``WebSession``, used under Pyodide.

Requests go through the relay: SEC, FRED and Yahoo do not send CORS headers, so a page on
github.io cannot call them directly. Browsers also refuse to set a User-Agent header, so the
SEC identity travels as ``X-SEC-Identity`` and the relay turns it back into the User-Agent.
When the relay uses Cloudflare Turnstile, the page hands over a relay session (``call("session",
...)``) that goes with every request as ``X-Relay-Session``. Requests are synchronous XMLHttpRequests, which browsers allow inside a web worker; that keeps
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
RELAY_SESSION_MESSAGE = "ValueLens security check expired; press the button again"


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
        self.session = ""  # relay session from the page (Turnstile); empty when the relay has none
        self.session_expired = False  # set when the relay refused the session; the page gets a new one

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
            if self.session:
                xhr.setRequestHeader("X-Relay-Session", self.session)
            xhr.send()
        except Exception as exc:  # JsException: network error, CORS refusal, relay down
            # Never include the browser's message: it contains the request URL.
            raise TransportError("relay unreachable") from exc
        if not xhr.status:
            raise TransportError("relay unreachable")
        if int(xhr.status) == 429 and xhr.getResponseHeader("X-Relay-Limit"):
            raise TransportError(RELAY_LIMIT_MESSAGE)
        if int(xhr.status) == 401 and xhr.getResponseHeader("X-Relay-Session-Expired"):
            self.session_expired = True
            raise TransportError(RELAY_SESSION_MESSAGE)
        return HttpResponse(int(xhr.status), str(xhr.responseText or ""), url)

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------------------
# Entry points called from engine.mjs
# --------------------------------------------------------------------------------------
_session = None
_transport = None


def start(relay_base: str, cache_root: str, transport=None) -> str:
    global _session, _transport
    from ..config import load_config
    from .session import WebSession

    cfg = load_config()
    cfg.setdefault("cache", {})["dir"] = cache_root
    cfg["sec"]["user_agent"] = ""
    cfg.setdefault("fred", {})["api_key"] = ""
    _transport = transport or XhrTransport(relay_base)
    _session = WebSession(_transport, cfg)
    return json.dumps({"ok": True, "result": {"usage": _session.usage()}})


def call(cmd: str, arg: str = "", progress: Callable[[str], None] | None = None) -> str:
    """Run one command and return JSON: {"ok": true, "result": ...} or {"ok": false, "error": ...},
    plus ``"session_expired": true`` when the relay refused the relay session meanwhile."""
    out = _call(cmd, arg, progress)
    if getattr(_transport, "session_expired", False):
        _transport.session_expired = False
        out["session_expired"] = True
    return json.dumps(out, default=str)


def _call(cmd: str, arg: str, progress: Callable[[str], None] | None) -> dict[str, Any]:
    from .session import SessionError

    if _session is None:
        return {"ok": False, "error": "Engine not started."}
    try:
        if cmd == "session":
            _transport.session = arg
            result = {}
        elif cmd == "identity":
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
            return {"ok": False, "error": f"Unknown command {cmd!r}."}
    except SessionError as exc:
        return {"ok": False, "error": str(exc), "usage": _session.usage()}
    except Exception as exc:  # report, never crash the worker
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return {"ok": False, "error": f"Unexpected error: {detail}", "usage": _session.usage()}
    return {"ok": True, "result": result}
