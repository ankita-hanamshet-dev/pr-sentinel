"""Tests for scoring, escalation, and presentation-shaping rules."""

from __future__ import annotations

from pr_sentinel.core.scoring import (
    apply_nit_cap,
    file_score,
    needs_human_review,
    partition_by_confidence,
    pr_score,
)
from pr_sentinel.models import Finding


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "agent": "bug",
        "file": "app/db.py",
        "line_start": 10,
        "line_end": 10,
        "severity": "medium",
        "confidence": 0.8,
        "rule_id": "CWE-476",
        "title": "Possible null dereference",
        "fact": "x may be None here.",
        "assumption": None,
        "impact": "Crashes at runtime.",
        "recommendation": "Add a None check.",
        "evidence_quote": "x.value",
    }
    base.update(overrides)
    return Finding.model_validate(base)


def test_score_is_zero_when_every_specialist_agent_errored() -> None:
    # Written first per CLAUDE.md/BUILD_PLAN: a previous iteration shipped a false
    # 100/100 when a bare except swallowed LLM errors -- this must never happen again.
    agent_errors = {
        "bug": "boom",
        "security": "boom",
        "style": "boom",
        "improvement": "boom",
    }
    score, per_file = pr_score([], {"app/db.py": 10}, agent_errors)
    assert score == 0.0
    assert per_file["app/db.py"] == 100.0  # per-file math is still computed, just overridden


def test_score_not_zeroed_when_only_some_agents_errored() -> None:
    agent_errors = {"bug": "boom"}
    score, _per_file = pr_score([], {"app/db.py": 10}, agent_errors)
    assert score == 100.0


def test_file_score_formula() -> None:
    findings = [
        _finding(severity="critical"),
        _finding(severity="high"),
        _finding(severity="medium"),
        _finding(severity="low"),
    ]
    # 100 - (20 + 10 + 5 + 1) = 64
    assert file_score(findings) == 64.0


def test_file_score_floors_at_zero() -> None:
    findings = [_finding(severity="critical") for _ in range(10)]
    assert file_score(findings) == 0.0


def test_file_score_empty_is_100() -> None:
    assert file_score([]) == 100.0


def test_pr_score_weighted_by_changed_lines() -> None:
    findings = [_finding(file="a.py", severity="critical")]  # a.py score = 80
    changed_lines = {"a.py": 10, "b.py": 30}  # b.py score = 100 (no findings)
    score, per_file = pr_score(findings, changed_lines, {})
    assert per_file["a.py"] == 80.0
    assert per_file["b.py"] == 100.0
    # (80*10 + 100*30) / 40 = 95.0
    assert score == 95.0


def test_pr_score_empty_diff_is_100() -> None:
    score, per_file = pr_score([], {}, {})
    assert score == 100.0
    assert per_file == {}


def test_needs_human_review_on_critical_finding() -> None:
    findings = [_finding(severity="critical")]
    flag, reason = needs_human_review(
        findings,
        {},
        injection_detected=False,
        budget_exhausted=False,
        diff_lines=10,
        max_diff_lines=5000,
    )
    assert flag is True
    assert reason is not None and "critical" in reason


def test_needs_human_review_on_injection() -> None:
    flag, reason = needs_human_review(
        [],
        {},
        injection_detected=True,
        budget_exhausted=False,
        diff_lines=10,
        max_diff_lines=5000,
    )
    assert flag is True
    assert reason is not None and "injection" in reason


def test_needs_human_review_on_budget_exhaustion() -> None:
    flag, reason = needs_human_review(
        [],
        {},
        injection_detected=False,
        budget_exhausted=True,
        diff_lines=10,
        max_diff_lines=5000,
    )
    assert flag is True
    assert reason is not None and "budget" in reason


def test_needs_human_review_on_two_agent_errors() -> None:
    flag, reason = needs_human_review(
        [],
        {"bug": "x", "style": "y"},
        injection_detected=False,
        budget_exhausted=False,
        diff_lines=10,
        max_diff_lines=5000,
    )
    assert flag is True
    assert reason is not None and "2 agents" in reason


def test_needs_human_review_not_triggered_by_a_single_agent_error() -> None:
    flag, _reason = needs_human_review(
        [],
        {"bug": "x"},
        injection_detected=False,
        budget_exhausted=False,
        diff_lines=10,
        max_diff_lines=5000,
    )
    assert flag is False


def test_needs_human_review_on_diff_exceeding_max_lines() -> None:
    flag, reason = needs_human_review(
        [],
        {},
        injection_detected=False,
        budget_exhausted=False,
        diff_lines=6000,
        max_diff_lines=5000,
    )
    assert flag is True
    assert reason is not None and "MAX_DIFF_LINES" in reason


def test_needs_human_review_false_when_nothing_triggers() -> None:
    flag, reason = needs_human_review(
        [_finding(severity="low")],
        {},
        injection_detected=False,
        budget_exhausted=False,
        diff_lines=10,
        max_diff_lines=5000,
    )
    assert flag is False
    assert reason is None


def test_partition_by_confidence() -> None:
    high = _finding(confidence=0.9)
    low = _finding(confidence=0.3)
    confident, low_confidence = partition_by_confidence([high, low], floor=0.55)
    assert confident == [high]
    assert low_confidence == [low]


def test_apply_nit_cap_keeps_up_to_max_low() -> None:
    lows = [_finding(severity="low", title=f"nit {i}") for i in range(7)]
    kept, overflow = apply_nit_cap(lows, max_low=5)
    assert len(kept) == 5
    assert len(overflow) == 2


def test_apply_nit_cap_does_not_touch_non_low_findings() -> None:
    findings = [_finding(severity="high")] + [
        _finding(severity="low", title=f"nit {i}") for i in range(7)
    ]
    kept, overflow = apply_nit_cap(findings, max_low=5)
    assert sum(1 for f in kept if f.severity == "high") == 1
    assert sum(1 for f in kept if f.severity == "low") == 5
    assert len(overflow) == 2
