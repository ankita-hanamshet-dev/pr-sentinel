"""CLI Phase 6 wiring: aggregate fan-in (bundle -> report) and the publish command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

import pr_sentinel.cli as cli
from pr_sentinel.agents.critic import CriticAgent
from pr_sentinel.cli import _aggregate_from_bundle, publish
from pr_sentinel.llm.provider import LLMRequest, LLMResponse
from pr_sentinel.models import Hunk
from pr_sentinel.settings import get_settings

BASE = "https://api.github.com"


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=self._texts.pop(0), tokens_in=1, tokens_out=1, latency_ms=0, model="scripted"
        )


def _critic(texts: list[str]) -> CriticAgent:
    return CriticAgent(
        _ScriptedProvider(texts),  # type: ignore[arg-type]
        cache=None,
        governor=None,
        provider_name="scripted",
        model="test",
        max_output_tokens=256,
        run_id="run",
    )


def _finding_json() -> dict[str, object]:
    return {
        "agent": "security",
        "file": "app/db.py",
        "line_start": 2,
        "line_end": 2,
        "severity": "critical",
        "confidence": 0.9,
        "rule_id": "CWE-89",
        "title": "Parameterize the query",
        "fact": "concatenated sql",
        "assumption": None,
        "impact": "sqli",
        "recommendation": "use params",
        "evidence_quote": "import sys",
    }


def _bundle() -> dict[str, object]:
    hunk = Hunk(
        file="app/db.py",
        old_start=1,
        old_len=2,
        new_start=1,
        new_len=3,
        lines=[" import os", "+import sys", " x = 1"],
    )
    return {
        "pr_number": 7,
        "head_sha": "abc",
        "model": "claude-sonnet-5",
        "diff_text": "irrelevant",
        "raw_findings": [_finding_json()],
        "hunks": [hunk.model_dump(mode="json")],
        "changed_lines_by_file": {"app/db.py": 1},
        "max_diff_lines": 5000,
        "diff_lines": 3,
        "confidence_floor": 0.55,
        "injection_detected": False,
        "budget_exhausted": False,
        "prompt_versions": {"triage": "1"},
        "agent_errors": {},
        "budget_used": 0,
    }


def test_aggregate_from_bundle_produces_report() -> None:
    kept = json.dumps({"findings": [_finding_json()], "drops": []})
    report = _aggregate_from_bundle(_bundle(), _critic([kept]))
    assert len(report.findings) == 1
    assert report.findings[0].rule_id == "CWE-89"
    assert report.score < 100.0
    assert report.pr_number == 7


def test_aggregate_from_bundle_drops_ungrounded() -> None:
    bundle = _bundle()
    bad = _finding_json()
    bad["evidence_quote"] = "not in the diff at all"
    bundle["raw_findings"] = [bad]
    report = _aggregate_from_bundle(bundle, _critic([]))  # critic never called
    assert report.findings == []


def test_publish_command_reads_payload_and_posts(
    httpx_mock: HTTPXMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    get_settings.cache_clear()

    report = {
        "pr_number": 7,
        "head_sha": "deadbeef",
        "model": "claude-sonnet-5",
        "findings": [_finding_json()],
        "score": 80.0,
        "needs_human_review": True,
    }
    Path("review-payload.json").write_text(json.dumps(report))
    Path("pr-meta.json").write_text(json.dumps({"pr_number": 7, "head_sha": "deadbeef"}))

    httpx_mock.add_response(method="POST", url=f"{BASE}/repos/o/r/pulls/7/reviews", json={"id": 1})
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/repos/o/r/issues/7/comments?per_page=100", json=[]
    )
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/repos/o/r/issues/7/comments", json={"id": 2}
    )
    httpx_mock.add_response(method="POST", url=f"{BASE}/repos/o/r/check-runs", json={"id": 3})

    try:
        publish(repo="o/r", pr=7)
    finally:
        get_settings.cache_clear()

    posts = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/reviews")]
    assert len(posts) == 1
    assert json.loads(posts[0].content)["event"] == "COMMENT"


def _link_prompts(tmp_path: Path) -> None:
    """Agents load prompts/ relative to CWD; expose the repo's copy in the temp dir."""
    (tmp_path / "prompts").symlink_to(
        Path(__file__).resolve().parent.parent / "src" / "pr_sentinel" / "prompts"
    )


def _context(hunks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "pr_number": 7,
        "head_sha": "abc",
        "model": "m",
        "diff_text": "d",
        "hunks": hunks,
        "changed_lines_by_file": {"app/db.py": 1},
        "languages": {},
        "triage": [],
        "prompt_versions": {"triage": "1"},
        "max_diff_lines": 5000,
        "diff_lines": 3,
        "confidence_floor": 0.55,
    }


def test_agent_command_writes_artifact_without_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty triage -> the specialist has nothing to review -> no LLM call, but the
    # agent still writes its result artifact (so the fan-in always has four files).
    monkeypatch.chdir(tmp_path)
    _link_prompts(tmp_path)
    monkeypatch.setattr(cli, "get_provider", lambda settings: _ScriptedProvider([]))
    get_settings.cache_clear()
    Path("context.json").write_text(json.dumps(_context([])))
    try:
        cli.agent(role="bug", context="context.json", provider="", out="agent-bug.json")
    finally:
        get_settings.cache_clear()
    result = json.loads(Path("agent-bug.json").read_text())
    assert result["agent"] == "bug"
    assert result["findings"] == []
    assert result["errors"] == []


