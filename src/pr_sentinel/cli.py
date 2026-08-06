"""PR Sentinel command-line interface (typer). Commands are wired in later phases."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from pr_sentinel.agents.base import ChunkAgent
from pr_sentinel.agents.bug import BugAgent
from pr_sentinel.agents.critic import CriticAgent
from pr_sentinel.agents.improvement import ImprovementAgent
from pr_sentinel.agents.security import SecurityAgent
from pr_sentinel.agents.style import StyleAgent
from pr_sentinel.agents.triage import TriageAgent
from pr_sentinel.audit import DEFAULT_AUDIT_PATH, AuditLog
from pr_sentinel.core.chunking import budget_from_settings, chunk_hunks
from pr_sentinel.core.dedup import dedup_findings
from pr_sentinel.core.grounding import GroundingRejection, ground_findings
from pr_sentinel.core.language import detect_language
from pr_sentinel.core.scoring import (
    SPECIALIST_AGENTS,
    apply_nit_cap,
    needs_human_review,
    partition_by_confidence,
    pr_score,
)
from pr_sentinel.gh.diff import FileDiff, added_lines, parse_diff
from pr_sentinel.guardrails.policy import is_ignored_path
from pr_sentinel.llm.budget import BudgetExhausted, BudgetGovernor
from pr_sentinel.llm.cache import LLMCache
from pr_sentinel.llm.provider import LLMError, LLMRequest, call_llm, get_provider
from pr_sentinel.models import Finding, Hunk, ReviewReport
from pr_sentinel.settings import get_settings

_CHUNK_AGENT_CLASSES: dict[str, type[ChunkAgent]] = {
    "bug": BugAgent,
    "security": SecurityAgent,
    "style": StyleAgent,
    "improvement": ImprovementAgent,
}

app = typer.Typer(
    name="pr-sentinel",
    no_args_is_help=True,
    add_completion=False,
    help="Multi-agent AI code reviewer that runs inside GitHub Actions.",
)


def _pending(command: str, phase: str) -> NoReturn:
    """Report that a command's implementation arrives in a later build phase."""
    typer.echo(f"'{command}' is not implemented yet (arrives in {phase}).", err=True)
    raise typer.Exit(code=1)


@app.command()
def analyze(
    repo: Annotated[str, typer.Option(help="owner/name of the repository")],
    pr: Annotated[int, typer.Option(help="pull request number")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="write payload, do not post")] = False,
) -> None:
    """Run the specialist agents over a PR diff and emit a review payload."""
    _pending("analyze", "Phase 6")


@app.command()
def aggregate(
    payload: Annotated[
        str, typer.Option(help="path for the merged payload")
    ] = "review-payload.json",
) -> None:
    """Merge per-agent artifacts through the grounding/critic/scoring pipeline."""
    _pending("aggregate", "Phase 6")


@app.command()
def publish(
    repo: Annotated[str, typer.Option(help="owner/name of the repository")],
    pr: Annotated[int, typer.Option(help="pull request number")],
    payload: Annotated[str, typer.Option(help="review payload to publish")] = "review-payload.json",
) -> None:
    """Post the consolidated review, summary comment, and check run to a PR."""
    _pending("publish", "Phase 6")


def _snippet(hunks: list[Hunk]) -> str:
    """Reconstruct post-image context+added text from hunks, for language detection."""
    lines: list[str] = []
    for hunk in hunks:
        lines.extend(raw[1:] for raw in hunk.lines if raw[:1] in (" ", "+"))
    return "\n".join(lines)


def _changed_lines_by_file(hunks_by_file: dict[str, list[Hunk]]) -> dict[str, int]:
    return {
        file: sum(len(added_lines(hunk)) for hunk in hunks) for file, hunks in hunks_by_file.items()
    }


def _build_files_summary(files: list[FileDiff], languages: dict[str, str]) -> str:
    lines = []
    for f in files:
        added = sum(len(added_lines(h)) for h in f.hunks)
        lines.append(
            f"{f.path} ({languages.get(f.path, 'unknown')}, {len(f.hunks)} hunks, "
            f"{added} added lines)"
        )
    return "\n".join(lines) or "(no files changed)"


