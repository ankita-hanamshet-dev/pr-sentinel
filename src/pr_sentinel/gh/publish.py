"""Post a consolidated review to a PR: inline comments, a sticky summary, a Check Run.

Uses the modern line-based Reviews API -- comments carry {path, line, side, body}
(and start_line/start_side for multi-line), never legacy diff-position offsets. The
event is always COMMENT; submitting APPROVE is a policy-denied action (CLAUDE.md).
Every privileged call is recorded in the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from pr_sentinel.audit import AuditLog
from pr_sentinel.gh.client import GitHubClient
from pr_sentinel.guardrails.policy import check_action, check_comment_tone
from pr_sentinel.models import Finding, ReviewReport

logger = structlog.get_logger()

SUMMARY_MARKER = "<!-- pr-sentinel:summary -->"
CHECK_RUN_NAME = "PR Sentinel"
_SEVERITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class PublishResult:
    """What publishing did: counts and the Check Run verdict."""

    comments_posted: int
    comments_suppressed: int
    summary_action: str  # "created" | "updated"
    check_conclusion: str  # "success" | "neutral" | "failure"


def rule_link(finding: Finding) -> str | None:
    """A canonical reference URL for a finding's rule_id, if one is derivable."""
    if finding.references:
        return finding.references[0]
    rule = finding.rule_id
    if rule.startswith("CWE-"):
        return f"https://cwe.mitre.org/data/definitions/{rule[4:]}.html"
    if rule.startswith("OWASP-"):
        return "https://owasp.org/Top10/"
    if rule.startswith("PEP8-"):
        return "https://peps.python.org/pep-0008/"
    return None


def _sanitize_tone(body: str) -> str:
    """Deterministic tone post-filter: exclamation marks are not allowed (CLAUDE.md)."""
    return body.replace("!", ".")


def comment_body(finding: Finding, *, author: str | None = None) -> str:
    """Render the fixed template: title -> Fact -> Impact -> Recommendation -> rule -> confidence.

    Severity is a text label (never colour-only), for accessibility.
    """
    link = rule_link(finding)
    rule_line = f"**Rule:** [{finding.rule_id}]({link})" if link else f"**Rule:** {finding.rule_id}"
    parts = [
        f"**[{finding.severity.upper()}] {finding.title}**",
        "",
        f"**Fact:** {finding.fact}",
        f"**Impact:** {finding.impact}",
        f"**Recommendation:** {finding.recommendation}",
        rule_line,
        f"**Confidence:** {finding.confidence:.2f}",
    ]
    detail = [f"Produced by the {finding.agent} agent."]
    if finding.assumption:
        detail.append(f"Assumption: {finding.assumption}")
    detail.append(f"Evidence: `{finding.evidence_quote}`")
    parts += ["", "<details>", "<summary>PR Sentinel detail</summary>", "", *detail, "</details>"]
    body = "\n".join(parts)

    violations = check_comment_tone(body, author=author)
    if violations:
        logger.info("comment_tone_sanitized", rule_id=finding.rule_id, violations=violations)
    return _sanitize_tone(body)


def build_review_comments(
    findings: list[Finding], *, author: str | None = None
) -> list[dict[str, object]]:
    """Build line-based review comments for the Reviews API (post-image side, RIGHT)."""
    comments: list[dict[str, object]] = []
    for finding in findings:
        comment: dict[str, object] = {
            "path": finding.file,
            "body": comment_body(finding, author=author),
        }
        if finding.line_end > finding.line_start:
            comment["start_line"] = finding.line_start
            comment["start_side"] = "RIGHT"
        comment["line"] = finding.line_end
        comment["side"] = "RIGHT"
        comments.append(comment)
    return comments


def check_run_conclusion(report: ReviewReport) -> str:
    """failure only on a critical finding; neutral if any finding/escalation; else success."""
    if any(f.severity == "critical" for f in report.findings):
        return "failure"
    if report.findings or report.needs_human_review:
        return "neutral"
    return "success"


