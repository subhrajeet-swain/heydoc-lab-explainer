"""Disk cache for model calls, keyed by exactly what was sent.

The eval set runs many times while the prompts are being tuned. Without this, every
rerun pays for every unchanged case in tokens and in free-tier rate limit, and reruns
that are expensive stop happening. Change the prompt or the schema and the key changes,
so a stale answer can never be served for a changed question.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache"

# Bump when the response shape changes, to invalidate every stored entry at once.
SCHEMA_VERSION = "1"


def _key(model: str, prompt: str) -> str:
    raw = f"{model}\x00{prompt}\x00{SCHEMA_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(model: str, prompt: str) -> dict[str, Any] | None:
    path = CACHE_DIR / f"{_key(model, prompt)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A truncated write from an interrupted run. Treat as a miss, not a crash.
        return None


def put(model: str, prompt: str, response: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{_key(model, prompt)}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def entries() -> int:
    return len(list(CACHE_DIR.glob("*.json"))) if CACHE_DIR.exists() else 0