def _run_local_review(
    fixture_path: Path, provider_name: str
) -> tuple[ReviewReport, str | None, list[GroundingRejection], list[Finding], list[Finding]]:
    """Linear (non-LangGraph) pipeline: triage -> chunk -> specialists -> dedup ->
    ground -> critic -> score. Phase 6 re-homes this orchestration into
    graph/build.py's LangGraph pipeline without changing these building blocks.
    """
    start = time.perf_counter()
    diff_text = (fixture_path / "diff.patch").read_text(encoding="utf-8")

    # .aireviewignore-matched files never reach a specialist or the model at all --
    # the same rule policy.check_action("send_to_model", path) enforces.
    file_diffs = [
        f for f in parse_diff(diff_text) if not f.is_binary and not is_ignored_path(f.path)
    ]
    hunks_by_file: dict[str, list[Hunk]] = {f.path: f.hunks for f in file_diffs if f.hunks}
    all_hunks = [hunk for hunks in hunks_by_file.values() for hunk in hunks]
    languages = {f.path: detect_language(f.path, _snippet(f.hunks) or None) for f in file_diffs}
    file_paths = list(hunks_by_file)
    changed_file_paths = frozenset(file_paths)

    settings = get_settings().model_copy(update={"llm_provider": provider_name})
    provider = get_provider(settings)
    governor = BudgetGovernor(settings)
    cache = LLMCache(path=Path(".sentinel/cache.sqlite"), ttl_days=settings.cache_ttl_days)
    AuditLog(DEFAULT_AUDIT_PATH)  # ensures .sentinel/ exists; wired into agents in Phase 6/7
    run_id = uuid.uuid4().hex[:12]
    agent_kwargs: dict[str, object] = {
        "cache": cache,
        "governor": governor,
        "provider_name": provider_name,
        "model": settings.model,
        "max_output_tokens": settings.max_output_tokens,
        "run_id": run_id,
    }

    triage_agent = TriageAgent(provider, **agent_kwargs)  # type: ignore[arg-type]
    triage_plan = triage_agent.plan(
        call_budget=settings.max_llm_calls_per_run,
        files_summary=_build_files_summary(file_diffs, languages),
        pr_metadata="",
        ci_results="",
        file_paths=file_paths,
        languages=languages,
    )

    findings: list[Finding] = []
    agent_error_lists: dict[str, list[str]] = defaultdict(list)
    injection_detected = False
    budget_exhausted = False
    prompt_versions: dict[str, str] = {"triage": triage_agent.prompt_version}

    for file_plan in triage_plan.files:
        if file_plan.skip_reason is not None:
            continue
        hunks = hunks_by_file.get(file_plan.file, [])
        if not hunks:
            continue
        chunks = chunk_hunks(hunks, max_tokens=budget_from_settings())
        for role in file_plan.agents_to_run:
            agent_cls = _CHUNK_AGENT_CLASSES.get(role)
            if agent_cls is None:
                continue
            agent = agent_cls(provider, **agent_kwargs)  # type: ignore[arg-type]
            prompt_versions[role] = agent.prompt_version
            for chunk in chunks:
                try:
                    if role == "style":
                        outcome = agent.review(
                            chunk,
                            language=file_plan.language,
                            changed_file_paths=changed_file_paths,
                        )
                    else:
                        outcome = agent.review(chunk, language=file_plan.language)
                except BudgetExhausted:
                    budget_exhausted = True
                    continue
                findings.extend(outcome.findings)
                findings.extend(outcome.injection_findings)
                findings.extend(outcome.redaction_findings)
                if outcome.injection_findings:
                    injection_detected = True
                agent_error_lists[role].extend(outcome.errors)

    merged_findings, _corroboration = dedup_findings(findings)
    kept_findings, rejects = ground_findings(merged_findings, all_hunks)

    final_findings = kept_findings
    if kept_findings:
        critic_agent = CriticAgent(provider, **agent_kwargs)  # type: ignore[arg-type]
        prompt_versions["critic"] = critic_agent.prompt_version
        try:
            critic_outcome = critic_agent.review(kept_findings, diff_text=diff_text)
            final_findings = critic_outcome.findings
        except BudgetExhausted:
            budget_exhausted = True

    agent_errors = {role: "; ".join(msgs) for role, msgs in agent_error_lists.items() if msgs}
    changed_lines_by_file = _changed_lines_by_file(hunks_by_file)
    score, per_file_scores = pr_score(final_findings, changed_lines_by_file, agent_errors)

    diff_line_count = sum(len(hunk.lines) for hunk in all_hunks)
    escalate, escalate_reason = needs_human_review(
        final_findings,
        agent_errors,
        injection_detected=injection_detected,
        budget_exhausted=budget_exhausted,
        diff_lines=diff_line_count,
        max_diff_lines=settings.max_diff_lines,
    )

    _confident, low_confidence = partition_by_confidence(final_findings, settings.confidence_floor)
    inline_findings, nit_overflow = apply_nit_cap(_confident)

    report = ReviewReport(
        pr_number=0,
        head_sha="local",
        model=settings.model,
        findings=inline_findings,
        score=score,
        per_file_scores=per_file_scores,
        agent_errors=agent_errors,
        budget_used=governor.snapshot()["calls_used"],
        grounding_rejects=len(rejects),
        needs_human_review=escalate,
        prompt_versions=prompt_versions,
        duration_ms=int((time.perf_counter() - start) * 1000),
    )
    return report, escalate_reason, rejects, low_confidence, nit_overflow


