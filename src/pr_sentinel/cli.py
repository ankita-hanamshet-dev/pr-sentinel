"""PR Sentinel command-line interface (typer). Commands are wired in later phases."""

from __future__ import annotations

from typing import Annotated, NoReturn

import typer

from pr_sentinel.llm.budget import BudgetExhausted, BudgetGovernor
from pr_sentinel.llm.provider import LLMError, LLMRequest, call_llm, get_provider
from pr_sentinel.settings import get_settings

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


@app.command()
def local(
    path: Annotated[str, typer.Option(help="fixture directory with a diff to review")],
    provider: Annotated[str, typer.Option(help="LLM provider override")] = "replay",
) -> None:
    """Run a single offline review case from a local fixture directory."""
    _pending("local", "Phase 5")


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
