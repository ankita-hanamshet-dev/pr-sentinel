"""Golden-set metrics and the CLAUDE.md threshold gate.

Cost (USD) and cache hit rate are first-class metrics here alongside recall and
precision: a reviewer that is accurate but expensive fails the quality bar just as a
cheap-but-inaccurate one does. Recall/precision are computed by overlap between a
produced finding's line range and a planted defect's (or decoy's) line range on the
same file — models legitimately vary the exact rule_id and wording, so location is
the ground truth, with rule/severity agreement reported separately.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from pr_sentinel.models import Finding


class ExpectedFinding(BaseModel):
    """A planted defect from a case's expected_findings.yaml."""

    rule_id: str
    file: str
    line_start: int
    line_end: int
    severity: str = "unknown"


class Decoy(BaseModel):
    """Correct-looking code a naive reviewer would wrongly flag (decoys.yaml)."""

    file: str
    line_start: int
    line_end: int


def _overlaps(f: str, a0: int, a1: int, g: str, b0: int, b1: int) -> bool:
    """True if two [start,end] line ranges on the same file intersect."""
    return f == g and a0 <= b1 and b0 <= a1


class CaseMetrics(BaseModel):
    """Per-case scoring plus the run accounting (calls, latency, cost, cache)."""

    case: str
    expected: int
    produced: int
    matched_expected: int
    true_positives: int
    false_positives: int
    decoy_false_positives: int
    recall: float
    precision: float
    f1: float
    groundedness: float
    duplicate_rate: float
    calls: int
    duration_ms: int
    cost_usd: float
    cache_hits: int
    cache_writes: int
    cache_hit_rate: float
    missed: list[str] = Field(default_factory=list)
    spurious: list[str] = Field(default_factory=list)
    error: str | None = None  # set when the pipeline could not run (e.g. missing replay)


def score_case(
    case: str,
    findings: list[Finding],
    expected: list[ExpectedFinding],
    decoys: list[Decoy],
    *,
    grounding_rejects: int,
    calls: int,
    duration_ms: int,
    cost_usd: float,
    cache_hits: int,
    cache_writes: int,
) -> CaseMetrics:
    """Score one case's produced findings against its planted defects and decoys."""
    matched_flags = [False] * len(expected)
    true_positives = 0
    decoy_fp = 0
    spurious: list[str] = []

    for finding in findings:
        hit = False
        for i, exp in enumerate(expected):
            if _overlaps(
                finding.file, finding.line_start, finding.line_end,
                exp.file, exp.line_start, exp.line_end,
            ):
                hit = True
                matched_flags[i] = True
        if hit:
            true_positives += 1
        else:
            spurious.append(f"{finding.file}:{finding.line_start} {finding.rule_id}")
            if any(
                _overlaps(
                    finding.file, finding.line_start, finding.line_end,
                    d.file, d.line_start, d.line_end,
                )
                for d in decoys
            ):
                decoy_fp += 1

    matched = sum(matched_flags)
    produced = len(findings)
    false_positives = produced - true_positives
    recall = matched / len(expected) if expected else 1.0
    precision = true_positives / produced if produced else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    groundedness = (
        produced / (produced + grounding_rejects) if (produced + grounding_rejects) else 1.0
    )

    seen: set[tuple[str, int, str]] = set()
    duplicates = 0
    for finding in findings:
        key = (finding.file, finding.line_start, finding.rule_id)
        if key in seen:
            duplicates += 1
        seen.add(key)
    duplicate_rate = duplicates / produced if produced else 0.0

    cache_total = cache_hits + cache_writes
    cache_hit_rate = cache_hits / cache_total if cache_total else 0.0
    missed = [
        f"{e.file}:{e.line_start} {e.rule_id}"
        for i, e in enumerate(expected)
        if not matched_flags[i]
    ]

    return CaseMetrics(
        case=case,
        expected=len(expected),
        produced=produced,
        matched_expected=matched,
        true_positives=true_positives,
        false_positives=false_positives,
        decoy_false_positives=decoy_fp,
        recall=recall,
        precision=precision,
        f1=f1,
        groundedness=groundedness,
        duplicate_rate=duplicate_rate,
        calls=calls,
        duration_ms=duration_ms,
        cost_usd=round(cost_usd, 6),
        cache_hits=cache_hits,
        cache_writes=cache_writes,
        cache_hit_rate=cache_hit_rate,
        missed=missed,
        spurious=spurious,
    )