def summary_body(report: ReviewReport, *, top_n: int = 3, suppressed: int = 0) -> str:
    """Lead with the top-N things to fix now; collapse the rest. Carries the sticky marker."""
    ranked = sorted(
        report.findings, key=lambda f: _SEVERITY_WEIGHT.get(f.severity, 0), reverse=True
    )
    lines = [
        "## PR Sentinel review",
        "",
        f"**Score:** {report.score:.0f}/100 &nbsp; "
        f"**Findings:** {len(report.findings)} &nbsp; "
        f"**Human review:** {'required' if report.needs_human_review else 'not required'}",
        "",
        "### Worth fixing now",
    ]
    if ranked:
        for finding in ranked[:top_n]:
            lines.append(
                f"- **[{finding.severity.upper()}]** `{finding.file}:{finding.line_start}` "
                f"{finding.title} ({finding.rule_id})"
            )
    else:
        lines.append("- No inline findings.")
    if len(ranked) > top_n or suppressed:
        remainder = max(0, len(ranked) - top_n)
        lines += [
            "",
            "<details><summary>More</summary>",
            "",
            f"{remainder} more inline finding(s); {suppressed} suppressed by the comment cap.",
            "</details>",
        ]
    lines += [
        "",
        f"_Model: {report.model} · calls: {report.budget_used} · cost: $0_",
        "",
        SUMMARY_MARKER,
    ]
    return "\n".join(lines)


def post_review(
    client: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    report: ReviewReport,
    *,
    max_comments: int,
    author: str | None,
    run_id: str,
    audit: AuditLog,
) -> tuple[int, int]:
    """POST one line-based COMMENT review. Returns (posted, suppressed)."""
    findings = report.findings
    suppressed = 0
    if len(findings) > max_comments:
        check_action("exceed_max_comments", str(len(findings)))  # policy: cap, never exceed
        suppressed = len(findings) - max_comments
        findings = findings[:max_comments]

    comments = build_review_comments(findings, author=author)
    payload: dict[str, object] = {
        "commit_id": report.head_sha,
        "event": "COMMENT",  # never APPROVE (policy: submit_approve_review is denied)
        "body": "PR Sentinel reviewed this PR — see inline comments and the summary below.",
        "comments": comments,
    }
    client.post(f"/repos/{owner}/{repo}/pulls/{number}/reviews", payload)
    audit.record(
        run_id=run_id,
        actor="pr-sentinel",
        action="post_review_comment",
        target=f"{owner}/{repo}#{number}",
        decision="allow",
        reason=f"posted {len(comments)} inline comment(s), {suppressed} suppressed",
    )
    return len(comments), suppressed


def upsert_summary(
    client: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    body: str,
    *,
    run_id: str,
    audit: AuditLog,
) -> str:
    """Update the sticky summary comment in place if present, else create it once."""
    existing_id: int | None = None
    for comment in client.paginate(f"/repos/{owner}/{repo}/issues/{number}/comments"):
        if isinstance(comment, dict) and SUMMARY_MARKER in str(comment.get("body", "")):
            existing_id = int(comment["id"])
            break

    if existing_id is not None:
        client.patch(f"/repos/{owner}/{repo}/issues/comments/{existing_id}", {"body": body})
        action = "updated"
        policy_action = "update_summary_comment"
    else:
        client.post(f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body})
        action = "created"
        policy_action = "post_summary_comment"
    audit.record(
        run_id=run_id,
        actor="pr-sentinel",
        action=policy_action,
        target=f"{owner}/{repo}#{number}",
        decision="allow",
        reason=f"summary comment {action}",
    )
    return action


def _link_line(details_url: str | None) -> str:
    return f"\n\n[View the full review run]({details_url})" if details_url else ""


def start_check_run(
    client: GitHubClient,
    owner: str,
    repo: str,
    head_sha: str,
    *,
    details_url: str | None = None,
    run_id: str,
    audit: AuditLog,
) -> int:
    """POST an in-progress Check Run against head_sha so the PR shows a pending
    'PR Sentinel' check immediately; returns its id for later completion.

    Created early on the publish side (against workflow_run.head_sha, reachable in
    the base repo via refs/pull/N/head even for fork PRs) so the PR page shows a
    signal before any inference runs.
    """
    payload: dict[str, object] = {
        "name": CHECK_RUN_NAME,
        "head_sha": head_sha,
        "status": "in_progress",
        "output": {
            "title": "PR Sentinel: analyzing",
            "summary": f"Review in progress.{_link_line(details_url)}",
        },
    }
    if details_url:
        payload["details_url"] = details_url
    response = client.post(f"/repos/{owner}/{repo}/check-runs", payload)
    body = response.json_body
    check_run_id = int(body["id"]) if isinstance(body, dict) and "id" in body else 0
    audit.record(
        run_id=run_id,
        actor="pr-sentinel",
        action="start_check_run",
        target=f"{owner}/{repo}@{head_sha}",
        decision="in_progress",
        reason="early in-progress check run",
    )
    return check_run_id


