"""Generate REFERENCE replay recordings for the golden set, offline (no LLM key).

This is an authoring tool, not product code. It drives the real review pipeline with
an oracle provider whose responses are synthesised from each case's ground truth
(expected_findings.yaml + the diff): the mapped specialist "finds" exactly the planted
defect with a verbatim evidence quote, and never flags a decoy. The RecordingProvider
saves those responses into fixtures/replay/ keyed by prompt hash, so
`pr-sentinel eval --suite golden` runs offline and green.

These are REFERENCE recordings, deliberately labelled as such: they let CI exercise the
whole harness (grounding, dedup, scoring, cost, thresholds) end to end, but the
recall/precision they yield reflect the authored responses, not a live model. Replace
them with a real `pr-sentinel eval --record` run once PR_SENTINEL_LLM_API_KEY is set to
get a true model measurement.

Usage:  uv run python scripts/record_reference.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from pr_sentinel.gh.diff import parse_diff
from pr_sentinel.llm.provider import LLMRequest, LLMResponse
from pr_sentinel.llm.replay import RecordingProvider
from pr_sentinel.models import Hunk

SUITE_DIR = Path("fixtures/synthetic_prs")
_ROLE_RE = re.compile(r"You are the (\w+) agent")
_EXT_LANG = {
    ".py": "python", ".ts": "typescript", ".js": "javascript", ".go": "go",
    ".java": "java", ".sh": "bash", ".yml": "yaml", ".yaml": "yaml",
}


def _role_to_security_or_bug(rule_id: str) -> str:
    if rule_id.startswith(("CWE", "OWASP", "SENTINEL-SEC")):
        return "security"
    if rule_id.startswith("SENTINEL-BUG"):
        return "bug"
    if rule_id.startswith(("PEP8", "SENTINEL-STYLE")):
        return "style"
    return "improvement"


def _numbered_lines(hunk: Hunk) -> dict[int, str]:
    numbered: dict[int, str] = {}
    line_no = hunk.new_start
    for raw in hunk.lines:
        tag, content = raw[:1], raw[1:]
        if tag in (" ", "+"):
            numbered[line_no] = content
            line_no += 1
    return numbered


def _claimed_text(hunks: list[Hunk], file: str, l0: int, l1: int) -> str:
    for hunk in hunks:
        if hunk.file != file:
            continue
        numbered = _numbered_lines(hunk)
        if l0 in numbered:
            return "\n".join(numbered[n] for n in range(l0, l1 + 1) if n in numbered)
    return ""


def _title(description: str, rule_id: str) -> str:
    first = description.strip().split(". ")[0].strip()
    return (first[:77] + "...") if len(first) > 80 else (first or f"Address {rule_id}")


def _finding(exp: dict[str, object], role: str, evidence: str) -> dict[str, object]:
    desc = str(exp.get("description", "")).strip()
    return {
        "agent": role,
        "file": exp["file"],
        "line_start": exp["line_start"],
        "line_end": exp["line_end"],
        "severity": exp.get("severity", "medium"),
        "confidence": 0.9,
        "rule_id": exp["rule_id"],
        "title": _title(desc, str(exp["rule_id"])),
        "fact": desc or "The changed code exhibits the flagged pattern.",
        "assumption": None,
        "impact": "Exploitable / incorrect behaviour as described.",
        "recommendation": "Remediate per the rule guidance.",
        "evidence_quote": evidence,
        "suggested_patch": None,
        "references": [],
    }


class _Oracle:
    """Per-case provider: returns crafted, grounded responses from the ground truth."""

    name = "reference-oracle"

    def __init__(self, case_dir: Path) -> None:
        diff_text = (case_dir / "diff.patch").read_text(encoding="utf-8")
        self._hunks = [h for fd in parse_diff(diff_text) for h in fd.hunks]
        self._files = sorted({fd.path for fd in parse_diff(diff_text) if fd.hunks})
        expected = yaml.safe_load((case_dir / "expected_findings.yaml").read_text())
        self._expected = expected.get("findings", []) if expected else []

    def _findings_for_role(self, role: str) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for exp in self._expected:
            if _role_to_security_or_bug(str(exp["rule_id"])) != role:
                continue
            evidence = _claimed_text(
                self._hunks, str(exp["file"]), int(exp["line_start"]), int(exp["line_end"])
            )
            if evidence:
                out.append(_finding(exp, role, evidence))
        return out

    def _all_findings(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for exp in self._expected:
            role = _role_to_security_or_bug(str(exp["rule_id"]))
            evidence = _claimed_text(
                self._hunks, str(exp["file"]), int(exp["line_start"]), int(exp["line_end"])
            )
            if evidence:
                out.append(_finding(exp, role, evidence))
        return out

    def _triage_plan(self) -> dict[str, object]:
        return {
            "files": [
                {
                    "file": f,
                    "language": _EXT_LANG.get(Path(f).suffix, "unknown"),
                    "risk": "high",
                    "agents_to_run": ["bug", "security", "style", "improvement"],
                    "skip_reason": None,
                }
                for f in self._files
            ],
            "llm_call_budget": 12,
        }

    def complete(self, request: LLMRequest) -> LLMResponse:
        import json

        match = _ROLE_RE.search(request.system)
        role = match.group(1).lower() if match else ""
        if role == "triage":
            text = json.dumps(self._triage_plan())
        elif role == "critic":
            text = json.dumps({"findings": self._all_findings(), "drops": []})
        elif role in {"bug", "security", "style", "improvement"}:
            text = json.dumps({"findings": self._findings_for_role(role)})
        else:
            text = json.dumps({"findings": []})
        tokens_in = max(1, len(request.system + request.user) // 4)
        tokens_out = max(1, len(text) // 4)
        return LLMResponse(
            text=text, tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=0,
            model="claude-sonnet-5",
        )


def main() -> int:
    from pr_sentinel.cli import _run_local_review

    cases = sorted(p for p in SUITE_DIR.iterdir() if (p / "diff.patch").exists())
    for case_dir in cases:
        oracle = _Oracle(case_dir)
        recorder = RecordingProvider(oracle)
        report, *_ = _run_local_review(
            case_dir, "anthropic", provider_obj=recorder, use_cache=False
        )
        print(
            f"{case_dir.name:26} findings={len(report.findings)} "
            f"grounding_rejects={report.grounding_rejects} cost=${report.cost_usd:.4f}"
        )
    print(f"\nrecorded reference fixtures for {len(cases)} cases into fixtures/replay/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