class ThresholdResult(BaseModel):
    """One graded metric: its value, the bound, and whether it passed."""

    name: str
    value: float
    threshold: float
    comparison: str  # ">=" or "<="
    passed: bool


class SuiteMetrics(BaseModel):
    """Micro-averaged suite scoring plus the graded thresholds and cost totals."""

    cases: list[CaseMetrics]
    recall: float
    precision: float
    f1: float
    groundedness: float
    duplicate_rate: float
    max_decoy_fp_per_case: int
    max_calls_per_case: int
    p95_latency_ms: float
    total_cost_usd: float
    mean_cost_usd: float
    cache_hit_rate: float
    thresholds: list[ThresholdResult]

    @property
    def passed(self) -> bool:
        """True only if every graded threshold passed."""
        return all(t.passed for t in self.thresholds)


def _percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = pct / 100.0 * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[lo])
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


# CLAUDE.md §Testing thresholds. Cost/cache are reported but not hard-gated (no numeric
# bound is specified in the spec); a per-case cost overflow is surfaced in the report.
def build_suite(cases: list[CaseMetrics]) -> SuiteMetrics:
    """Micro-average the cases and grade them against the CLAUDE.md thresholds."""
    total_expected = sum(c.expected for c in cases)
    total_matched = sum(c.matched_expected for c in cases)
    total_produced = sum(c.produced for c in cases)
    total_tp = sum(c.true_positives for c in cases)
    total_rejects_denom = sum(c.produced for c in cases)
    grounded = sum(c.groundedness * c.produced for c in cases)

    recall = total_matched / total_expected if total_expected else 1.0
    precision = total_tp / total_produced if total_produced else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    groundedness = grounded / total_rejects_denom if total_rejects_denom else 1.0
    total_dupes = sum(c.duplicate_rate * c.produced for c in cases)
    duplicate_rate = total_dupes / total_produced if total_produced else 0.0

    max_decoy = max((c.decoy_false_positives for c in cases), default=0)
    max_calls = max((c.calls for c in cases), default=0)
    p95 = _percentile([c.duration_ms for c in cases], 95.0)
    total_cost = round(sum(c.cost_usd for c in cases), 6)
    mean_cost = round(total_cost / len(cases), 6) if cases else 0.0
    hits = sum(c.cache_hits for c in cases)
    writes = sum(c.cache_writes for c in cases)
    cache_hit_rate = hits / (hits + writes) if (hits + writes) else 0.0

    thresholds = [
        ThresholdResult(
            name="recall", value=recall, threshold=0.90, comparison=">=", passed=recall >= 0.90
        ),
        ThresholdResult(
            name="precision", value=precision, threshold=0.70, comparison=">=",
            passed=precision >= 0.70,
        ),
        ThresholdResult(
            name="groundedness", value=groundedness, threshold=0.95, comparison=">=",
            passed=groundedness >= 0.95,
        ),
        ThresholdResult(
            name="duplicate_rate", value=duplicate_rate, threshold=0.05, comparison="<=",
            passed=duplicate_rate <= 0.05,
        ),
        ThresholdResult(
            name="decoy_fp_per_pr", value=float(max_decoy), threshold=1.0, comparison="<=",
            passed=max_decoy <= 1,
        ),
        ThresholdResult(
            name="calls_per_pr", value=float(max_calls), threshold=12.0, comparison="<=",
            passed=max_calls <= 12,
        ),
        ThresholdResult(
            name="p95_latency_ms", value=p95, threshold=180000.0, comparison="<=",
            passed=p95 <= 180000.0,
        ),
    ]

    return SuiteMetrics(
        cases=cases,
        recall=recall,
        precision=precision,
        f1=f1,
        groundedness=groundedness,
        duplicate_rate=duplicate_rate,
        max_decoy_fp_per_case=max_decoy,
        max_calls_per_case=max_calls,
        p95_latency_ms=p95,
        total_cost_usd=total_cost,
        mean_cost_usd=mean_cost,
        cache_hit_rate=cache_hit_rate,
        thresholds=thresholds,
    )
