"""The four allowlisted agent tools (CLAUDE.md): narrow, read-only, argument-validated.

Out-of-scope arguments are refused, logged via audit.py, and counted as a misuse
signal -- never silently ignored or widened in scope. No tool writes anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn

from rank_bm25 import BM25Okapi

from pr_sentinel.audit import AuditLog
from pr_sentinel.core.grounding import TAXONOMY_PATTERNS

MAX_CONTEXT_RADIUS = 40

_TOOL_ALLOWED_AGENTS: dict[str, frozenset[str]] = {
    "get_file_context": frozenset({"bug", "security"}),
    "search_team_conventions": frozenset({"style", "improvement"}),
    "get_ci_result": frozenset({"bug"}),
    "lookup_rule": frozenset(
        {"bug", "security", "style", "improvement", "critic", "triage", "fixer"}
    ),
}

_KNOWN_RULE_DESCRIPTIONS: dict[str, str] = {
    "CWE-89": "SQL Injection",
    "CWE-79": "Cross-site Scripting (XSS)",
    "CWE-798": "Use of Hard-coded Credentials",
    "CWE-476": "NULL Pointer Dereference",
    "CWE-22": "Path Traversal",
    "CWE-918": "Server-Side Request Forgery (SSRF)",
    "CWE-502": "Deserialization of Untrusted Data",
    "CWE-327": "Use of a Broken or Risky Cryptographic Algorithm",
    "OWASP-A01:2021": "Broken Access Control",
    "OWASP-A02:2021": "Cryptographic Failures",
    "OWASP-A03:2021": "Injection",
    "OWASP-A05:2021": "Security Misconfiguration",
    "OWASP-A08:2021": "Software and Data Integrity Failures",
    "PEP8-E722": "Do not use bare 'except'",
    "SENTINEL-SEC-001": "Possible prompt injection attempt",
}


class ToolMisuseError(Exception):
    """Raised when an agent calls a tool it isn't allowed to, or with an out-of-scope argument."""


@dataclass(frozen=True)
class ToolContext:
    """Per-run context every tool call is checked against."""

    agent: str
    changed_file_paths: frozenset[str]
    run_id: str
    audit: AuditLog | None = None


def _refuse(ctx: ToolContext, tool: str, reason: str) -> NoReturn:
    if ctx.audit is not None:
        ctx.audit.record(
            run_id=ctx.run_id,
            actor=ctx.agent,
            action=f"tool:{tool}",
            target=reason,
            decision="deny",
            reason=reason,
        )
    raise ToolMisuseError(f"{ctx.agent} misused {tool}: {reason}")


def _check_allowed(ctx: ToolContext, tool: str) -> None:
    if ctx.agent not in _TOOL_ALLOWED_AGENTS[tool]:
        _refuse(ctx, tool, f"agent {ctx.agent!r} is not allowed to call {tool}")


def get_file_context(
    ctx: ToolContext,
    path: str,
    line: int,
    radius: int,
    file_lines: Sequence[str] | None,
) -> list[str] | None:
    """Return up to `radius` lines of context around `line` in `path` (Bug, Security).

    `file_lines` is the caller-supplied content of `path`, if available (Phase 6 will
    supply real file bytes via gh/client.py); returns None when unavailable rather
    than fabricating content.
    """
    _check_allowed(ctx, "get_file_context")
    if path not in ctx.changed_file_paths:
        _refuse(ctx, "get_file_context", f"path {path!r} is not in this PR's changed files")
    if radius > MAX_CONTEXT_RADIUS:
        _refuse(
            ctx,
            "get_file_context",
            f"radius {radius} exceeds MAX_CONTEXT_RADIUS={MAX_CONTEXT_RADIUS}",
        )
    if line < 1:
        _refuse(ctx, "get_file_context", f"line {line} is not a valid 1-based line number")

    if file_lines is None:
        return None
    start = max(0, line - 1 - radius)
    end = min(len(file_lines), line + radius)
    return list(file_lines[start:end])


def search_team_conventions(ctx: ToolContext, query: str, corpus: Sequence[str]) -> list[str]:
    """Read-only BM25 top-3 retrieval over `corpus` (Style, Improvement).

    `corpus` comes from gh/history.py's team_conventions.md (Phase 6); an empty
    corpus (nothing mined yet) returns no results rather than fabricating any.

    Relevance is gated on plain lexical token overlap, not on BM25's score sign --
    BM25's IDF term goes negative for a term appearing in most/all documents of a
    small corpus (a well-known edge case), which would otherwise silently reject a
    genuine match whenever the corpus has only a handful of entries. BM25 is still
    used to *rank* the overlapping candidates.
    """
    _check_allowed(ctx, "search_team_conventions")
    if not corpus:
        return []
    query_tokens = set(query.lower().split())
    tokenized_corpus = [doc.lower().split() for doc in corpus]
    overlapping = [i for i, tokens in enumerate(tokenized_corpus) if query_tokens & set(tokens)]
    if not overlapping:
        return []
    bm25 = BM25Okapi(tokenized_corpus)
    scores = bm25.get_scores(query.lower().split())
    ranked = sorted(overlapping, key=lambda i: scores[i], reverse=True)
    return [corpus[i] for i in ranked[:3]]


def get_ci_result(ctx: ToolContext, test_name: str, ci_results: dict[str, object]) -> str | None:
    """Look up one test's status from an already-loaded ci-results.json (Bug only)."""
    _check_allowed(ctx, "get_ci_result")
    result = ci_results.get(test_name)
    if result is None:
        return None
    if not isinstance(result, str):
        _refuse(ctx, "get_ci_result", f"ci_results[{test_name!r}] is not a string status")
    return result


def lookup_rule(ctx: ToolContext, rule_id: str) -> str | None:
    """Static rule-id -> description lookup, no network (all agents)."""
    _check_allowed(ctx, "lookup_rule")
    if rule_id in _KNOWN_RULE_DESCRIPTIONS:
        return _KNOWN_RULE_DESCRIPTIONS[rule_id]
    if any(pattern.match(rule_id) for pattern in TAXONOMY_PATTERNS):
        return f"{rule_id} (recognized taxonomy, no further description on file)"
    return None
