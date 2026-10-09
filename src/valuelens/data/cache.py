"""Tiny file cache with a time-to-live, used for SEC/FRED payloads."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class FileCache:
    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.root = root
        self.enabled = enabled
        if enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str, key: str) -> Path:
        safe = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self.root / namespace / f"{safe}.json"

    def get(self, namespace: str, key: str, ttl_hours: float) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(namespace, key)
        if not path.is_file():
            return None
        if time.time() - path.stat().st_mtime > ttl_hours * 3600:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value), encoding="utf-8")
        tmp.replace(path)
