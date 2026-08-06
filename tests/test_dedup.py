"""Tests for cross-agent finding deduplication."""

from __future__ import annotations

from pr_sentinel.core.dedup import dedup_findings
from pr_sentinel.models import Finding


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "agent": "security",
        "file": "app/db.py",
        "line_start": 10,
        "line_end": 10,
        "severity": "high",
        "confidence": 0.8,
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


def test_distinct_findings_are_not_merged() -> None:
    a = _finding(title="SQL injection in query builder")
    b = _finding(file="app/other.py", title="Totally unrelated null check", rule_id="CWE-476")
    merged, corroboration = dedup_findings([a, b])
    assert len(merged) == 2
    assert corroboration[a.id] == ["security"]
    assert corroboration[b.id] == ["security"]


def test_near_duplicate_findings_on_same_file_are_merged() -> None:
    a = _finding(agent="security", severity="high", confidence=0.7)
    b = _finding(agent="bug", severity="critical", confidence=0.9, rule_id="CWE-89")
    merged, corroboration = dedup_findings([a, b])
    assert len(merged) == 1
    # higher severity wins as the representative
    assert merged[0].severity == "critical"
    assert merged[0].id == b.id
    assert corroboration[b.id] == ["bug", "security"]


def test_similar_title_different_file_is_not_merged() -> None:
    a = _finding(file="app/db.py")
    b = _finding(file="app/other_db.py")
    merged, _corroboration = dedup_findings([a, b])
    assert len(merged) == 2


def test_same_severity_prefers_higher_confidence() -> None:
    a = _finding(agent="security", severity="high", confidence=0.5)
    b = _finding(agent="bug", severity="high", confidence=0.95, rule_id="CWE-89")
    merged, _corroboration = dedup_findings([a, b])
    assert len(merged) == 1
    assert merged[0].id == b.id


def test_empty_input_returns_empty_output() -> None:
    merged, corroboration = dedup_findings([])
    assert merged == []
    assert corroboration == {}


def test_single_finding_is_its_own_corroborator() -> None:
    a = _finding()
    merged, corroboration = dedup_findings([a])
    assert merged == [a]
    assert corroboration[a.id] == ["security"]


def test_three_way_cluster_records_all_corroborating_agents() -> None:
    a = _finding(agent="security", severity="medium", confidence=0.6)
    b = _finding(agent="bug", severity="high", confidence=0.7, rule_id="CWE-89")
    c = _finding(agent="style", severity="low", confidence=0.5, rule_id="SENTINEL-STYLE-001")
    merged, corroboration = dedup_findings([a, b, c])
    assert len(merged) == 1
    assert merged[0].id == b.id
    assert corroboration[b.id] == ["bug", "security", "style"]