def fail_check_run(
    client: GitHubClient,
    owner: str,
    repo: str,
    check_run_id: int,
    head_sha: str,
    *,
    summary: str = "The review could not be completed.",
    details_url: str | None = None,
    run_id: str,
    audit: AuditLog,
) -> None:
    """PATCH the in-progress check to a visible failure (the silent-failure path)."""
    payload: dict[str, object] = {
        "name": CHECK_RUN_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "failure",
        "output": {
            "title": "PR Sentinel: review could not be completed",
            "summary": f"{summary}{_link_line(details_url)}",
        },
    }
    if details_url:
        payload["details_url"] = details_url
    client.patch(f"/repos/{owner}/{repo}/check-runs/{check_run_id}", payload)
    audit.record(
        run_id=run_id,
        actor="pr-sentinel",
        action="fail_check_run",
        target=f"{owner}/{repo}@{head_sha}",
        decision="failure",
        reason="review could not be completed",
    )


def create_check_run(
    client: GitHubClient,
    owner: str,
    repo: str,
    report: ReviewReport,
    *,
    run_id: str,
    audit: AuditLog,
    check_run_id: int | None = None,
    details_url: str | None = None,
) -> str:
    """Finish the Check Run: update the early in-progress one if given its id, else
    POST a fresh completed one. failure only on a critical finding."""
    conclusion = check_run_conclusion(report)
    summary = (
        f"Score {report.score:.0f}/100, {len(report.findings)} finding(s).{_link_line(details_url)}"
    )
    payload: dict[str, object] = {
        "name": CHECK_RUN_NAME,
        "head_sha": report.head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {"title": f"PR Sentinel: {conclusion}", "summary": summary},
    }
    if details_url:
        payload["details_url"] = details_url
    if check_run_id is not None:
        client.patch(f"/repos/{owner}/{repo}/check-runs/{check_run_id}", payload)
        action = "update_check_run"
    else:
        client.post(f"/repos/{owner}/{repo}/check-runs", payload)
        action = "create_check_run"
    audit.record(
        run_id=run_id,
        actor="pr-sentinel",
        action=action,
        target=f"{owner}/{repo}@{report.head_sha}",
        decision=conclusion,
        reason=f"advisory check run ({conclusion})",
    )
    return conclusion


def publish_report(
    client: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    report: ReviewReport,
    *,
    max_comments: int,
    author: str | None = None,
    run_id: str,
    audit: AuditLog,
    check_run_id: int | None = None,
    details_url: str | None = None,
) -> PublishResult:
    """Post the review, upsert the sticky summary, and finish the Check Run.

    If check_run_id is given, the early in-progress check from start_check_run is
    completed in place instead of a new one being created.
    """
    posted, suppressed = post_review(
        client,
        owner,
        repo,
        number,
        report,
        max_comments=max_comments,
        author=author,
        run_id=run_id,
        audit=audit,
    )
    summary_text = summary_body(report, suppressed=suppressed)
    if details_url:
        summary_text += f"\n\n<sub>[Full review run]({details_url})</sub>"
    action = upsert_summary(
        client,
        owner,
        repo,
        number,
        summary_text,
        run_id=run_id,
        audit=audit,
    )
    conclusion = create_check_run(
        client,
        owner,
        repo,
        report,
        run_id=run_id,
        audit=audit,
        check_run_id=check_run_id,
        details_url=details_url,
    )
    return PublishResult(
        comments_posted=posted,
        comments_suppressed=suppressed,
        summary_action=action,
        check_conclusion=conclusion,
    )
