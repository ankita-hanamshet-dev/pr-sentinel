"""Tests for the 3-step verbatim-evidence grounding filter."""

from __future__ import annotations

from pr_sentinel.core.grounding import GroundingRejection, ground_findings
from pr_sentinel.models import Finding, Hunk


def _hunk(**overrides: object) -> Hunk:
    base: dict[str, object] = {
        "file": "app/db.py",
        "old_start": 8,
        "old_len": 2,
        "new_start": 8,
        "new_len": 3,
        "lines": [
            " ctx_before",
            '+cursor.execute("SELECT * FROM t WHERE x=" + user_input)',
            " ctx_after",
        ],
    }
    base.update(overrides)
    return Hunk.model_validate(base)


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "agent": "security",
        "file": "app/db.py",
        "line_start": 9,
        "line_end": 9,
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


def test_valid_finding_is_kept() -> None:
    kept, rejects = ground_findings([_finding()], [_hunk()])
    assert len(kept) == 1
    assert rejects == []


def test_evidence_from_a_context_line_is_kept() -> None:
    finding = _finding(line_start=8, line_end=8, evidence_quote="ctx_before")
    kept, rejects = ground_findings([finding], [_hunk()])
    assert len(kept) == 1
    assert rejects == []


def test_unknown_rule_id_is_rejected() -> None:
    finding = _finding(rule_id="MADE-UP-RULE")
    kept, rejects = ground_findings([finding], [_hunk()])
    assert kept == []
    assert len(rejects) == 1
    assert "taxonomy" in rejects[0].reason


def test_line_outside_any_hunk_span_is_rejected() -> None:
    finding = _finding(line_start=500, line_end=500)
    kept, rejects = ground_findings([finding], [_hunk()])
    assert kept == []
    assert "changed line range" in rejects[0].reason


def test_fabricated_evidence_quote_is_rejected() -> None:
    finding = _finding(evidence_quote="this text does not appear anywhere in the hunk")
    kept, rejects = ground_findings([finding], [_hunk()])
    assert kept == []
    assert "verbatim" in rejects[0].reason


def test_empty_evidence_quote_is_rejected() -> None:
    finding = _finding(evidence_quote="   ")
    kept, rejects = ground_findings([finding], [_hunk()])
    assert kept == []
    assert "empty" in rejects[0].reason


def test_real_quote_from_a_different_line_is_rejected() -> None:
    # A finding claiming line 9 (the vulnerable line) but quoting line 8's harmless
    # context text is a fabrication -- the quote is real, but not FOR this line.
    # evidence_quote and line_start/line_end must be cross-validated, not checked
    # independently against "anywhere in the hunk".
    hunk = _hunk(new_len=2, lines=[" safe_helper_call()", "+cursor.execute(query)"])
    finding = _finding(line_start=9, line_end=9, evidence_quote="safe_helper_call()")
    kept, rejects = ground_findings([finding], [hunk])
    assert kept == []
    assert "claimed line range" in rejects[0].reason


def test_no_hunks_for_file_is_rejected() -> None:
    finding = _finding(file="other/file.py")
    kept, rejects = ground_findings([finding], [_hunk()])
    assert kept == []
    assert "no hunks found" in rejects[0].reason


def test_evidence_matches_after_whitespace_normalization() -> None:
    hunk = _hunk(lines=[" ctx_before", "+cursor.execute(  'SELECT 1'  )", " ctx_after"])
    finding = _finding(evidence_quote="cursor.execute( 'SELECT 1' )")
    kept, rejects = ground_findings([finding], [hunk])
    assert len(kept) == 1
    assert rejects == []


def test_removed_lines_are_not_valid_evidence() -> None:
    hunk = _hunk(
        lines=[
            " ctx_before",
            "-cursor.execute(unsafe_query)",
            "+cursor.execute(safe_query, params)",
            " ctx_after",
        ]
    )
    finding = _finding(evidence_quote="cursor.execute(unsafe_query)")
    kept, rejects = ground_findings([finding], [hunk])
    assert kept == []
    assert "verbatim" in rejects[0].reason


def test_taxonomy_accepts_all_four_patterns() -> None:
    for rule_id in ("CWE-89", "OWASP-A03:2021", "PEP8-E722", "SENTINEL-BUG-014"):
        finding = _finding(rule_id=rule_id)
        kept, rejects = ground_findings([finding], [_hunk()])
        assert len(kept) == 1, (rule_id, rejects)


def test_taxonomy_rejects_near_miss_variants() -> None:
    for rule_id in ("CWE-abc", "OWASP-A3:2021", "PEP8E722", "SENTINEL-bug-014", "RANDOM-1"):
        finding = _finding(rule_id=rule_id)
        kept, rejects = ground_findings([finding], [_hunk()])
        assert kept == [], rule_id
        assert len(rejects) == 1


def test_grounding_rejection_is_a_plain_dataclass() -> None:
    rejection = GroundingRejection(finding_id="abc123", reason="test reason")
    assert rejection.finding_id == "abc123"
    assert rejection.reason == "test reason"


def test_multiple_findings_partition_correctly() -> None:
    good = _finding()
    bad = _finding(rule_id="NOT-REAL", title="Different title")
    kept, rejects = ground_findings([good, bad], [_hunk()])
    assert kept == [good]
    assert len(rejects) == 1
    assert rejects[0].finding_id == bad.id
