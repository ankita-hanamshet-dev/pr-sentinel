"""Deterministic offline provider: replays fixtures/replay/ instead of calling an LLM.

Selected via PR_SENTINEL_LLM_PROVIDER=replay. Zero HTTP, zero sqlite — CI runs
this provider so `uv run pytest` never spends a real LLM call. Keys are content
hashes of the request only (no agent/prompt_version), so the LLMProvider Protocol
stays identical across every adapter.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pr_sentinel.llm.provider import LLMError, LLMRequest, LLMResponse

_DEFAULT_BASE_DIR = Path("fixtures/replay")


class ReplayNotFound(LLMError):
    """No recorded fixture matches this request."""


def replay_key(request: LLMRequest) -> str:
    """Return the content-hash key used to name a fixture file."""
    raw = request.system + "\x00" + request.user
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ReplayProvider:
    """Reads a canned {text, tokens_in, tokens_out, model} JSON fixture per request."""

    name = "replay"

    def __init__(self, base_dir: Path = _DEFAULT_BASE_DIR) -> None:
        self._base_dir = base_dir

    def complete(self, request: LLMRequest) -> LLMResponse:
        key = replay_key(request)
        fixture_path = self._base_dir / f"{key}.json"
        if not fixture_path.exists():
            raise ReplayNotFound(
                f"no replay fixture at {fixture_path} for this request; run with "
                "--record against a live provider first"
            )
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        return LLMResponse(
            text=data["text"],
            tokens_in=data["tokens_in"],
            tokens_out=data["tokens_out"],
            latency_ms=0,
            model=data["model"],
        )