def test_aggregate_fanin_merges_agents_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _link_prompts(tmp_path)
    kept = json.dumps({"findings": [_finding_json()], "drops": []})
    monkeypatch.setattr(cli, "get_provider", lambda settings: _ScriptedProvider([kept]))
    get_settings.cache_clear()

    hunk = Hunk(
        file="app/db.py",
        old_start=1,
        old_len=2,
        new_start=1,
        new_len=3,
        lines=[" import os", "+import sys", " x = 1"],
    )
    Path("context.json").write_text(json.dumps(_context([hunk.model_dump(mode="json")])))
    Path("agent-bug.json").write_text(
        json.dumps(
            {
                "agent": "bug",
                "findings": [{**_finding_json(), "agent": "bug"}],
                "errors": [],
                "injection_detected": False,
                "budget_used": 1,
                "prompt_version": "1",
            }
        )
    )
    Path("agent-security.json").write_text(
        json.dumps(
            {
                "agent": "security",
                "findings": [_finding_json()],
                "errors": ["security agent inference failed: boom"],
                "injection_detected": False,
                "budget_used": 1,
                "prompt_version": "1",
            }
        )
    )
    try:
        cli.aggregate(
            context="context.json",
            agent_result=["agent-bug.json", "agent-security.json"],
            out="review-payload.json",
            provider="",
        )
    finally:
        get_settings.cache_clear()

    report = json.loads(Path("review-payload.json").read_text())
    assert Path("pr-meta.json").exists()
    assert "security" in report["agent_errors"]  # agent error propagated to the report
    assert len(report["findings"]) >= 1  # grounded + critic-kept


def test_aggregate_marks_missing_agent_artifact_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-suspenders for the false 100/100: a specialist that crashes HARD (OOM,
    timeout, uncaught error) writes no artifact at all. The fan-in must treat each expected
    artifact's absence as an agent error, not silently skip it and score 100."""
    monkeypatch.chdir(tmp_path)
    _link_prompts(tmp_path)
    monkeypatch.setattr(cli, "get_provider", lambda settings: _ScriptedProvider([]))
    get_settings.cache_clear()

    hunk = Hunk(
        file="app/db.py",
        old_start=1,
        old_len=2,
        new_start=1,
        new_len=3,
        lines=[" import os", "+import sys", " x = 1"],
    )
    Path("context.json").write_text(json.dumps(_context([hunk.model_dump(mode="json")])))
    # Only the bug specialist produced an artifact (clean, no findings/errors); the other
    # three crashed before writing anything.
    Path("agent-bug.json").write_text(
        json.dumps(
            {
                "agent": "bug",
                "findings": [],
                "errors": [],
                "injection_detected": False,
                "budget_used": 0,
                "prompt_version": "1",
            }
        )
    )
    try:
        cli.aggregate(
            context="context.json",
            agent_result=[
                "agent-bug.json",
                "agent-security.json",
                "agent-style.json",
                "agent-improvement.json",
            ],
            out="review-payload.json",
            provider="",
        )
    finally:
        get_settings.cache_clear()

    report = json.loads(Path("review-payload.json").read_text())
    for role in ("security", "style", "improvement"):
        assert role in report["agent_errors"]  # absent artifact surfaced as an error
    assert "bug" not in report["agent_errors"]  # produced a clean artifact
    assert report["needs_human_review"] is True  # >= 2 agents errored


def test_agent_401_forces_human_review_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The exact regression this whole fix exists for, end to end through `agent` ->
    `aggregate`: a specialist whose model call returns 401 must NOT crash the process
    silently. Its failure lands in agent_errors, the missing peers surface too, human
    review is forced, and the Check Run conclusion is NOT `success` (no false 100/100)."""
    from pr_sentinel.gh.publish import check_run_conclusion
    from pr_sentinel.models import ReviewReport

    monkeypatch.chdir(tmp_path)
    _link_prompts(tmp_path)
    monkeypatch.setenv("PR_SENTINEL_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("PR_SENTINEL_LLM_API_KEY", "sk-rate-limited")
    get_settings.cache_clear()

    # The bug specialist will call the model on this high-risk hunk; the key is rejected.
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=401,
        json={"error": {"type": "authentication_error", "message": "invalid x-api-key"}},
    )
    hunk = Hunk(
        file="app/db.py",
        old_start=1,
        old_len=1,
        new_start=1,
        new_len=2,
        lines=[" import os", "+result = eval(payload)"],
    )
    ctx = _context([hunk.model_dump(mode="json")])
    ctx["triage"] = [
        {
            "file": "app/db.py",
            "language": "python",
            "risk": "high",
            "agents_to_run": ["bug"],
            "skip_reason": None,
        }
    ]
    Path("context.json").write_text(json.dumps(ctx))

    try:
        # Must NOT raise: the 401 is recorded, the artifact is still written.
        cli.agent(role="bug", context="context.json", provider="", out="agent-bug.json")
        bug_result = json.loads(Path("agent-bug.json").read_text())
        assert bug_result["errors"], "the 401 must be recorded in the agent's own errors"

        cli.aggregate(
            context="context.json",
            agent_result=[
                "agent-bug.json",
                "agent-security.json",
                "agent-style.json",
                "agent-improvement.json",
            ],
            out="review-payload.json",
            provider="",
        )
    finally:
        get_settings.cache_clear()

    report = json.loads(Path("review-payload.json").read_text())
    assert "bug" in report["agent_errors"]  # the 401 propagated (fix #1)
    assert report["needs_human_review"] is True  # >= 2 agents unaccounted for
    conclusion = check_run_conclusion(ReviewReport.model_validate(report))
    assert conclusion != "success"  # never a false green check
