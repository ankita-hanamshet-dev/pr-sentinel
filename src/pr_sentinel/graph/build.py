"""The aggregate pipeline as a LangGraph StateGraph: dedup -> ground -> critic ->
(conditional re-critic, max 2 rounds) -> score -> format.

A linear pipeline with exactly one cycle -- no fan-out (CLAUDE.md: the four
specialists are separate Actions jobs, never parallel LangGraph branches within one
process). See graph/state.py for the reducer rationale behind every ReviewState key.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, StateGraph

from pr_sentinel.agents.critic import CriticAgent
from pr_sentinel.core.dedup import dedup_findings
from pr_sentinel.core.grounding import ground_findings
from pr_sentinel.core.scoring import (
    apply_nit_cap,
    needs_human_review,
    partition_by_confidence,
    pr_score,
)
from pr_sentinel.graph.state import ReviewState
from pr_sentinel.models import Finding, Hunk, ReviewReport

MAX_CRITIC_ROUNDS = 2


def dedup_node(state: ReviewState) -> dict[str, object]:
    """Sole writer of deduped_findings/corroboration; runs exactly once."""
    merged, corroboration = dedup_findings(state["raw_findings"])
    return {"deduped_findings": merged, "corroboration": corroboration}


def ground_node(state: ReviewState) -> dict[str, object]:
    """Sole writer of grounded_findings/grounding_rejects; runs exactly once."""
    kept, rejects = ground_findings(state["deduped_findings"], state["hunks"])
    return {"grounded_findings": kept, "grounding_rejects": rejects}


def make_critic_node(
    critic_agent: CriticAgent, diff_text: str
) -> Callable[[ReviewState], dict[str, object]]:
    """Close over the CriticAgent (an LLM-calling dependency) rather than storing it
    in ReviewState, which stays plain, serializable data.
    """

    def critic_node(state: ReviewState) -> dict[str, object]:
        # Round 1 reviews the grounded set; round 2 (if triggered) re-reviews round
        # 1's own output, since critic_findings overwrites (not accumulates) across
        # rounds.
        if state["critic_rounds"] > 0:
            candidates = state["critic_findings"]
        else:
            candidates = state["grounded_findings"]
        outcome = critic_agent.review(candidates, diff_text=diff_text)
        update: dict[str, object] = {
            "critic_findings": outcome.findings,
            "critic_drops": outcome.drops,
            "critic_rounds": 1,
        }
        if outcome.error is not None:
            # merge_str_dicts only unions -- a round-1 error is never cleared by a
            # successful round 2. That's deliberate, not an oversight: "the critic
            # needed a retry" is itself a signal worth keeping for human review,
            # even once it recovers, rather than silently erasing that it happened.
            update["agent_errors"] = {"critic": outcome.error}
        return update

    return critic_node


def _should_recritic(state: ReviewState) -> str:
    """Re-run critic once more (max 2 total) only if it errored and gave up."""
    if state["critic_rounds"] < MAX_CRITIC_ROUNDS and "critic" in state["agent_errors"]:
        return "critic"
    return "score"


def score_node(state: ReviewState) -> dict[str, object]:
    """Sole writer of score/per_file_scores/needs_human_review*; runs exactly once."""
    score, per_file_scores = pr_score(
        state["critic_findings"], state["changed_lines_by_file"], state["agent_errors"]
    )
    escalate, reason = needs_human_review(
        state["critic_findings"],
        state["agent_errors"],
        injection_detected=state["injection_detected"],
        budget_exhausted=state["budget_exhausted"],
        diff_lines=state["diff_lines"],
        max_diff_lines=state["max_diff_lines"],
    )
    return {
        "score": score,
        "per_file_scores": per_file_scores,
        "needs_human_review": escalate,
        "needs_human_review_reason": reason,
    }


def format_node(state: ReviewState) -> dict[str, object]:
    """Sole writer of review_report; runs exactly once."""
    confident, _low_confidence = partition_by_confidence(
        state["critic_findings"], state["confidence_floor"]
    )
    inline_findings, _nit_overflow = apply_nit_cap(confident)
    report = ReviewReport(
        pr_number=state["pr_number"],
        head_sha=state["head_sha"],
        model=state["model"],
        findings=inline_findings,
        score=state["score"],
        per_file_scores=state["per_file_scores"],
        agent_errors=state["agent_errors"],
        budget_used=state["budget_used"],
        grounding_rejects=len(state["grounding_rejects"]),
        needs_human_review=state["needs_human_review"],
        prompt_versions=state["prompt_versions"],
        duration_ms=0,  # patched by the caller; spans more than just this graph
    )
    return {"review_report": report}


def build_aggregate_graph(critic_agent: CriticAgent, diff_text: str) -> Any:
    """Compile the aggregate StateGraph. `Any` return type: langgraph ships no
    usable stubs under our mypy config (see pyproject.toml's override).
    """
    graph: StateGraph[ReviewState] = StateGraph(ReviewState)
    graph.add_node("dedup", dedup_node)
    graph.add_node("ground", ground_node)
    graph.add_node("critic", make_critic_node(critic_agent, diff_text))
    graph.add_node("score", score_node)
    graph.add_node("format", format_node)
    graph.set_entry_point("dedup")
    graph.add_edge("dedup", "ground")
    graph.add_edge("ground", "critic")
    graph.add_conditional_edges("critic", _should_recritic, {"critic": "critic", "score": "score"})
    graph.add_edge("score", "format")
    graph.add_edge("format", END)
    return graph.compile()


def run_aggregate_pipeline(
    critic_agent: CriticAgent,
    diff_text: str,
    *,
    pr_number: int,
    head_sha: str,
    model: str,
    raw_findings: list[Finding],
    hunks: list[Hunk],
    changed_lines_by_file: dict[str, int],
    max_diff_lines: int,
    diff_lines: int,
    confidence_floor: float,
    injection_detected: bool,
    budget_exhausted: bool,
    prompt_versions: dict[str, str],
    agent_errors: dict[str, str],
    budget_used: int,
) -> ReviewReport:
    """Run the compiled graph once and return the resulting ReviewReport."""
    compiled = build_aggregate_graph(critic_agent, diff_text)
    initial_state: ReviewState = {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "model": model,
        "raw_findings": raw_findings,
        "hunks": hunks,
        "changed_lines_by_file": changed_lines_by_file,
        "max_diff_lines": max_diff_lines,
        "diff_lines": diff_lines,
        "confidence_floor": confidence_floor,
        "injection_detected": injection_detected,
        "budget_exhausted": budget_exhausted,
        "prompt_versions": prompt_versions,
        "budget_used": budget_used,
        "deduped_findings": [],
        "corroboration": {},
        "grounded_findings": [],
        "grounding_rejects": [],
        "critic_findings": [],
        "critic_drops": [],
        "critic_rounds": 0,
        "agent_errors": agent_errors,
        "score": 0.0,
        "per_file_scores": {},
        "needs_human_review": False,
        "needs_human_review_reason": None,
        "review_report": ReviewReport(pr_number=pr_number, head_sha=head_sha, model=model),
    }
    final_state = compiled.invoke(initial_state)
    report = final_state["review_report"]
    assert isinstance(report, ReviewReport)
    return report
