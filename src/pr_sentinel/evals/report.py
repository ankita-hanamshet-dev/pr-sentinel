"""Render eval results: a markdown table for $GITHUB_STEP_SUMMARY + a JSON artifact.

Cost (USD, at STANDARD published rates) and cache hit rate sit in the same table as
recall and precision — the amendment makes cost a first-class quality metric.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pr_sentinel.evals.metrics import SuiteMetrics

DEFAULT_RESULTS_DIR = Path("evals/results")


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_markdown(suite: SuiteMetrics, *, replay_note: bool = True) -> str:
    """Render the per-case table, the suite summary, and the threshold gate."""
    lines: list[str] = []
    lines.append("## PR Sentinel — golden eval")
    if replay_note:
        lines.append(
            "> Replay mode: model calls served from `fixtures/replay/`. If those are "
            "reference recordings rather than a live `--record` run, recall/precision "
            "reflect the recorded outputs, not a fresh model measurement."
        )
    lines.append("")
    lines.append(
        "| case | recall | precision | F1 | decoy FP | calls | ms | cost $ | cache hit |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for c in suite.cases:
        note = f" ⚠️ {c.error}" if c.error else ""
        lines.append(
            f"| {c.case}{note} | {_pct(c.recall)} | {_pct(c.precision)} | {c.f1:.2f} "
            f"| {c.decoy_false_positives} | {c.calls} | {c.duration_ms} "
            f"| {c.cost_usd:.4f} | {_pct(c.cache_hit_rate)} |"
        )
    lines.append(
        f"| **suite** | **{_pct(suite.recall)}** | **{_pct(suite.precision)}** "
        f"| **{suite.f1:.2f}** | max {suite.max_decoy_fp_per_case} "
        f"| max {suite.max_calls_per_case} | p95 {suite.p95_latency_ms:.0f} "
        f"| **{suite.total_cost_usd:.4f}** | **{_pct(suite.cache_hit_rate)}** |"
    )
    lines.append("")
    lines.append(
        f"**Cost:** total **${suite.total_cost_usd:.4f}**, mean "
        f"**${suite.mean_cost_usd:.4f}/case** (STANDARD $3/$15 per 1M). "
        f"**Cache hit rate:** {_pct(suite.cache_hit_rate)}."
    )
    lines.append("")
    lines.append("### Thresholds")
    lines.append("| metric | value | bound | result |")
    lines.append("|---|---|---|---|")
    for t in suite.thresholds:
        verdict = "PASS" if t.passed else "FAIL"
        lines.append(
            f"| {t.name} | {t.value:.3f} | {t.comparison} {t.threshold:g} | {verdict} |"
        )
    lines.append("")
    overall = "PASS" if suite.passed else "FAIL"
    lines.append(f"**Gate: {overall}**")
    return "\n".join(lines)


def write_results(suite: SuiteMetrics, results_dir: Path = DEFAULT_RESULTS_DIR) -> Path:
    """Persist the full suite result as JSON under evals/results/<timestamp>.json."""
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = results_dir / f"{stamp}.json"
    path.write_text(json.dumps(suite.model_dump(), indent=2), encoding="utf-8")
    return path
