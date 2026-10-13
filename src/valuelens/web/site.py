"""Build the static website (for GitHub Pages) and the Python bundle the browser runs.

    python -m valuelens.web.site --relay https://valuelens-relay.<you>.workers.dev --out _site

The output folder holds index.html, app.js, engine.mjs, style.css, config.js, version.json,
py/valuelens-<hash>.zip (this package plus its pure-Python dependency ``rich``; numpy and pandas
come from Pyodide's own distribution) and pyodide/<version>/ (Pyodide's core, checked against
pinned hashes). The page loads its files as ``name?v=<site version>``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import zipfile
from importlib import resources
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import urlsplit

from ..data.http import HttpxTransport, Transport, TransportError

PYODIDE_VERSION = "314.0.7"  # https://pyodide.org/en/stable/project/changelog.html
PYODIDE_CDN = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"
# The site serves Pyodide's core itself, so the CDN cannot change the code the page runs: the
# build downloads these files and stops if any SHA-256 differs (pinned from jsDelivr and matched
# against the pyodide-core release on GitHub). numpy and pandas still download from the CDN, and
# the browser refuses them unless they match the SHA-256 in this pyodide-lock.json.
PYODIDE_CORE = {
    "pyodide.mjs": "6f1d60f7bf529beb300f0f47983c921d3982363640ba20af0e38efdddbc66109",
    "pyodide.asm.mjs": "f7cdc8ece80678ceb712f8e65ebe6d3a83203a180c399865f49612a051693635",
    "pyodide.asm.wasm": "cc36e3cab04fdfc9a63ff13eb52eae2b911bf46c025cc7b281f394bd3de1d5e6",
    "python_stdlib.zip": "fa1957e5777068fc4f7437f96d860ae2fbe9c19732ba06c84e004ec16dd7dd7a",
    "pyodide-lock.json": "5dc2fc119108bc148c7457dc86e7675b5c87e1cafd420b9c34c1eaef7b36c010",
}
PYODIDE_DIR = f"pyodide/{PYODIDE_VERSION}/"
TURNSTILE = "https://challenges.cloudflare.com"
SITEKEY_RE = re.compile(r"^[0-9A-Za-z_-]{10,100}$")
VENDORED = ("rich",)  # pure-Python packages not in Pyodide's distribution at the version we need
STATIC_FILES = ("index.html", "app.js", "engine.mjs", "style.css")
SKIP_DIRS = {"__pycache__", "static"}


def _package_dir(name: str) -> Path:
    spec = find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(f"Package {name!r} is not installed.")
    return Path(next(iter(spec.submodule_search_locations)))


def python_bundle() -> bytes:
    """Deterministic zip of valuelens + vendored packages (sorted entries, fixed timestamps)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in ("valuelens", *VENDORED):
            root = _package_dir(name)
            for path in sorted(root.rglob("*")):
                rel = path.relative_to(root)
                if path.is_dir() or SKIP_DIRS.intersection(rel.parts) or path.suffix in (".pyc", ".pyo"):
                    continue
                info = zipfile.ZipInfo(f"{name}/{rel.as_posix()}", date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())
    return buf.getvalue()


def vendor_pyodide(dest: Path, transport: Transport | None = None) -> None:
    """Download Pyodide's core into ``dest``, refusing any file that is not the pinned one."""
    t = transport or HttpxTransport(timeout=120.0)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        for name, digest in PYODIDE_CORE.items():
            try:
                resp = t.request("GET", PYODIDE_CDN + name)
            except TransportError as exc:
                raise SystemExit(f"Could not download Pyodide's {name}: {exc}") from exc
            if resp.status != 200:
                raise SystemExit(f"Could not download Pyodide's {name} (HTTP {resp.status}).")
            got = hashlib.sha256(resp.content).hexdigest()
            if got != digest:
                raise SystemExit(f"Pyodide's {name} does not match its pinned SHA-256 (got {got}).")
            (dest / name).write_bytes(resp.content)
    finally:
        if transport is None:
            t.close()


def config_js(
    relay: str,
    bundle: str,
    pyodide: str = PYODIDE_CDN,
    version: str = "",
    packages: str = "",
    turnstile: str = "",
) -> str:
    cfg = {"relay": relay.rstrip("/"), "pyodide": pyodide, "bundle": bundle}
    if packages:
        cfg["packages"] = packages
    if turnstile:
        cfg["turnstile"] = turnstile
    if version:
        cfg |= {"version": version, "engine": f"engine.mjs?v={version}"}
    return f"window.VALUELENS_CONFIG = {json.dumps(cfg, indent=2)};\n"


