"""Heuristic triage: per-file risk classification with zero LLM calls (CLAUDE.md role).

This is the analyze-side triage. It emits the SAME TriagePlan schema the LLM triage
agent does, so the strategy is a config flag (PR_SENTINEL_TRIAGE_STRATEGY):

  * heuristic - this module alone; nothing escalates.
  * llm       - the LLM agent classifies every file.
  * hybrid    - this module first, then the LLM refines ONLY risk="unknown" files.

A file is marked risk="unknown" when the heuristic cannot classify it confidently
(unclassified language, or the whole diff is oversized). Unknown files still get a
conservative agent set so pure-heuristic mode never leaves a file unreviewed; the
"unknown" label is purely the signal that the publish side may escalate it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pr_sentinel.models import AgentName, Risk, TriageFilePlan, TriagePlan

# Path fragments marking security/correctness-sensitive code -> high risk.
_HIGH_RISK = re.compile(
    r"auth|login|logout|passwd|password|secret|token|credential|session|cookie|"
    r"crypto|cipher|encrypt|decrypt|jwt|oauth|saml|"
    r"payment|billing|charge|invoice|checkout|"
    r"sql|query|database|migrat|"
    r"exec|eval|subprocess|shell|deserial|pickle|marshal|"
    r"admin|permission|privil|"
    r"upload|download|redirect|proxy|ssrf|"
    r"route|handler|endpoint|controller|middleware",
    re.IGNORECASE,
)
_TEST = re.compile(
    r"(^|/)(tests?|spec|__tests__)/|(^|/)test_|_test\.|\.spec\.|\.test\.", re.IGNORECASE
)
_DOCS_CONFIG = re.compile(
    r"\.(md|rst|txt|json|ya?ml|toml|ini|cfg|lock)$|(^|/)docs?/", re.IGNORECASE
)

# A single file changing this many lines is a high-risk change regardless of path.
_HIGH_CHANGE_LINES = 120

# security is never skipped (CLAUDE.md); every bucket includes it.
_AGENTS_BY_RISK: dict[Risk, list[AgentName]] = {
    "high": ["bug", "security", "style", "improvement"],
    "medium": ["bug", "security", "style"],
    "low": ["security"],
    "unknown": ["bug", "security", "style"],  # conservative if never refined
}


@dataclass(frozen=True)
class TriageInput:
    """One file's inputs to the heuristic: path, detected language, changed-line count."""

    file: str
    language: str
    changed_lines: int


def agents_for_risk(risk: Risk) -> list[AgentName]:
    """Return the specialist set for a risk level (security always included)."""
    return list(_AGENTS_BY_RISK[risk])


def classify_file(item: TriageInput) -> Risk:
    """Classify one file's risk; 'unknown' means 'escalate to LLM triage'."""
    if item.language == "unknown":
        return "unknown"
    if _HIGH_RISK.search(item.file) or item.changed_lines >= _HIGH_CHANGE_LINES:
        return "high"
    if _TEST.search(item.file) or _DOCS_CONFIG.search(item.file):
        return "low"
    return "medium"


def heuristic_triage(
    inputs: list[TriageInput],
    *,
    call_budget: int,
    max_diff_lines: int,
    total_diff_lines: int,
) -> TriagePlan:
    """Classify every file with no model call; an oversized diff marks all files unknown."""
    oversized = total_diff_lines > max_diff_lines
    files: list[TriageFilePlan] = []
    for item in inputs:
        risk: Risk = "unknown" if oversized else classify_file(item)
        files.append(
            TriageFilePlan(
                file=item.file,
                language=item.language,
                risk=risk,
                agents_to_run=agents_for_risk(risk),
            )
        )
    return TriagePlan(files=files, llm_call_budget=call_budget)
