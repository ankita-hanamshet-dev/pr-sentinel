"""Run the review pipeline over the golden set and score it.

Offline by default (PR_SENTINEL_LLM_PROVIDER=replay): every model call is served from
fixtures/replay/, so CI never spends a real request. A case whose recordings are
missing is scored as a total miss (recall 0) with an error note rather than crashing
the suite — the honest signal that the golden set has not been recorded yet.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from pr_sentinel.evals.metrics import (
    CaseMetrics,
    Decoy,
    ExpectedFinding,
    SuiteMetrics,
    build_suite,
    score_case,
)
from pr_sentinel.llm.provider import LLMError

DEFAULT_SUITE_DIR = Path("fixtures/synthetic_prs")


def discover_cases(suite_dir: Path = DEFAULT_SUITE_DIR) -> list[Path]:
    """Return every case directory (has a diff.patch), sorted by name."""
    return sorted(
        p for p in suite_dir.iterdir() if p.is_dir() and (p / "diff.patch").exists()
    )


def _load_expected(case_dir: Path) -> list[ExpectedFinding]:
    path = case_dir / "expected_findings.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [ExpectedFinding.model_validate(f) for f in data.get("findings", [])]


def _load_decoys(case_dir: Path) -> list[Decoy]:
    path = case_dir / "decoys.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Decoy.model_validate(d) for d in data.get("decoys", [])]


def _empty_case(case: str, expected: list[ExpectedFinding], error: str) -> CaseMetrics:
    """A case that could not run: everything missed, error recorded, nothing spurious."""
    return CaseMetrics(
        case=case,
        expected=len(expected),
        produced=0,
        matched_expected=0,
        true_positives=0,
        false_positives=0,
        decoy_false_positives=0,
        recall=0.0 if expected else 1.0,
        precision=1.0,
        f1=0.0,
        groundedness=1.0,
        duplicate_rate=0.0,
        calls=0,
        duration_ms=0,
        cost_usd=0.0,
        cache_hits=0,
        cache_writes=0,
        cache_hit_rate=0.0,
        missed=[f"{e.file}:{e.line_start} {e.rule_id}" for e in expected],
        error=error,
    )


def run_case(
    case_dir: Path,
    provider_name: str = "replay",
    *,
    provider_obj: object | None = None,
    use_cache: bool = True,
) -> CaseMetrics:
    """Review one case's diff and score it against its planted defects + decoys."""
    from pr_sentinel.cli import _run_local_review  # lazy: avoids a CLI import cycle

    expected = _load_expected(case_dir)
    decoys = _load_decoys(case_dir)
    try:
        report, _reason, _rejects, _low, _nits = _run_local_review(
            case_dir, provider_name, provider_obj=provider_obj, use_cache=use_cache
        )
    except LLMError as exc:
        return _empty_case(case_dir.name, expected, f"{type(exc).__name__}: {exc}")

    return score_case(
        case_dir.name,
        report.findings,
        expected,
        decoys,
        grounding_rejects=report.grounding_rejects,
        calls=report.budget_used,
        duration_ms=report.duration_ms,
        cost_usd=report.cost_usd,
        cache_hits=report.cache_hits,
        cache_writes=report.cache_writes,
    )


def run_suite(
    suite_dir: Path = DEFAULT_SUITE_DIR,
    provider_name: str = "replay",
    *,
    provider_obj: object | None = None,
    use_cache: bool = True,
) -> SuiteMetrics:
    """Run and score every case; return the micro-averaged, threshold-graded suite."""
    cases = [
        run_case(case_dir, provider_name, provider_obj=provider_obj, use_cache=use_cache)
        for case_dir in discover_cases(suite_dir)
    ]
    return build_suite(cases)
