"""ReviewState: the aggregate pipeline's LangGraph state, with a reducer choice
documented at every key.

This graph has no fan-out (CLAUDE.md: the four specialists are separate Actions
jobs, never parallel LangGraph branches) -- it is a linear pipeline with exactly
one cycle (the critic can re-run once, max 2 rounds). Most keys therefore have
exactly one writer and need NO reducer; a reducer is added only where there is a
genuine second writer or a deliberate cross-round accumulation choice.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from pr_sentinel.agents.critic import CriticDrop
from pr_sentinel.core.grounding import GroundingRejection
from pr_sentinel.models import Finding, Hunk, ReviewReport


def merge_str_dicts(a: dict[str, str], b: dict[str, str]) -> dict[str, str]:
    """Custom reducer for `agent_errors`: dict union, later writer wins per key.

    `operator.add` does not work on dict (raises TypeError at runtime) -- this is
    the one key in this graph with a genuine second writer: the initial state
    seeds it with the four specialists' errors from Phase 5, and the `critic` node
    may add its own entry if its LLM call gives up.
    """
    return {**a, **b}


class ReviewState(TypedDict):
    """Aggregate-pipeline state. Reducer for each key is documented at its definition."""

    # ---- Inputs: set once in the initial state, never written by any node ----
    pr_number: int
    head_sha: str
    model: str
    raw_findings: list[Finding]
    hunks: list[Hunk]
    changed_lines_by_file: dict[str, int]
    max_diff_lines: int
    diff_lines: int
    confidence_floor: float
    injection_detected: bool
    budget_exhausted: bool
    prompt_versions: dict[str, str]
    budget_used: int

    # ---- dedup node: sole writer, runs exactly once ----
    deduped_findings: list[Finding]
    corroboration: dict[str, list[str]]

    # ---- ground node: sole writer, runs exactly once. Re-grounding after critic
    # would be redundant -- critic can only ever change a finding's severity
    # (agents/critic.py's _validate_critic_output enforces this), and grounding
    # never looks at severity, so a second ground pass could never change the
    # outcome. No reducer needed: single writer, single invocation.
    grounded_findings: list[Finding]
    grounding_rejects: list[GroundingRejection]

    # ---- critic node: sole writer, but invoked up to twice (the conditional
    # re-critic loop). Three different reducer choices for three different reasons:

    # Deliberate OVERWRITE, no reducer: round 2's findings supersede round 1's --
    # concatenating would double-count. "Last write wins" is correct here despite
    # two invocations, since only one round's verdict should ever be current.
    critic_findings: list[Finding]

    # ACCUMULATE across rounds: a design choice, not a crash-avoidance necessity
    # (this graph has no fan-out, so nothing would crash without a reducer here).
    # Round 1's drop reasons stay on record even if round 2 runs, for a complete
    # audit trail of everything the critic ever removed.
    critic_drops: Annotated[list[CriticDrop], operator.add]

    # ACCUMULATE: each critic invocation returns a delta of 1; the conditional
    # edge reads the running total against a max-2 cap.
    critic_rounds: Annotated[int, operator.add]

    # CUSTOM reducer: the one genuine two-different-writers key in this graph --
    # see merge_str_dicts() above.
    agent_errors: Annotated[dict[str, str], merge_str_dicts]

    # ---- score node: sole writer, runs exactly once ----
    score: float
    per_file_scores: dict[str, float]
    needs_human_review: bool
    needs_human_review_reason: str | None

    # ---- format node: sole writer, runs exactly once. Note: `duration_ms` on the
    # resulting ReviewReport is intentionally left at 0 here and patched by the
    # caller after graph.invoke() returns -- wall-clock duration spans the
    # specialist-agent phase too, which happens entirely outside this graph.
    review_report: ReviewReport
