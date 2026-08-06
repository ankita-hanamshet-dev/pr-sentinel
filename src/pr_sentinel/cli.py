"""PR Sentinel command-line interface (typer). Commands are wired in later phases."""

from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Any, NoReturn

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
from pr_sentinel.core.triage import TriageInput, heuristic_triage
from pr_sentinel.gh.client import GitHubClient, GitHubError
from pr_sentinel.gh.context import PRContext, fetch_pr_context
from pr_sentinel.gh.diff import FileDiff, added_lines, parse_diff
from pr_sentinel.gh.history import TeamConventions
from pr_sentinel.gh.publish import fail_check_run, publish_report, start_check_run
from pr_sentinel.graph.build import run_aggregate_pipeline
from pr_sentinel.guardrails.policy import is_ignored_path
from pr_sentinel.llm.budget import BudgetExhausted, BudgetGovernor
from pr_sentinel.llm.cache import LLMCache
from pr_sentinel.llm.provider import LLMError, LLMRequest, call_llm, get_provider
from pr_sentinel.models import Finding, Hunk, ReviewReport, TriageFilePlan, TriagePlan
from pr_sentinel.settings import Settings, get_settings

DEFAULT_PAYLOAD_PATH = "review-payload.json"
DEFAULT_META_PATH = "pr-meta.json"
DEFAULT_BUNDLE_PATH = "analysis-bundle.json"
DEFAULT_CONTEXT_PATH = "context.json"

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


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        typer.echo(f"--repo must be owner/name, got {repo!r}", err=True)
        raise typer.Exit(code=1)
    return owner, name


def _require_token() -> str:
    token = get_settings().github_token
    if not token:
        typer.echo("GITHUB_TOKEN is not set (needed to read the PR / post the review).", err=True)
        raise typer.Exit(code=1)
    return token


def _agent_env(
    provider_override: str | None,
) -> tuple[Settings, object, BudgetGovernor, str, dict[str, object]]:
    """Build the shared LLM environment (settings, provider, governor, run_id, kwargs)."""
    settings = get_settings()
    if provider_override:
        settings = settings.model_copy(update={"llm_provider": provider_override})
    provider = get_provider(settings)
    governor = BudgetGovernor(settings)
    cache = LLMCache(path=Path(".sentinel/cache.sqlite"), ttl_days=settings.cache_ttl_days)
    AuditLog(DEFAULT_AUDIT_PATH)  # ensures .sentinel/ exists
    run_id = uuid.uuid4().hex[:12]
    agent_kwargs: dict[str, object] = {
        "cache": cache,
        "governor": governor,
        "provider_name": settings.llm_provider,
        "model": settings.model,
        "max_output_tokens": settings.max_output_tokens,
        "run_id": run_id,
    }
    return settings, provider, governor, run_id, agent_kwargs


