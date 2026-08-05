"""Tests for boundary models: serialization round-trip, id stability, severity order."""

from __future__ import annotations

from pr_sentinel.models import SEVERITY_ORDER, Finding, ReviewReport, finding_id


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "agent": "security",
        "file": "app/db.py",
        "line_start": 10,
        "line_end": 10,
        "severity": "critical",
        "confidence": 0.9,
        "rule_id": "CWE-89",
        "title": "Parameterize the SQL query",
        "fact": "User input is concatenated into a SQL string.",
        "assumption": None,
        "impact": "Allows SQL injection.",
        "recommendation": "Use a parameterized query.",
        "evidence_quote": 'cursor.execute("SELECT * FROM t WHERE x=" + user_input)',
    }
    base.update(overrides)
    return Finding.model_validate(base)


def test_finding_round_trip() -> None:
    finding = _finding()
    restored = Finding.model_validate(finding.model_dump())
    assert restored.model_dump() == finding.model_dump()


def test_finding_id_is_stable_and_computed() -> None:
    first = _finding()
    second = _finding()
    assert first.id == second.id
    assert first.id == finding_id("app/db.py", 10, "CWE-89", "Parameterize the SQL query")
    assert len(first.id) == 12


def test_finding_id_changes_with_identity_tuple() -> None:
    first = _finding()
    moved = _finding(line_start=11)
    assert first.id != moved.id


def test_severity_ordering() -> None:
    findings = [
        _finding(severity="low", rule_id="R1"),
        _finding(severity="critical", rule_id="R2"),
        _finding(severity="medium", rule_id="R3"),
        _finding(severity="high", rule_id="R4"),
    ]
    ordered = sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity], reverse=True)
    assert [f.severity for f in ordered] == ["critical", "high", "medium", "low"]


def test_severity_rank_matches_order() -> None:
    assert _finding(severity="critical").severity_rank == SEVERITY_ORDER["critical"]
    assert _finding(severity="low").severity_rank == SEVERITY_ORDER["low"]


def test_review_report_defaults() -> None:
    report = ReviewReport(pr_number=1, head_sha="abc123", model="claude-sonnet-5")
    assert report.score == 100.0
    assert report.findings == []
    assert report.needs_human_review is False