def static_file(name: str) -> bytes:
    return resources.files("valuelens.web").joinpath("static", name).read_bytes()


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


def csp(relay: str, pyodide: str = PYODIDE_CDN, packages: str = "", turnstile: bool = False) -> str:
    """Content-Security-Policy for the page. Safari applies the page's policy to the engine
    worker too, so it must allow Pyodide (script + WebAssembly compile) and the relay. A relative
    ``pyodide`` is served by the site itself; ``packages`` is where numpy and pandas download from.
    """
    py = _origin(pyodide)
    ts = TURNSTILE if turnstile else None
    script = " ".join(filter(None, ["'self'", "'wasm-unsafe-eval'", py, ts]))
    connect = " ".join(dict.fromkeys(filter(None, ["'self'", py, _origin(packages), _origin(relay)])))
    frame = f"frame-src {TURNSTILE}; " if turnstile else ""
    return (
        f"default-src 'none'; script-src {script}; worker-src 'self'; connect-src {connect}; {frame}"
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'"
    )


def index_html(
    relay: str, pyodide: str = PYODIDE_CDN, version: str = "", packages: str = "", turnstile: bool = False
) -> bytes:
    """The page with its CSP filled in. With ``version``, every file it loads is named
    ``file?v=<version>`` so a cached copy of an older file can never be mixed with this page."""
    policy = csp(relay, pyodide, packages, turnstile)
    page = static_file("index.html").replace(b"__CSP__", policy.encode())
    if version:
        for ref in (b'href="style.css"', b'src="config.js"', b'src="app.js"'):
            page = page.replace(ref, ref[:-1] + f'?v={version}"'.encode())
    return page


def site_version(relay: str, pyodide: str, bundle: bytes, packages: str = "", turnstile: str = "") -> str:
    """Short hash of everything the site serves: changes whenever any of it changes."""
    h = hashlib.sha256(f"{relay}\n{pyodide}\n{packages}\n{turnstile}\n".encode())
    for name in STATIC_FILES:
        h.update(static_file(name))
    h.update(bundle)
    return h.hexdigest()[:12]


def build_site(
    out: Path,
    relay: str,
    pyodide: str | None = None,
    turnstile: str = "",
    transport: Transport | None = None,
) -> Path:
    """Without ``pyodide`` the site serves Pyodide's core itself (downloaded with ``transport``)."""
    if not relay.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise SystemExit("--relay must be the https:// address of your deployed relay Worker.")
    if turnstile and not SITEKEY_RE.match(turnstile):
        raise SystemExit("--turnstile must be the widget's site key, e.g. 0x4AAAAAAA...")
    if out.exists():
        shutil.rmtree(out)
    (out / "py").mkdir(parents=True)
    relay = relay.rstrip("/")
    packages = ""
    if pyodide is None:
        vendor_pyodide(out / PYODIDE_DIR, transport)
        pyodide, packages = PYODIDE_DIR, PYODIDE_CDN
    data = python_bundle()
    version = site_version(relay, pyodide, data, packages, turnstile)
    for name in STATIC_FILES:
        if name == "index.html":
            body = index_html(relay, pyodide, version, packages, bool(turnstile))
        else:
            body = static_file(name)
        (out / name).write_bytes(body)
    bundle = f"py/valuelens-{hashlib.sha256(data).hexdigest()[:12]}.zip"
    (out / bundle).write_bytes(data)
    (out / "config.js").write_text(
        config_js(relay, bundle, pyodide, version, packages, turnstile), encoding="utf-8"
    )
    # Fetched uncached by the page: tells a page restored from the browser's cache that it is old.
    (out / "version.json").write_text(json.dumps({"version": version}) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m valuelens.web.site", description=__doc__.splitlines()[0])
    p.add_argument(
        "--relay",
        required=True,
        help="Address of the relay Worker, e.g. https://valuelens-relay.you.workers.dev",
    )
    p.add_argument("--out", default="_site", help="Output folder (default _site)")
    p.add_argument(
        "--turnstile",
        default="",
        help="Cloudflare Turnstile site key: visitors pass Turnstile to get a relay session "
        "(the relay needs the TURNSTILE_SECRET and SESSION_KEY secrets)",
    )
    p.add_argument(
        "--pyodide",
        default=None,
        help="Load Pyodide from this URL instead of serving its core from the site",
    )
    args = p.parse_args(argv)
    out = build_site(Path(args.out), args.relay, args.pyodide, args.turnstile)
    print(f"Site written to {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
