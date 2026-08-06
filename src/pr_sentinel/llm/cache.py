"""Content-hash LLM response cache backed by SQLite (CLAUDE.md: .sentinel/cache.sqlite).

Key = sha256(provider|model|prompt_version|agent|normalised_payload). A re-push
touching one file must not re-pay for the others — this is what makes the daily
call quota survivable.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from pr_sentinel.llm.provider import LLMResponse

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""


def cache_key(provider: str, model: str, prompt_version: str, agent: str, payload: str) -> str:
    """Return the stable cache key for a call's identity tuple."""
    normalised = " ".join(payload.split())
    raw = f"{provider}|{model}|{prompt_version}|{agent}|{normalised}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LLMCache:
    """SQLite-backed cache with TTL-on-read expiry."""

    def __init__(self, path: Path, ttl_days: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl_seconds = ttl_days * 86400
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    def get(self, key: str) -> LLMResponse | None:
        """Return the cached response, or None on miss or TTL expiry."""
        row = self._conn.execute(
            "SELECT response_json, created_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        response_json, created_at = row
        if time.time() - created_at > self._ttl_seconds:
            self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            self._conn.commit()
            return None
        data = json.loads(response_json)
        return LLMResponse(**data)

    def put(self, key: str, response: LLMResponse) -> None:
        """Insert or replace the cached response for `key`."""
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (key, response_json, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(asdict(response)), time.time()),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