def _run_analyze(
    ctx: PRContext, provider_override: str | None
) -> tuple[ReviewReport, dict[str, object]]:
    """Triage -> specialists -> LangGraph aggregate. Returns the report and a re-usable bundle."""
    start = time.perf_counter()
    settings, provider, governor, _run_id, agent_kwargs = _agent_env(provider_override)

    hunks_by_file = ctx.hunks_by_file()
    all_hunks = [hunk for hunks in hunks_by_file.values() for hunk in hunks]
    changed_file_paths = frozenset(hunks_by_file)

    triage_agent = TriageAgent(provider, **agent_kwargs)  # type: ignore[arg-type]
    triage_plan = triage_agent.plan(
        call_budget=settings.max_llm_calls_per_run,
        files_summary=ctx.files_summary(),
        pr_metadata=f"{ctx.title}\n\n{ctx.body}".strip(),
        ci_results=ctx.ci_summary(),
        file_paths=list(hunks_by_file),
        languages=ctx.languages,
    )
    prompt_versions: dict[str, str] = {"triage": triage_agent.prompt_version}
    findings, injection, budget_ex, agent_error_lists = _collect_findings(
        provider,
        agent_kwargs,
        triage_plan=triage_plan,
        hunks_by_file=hunks_by_file,
        changed_file_paths=changed_file_paths,
        prompt_versions=prompt_versions,
    )
    agent_errors = {role: "; ".join(msgs) for role, msgs in agent_error_lists.items() if msgs}
    changed_lines_by_file = ctx.changed_lines_by_file()
    diff_lines = sum(len(hunk.lines) for hunk in all_hunks)

    critic_agent = CriticAgent(provider, **agent_kwargs)  # type: ignore[arg-type]
    prompt_versions["critic"] = critic_agent.prompt_version
    report = run_aggregate_pipeline(
        critic_agent,
        diff_text=ctx.diff_text,
        pr_number=ctx.number,
        head_sha=ctx.head_sha,
        model=settings.model,
        raw_findings=findings,
        hunks=all_hunks,
        changed_lines_by_file=changed_lines_by_file,
        max_diff_lines=settings.max_diff_lines,
        diff_lines=diff_lines,
        confidence_floor=settings.confidence_floor,
        injection_detected=injection,
        budget_exhausted=budget_ex,
        prompt_versions=prompt_versions,
        agent_errors=agent_errors,
        budget_used=0,
    )
    budget_used = governor.snapshot()["calls_used"]
    report = report.model_copy(
        update={
            "budget_used": budget_used,
            "duration_ms": int((time.perf_counter() - start) * 1000),
        }
    )
    bundle: dict[str, object] = {
        "pr_number": ctx.number,
        "head_sha": ctx.head_sha,
        "model": settings.model,
        "diff_text": ctx.diff_text,
        "raw_findings": [f.model_dump(mode="json") for f in findings],
        "hunks": [h.model_dump(mode="json") for h in all_hunks],
        "changed_lines_by_file": changed_lines_by_file,
        "max_diff_lines": settings.max_diff_lines,
        "diff_lines": diff_lines,
        "confidence_floor": settings.confidence_floor,
        "injection_detected": injection,
        "budget_exhausted": budget_ex,
        "prompt_versions": prompt_versions,
        "agent_errors": agent_errors,
        "budget_used": budget_used,
    }
    return report, bundle


def _aggregate_from_bundle(data: dict[str, Any], critic_agent: CriticAgent) -> ReviewReport:
    """Run the LangGraph aggregate over a serialized analysis bundle (the fan-in step)."""
    raw_findings = [Finding.model_validate(f) for f in data["raw_findings"]]
    hunks = [Hunk.model_validate(h) for h in data["hunks"]]
    return run_aggregate_pipeline(
        critic_agent,
        diff_text=str(data.get("diff_text", "")),
        pr_number=int(data["pr_number"]),
        head_sha=str(data["head_sha"]),
        model=str(data["model"]),
        raw_findings=raw_findings,
        hunks=hunks,
        changed_lines_by_file=dict(data.get("changed_lines_by_file", {})),
        max_diff_lines=int(data.get("max_diff_lines", 5000)),
        diff_lines=int(data.get("diff_lines", 0)),
        confidence_floor=float(data.get("confidence_floor", 0.55)),
        injection_detected=bool(data.get("injection_detected", False)),
        budget_exhausted=bool(data.get("budget_exhausted", False)),
        prompt_versions=dict(data.get("prompt_versions", {})),
        agent_errors=dict(data.get("agent_errors", {})),
        budget_used=int(data.get("budget_used", 0)),
    )


