"""Chunking tests: token counting, budget, context radius, never-split packing."""

from __future__ import annotations

from pr_sentinel.core.chunking import (
    DEFAULT_PROMPT_OVERHEAD,
    budget_from_settings,
    chunk_hunks,
    count_tokens,
    render_hunk,
)
from pr_sentinel.models import Hunk


def _hunk(file: str, new_start: int, added: list[str]) -> Hunk:
    lines = [f"+{line}" for line in added]
    return Hunk(
        file=file,
        old_start=new_start,
        old_len=0,
        new_start=new_start,
        new_len=len(added),
        lines=lines,
    )


def test_count_tokens_is_positive() -> None:
    assert count_tokens("hello world") > 0
    assert count_tokens("") == 0


def test_budget_from_settings() -> None:
    assert budget_from_settings() == 8000 - DEFAULT_PROMPT_OVERHEAD


def test_render_hunk_plain_has_header_and_body() -> None:
    hunk = _hunk("a.py", 5, ["x = 1"])
    text = render_hunk(hunk)
    assert "@@ -5,0 +5,1 @@" in text
    assert "+x = 1" in text
    assert "--- a/plain" not in text


def test_render_hunk_with_context_radius() -> None:
    file_lines = [f"line{i}" for i in range(1, 21)]  # post-image lines 1..20
    hunk = Hunk(file="a.py", old_start=10, old_len=1, new_start=10, new_len=1, lines=["+changed"])
    text = render_hunk(hunk, context_radius=2, file_lines=file_lines)
    assert " line8" in text and " line9" in text  # two lines before line 10
    assert " line11" in text and " line12" in text  # two lines after
    assert "+changed" in text


def test_chunk_never_splits_and_respects_budget() -> None:
    hunks = [_hunk("a.py", i, [f"payload_{i}_" * 20]) for i in range(1, 9)]
    per_hunk = count_tokens(render_hunk(hunks[0]))
    max_tokens = per_hunk * 3  # room for ~3 hunks per chunk
    chunks = chunk_hunks(hunks, max_tokens=max_tokens)
    assert sum(len(c.hunks) for c in chunks) == len(hunks)  # nothing dropped or split
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.token_count <= max_tokens or len(chunk.hunks) == 1


def test_chunk_keeps_files_separate() -> None:
    hunks = [_hunk("a.py", 1, ["aaa"]), _hunk("b.py", 1, ["bbb"])]
    chunks = chunk_hunks(hunks, max_tokens=10_000)
    assert {c.file for c in chunks} == {"a.py", "b.py"}
    for chunk in chunks:
        assert all(h.file == chunk.file for h in chunk.hunks)


def test_oversize_hunk_becomes_its_own_chunk() -> None:
    big = _hunk("a.py", 1, ["z" * 500])
    chunks = chunk_hunks([big], max_tokens=5)
    assert len(chunks) == 1
    assert len(chunks[0].hunks) == 1
    assert chunks[0].token_count > 5  # over budget, but never split
