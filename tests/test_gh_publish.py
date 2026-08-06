"""Publishing tests: line-based Reviews API, sticky summary upsert, Check Runs, tone."""

from __future__ import annotations

import json
from pathlib import Path

from pytest_httpx import HTTPXMock

from pr_sentinel.audit import AuditLog
from pr_sentinel.gh.client import GitHubClient
from pr_sentinel.gh.publish import (
    SUMMARY_MARKER,
    build_review_comments,
    check_run_conclusion,
    comment_body,
    post_review,
    publish_report,
    rule_link,
    summary_body,
    upsert_summary,
)
from pr_sentinel.models import Finding, ReviewReport

BASE = "https://api.github.com"
COMMENTS_URL = f"{BASE}/repos/o/r/issues/7/comments?per_page=100"


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "agent": "security",
        "file": "app/db.py",
        "line_start": 12,
        "line_end": 12,
        "severity": "critical",
        "confidence": 0.91,
        "rule_id": "CWE-89",
        "title": "Parameterize the SQL query",
        "fact": "User input is concatenated into SQL.",
        "assumption": None,
        "impact": "SQL injection.",
        "recommendation": "Use parameters.",
        "evidence_quote": 'execute("... " + x)',
    }
    base.update(overrides)
    return Finding.model_validate(base)


def _report(findings: list[Finding], **overrides: object) -> ReviewReport:
    base: dict[str, object] = {
        "pr_number": 7,
        "head_sha": "deadbeef",
        "model": "claude-sonnet-5",
        "findings": findings,
        "score": 80.0,
        "needs_human_review": True,
    }
    base.update(overrides)
    return ReviewReport.model_validate(base)


def _client() -> GitHubClient:
    return GitHubClient("tok", cache=None)


def test_comment_body_follows_template_and_strips_exclamations() -> None:
    body = comment_body(_finding(recommendation="Fix it now!"))
    assert body.startswith("**[CRITICAL] Parameterize the SQL query**")
    assert "**Fact:**" in body and "**Impact:**" in body and "**Recommendation:**" in body
    assert "cwe.mitre.org/data/definitions/89.html" in body
    assert "**Confidence:** 0.91" in body
    assert "!" not in body  # tone post-filter


def test_build_review_comments_are_line_based_not_positional() -> None:
    single = build_review_comments([_finding(line_start=12, line_end=12)])[0]
    assert single["path"] == "app/db.py"
    assert single["line"] == 12
    assert single["side"] == "RIGHT"
    assert "position" not in single
    assert "start_line" not in single

    multi = build_review_comments([_finding(line_start=10, line_end=13)])[0]
    assert multi["start_line"] == 10
    assert multi["start_side"] == "RIGHT"
    assert multi["line"] == 13
    assert multi["side"] == "RIGHT"


def test_check_run_conclusion() -> None:
    assert check_run_conclusion(_report([_finding(severity="critical")])) == "failure"
    assert check_run_conclusion(_report([_finding(severity="high")])) == "neutral"
    assert check_run_conclusion(_report([], needs_human_review=False)) == "success"


def test_summary_body_has_marker_and_leads_with_top_findings() -> None:
    body = summary_body(_report([_finding(title="Top thing")]))
    assert SUMMARY_MARKER in body
    assert "Top thing" in body
    assert "Score:" in body


def test_upsert_summary_creates_when_absent(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    httpx_mock.add_response(method="GET", url=COMMENTS_URL, json=[])
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/repos/o/r/issues/7/comments", json={"id": 1}
    )
    audit = AuditLog(tmp_path / "audit.jsonl")

    action = upsert_summary(
        _client(), "o", "r", 7, "body " + SUMMARY_MARKER, run_id="run", audit=audit
    )
    assert action == "created"
    assert sum(r.method == "POST" for r in httpx_mock.get_requests()) == 1


def test_upsert_summary_updates_when_marker_present(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    existing = [{"id": 99, "body": f"old summary {SUMMARY_MARKER}"}]
    httpx_mock.add_response(method="GET", url=COMMENTS_URL, json=existing)
    httpx_mock.add_response(
        method="PATCH", url=f"{BASE}/repos/o/r/issues/comments/99", json={"id": 99}
    )
    audit = AuditLog(tmp_path / "audit.jsonl")

    action = upsert_summary(
        _client(), "o", "r", 7, "new " + SUMMARY_MARKER, run_id="run", audit=audit
    )
    assert action == "updated"
    assert any(r.method == "PATCH" for r in httpx_mock.get_requests())


def test_publish_report_posts_one_review_and_audits(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/repos/o/r/pulls/7/reviews", json={"id": 5}
    )
    httpx_mock.add_response(method="GET", url=COMMENTS_URL, json=[])
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/repos/o/r/issues/7/comments", json={"id": 6}
    )
    httpx_mock.add_response(method="POST", url=f"{BASE}/repos/o/r/check-runs", json={"id": 8})
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path)

    result = publish_report(
        _client(), "o", "r", 7, _report([_finding()]),
        max_comments=25, author="octocat", run_id="run", audit=audit,
    )
    assert result.comments_posted == 1
    assert result.summary_action == "created"
    assert result.check_conclusion == "failure"  # a critical finding

    review_req = next(r for r in httpx_mock.get_requests() if r.url.path.endswith("/reviews"))
    payload = json.loads(review_req.content)
    assert payload["event"] == "COMMENT"  # never APPROVE
    assert payload["comments"][0]["side"] == "RIGHT"
    assert payload["comments"][0]["line"] == 12

    actions = [json.loads(line)["action"] for line in audit_path.read_text().splitlines()]
    assert "post_review_comment" in actions
    assert "create_check_run" in actions


def test_rule_link_variants() -> None:
    assert rule_link(_finding(rule_id="OWASP-A03:2021")) == "https://owasp.org/Top10/"
    assert rule_link(_finding(rule_id="PEP8-E722")) == "https://peps.python.org/pep-0008/"
    assert rule_link(_finding(rule_id="SENTINEL-BUG-014")) is None
    assert rule_link(_finding(references=["https://example.com/x"])) == "https://example.com/x"


def test_comment_body_includes_assumption_when_present() -> None:
    body = comment_body(_finding(assumption="the input is attacker-controlled"))
    assert "Assumption: the input is attacker-controlled" in body


def test_summary_body_collapses_remainder_and_reports_suppressed() -> None:
    findings = [_finding(severity="low", rule_id=f"R{i}", line_start=i) for i in range(1, 6)]
    body = summary_body(_report(findings), suppressed=2)
    assert "More" in body
    assert "suppressed" in body


def test_post_review_suppresses_beyond_max_comments(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/repos/o/r/pulls/7/reviews", json={"id": 1}
    )
    findings = [_finding(rule_id=f"R{i}", line_start=i) for i in range(1, 4)]
    posted, suppressed = post_review(
        _client(), "o", "r", 7, _report(findings),
        max_comments=1, author=None, run_id="run", audit=AuditLog(tmp_path / "a.jsonl"),
    )
    assert posted == 1
    assert suppressed == 2
