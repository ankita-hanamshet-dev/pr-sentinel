"""Cross-agent finding deduplication (CLAUDE.md): merge near-duplicates, keep the
more severe verdict, and record which agents corroborated each surviving finding.

Findings are only ever merged within the same file (a similar title on two
different files is coincidence, not duplication). Corroboration is returned as a
side-channel dict rather than added to Finding, since CLAUDE.md pins the Finding
schema exactly.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from pr_sentinel.models import SEVERITY_ORDER, Finding

SIMILARITY_THRESHOLD = 75.0


def _similarity(a: Finding, b: Finding) -> float:
    text_a = f"{a.title} {a.evidence_quote}"
    text_b = f"{b.title} {b.evidence_quote}"
    return float(fuzz.token_sort_ratio(text_a, text_b))


def _is_duplicate(a: Finding, b: Finding) -> bool:
    return a.file == b.file and _similarity(a, b) >= SIMILARITY_THRESHOLD


def _winner(cluster: list[Finding]) -> Finding:
    return max(cluster, key=lambda f: (SEVERITY_ORDER[f.severity], f.confidence))


def dedup_findings(findings: list[Finding]) -> tuple[list[Finding], dict[str, list[str]]]:
    """Merge near-duplicate findings; return (merged, {winner_id: [corroborating agents]})."""
    clusters: list[list[Finding]] = []
    for finding in findings:
        placed = False
        for cluster in clusters:
            if _is_duplicate(finding, cluster[0]):
                cluster.append(finding)
                placed = True
                break
        if not placed:
            clusters.append([finding])

    merged: list[Finding] = []
    corroboration: dict[str, list[str]] = {}
    for cluster in clusters:
        winner = _winner(cluster)
        merged.append(winner)
        corroboration[winner.id] = sorted({f.agent for f in cluster})
    return merged, corroboration
