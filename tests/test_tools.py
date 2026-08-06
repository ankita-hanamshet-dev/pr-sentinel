"""Tests for the four allowlisted agent tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pr_sentinel.agents.tools import (
    ToolContext,
    ToolMisuseError,
    get_ci_result,
    get_file_context,
    lookup_rule,
    search_team_conventions,
)
from pr_sentinel.audit import AuditLog


def _ctx(agent: str, *, audit: AuditLog | None = None, run_id: str = "run-1") -> ToolContext:
    return ToolContext(
        agent=agent, changed_file_paths=frozenset({"app/db.py"}), run_id=run_id, audit=audit
    )


# ---------------------------------------------------------------------------
# get_file_context
# ---------------------------------------------------------------------------


def test_get_file_context_allowed_for_bug_and_security() -> None:
    lines = [f"line{i}" for i in range(1, 21)]
    for agent in ("bug", "security"):
        result = get_file_context(_ctx(agent), "app/db.py", line=10, radius=2, file_lines=lines)
        assert result == ["line8", "line9", "line10", "line11", "line12"]


def test_get_file_context_denied_for_style() -> None:
    with pytest.raises(ToolMisuseError):
        get_file_context(_ctx("style"), "app/db.py", line=10, radius=2, file_lines=None)


def test_get_file_context_denied_for_path_outside_pr() -> None:
    with pytest.raises(ToolMisuseError):
        get_file_context(_ctx("bug"), "evil/other.py", line=1, radius=1, file_lines=None)


def test_get_file_context_denied_for_excessive_radius() -> None:
    with pytest.raises(ToolMisuseError):
        get_file_context(_ctx("bug"), "app/db.py", line=1, radius=41, file_lines=None)


def test_get_file_context_denied_for_invalid_line() -> None:
    with pytest.raises(ToolMisuseError):
        get_file_context(_ctx("bug"), "app/db.py", line=0, radius=1, file_lines=None)


def test_get_file_context_returns_none_when_content_unavailable() -> None:
    assert get_file_context(_ctx("bug"), "app/db.py", line=1, radius=1, file_lines=None) is None


def test_get_file_context_clamps_to_file_bounds() -> None:
    lines = ["a", "b", "c"]
    result = get_file_context(_ctx("bug"), "app/db.py", line=1, radius=10, file_lines=lines)
    assert result == ["a", "b", "c"]


def test_misuse_is_logged_to_audit(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(ToolMisuseError):
        get_file_context(_ctx("style", audit=audit), "app/db.py", 1, 1, None)
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["decision"] == "deny"
    assert record["action"] == "tool:get_file_context"


# ---------------------------------------------------------------------------
# search_team_conventions
# ---------------------------------------------------------------------------


def test_search_team_conventions_allowed_for_style_and_improvement() -> None:
    corpus = [
        "prefer pathlib over os.path in this repo",
        "always use f-strings for formatting",
        "unrelated comment about ci timeouts",
    ]
    for agent in ("style", "improvement"):
        results = search_team_conventions(_ctx(agent), "pathlib os.path", corpus)
        assert results
        assert "pathlib" in results[0]


def test_search_team_conventions_denied_for_bug() -> None:
    with pytest.raises(ToolMisuseError):
        search_team_conventions(_ctx("bug"), "query", ["doc"])


def test_search_team_conventions_empty_corpus_returns_empty() -> None:
    assert search_team_conventions(_ctx("style"), "query", []) == []


def test_search_team_conventions_returns_at_most_three() -> None:
    corpus = [f"convention about pathlib number {i}" for i in range(10)]
    results = search_team_conventions(_ctx("style"), "pathlib", corpus)
    assert len(results) <= 3


def test_search_team_conventions_no_match_returns_empty() -> None:
    corpus = ["totally unrelated text about bananas"]
    results = search_team_conventions(_ctx("style"), "xyzzy quux", corpus)
    assert results == []


# ---------------------------------------------------------------------------
# get_ci_result
# ---------------------------------------------------------------------------


def test_get_ci_result_allowed_for_bug_only() -> None:
    ci_results = {"test_hunk_boundaries": "failed"}
    assert get_ci_result(_ctx("bug"), "test_hunk_boundaries", ci_results) == "failed"


def test_get_ci_result_denied_for_security() -> None:
    with pytest.raises(ToolMisuseError):
        get_ci_result(_ctx("security"), "test_x", {})


def test_get_ci_result_missing_test_returns_none() -> None:
    assert get_ci_result(_ctx("bug"), "test_does_not_exist", {}) is None


def test_get_ci_result_non_string_value_is_refused() -> None:
    with pytest.raises(ToolMisuseError):
        get_ci_result(_ctx("bug"), "test_x", {"test_x": 1})


# ---------------------------------------------------------------------------
# lookup_rule
# ---------------------------------------------------------------------------


_ALL_AGENTS = ["bug", "security", "style", "improvement", "critic", "triage", "fixer"]


@pytest.mark.parametrize("agent", _ALL_AGENTS)
def test_lookup_rule_allowed_for_every_agent(agent: str) -> None:
    assert lookup_rule(_ctx(agent), "CWE-89") == "SQL Injection"


def test_lookup_rule_recognizes_taxonomy_pattern_without_static_description() -> None:
    result = lookup_rule(_ctx("bug"), "SENTINEL-BUG-014")
    assert result is not None
    assert "SENTINEL-BUG-014" in result


def test_lookup_rule_unknown_returns_none() -> None:
    assert lookup_rule(_ctx("bug"), "NOT-A-REAL-RULE") is None
