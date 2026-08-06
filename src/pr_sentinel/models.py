"""Pydantic boundary types shared across PR Sentinel: Finding, Hunk, plans, reports."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field, computed_field

Severity = Literal["critical", "high", "medium", "low"]
AgentName = Literal["bug", "security", "style", "improvement"]
Risk = Literal["high", "medium", "low", "unknown"]
LineEnding = Literal["lf", "crlf", "mixed", "none"]

SEVERITY_ORDER: dict[str, int] = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def finding_id(file: str, line_start: int, rule_id: str, title: str) -> str:
    """Return the stable 12-char id for a finding's identity tuple."""
    payload = f"{file}|{line_start}|{rule_id}|{title}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


class Finding(BaseModel):
    """A single review observation with verbatim evidence and a taxonomy rule id."""

    agent: AgentName
    file: str
    line_start: int
    line_end: int
    severity: Severity
    confidence: float
    rule_id: str
    title: str
    fact: str
    assumption: str | None = None
    impact: str
    recommendation: str
    evidence_quote: str
    suggested_patch: str | None = None
    references: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        """Stable identity hash: sha256(file|line_start|rule_id|title)[:12]."""
        return finding_id(self.file, self.line_start, self.rule_id, self.title)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def severity_rank(self) -> int:
        """Numeric rank for ordering; higher is more severe."""
        return SEVERITY_ORDER[self.severity]


class Hunk(BaseModel):
    """A unified-diff hunk retaining post-image line numbers for added lines.

    `lines` are normalized to LF (any CRLF is stripped); `line_ending` records
    what the source file actually used so downstream can restore it.
    """

    file: str
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = Field(default_factory=list)
    line_ending: LineEnding = "lf"


class TriageFilePlan(BaseModel):
    """Per-file triage decision emitted by the Triage agent."""

    file: str
    language: str
    risk: Risk
    agents_to_run: list[AgentName] = Field(default_factory=list)
    skip_reason: str | None = None


class TriagePlan(BaseModel):
    """The full triage output for a PR: per-file plans plus the call budget."""

    files: list[TriageFilePlan] = Field(default_factory=list)
    llm_call_budget: int = 12


class AgentResult(BaseModel):
    """One specialist's JSON artifact: its findings plus run accounting."""

    agent: AgentName
    findings: list[Finding] = Field(default_factory=list)
    error: str | None = None
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0


class ReviewReport(BaseModel):
    """The consolidated review posted to a PR, per CLAUDE.md."""

    pr_number: int
    head_sha: str
    model: str
    findings: list[Finding] = Field(default_factory=list)
    score: float = 100.0
    per_file_scores: dict[str, float] = Field(default_factory=dict)
    agent_errors: dict[str, str] = Field(default_factory=dict)
    budget_used: int = 0
    grounding_rejects: int = 0
    needs_human_review: bool = False
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    duration_ms: int = 0
