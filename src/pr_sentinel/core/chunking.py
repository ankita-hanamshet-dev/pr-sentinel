"""Token-budgeted, hunk-aligned chunking. Never splits a hunk (CLAUDE.md C3)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import tiktoken

from pr_sentinel.models import Hunk
from pr_sentinel.settings import get_settings

# Reserve for the system/prompt scaffolding wrapped around each chunk (CLAUDE.md).
DEFAULT_PROMPT_OVERHEAD = 1800


@dataclass
class Chunk:
    """A per-file group of whole hunks whose rendered text fits the budget."""

    file: str
    hunks: list[Hunk] = field(default_factory=list)
    text: str = ""
    token_count: int = 0


@lru_cache(maxsize=1)
def _encoding() -> Any:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Return the tiktoken token count for a string."""
    return len(_encoding().encode(text))


def budget_from_settings() -> int:
    """Effective per-request input budget: MAX_INPUT_TOKENS - PROMPT_OVERHEAD."""
    return get_settings().max_input_tokens - DEFAULT_PROMPT_OVERHEAD


def render_hunk(
    hunk: Hunk,
    *,
    context_radius: int = 0,
    file_lines: Sequence[str] | None = None,
) -> str:
    """Render a hunk as text, optionally padded with post-image context lines."""
    header = f"@@ -{hunk.old_start},{hunk.old_len} +{hunk.new_start},{hunk.new_len} @@"
    parts = [f"--- {hunk.file}", header]
    if context_radius > 0 and file_lines is not None:
        span_start = hunk.new_start
        span_end = hunk.new_start + hunk.new_len - 1
        before = file_lines[max(0, span_start - 1 - context_radius) : max(0, span_start - 1)]
        after = file_lines[span_end : span_end + context_radius]
        parts.extend(f" {line}" for line in before)
        parts.extend(hunk.lines)
        parts.extend(f" {line}" for line in after)
    else:
        parts.extend(hunk.lines)
    return "\n".join(parts)


def _make_chunk(file: str, hunks: list[Hunk], texts: list[str]) -> Chunk:
    text = "\n".join(texts)
    return Chunk(file=file, hunks=list(hunks), text=text, token_count=count_tokens(text))


def chunk_hunks(
    hunks: Iterable[Hunk],
    max_tokens: int,
    *,
    context_radius: int = 0,
    file_lines_by_file: dict[str, Sequence[str]] | None = None,
) -> list[Chunk]:
    """Pack hunks from the same file into chunks under ``max_tokens``.

    Hunks are never split: a single hunk larger than the budget becomes its own
    (over-budget) chunk rather than being cut.
    """
    by_file: dict[str, list[Hunk]] = {}
    for hunk in hunks:
        by_file.setdefault(hunk.file, []).append(hunk)

    chunks: list[Chunk] = []
    for file, file_hunks in by_file.items():
        file_lines = (file_lines_by_file or {}).get(file)
        pending: list[Hunk] = []
        pending_texts: list[str] = []
        pending_tokens = 0
        for hunk in file_hunks:
            text = render_hunk(hunk, context_radius=context_radius, file_lines=file_lines)
            tokens = count_tokens(text)
            if pending and pending_tokens + tokens > max_tokens:
                chunks.append(_make_chunk(file, pending, pending_texts))
                pending, pending_texts, pending_tokens = [], [], 0
            pending.append(hunk)
            pending_texts.append(text)
            pending_tokens += tokens
        # by_file only holds files with >=1 hunk, so pending is always non-empty here.
        chunks.append(_make_chunk(file, pending, pending_texts))
    return chunks
