"""``valuelens-dashboard``: the website, served from your own computer.

Serves the same page as GitHub Pages plus the local relay at /relay, so it works without
deploying anything. Binds to 127.0.0.1 only and rejects other Host headers (DNS rebinding).
"""

from __future__ import annotations

import argparse
import logging
import socket
import threading
import webbrowser
from functools import cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from .relay import Relay
from .site import PYODIDE_CDN, STATIC_FILES, config_js, csp, index_html, python_bundle, static_file

HOST = "127.0.0.1"
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}  # fmt: skip


def create_app(*, relay=None, pyodide: str | None = None, pyodide_dir: str | None = None, allowed_hosts=None):
    app = FastAPI(title="ValueLens", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOCAL_HOSTS)
    relay = relay or Relay()
    pyodide_url = "/pyodide/" if pyodide_dir else (pyodide or PYODIDE_CDN)
    if pyodide_dir:
        app.mount("/pyodide", StaticFiles(directory=pyodide_dir), name="pyodide")

    @cache
    def bundle() -> bytes:
        return python_bundle()

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    policy = csp("/relay", pyodide_url)

    @app.get("/")
    def index() -> Response:
        return Response(index_html("/relay", pyodide_url), media_type=TYPES[".html"])

    @app.get("/config.js")
    def config() -> Response:
        return Response(config_js("/relay", "py/valuelens.zip", pyodide_url), media_type=TYPES[".js"])

    @app.get("/py/valuelens.zip")
    def py_bundle() -> Response:
        return Response(bundle(), media_type="application/zip")

    @app.api_route("/relay/{path:path}", methods=["GET", "HEAD"])
    def relay_route(path: str, request: Request) -> Response:
        r = relay.handle(
            request.method, path, list(request.query_params.multi_items()), dict(request.headers)
        )
        return Response(r.body, status_code=r.status, media_type=r.content_type)

    @app.get("/{name}")
    def static(name: str) -> Response:
        if name not in STATIC_FILES or name == "index.html":
            raise HTTPException(404)
        # The worker gets the page's policy as a header, as Safari would apply it.
        headers = {"Content-Security-Policy": policy} if name.endswith(".mjs") else {}
        return Response(static_file(name), media_type=TYPES[Path(name).suffix], headers=headers)

    return app


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free local port available.")


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    p = argparse.ArgumentParser(prog="valuelens-dashboard", description="Open the ValueLens website locally.")
    p.add_argument("--port", type=int, default=8765, help="Preferred local port (default 8765)")
    p.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    p.add_argument("--pyodide-dir", help="Serve Pyodide from this local folder instead of the CDN")
    args = p.parse_args(argv)

    logging.getLogger("httpx2").setLevel(logging.WARNING)
    app = create_app(pyodide_dir=args.pyodide_dir)
    port = free_port(args.port)
    url = f"http://{HOST}:{port}/"
    print(f"ValueLens running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=HOST, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
