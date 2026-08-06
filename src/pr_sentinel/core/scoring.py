"""Scoring, human-review escalation, and presentation-shaping rules (CLAUDE.md).

Score = max(0, 100 - (critical*20 + high*10 + medium*5 + low*1)), per file first;
PR score = Sum(file_score * changed_lines) / Sum(changed_lines). If every specialist
agent errored, the score is 0 regardless of findings -- never a false 100.
"""

from __future__ import annotations

from pr_sentinel.models import Finding

SEVERITY_PENALTY: dict[str, int] = {"critical": 20, "high": 10, "medium": 5, "low": 1}
SPECIALIST_AGENTS = frozenset({"bug", "security", "style", "improvement"})


def file_score(findings: list[Finding]) -> float:
    """Score for a single file: 100 minus the weighted penalty of its findings."""
    penalty = sum(SEVERITY_PENALTY[f.severity] for f in findings)
    return max(0.0, 100.0 - penalty)


def pr_score(
    findings: list[Finding],
    changed_lines_by_file: dict[str, int],
    agent_errors: dict[str, str],
) -> tuple[float, dict[str, float]]:
    """Return (pr_score, per_file_scores).

    If every specialist agent (bug/security/style/improvement) errored, the score
    is forced to 0 -- a previous iteration of this project shipped a false 100/100
    because a bare except swallowed LLM errors; this is the guard against that.
    """
    per_file_scores: dict[str, float] = {
        file: file_score([f for f in findings if f.file == file]) for file in changed_lines_by_file
    }

    if SPECIALIST_AGENTS <= set(agent_errors):
        return 0.0, per_file_scores

    total_lines = sum(changed_lines_by_file.values())
    if total_lines == 0:
        return 100.0, per_file_scores

    weighted = sum(per_file_scores[file] * lines for file, lines in changed_lines_by_file.items())
    return weighted / total_lines, per_file_scores


def needs_human_review(
    findings: list[Finding],
    agent_errors: dict[str, str],
    *,
    injection_detected: bool,
    budget_exhausted: bool,
    diff_lines: int,
    max_diff_lines: int,
) -> tuple[bool, str | None]:
    """CLAUDE.md's escalation triggers, checked in a fixed priority order."""
    if any(f.severity == "critical" for f in findings):
        return True, "a critical finding exists"
    if injection_detected:
        return True, "prompt injection was detected in the diff"
    if budget_exhausted:
        return True, "the LLM call budget was exhausted before full coverage"
    if len(agent_errors) >= 2:
        return True, f"{len(agent_errors)} agents errored"
    if diff_lines > max_diff_lines:
        return True, f"diff has {diff_lines} lines, exceeding MAX_DIFF_LINES={max_diff_lines}"
    return False, None


def partition_by_confidence(
    findings: list[Finding], floor: float
) -> tuple[list[Finding], list[Finding]]:
    """Split into (confident, low_confidence); the latter collapses, never posted inline."""
    confident = [f for f in findings if f.confidence >= floor]
    low_confidence = [f for f in findings if f.confidence < floor]
    return confident, low_confidence


def apply_nit_cap(findings: list[Finding], max_low: int = 5) -> tuple[list[Finding], list[Finding]]:
    """Cap inline "low"-severity findings at `max_low`; the rest collapse into the summary."""
    low = [f for f in findings if f.severity == "low"]
    other = [f for f in findings if f.severity != "low"]
    return other + low[:max_low], low[max_low:]