def _print_local_report(
    report: ReviewReport,
    escalate_reason: str | None,
    rejects: list[GroundingRejection],
    low_confidence: list[Finding],
    nit_overflow: list[Finding],
) -> None:
    if SPECIALIST_AGENTS <= set(report.agent_errors):
        typer.echo("=" * 60)
        typer.echo("FAILURE: every specialist agent errored -- score is 0, not a false 100")
        typer.echo("=" * 60)
    typer.echo(f"score: {report.score:.1f}/100  needs_human_review={report.needs_human_review}")
    if escalate_reason is not None:
        typer.echo(f"  reason: {escalate_reason}")
    typer.echo(
        f"budget_used={report.budget_used}  grounding_rejects={report.grounding_rejects}  "
        f"duration_ms={report.duration_ms}"
    )
    if report.agent_errors:
        typer.echo(f"agent_errors: {report.agent_errors}")
    typer.echo(f"prompt_versions: {report.prompt_versions}")

    typer.echo(f"\nfindings ({len(report.findings)}):")
    for finding in report.findings:
        typer.echo(
            f"  [{finding.severity.upper()}] {finding.file}:{finding.line_start} "
            f"{finding.rule_id} {finding.title}"
        )
        typer.echo(f"      evidence: {finding.evidence_quote!r}")

    if low_confidence:
        typer.echo(f"\nLow-confidence observations (collapsed, {len(low_confidence)}):")
        for finding in low_confidence:
            severity = finding.severity.upper()
            typer.echo(f"  [{severity}] {finding.file}:{finding.line_start} {finding.title}")

    if nit_overflow:
        typer.echo(f"\nAdditional low-severity nits (collapsed, {len(nit_overflow)}):")
        for finding in nit_overflow:
            typer.echo(f"  {finding.file}:{finding.line_start} {finding.title}")

    if rejects:
        typer.echo(f"\ngrounding_rejects ({len(rejects)}):")
        for reject in rejects:
            typer.echo(f"  {reject.finding_id}: {reject.reason}")


@app.command()
def local(
    path: Annotated[str, typer.Option(help="fixture directory with a diff to review")],
    provider: Annotated[str, typer.Option(help="LLM provider override")] = "replay",
) -> None:
    """Run a single offline review case from a local fixture directory."""
    fixture_path = Path(path)
    diff_path = fixture_path / "diff.patch"
    if not diff_path.exists():
        typer.echo(f"no diff.patch found at {diff_path}", err=True)
        raise typer.Exit(code=1)

    try:
        report, escalate_reason, rejects, low_confidence, nit_overflow = _run_local_review(
            fixture_path, provider
        )
    except LLMError as exc:
        typer.echo(f"LLM call failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    _print_local_report(report, escalate_reason, rejects, low_confidence, nit_overflow)


@app.command()
def smoke(
    model: Annotated[str, typer.Option(help="model id to exercise")] = "claude-sonnet-5",
) -> None:
    """Make one live LLM call to verify provider wiring and the budget ledger."""
    settings = get_settings()
    settings = settings.model_copy(update={"model": model})
    governor = BudgetGovernor(settings)

    try:
        provider = get_provider(settings)
    except LLMError as exc:
        typer.echo(f"provider setup failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    request = LLMRequest(
        system="You are a smoke test.",
        user="Reply with the single word: pong",
        max_output_tokens=16,
    )

    try:
        response = call_llm(
            provider,
            request,
            cache=None,
            governor=governor,
            provider_name=settings.llm_provider,
            model=settings.model,
            prompt_version="smoke-v1",
            agent="smoke",
        )
    except BudgetExhausted as exc:
        typer.echo(f"budget exhausted: {exc.reason}", err=True)
        raise typer.Exit(code=1) from exc
    except LLMError as exc:
        typer.echo(f"LLM call failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    snapshot = governor.snapshot()
    typer.echo(f"response: {response.text!r}")
    typer.echo(
        f"tokens_in={response.tokens_in} tokens_out={response.tokens_out} "
        f"latency_ms={response.latency_ms}"
    )
    typer.echo(
        f"budget: {snapshot['calls_used']}/{settings.max_llm_calls_per_run} calls used this run"
    )


@app.command(name="eval")
def eval_(
    suite: Annotated[str, typer.Option(help="eval suite name")] = "golden",
) -> None:
    """Replay an eval suite and check metrics against the CLAUDE.md thresholds."""
    _pending("eval", "Phase 8")


@app.command()
def report() -> None:
    """Generate the rolling repo-health report from run history."""
    _pending("report", "Phase 8")


if __name__ == "__main__":
    app()
