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
    (tmp_path / "prompts").symlink_to(Path(__file__).resolve().parent.parent / "prompts")


def _context(hunks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "pr_number": 7, "head_sha": "abc", "model": "m", "diff_text": "d",
        "hunks": hunks, "changed_lines_by_file": {"app/db.py": 1}, "languages": {},
        "triage": [], "prompt_versions": {"triage": "1"},
        "max_diff_lines": 5000, "diff_lines": 3, "confidence_floor": 0.55,
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
        file="app/db.py", old_start=1, old_len=2, new_start=1, new_len=3,
        lines=[" import os", "+import sys", " x = 1"],
    )
    Path("context.json").write_text(json.dumps(_context([hunk.model_dump(mode="json")])))
    Path("agent-bug.json").write_text(json.dumps({
        "agent": "bug", "findings": [{**_finding_json(), "agent": "bug"}], "errors": [],
        "injection_detected": False, "budget_used": 1, "prompt_version": "1",
    }))
    Path("agent-security.json").write_text(json.dumps({
        "agent": "security", "findings": [_finding_json()],
        "errors": ["security agent inference failed: boom"],
        "injection_detected": False, "budget_used": 1, "prompt_version": "1",
    }))
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
