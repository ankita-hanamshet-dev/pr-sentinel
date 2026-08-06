"""Heuristic triage: risk classification, unknown escalation, security-never-skipped."""

from __future__ import annotations

from pr_sentinel.core.triage import (
    TriageInput,
    agents_for_risk,
    classify_file,
    heuristic_triage,
)


def _inp(file: str, language: str = "python", changed: int = 10) -> TriageInput:
    return TriageInput(file=file, language=language, changed_lines=changed)


def test_high_risk_paths_classify_high() -> None:
    assert classify_file(_inp("app/auth/login.py")) == "high"
    assert classify_file(_inp("src/db/query_builder.py")) == "high"
    assert classify_file(_inp("services/payment_processor.go", language="go")) == "high"


def test_large_change_is_high_regardless_of_path() -> None:
    assert classify_file(_inp("app/util/helpers.py", changed=200)) == "high"


def test_tests_and_docs_are_low() -> None:
    assert classify_file(_inp("tests/test_helpers.py")) == "low"
    assert classify_file(_inp("app/widget.test.js", language="javascript")) == "low"
    assert classify_file(_inp("docs/guide.md", language="markdown")) == "low"
    assert classify_file(_inp("pyproject.toml", language="toml")) == "low"


def test_plain_source_is_medium() -> None:
    assert classify_file(_inp("app/util/helpers.py")) == "medium"


def test_unclassified_language_is_unknown() -> None:
    assert classify_file(_inp("data/blob.xyz", language="unknown")) == "unknown"


def test_security_is_in_every_risk_bucket() -> None:
    for risk in ("high", "medium", "low", "unknown"):
        assert "security" in agents_for_risk(risk)  # type: ignore[arg-type]


def test_unknown_gets_conservative_coverage_not_empty() -> None:
    # In pure-heuristic mode an unknown file is never refined, so it must still be
    # reviewed -- treated like medium (bug+security+style), never left with nothing.
    agents = agents_for_risk("unknown")
    assert set(agents) == {"bug", "security", "style"}


def test_oversized_diff_marks_every_file_unknown() -> None:
    inputs = [_inp("app/auth/login.py"), _inp("tests/test_x.py"), _inp("app/util.py")]
    plan = heuristic_triage(inputs, call_budget=12, max_diff_lines=500, total_diff_lines=900)
    assert {fp.risk for fp in plan.files} == {"unknown"}
    assert plan.llm_call_budget == 12


def test_normal_diff_classifies_per_file() -> None:
    inputs = [_inp("app/auth/login.py"), _inp("tests/test_x.py"), _inp("app/util.py")]
    plan = heuristic_triage(inputs, call_budget=12, max_diff_lines=5000, total_diff_lines=30)
    by_file = {fp.file: fp.risk for fp in plan.files}
    assert by_file == {
        "app/auth/login.py": "high",
        "tests/test_x.py": "low",
        "app/util.py": "medium",
    }
