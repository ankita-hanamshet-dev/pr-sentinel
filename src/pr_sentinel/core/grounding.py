"""The 3-step verbatim-evidence grounding filter (CLAUDE.md): the single most
important quality mechanism. A finding failing any step is dropped, counted in
grounding_rejects, and never posted.

Interpretation note on step 2 ("line_start/line_end fall inside a changed line
range of that file in this PR"): read as the union of every hunk's new-image span
(new_start..new_start+new_len-1) for that file. This lets a finding cite a context
line the diff shows immediately next to a real change (still within the same
hunk), while rejecting anything outside every hunk entirely -- stricter than "any
line in the file", looser than "must itself be a '+' line".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pr_sentinel.models import Finding, Hunk

TAXONOMY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^CWE-\d+$"),
    re.compile(r"^OWASP-A\d{2}:2021$"),
    re.compile(r"^PEP8-[A-Z]\d+$"),
    re.compile(r"^SENTINEL-[A-Z]+-\d+$"),
)


@dataclass(frozen=True)
class GroundingRejection:
    """One finding dropped by the grounding filter, and why."""

    finding_id: str
    reason: str


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _rule_id_known(rule_id: str) -> bool:
    return any(pattern.match(rule_id) for pattern in TAXONOMY_PATTERNS)


def _hunk_span_contains(hunk: Hunk, line_start: int, line_end: int) -> bool:
    span_start = hunk.new_start
    span_end = hunk.new_start + hunk.new_len - 1
    return line_start >= span_start and line_end <= span_end and line_start <= line_end


def _numbered_lines(hunk: Hunk) -> dict[int, str]:
    """Map each post-image line number in `hunk` to its content (context + added only).

    Removed ("-") lines have no post-image line number and are never recorded.
    """
    numbered: dict[int, str] = {}
    line_no = hunk.new_start
    for raw in hunk.lines:
        tag, content = raw[:1], raw[1:]
        if tag in (" ", "+"):
            numbered[line_no] = content
            line_no += 1
    return numbered


def _claimed_range_text(hunk: Hunk, line_start: int, line_end: int) -> str:
    numbered = _numbered_lines(hunk)
    return "\n".join(numbered[n] for n in range(line_start, line_end + 1) if n in numbered)


def _grounding_failure_reason(finding: Finding, file_hunks: list[Hunk]) -> str | None:
    if not _rule_id_known(finding.rule_id):
        return f"rule_id {finding.rule_id!r} does not match any known taxonomy"

    if not file_hunks:
        return f"no hunks found for file {finding.file!r} in this diff"

    matching_hunks = [
        hunk
        for hunk in file_hunks
        if _hunk_span_contains(hunk, finding.line_start, finding.line_end)
    ]
    if not matching_hunks:
        return "line_start/line_end do not fall within any changed line range for this file"

    evidence_norm = _normalize(finding.evidence_quote)
    if not evidence_norm:
        return "evidence_quote is empty"

    # evidence_quote must appear within the SPECIFIC line_start..line_end range it
    # claims, not just anywhere in the hunk -- otherwise a finding could cite line 9
    # while quoting line 8's harmless text, and grounding would never notice.
    for hunk in matching_hunks:
        claimed = _claimed_range_text(hunk, finding.line_start, finding.line_end)
        if evidence_norm in _normalize(claimed):
            return None
    return "evidence_quote is not a verbatim (normalized) substring of the claimed line range"


def ground_findings(
    findings: list[Finding], hunks: list[Hunk]
) -> tuple[list[Finding], list[GroundingRejection]]:
    """Apply the 3-step verbatim-evidence filter; return (kept, rejected)."""
    hunks_by_file: dict[str, list[Hunk]] = {}
    for hunk in hunks:
        hunks_by_file.setdefault(hunk.file, []).append(hunk)

    kept: list[Finding] = []
    rejects: list[GroundingRejection] = []
    for finding in findings:
        reason = _grounding_failure_reason(finding, hunks_by_file.get(finding.file, []))
        if reason is None:
            kept.append(finding)
        else:
            rejects.append(GroundingRejection(finding_id=finding.id, reason=reason))
    return kept, rejects