@app.command()
def analyze(
    repo: Annotated[str, typer.Option(help="owner/name of the repository")],
    pr: Annotated[int, typer.Option(help="pull request number")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="write payload, do not post")] = False,
    provider: Annotated[str, typer.Option(help="LLM provider override")] = "",
    out: Annotated[str, typer.Option(help="review payload output path")] = DEFAULT_PAYLOAD_PATH,
) -> None:
    """Run the specialist agents over a PR diff and emit a review payload."""
    owner, name = _split_repo(repo)
    client = GitHubClient(_require_token())
    try:
        ctx = fetch_pr_context(client, owner, name, pr)
    except GitHubError as exc:
        typer.echo(f"failed to fetch PR: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        TeamConventions.from_github(client, owner, name)  # writes team_conventions.md (best effort)
    except GitHubError as exc:
        typer.echo(f"note: could not mine review history: {exc}", err=True)

    try:
        report, bundle = _run_analyze(ctx, provider or None)
    except LLMError as exc:
        typer.echo(f"LLM call failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    Path(out).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    Path(DEFAULT_META_PATH).write_text(
        json.dumps({"pr_number": ctx.number, "head_sha": ctx.head_sha}), encoding="utf-8"
    )
    Path(DEFAULT_BUNDLE_PATH).write_text(json.dumps(bundle), encoding="utf-8")
    typer.echo(
        f"wrote {out} (score {report.score:.0f}/100, {len(report.findings)} findings, "
        f"needs_human_review={report.needs_human_review})"
    )

    if dry_run:
        typer.echo("--dry-run: not posting.")
        return
    audit = AuditLog(DEFAULT_AUDIT_PATH)
    result = publish_report(
        client,
        owner,
        name,
        ctx.number,
        report,
        max_comments=get_settings().max_comments,
        author=ctx.author,
        run_id=uuid.uuid4().hex[:12],
        audit=audit,
    )
    typer.echo(
        f"published: {result.comments_posted} comments, summary {result.summary_action}, "
        f"check {result.check_conclusion}"
    )


def _write_json(path: str, data: object) -> None:
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _run_one_agent(
    role: str,
    provider: object,
    agent_kwargs: dict[str, object],
    *,
    triage_plan: TriagePlan,
    hunks_by_file: dict[str, list[Hunk]],
    changed_file_paths: frozenset[str],
) -> tuple[list[Finding], bool, list[str], str]:
    """Run a SINGLE specialist over the triaged files (one Actions job's worth of work)."""
    agent = _CHUNK_AGENT_CLASSES[role](provider, **agent_kwargs)  # type: ignore[arg-type]
    findings: list[Finding] = []
    errors: list[str] = []
    injection = False
    for file_plan in triage_plan.files:
        if file_plan.skip_reason is not None or role not in file_plan.agents_to_run:
            continue
        hunks = hunks_by_file.get(file_plan.file, [])
        if not hunks:
            continue
        for chunk in chunk_hunks(hunks, max_tokens=budget_from_settings()):
            try:
                if role == "style":
                    outcome = agent.review(
                        chunk, language=file_plan.language, changed_file_paths=changed_file_paths
                    )
                else:
                    outcome = agent.review(chunk, language=file_plan.language)
            except BudgetExhausted:
                errors.append("budget exhausted before full coverage")
                continue
            findings.extend(outcome.findings)
            findings.extend(outcome.injection_findings)
            findings.extend(outcome.redaction_findings)
            if outcome.injection_findings:
                injection = True
            errors.extend(outcome.errors)
    return findings, injection, errors, agent.prompt_version


@app.command()
def extract(
    repo: Annotated[str, typer.Option(help="owner/name of the repository")],
    pr: Annotated[int, typer.Option(help="pull request number")],
    out: Annotated[str, typer.Option(help="context artifact output path")] = DEFAULT_CONTEXT_PATH,
) -> None:
    """Analyze-side step: fetch the PR, parse the diff, run HEURISTIC triage.

    No model call and no LLM secret -- safe on fork-triggered runs. Files the
    heuristic cannot classify get risk="unknown" for the publish side to escalate.
    """
    owner, name = _split_repo(repo)
    client = GitHubClient(_require_token())
    try:
        ctx = fetch_pr_context(client, owner, name, pr)
    except GitHubError as exc:
        typer.echo(f"failed to fetch PR: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    settings = get_settings()
    hunks_by_file = ctx.hunks_by_file()
    all_hunks = [hunk for hunks in hunks_by_file.values() for hunk in hunks]
    changed = ctx.changed_lines_by_file()
    total_diff_lines = sum(len(hunk.lines) for hunk in all_hunks)
    inputs = [
        TriageInput(
            file=path,
            language=ctx.languages.get(path, "unknown"),
            changed_lines=changed.get(path, 0),
        )
        for path in hunks_by_file
    ]
    plan = heuristic_triage(
        inputs,
        call_budget=settings.max_llm_calls_per_run,
        max_diff_lines=settings.max_diff_lines,
        total_diff_lines=total_diff_lines,
    )

    _write_json(
        out,
        {
            "pr_number": ctx.number,
            "head_sha": ctx.head_sha,
            "model": settings.model,
            "diff_text": ctx.diff_text,
            "hunks": [hunk.model_dump(mode="json") for hunk in all_hunks],
            "changed_lines_by_file": changed,
            "languages": ctx.languages,
            "triage": [fp.model_dump(mode="json") for fp in plan.files],
            "prompt_versions": {"triage": "heuristic"},
            "max_diff_lines": settings.max_diff_lines,
            "diff_lines": total_diff_lines,
            "confidence_floor": settings.confidence_floor,
        },
    )
    unknown = sum(1 for fp in plan.files if fp.risk == "unknown")
    typer.echo(
        f"wrote {out}: {len(plan.files)} files triaged ({unknown} unknown), {len(all_hunks)} hunks"
    )


@app.command()
def triage(
    repo: Annotated[str, typer.Option(help="owner/name of the repository")],
    pr: Annotated[int, typer.Option(help="pull request number")],
    provider: Annotated[str, typer.Option(help="LLM provider override")] = "",
    out: Annotated[str, typer.Option(help="context artifact output path")] = DEFAULT_CONTEXT_PATH,
) -> None:
    """Fan-out step 1: fetch the PR, run triage, write the shared context artifact."""
    owner, name = _split_repo(repo)
    client = GitHubClient(_require_token())
    try:
        ctx = fetch_pr_context(client, owner, name, pr)
    except GitHubError as exc:
        typer.echo(f"failed to fetch PR: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    settings, provider_obj, _governor, _run_id, agent_kwargs = _agent_env(provider or None)
    hunks_by_file = ctx.hunks_by_file()
    all_hunks = [hunk for hunks in hunks_by_file.values() for hunk in hunks]
    triage_agent = TriageAgent(provider_obj, **agent_kwargs)  # type: ignore[arg-type]
    try:
        plan = triage_agent.plan(
            call_budget=settings.max_llm_calls_per_run,
            files_summary=ctx.files_summary(),
            pr_metadata=f"{ctx.title}\n\n{ctx.body}".strip(),
            ci_results=ctx.ci_summary(),
            file_paths=list(hunks_by_file),
            languages=ctx.languages,
        )
    except LLMError as exc:
        typer.echo(f"triage inference failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    _write_json(
        out,
        {
            "pr_number": ctx.number,
            "head_sha": ctx.head_sha,
            "model": settings.model,
            "diff_text": ctx.diff_text,
            "hunks": [hunk.model_dump(mode="json") for hunk in all_hunks],
            "changed_lines_by_file": ctx.changed_lines_by_file(),
            "languages": ctx.languages,
            "triage": [fp.model_dump(mode="json") for fp in plan.files],
            "prompt_versions": {"triage": triage_agent.prompt_version},
            "max_diff_lines": settings.max_diff_lines,
            "diff_lines": sum(len(hunk.lines) for hunk in all_hunks),
            "confidence_floor": settings.confidence_floor,
        },
    )
    typer.echo(f"wrote {out}: {len(plan.files)} files triaged, {len(all_hunks)} hunks")


@app.command()
def refine(
    context: Annotated[
        str, typer.Option(help="heuristic context artifact from extract")
    ] = DEFAULT_CONTEXT_PATH,
    provider: Annotated[str, typer.Option(help="LLM provider override")] = "",
    out: Annotated[str, typer.Option(help="output path (default: overwrite context)")] = "",
) -> None:
    """Publish-side triage: apply PR_SENTINEL_TRIAGE_STRATEGY to the heuristic plan.

    hybrid (default) runs the LLM triage agent ONLY on risk="unknown" files; llm
    re-triages every file; heuristic is a no-op. The TriagePlan schema is identical
    either way. On refinement failure the heuristic plan is kept (unknown files keep
    their conservative agent set), so this step never blocks the review.
    """
    out_path = out or context
    data = json.loads(Path(context).read_text(encoding="utf-8"))
    plan_files = [TriageFilePlan.model_validate(fp) for fp in data["triage"]]

    strategy = get_settings().triage_strategy
    if strategy == "heuristic":
        targets: list[TriageFilePlan] = []
    elif strategy == "llm":
        targets = list(plan_files)
    else:  # hybrid
        targets = [fp for fp in plan_files if fp.risk == "unknown"]

    message = f"strategy={strategy}: nothing to refine"
    if targets:
        languages = dict(data.get("languages", {}))
        changed = dict(data.get("changed_lines_by_file", {}))
        files_summary = "\n".join(
            f"{fp.file} ({fp.language}, {changed.get(fp.file, 0)} changed lines)" for fp in targets
        )
        try:
            settings, provider_obj, _governor, _run_id, agent_kwargs = _agent_env(provider or None)
            triage_agent = TriageAgent(provider_obj, **agent_kwargs)  # type: ignore[arg-type]
            refined = triage_agent.plan(
                call_budget=settings.max_llm_calls_per_run,
                files_summary=files_summary,
                pr_metadata="(re-triage of files the heuristic could not classify)",
                ci_results="(none provided)",
                file_paths=[fp.file for fp in targets],
                languages=languages,
            )
            refined_by_file = {fp.file: fp for fp in refined.files}
            plan_files = [refined_by_file.get(fp.file, fp) for fp in plan_files]
            data.setdefault("prompt_versions", {})["triage"] = triage_agent.prompt_version
            message = f"refined {len(targets)} files via LLM triage ({strategy})"
        except LLMError as exc:
            message = f"triage refinement failed ({exc}); kept heuristic plan"

    data["triage"] = [fp.model_dump(mode="json") for fp in plan_files]
    _write_json(out_path, data)
    typer.echo(f"wrote {out_path}: {message}")


@app.command()
def agent(
    role: Annotated[str, typer.Option(help="specialist: bug|security|style|improvement")],
    context: Annotated[
        str, typer.Option(help="context artifact from triage")
    ] = DEFAULT_CONTEXT_PATH,
    provider: Annotated[str, typer.Option(help="LLM provider override")] = "",
    out: Annotated[str, typer.Option(help="agent result output (default agent-<role>.json)")] = "",
) -> None:
    """Fan-out step 2: run ONE specialist over the context; write its result artifact.

    Inference failure is recorded in the result (not a crash) so the fan-in always
    has four artifacts and errors surface in agent_errors.
    """
    if role not in _CHUNK_AGENT_CLASSES:
        typer.echo(f"unknown role {role!r}", err=True)
        raise typer.Exit(code=1)
    out_path = out or f"agent-{role}.json"
    data = json.loads(Path(context).read_text(encoding="utf-8"))
    hunks = [Hunk.model_validate(h) for h in data["hunks"]]
    hunks_by_file: dict[str, list[Hunk]] = {}
    for hunk in hunks:
        hunks_by_file.setdefault(hunk.file, []).append(hunk)
    plan = TriagePlan(files=[TriageFilePlan.model_validate(fp) for fp in data["triage"]])

    _settings, provider_obj, governor, _run_id, agent_kwargs = _agent_env(provider or None)
    findings: list[Finding] = []
    errors: list[str] = []
    injection = False
    prompt_version = ""
    try:
        findings, injection, errors, prompt_version = _run_one_agent(
            role,
            provider_obj,
            agent_kwargs,
            triage_plan=plan,
            hunks_by_file=hunks_by_file,
            changed_file_paths=frozenset(hunks_by_file),
        )
    except LLMError as exc:
        errors.append(f"{role} agent inference failed: {exc}")

    snap = governor.snapshot()
    cost = governor.cost_snapshot()
    _write_json(
        out_path,
        {
            "agent": role,
            "findings": [f.model_dump(mode="json") for f in findings],
            "errors": errors,
            "injection_detected": injection,
            "budget_used": snap["calls_used"],
            "cache_hits": snap["cache_hits"],
            "cache_writes": snap["cache_writes"],
            "cost_usd": cost["cost_used_usd"],
            "prompt_version": prompt_version,
        },
    )
    typer.echo(
        f"wrote {out_path}: {len(findings)} findings, {len(errors)} errors, "
        f"cache {snap['cache_hits']} hit / {snap['cache_writes']} write, "
        f"${cost['cost_used_usd']:.4f}"
    )


@app.command()
def aggregate(
    context: Annotated[
        str, typer.Option(help="context artifact from triage")
    ] = DEFAULT_CONTEXT_PATH,
    agent_result: Annotated[
        list[str] | None, typer.Option("--agent", help="agent result json (repeatable)")
    ] = None,
    out: Annotated[str, typer.Option(help="review payload output path")] = DEFAULT_PAYLOAD_PATH,
    provider: Annotated[str, typer.Option(help="LLM provider override")] = "",
) -> None:
    """Fan-in: merge the per-agent results through grounding/critic/scoring."""
    ctx = json.loads(Path(context).read_text(encoding="utf-8"))
    agent_files = agent_result or sorted(str(p) for p in Path().glob("agent-*.json"))

    raw_findings: list[Finding] = []
    agent_errors: dict[str, str] = {}
    injection = False
    prompt_versions: dict[str, str] = dict(ctx.get("prompt_versions", {}))
    agent_budget = 0
    agent_cost = 0.0
    cache_hits = 0
    cache_writes = 0
    for path in agent_files:
        fp = Path(path)
        if not fp.exists():
            continue
        result = json.loads(fp.read_text(encoding="utf-8"))
        raw_findings.extend(Finding.model_validate(f) for f in result.get("findings", []))
        if result.get("errors"):
            agent_errors[result["agent"]] = "; ".join(result["errors"])
        injection = injection or bool(result.get("injection_detected"))
        prompt_versions[result["agent"]] = str(result.get("prompt_version", ""))
        agent_budget += int(result.get("budget_used", 0))
        agent_cost += float(result.get("cost_usd", 0.0))
        cache_hits += int(result.get("cache_hits", 0))
        cache_writes += int(result.get("cache_writes", 0))

    _settings, provider_obj, governor, _run_id, agent_kwargs = _agent_env(provider or None)
    critic_agent = CriticAgent(provider_obj, **agent_kwargs)  # type: ignore[arg-type]
    prompt_versions["critic"] = critic_agent.prompt_version
    try:
        report = run_aggregate_pipeline(
            critic_agent,
            diff_text=str(ctx.get("diff_text", "")),
            pr_number=int(ctx["pr_number"]),
            head_sha=str(ctx["head_sha"]),
            model=str(ctx["model"]),
            raw_findings=raw_findings,
            hunks=[Hunk.model_validate(h) for h in ctx["hunks"]],
            changed_lines_by_file=dict(ctx.get("changed_lines_by_file", {})),
            max_diff_lines=int(ctx.get("max_diff_lines", 5000)),
            diff_lines=int(ctx.get("diff_lines", 0)),
            confidence_floor=float(ctx.get("confidence_floor", 0.55)),
            injection_detected=injection,
            budget_exhausted=False,
            prompt_versions=prompt_versions,
            agent_errors=agent_errors,
            budget_used=agent_budget,
        )
    except LLMError as exc:
        typer.echo(f"LLM call failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    critic_snap = governor.snapshot()
    total_cost = round(agent_cost + governor.cost_used, 6)
    report = report.model_copy(
        update={
            "budget_used": agent_budget + critic_snap["calls_used"],
            "cost_usd": total_cost,
            "cache_hits": cache_hits + critic_snap["cache_hits"],
            "cache_writes": cache_writes + critic_snap["cache_writes"],
        }
    )

    _write_json(out, json.loads(report.model_dump_json()))
    _write_json(DEFAULT_META_PATH, {"pr_number": report.pr_number, "head_sha": report.head_sha})
    typer.echo(
        f"wrote {out}: score {report.score:.0f}/100, {len(report.findings)} findings, "
        f"${report.cost_usd:.4f} at standard rates, "
        f"cache {report.cache_hits} hit / {report.cache_writes} write"
    )


@app.command(name="check-start")
def check_start(
    repo: Annotated[str, typer.Option(help="owner/name of the repository")],
    sha: Annotated[str, typer.Option(help="head SHA to attach the check to")],
    details_url: Annotated[str, typer.Option(help="link to the publish run")] = "",
    out: Annotated[str, typer.Option(help="write {check_run_id} here")] = "check-meta.json",
) -> None:
    """Create an in-progress 'PR Sentinel' check run EARLY so the PR shows a signal.

    Works on fork PRs: head_sha is reachable in the base repo via refs/pull/N/head.
    Never fails the workflow -- if the check cannot be created, id 0 is recorded and
    the finish step simply creates a fresh completed check instead.
    """
    owner, name = _split_repo(repo)
    client = GitHubClient(_require_token())
    audit = AuditLog(DEFAULT_AUDIT_PATH)
    check_run_id = 0
    try:
        check_run_id = start_check_run(
            client,
            owner,
            name,
            sha,
            details_url=details_url or None,
            run_id=uuid.uuid4().hex[:12],
            audit=audit,
        )
    except GitHubError as exc:
        typer.echo(f"could not create in-progress check (continuing): {exc}", err=True)
    _write_json(out, {"check_run_id": check_run_id})
    typer.echo(f"wrote {out}: check_run_id={check_run_id}")


@app.command(name="check-fail")
def check_fail(
    repo: Annotated[str, typer.Option(help="owner/name of the repository")],
    sha: Annotated[str, typer.Option(help="head SHA the check is attached to")],
    check_run_id: Annotated[int, typer.Option(help="id from check-start")],
    summary: Annotated[str, typer.Option(help="short failure summary")] = (
        "The review could not be completed."
    ),
    details_url: Annotated[str, typer.Option(help="link to the publish run")] = "",
) -> None:
    """Mark the check run as failure (the silent-failure path when aggregate dies)."""
    owner, name = _split_repo(repo)
    if check_run_id <= 0:
        typer.echo("no check_run_id to fail; skipping", err=True)
        return
    client = GitHubClient(_require_token())
    audit = AuditLog(DEFAULT_AUDIT_PATH)
    try:
        fail_check_run(
            client,
            owner,
            name,
            check_run_id,
            sha,
            summary=summary,
            details_url=details_url or None,
            run_id=uuid.uuid4().hex[:12],
            audit=audit,
        )
    except GitHubError as exc:
        typer.echo(f"could not update check to failure: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"check {check_run_id} marked failure")


@app.command()
def publish(
    repo: Annotated[str, typer.Option(help="owner/name of the repository")],
    pr: Annotated[int, typer.Option(help="pull request number")],
    payload: Annotated[str, typer.Option(help="review payload to publish")] = DEFAULT_PAYLOAD_PATH,
    check_run_id: Annotated[int, typer.Option(help="in-progress check to finish")] = 0,
    details_url: Annotated[str, typer.Option(help="link to the publish run")] = "",
) -> None:
    """Post the consolidated review, summary comment, and check run to a PR."""
    owner, name = _split_repo(repo)
    payload_path = Path(payload)
    if not payload_path.exists():
        typer.echo(f"no payload found at {payload_path}", err=True)
        raise typer.Exit(code=1)
    report = ReviewReport.model_validate_json(payload_path.read_text(encoding="utf-8"))

    number = pr
    meta_path = Path(DEFAULT_META_PATH)
    if meta_path.exists():  # trust pr-meta.json over the branch/arg (CLAUDE.md)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        number = int(meta.get("pr_number", pr))

    client = GitHubClient(_require_token())
    audit = AuditLog(DEFAULT_AUDIT_PATH)
    try:
        result = publish_report(
            client,
            owner,
            name,
            number,
            report,
            max_comments=get_settings().max_comments,
            author=None,
            run_id=uuid.uuid4().hex[:12],
            audit=audit,
            check_run_id=check_run_id or None,
            details_url=details_url or None,
        )
    except GitHubError as exc:
        typer.echo(f"failed to publish: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"published: {result.comments_posted} comments ({result.comments_suppressed} suppressed), "
        f"summary {result.summary_action}, check {result.check_conclusion}"
    )


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


def _collect_findings(
    provider: object,
    agent_kwargs: dict[str, object],
    *,
    triage_plan: object,
    hunks_by_file: dict[str, list[Hunk]],
    changed_file_paths: frozenset[str],
    prompt_versions: dict[str, str],
) -> tuple[list[Finding], bool, bool, dict[str, list[str]]]:
    """Run each triaged file through its assigned specialists. Shared by local + analyze.

    Returns (findings, injection_detected, budget_exhausted, per-agent error lists).
    `prompt_versions` is mutated in place with each specialist's prompt version.
    """
    findings: list[Finding] = []
    agent_error_lists: dict[str, list[str]] = defaultdict(list)
    injection_detected = False
    budget_exhausted = False

    for file_plan in triage_plan.files:  # type: ignore[attr-defined]
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
    return findings, injection_detected, budget_exhausted, agent_error_lists


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

    prompt_versions: dict[str, str] = {"triage": triage_agent.prompt_version}
    findings, injection_detected, budget_exhausted, agent_error_lists = _collect_findings(
        provider,
        agent_kwargs,
        triage_plan=triage_plan,
        hunks_by_file=hunks_by_file,
        changed_file_paths=changed_file_paths,
        prompt_versions=prompt_versions,
    )

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
