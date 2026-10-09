"""Build the static website (for GitHub Pages) and the Python bundle the browser runs.

    python -m valuelens.web.site --relay https://valuelens-relay.<you>.workers.dev --out _site

The output folder holds index.html, app.js, engine.mjs, style.css, config.js and
py/valuelens-<hash>.zip (this package plus its pure-Python dependency ``rich``; numpy and pandas
come from Pyodide's own distribution).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import zipfile
from importlib import resources
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import urlsplit

PYODIDE_VERSION = "314.0.7"  # https://pyodide.org/en/stable/project/changelog.html
PYODIDE_CDN = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"
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


def config_js(relay: str, bundle: str, pyodide: str = PYODIDE_CDN) -> str:
    cfg = {"relay": relay.rstrip("/"), "pyodide": pyodide, "bundle": bundle}
    return f"window.VALUELENS_CONFIG = {json.dumps(cfg, indent=2)};\n"


def static_file(name: str) -> bytes:
    return resources.files("valuelens.web").joinpath("static", name).read_bytes()


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


def csp(relay: str, pyodide: str = PYODIDE_CDN) -> str:
    """Content-Security-Policy for the page. Safari applies the page's policy to the engine
    worker too, so it must allow Pyodide (script + WebAssembly compile) and the relay."""
    py = _origin(pyodide)
    rel = _origin(relay)
    script = " ".join(filter(None, ["'self'", "'wasm-unsafe-eval'", py]))
    connect = " ".join(dict.fromkeys(filter(None, ["'self'", py, rel])))
    return (
        f"default-src 'none'; script-src {script}; worker-src 'self'; connect-src {connect}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'"
    )


def index_html(relay: str, pyodide: str = PYODIDE_CDN) -> bytes:
    return static_file("index.html").replace(b"__CSP__", csp(relay, pyodide).encode())


def build_site(out: Path, relay: str, pyodide: str = PYODIDE_CDN) -> Path:
    if not relay.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise SystemExit("--relay must be the https:// address of your deployed relay Worker.")
    if out.exists():
        shutil.rmtree(out)
    (out / "py").mkdir(parents=True)
    for name in STATIC_FILES:
        (out / name).write_bytes(index_html(relay, pyodide) if name == "index.html" else static_file(name))
    data = python_bundle()
    bundle = f"py/valuelens-{hashlib.sha256(data).hexdigest()[:12]}.zip"
    (out / bundle).write_bytes(data)
    (out / "config.js").write_text(config_js(relay, bundle, pyodide), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m valuelens.web.site", description=__doc__.splitlines()[0])
    p.add_argument(
        "--relay",
        required=True,
        help="Address of the relay Worker, e.g. https://valuelens-relay.you.workers.dev",
    )
    p.add_argument("--out", default="_site", help="Output folder (default _site)")
    p.add_argument("--pyodide", default=PYODIDE_CDN, help="Pyodide distribution URL (default: jsDelivr)")
    args = p.parse_args(argv)
    out = build_site(Path(args.out), args.relay, args.pyodide)
    print(f"Site written to {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
