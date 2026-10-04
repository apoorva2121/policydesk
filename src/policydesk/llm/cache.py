"""An on-disk cache for model responses. This is the biggest money saver here.

Why a cache matters so much while learning
------------------------------------------
You will ask the same question dozens of times: fixing a prompt, chasing a bug,
re-running the test suite, demonstrating the system to someone. Without a cache
you pay for every single repeat. With one, you pay once.

In this project the cache typically removes 70-90% of development spend, and it
makes runs *instant*, which matters more than the money when you are iterating.

The one rule that makes a cache correct
---------------------------------------
The key must include **everything that could change the answer**: the model, the
full messages, the temperature, the token limit. Miss one, and you will get a
stale answer back after changing that very setting, and lose an hour wondering
why your fix did nothing.

We also set `temperature=0` everywhere, which asks the model to be as repeatable
as it can. Caching a random output would be misleading.

What this is NOT
----------------
This is a development convenience, not a production cache. A real one needs
expiry, size limits, and an answer to "what if the underlying documents changed".
Ours is keyed only on the request, so if you edit a document but ask the same
question, you get the old answer until you clear the cache.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class ResponseCache:
    """Stores one JSON file per cached response, keyed by a hash of the request."""

    def __init__(self, directory: Path, enabled: bool = True) -> None:
        self.directory = Path(directory)
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(payload: dict[str, Any]) -> str:
        """Hash the complete request.

        `sort_keys=True` matters: without it, two identical requests whose
        dictionaries happen to be ordered differently would hash differently and
        you would silently never get a cache hit.
        """
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _path_for(self, key: str) -> Path:
        # Two-character prefix directories keep any single folder small, which
        # matters once there are thousands of files.
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self._path_for(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt cache file must never break the app. Treat it as a miss
            # and let the real call overwrite it.
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, key: str, value: dict[str, Any]) -> None:
        if not self.enabled:
            return
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file then rename, so a crash mid-write cannot
        # leave a half-written file that looks valid.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @property
    def total_lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total_lookups if self.total_lookups else 0.0

    def summary_line(self) -> str:
        if not self.enabled:
            return "cache disabled"
        return f"cache {self.hits} hit / {self.misses} miss ({self.hit_rate:.0%} hit rate)"

    def clear(self) -> int:
        """Delete every cached response. Returns how many files were removed."""
        if not self.directory.exists():
            return 0
        removed = 0
        for path in self.directory.glob("**/*.json"):
            path.unlink()
            removed += 1
        return removed
