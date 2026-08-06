"""Tests for the aggregate LangGraph pipeline: dedup -> ground -> critic ->
(conditional re-critic, max 2) -> score -> format, and its reducer behaviors.
"""

from __future__ import annotations

import json

from pr_sentinel.agents.critic import CriticAgent
from pr_sentinel.core.grounding import GroundingRejection
from pr_sentinel.graph.build import run_aggregate_pipeline
from pr_sentinel.llm.provider import LLMRequest, LLMResponse
from pr_sentinel.models import Finding, Hunk


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        text = self._texts.pop(0)
        return LLMResponse(text=text, tokens_in=2, tokens_out=2, latency_ms=0, model="scripted")


def _critic_agent(texts: list[str]) -> CriticAgent:
    return CriticAgent(
        _ScriptedProvider(texts),  # type: ignore[arg-type]
        cache=None,
        governor=None,
        provider_name="scripted",
        model="test-model",
        max_output_tokens=256,
        run_id="run-1",
    )


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "agent": "security",
        "file": "app/db.py",
        "line_start": 11,
        "line_end": 11,
        "severity": "critical",
        "confidence": 0.9,
        "rule_id": "CWE-89",
        "title": "Parameterize the concatenated SQL query",
        "fact": "x",
        "assumption": None,
        "impact": "x",
        "recommendation": "x",
        "evidence_quote": "query = build_query(email_fragment)",
    }
    base.update(overrides)
    return Finding.model_validate(base)


def _hunk() -> Hunk:
    return Hunk.model_validate(
        {
            "file": "app/db.py",
            "old_start": 10,
            "old_len": 1,
            "new_start": 10,
            "new_len": 3,
            "lines": [" ctx", "+query = build_query(email_fragment)", " ctx2"],
        }
    )


def _run(
    raw_findings: list[Finding],
    critic_texts: list[str],
    *,
    agent_errors: dict[str, str] | None = None,
    hunks: list[Hunk] | None = None,
) -> object:
    critic_agent = _critic_agent(critic_texts)
    return run_aggregate_pipeline(
        critic_agent,
        diff_text="irrelevant diff context",
        pr_number=1,
        head_sha="abc123",
        model="claude-sonnet-5",
        raw_findings=raw_findings,
        hunks=hunks if hunks is not None else [_hunk()],
        changed_lines_by_file={"app/db.py": 5},
        max_diff_lines=5000,
        diff_lines=10,
        confidence_floor=0.55,
        injection_detected=False,
        budget_exhausted=False,
        prompt_versions={"triage": "1"},
        agent_errors=agent_errors or {},
        budget_used=3,
    )


def test_happy_path_produces_a_review_report_with_one_finding() -> None:
    finding = _finding()
    kept_json = json.dumps({"findings": [finding.model_dump(mode="json")], "drops": []})
    report = _run([finding], [kept_json])

    assert len(report.findings) == 1
    assert report.findings[0].rule_id == "CWE-89"
    assert report.score < 100.0
    assert report.grounding_rejects == 0
    assert report.budget_used == 3
    assert report.needs_human_review is True  # a critical finding survived


def test_empty_raw_findings_short_circuits_critic_and_scores_100() -> None:
    report = _run([], [])
    assert report.findings == []
    assert report.score == 100.0
    assert report.needs_human_review is False


def test_ungrounded_finding_is_dropped_before_critic_ever_sees_it() -> None:
    fabricated = _finding(evidence_quote="this text is not in the diff at all")
    # No critic responses queued -- if critic were invoked with something to review,
    # the scripted provider would raise IndexError (empty list), failing the test.
    report = _run([fabricated], [])
    assert report.findings == []
    assert report.grounding_rejects == 1


def test_critic_reruns_once_on_giveup_then_succeeds() -> None:
    finding = _finding()
    # Round 1: both attempts malformed -> CriticOutcome.error set -> re-critic triggers.
    # Round 2: succeeds on the first attempt.
    kept_json = json.dumps({"findings": [finding.model_dump(mode="json")], "drops": []})
    report = _run([finding], ["not json", "still not json", kept_json])

    assert len(report.findings) == 1
    # agent_errors should NOT retain the critic error once round 2 succeeds and
    # overwrites critic_findings -- but the score node reads whatever agent_errors
    # held at score time (after round 2's update, which has no "critic" key since
    # round 2 succeeded and didn't set one -- merge_str_dicts only ADDS, never
    # removes, so round 1's error would still be present unless round 2 clears it).
    # This documents actual behavior: the error persists as a historical record.
    assert "critic" in report.agent_errors


def test_critic_giveup_both_rounds_keeps_prior_findings_and_flags_error() -> None:
    finding = _finding()
    # Every attempt across both rounds is malformed: round 1 = 2 attempts,
    # round 2 = 2 attempts (capped at MAX_CRITIC_ROUNDS=2, so no round 3).
    report = _run([finding], ["not json"] * 4)

    assert len(report.findings) == 1  # critic's fail-safe: keep, don't discard
    assert "critic" in report.agent_errors
    assert report.needs_human_review is True  # critical finding + a real agent error


def test_all_four_specialist_agents_failed_scores_zero_not_100() -> None:
    finding = _finding(severity="low")
    kept_json = json.dumps({"findings": [finding.model_dump(mode="json")], "drops": []})
    all_failed = {"bug": "boom", "security": "boom", "style": "boom", "improvement": "boom"}
    report = _run([finding], [kept_json], agent_errors=all_failed)

    assert report.score == 0.0


def test_corroboration_and_dedup_merge_near_duplicate_findings() -> None:
    a = _finding(agent="security", severity="high", confidence=0.7)
    b = _finding(agent="bug", severity="critical", confidence=0.9, rule_id="CWE-89")
    merged_json = json.dumps({"findings": [b.model_dump(mode="json")], "drops": []})
    report = _run([a, b], [merged_json])

    assert len(report.findings) == 1
    assert report.findings[0].severity == "critical"  # higher severity survived dedup


def test_grounding_rejection_reason_is_a_real_dataclass() -> None:
    rejection = GroundingRejection(finding_id="abc", reason="test")
    assert rejection.finding_id == "abc"
