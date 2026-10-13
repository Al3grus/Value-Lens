"""Configuration loading: packaged defaults deep-merged with an optional user TOML file."""

from __future__ import annotations

import copy
import os
import sys
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

USER_CONFIG_NAMES = ("valuelens.toml", ".valuelens.toml")


class ConfigError(RuntimeError):
    pass


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_defaults() -> dict[str, Any]:
    text = resources.files("valuelens").joinpath("defaults.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


def find_user_config() -> Path | None:
    candidates = [Path.cwd() / n for n in USER_CONFIG_NAMES]
    candidates.append(Path.home() / ".config" / "valuelens" / "config.toml")
    return next((p for p in candidates if p.is_file()), None)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg = load_defaults()
    user_path = Path(path) if path else find_user_config()
    if path and not user_path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    if user_path:
        with user_path.open("rb") as fh:
            cfg = _deep_merge(cfg, tomllib.load(fh))
        cfg["_source"] = str(user_path)

    env_ua = os.environ.get("VALUELENS_SEC_USER_AGENT")
    if env_ua:
        cfg["sec"]["user_agent"] = env_ua
    env_fred = os.environ.get("VALUELENS_FRED_API_KEY")
    if env_fred:
        cfg.setdefault("fred", {})["api_key"] = env_fred
    return cfg


def cache_dir(cfg: dict[str, Any]) -> Path:
    configured = cfg.get("cache", {}).get("dir")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "valuelens" / "cache"
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "valuelens"
