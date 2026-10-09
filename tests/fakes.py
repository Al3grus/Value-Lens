"""A scripted ``Transport`` for offline tests: maps URL prefixes to responses and records calls."""

from __future__ import annotations

import json
from typing import Any

from valuelens.data.http import HttpResponse, TransportError, with_params


class FakeTransport:
    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        # value: (status, body) | callable(method, url, headers) -> (status, body) | Exception
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def add(self, prefix: str, response: Any) -> FakeTransport:
        self.routes[prefix] = response
        return self

    def request(self, method, url, *, params=None, headers=None) -> HttpResponse:
        full = with_params(url, params)
        self.calls.append((method, full, dict(headers or {})))
        for prefix in sorted(self.routes, key=len, reverse=True):
            if full.startswith(prefix):
                spec = self.routes[prefix]
                if isinstance(spec, Exception):
                    raise spec
                if callable(spec):
                    spec = spec(method, full, headers or {})
                status, body = spec
                text = body if isinstance(body, str) else json.dumps(body)
                return HttpResponse(status, text, full)
        raise TransportError(f"no fake route for {full}")

    def urls(self, contains: str = "") -> list[str]:
        return [u for _, u, _ in self.calls if contains in u]

    def close(self) -> None:
        pass
