"""HTTP transport used by every data source.

The analysis code never talks to an HTTP library directly; it calls a ``Transport``. On a normal
Python install that is ``HttpxTransport`` (httpx2). In the browser build (Pyodide on GitHub Pages)
it is ``valuelens.web.browser.XhrTransport``, which sends the same requests through the relay
Worker because SEC, FRED and Yahoo do not allow cross-origin browser requests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode


class TransportError(RuntimeError):
    """Network-level failure (no HTTP status): DNS, timeout, connection refused, relay down."""


@dataclass
class HttpResponse:
    status: int
    text: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse: ...


def get(transport: Transport, url: str, **kwargs: Any) -> HttpResponse:
    return transport.request("GET", url, **kwargs)


def with_params(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"


class HttpxTransport:
    """Default transport for the CLI. One pooled client, redirects followed, gzip accepted."""

    def __init__(self, timeout: float = 30.0) -> None:
        import httpx2

        self._httpx = httpx2
        self._client = httpx2.Client(
            timeout=httpx2.Timeout(timeout),
            follow_redirects=True,
            headers={"Accept-Encoding": "gzip, deflate"},
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        try:
            resp = self._client.request(method, url, params=params, headers=headers)
        except self._httpx.HTTPError as exc:
            # Without the query string: it may hold an API key.
            raise TransportError(f"{method} {url.split('?')[0]}: {type(exc).__name__}") from exc
        return HttpResponse(resp.status_code, resp.text, str(resp.url), dict(resp.headers))

    def close(self) -> None:
        self._client.close()
