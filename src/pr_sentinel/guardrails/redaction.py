"""Secret redaction: regex + entropy detection, run before any payload leaves the process.

CLAUDE.md: a redaction hit is itself emitted as a critical Finding, rule_id CWE-798.
Detectors run in specificity order (most-precise first) so a high-precision match (e.g. a
JWT) is replaced before the lower-precision generic-entropy scan could also claim it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from pr_sentinel.llm.provider import LLMRequest
from pr_sentinel.models import Finding

_PLACEHOLDER = "«REDACTED:{kind}»"

_PEM_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]+?"
    r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_GITHUB_PAT = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_CONNECTION_STRING = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@'\"]+:[^\s:/@'\"]+@[^\s'\"]+")
_BEARER_TOKEN = re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*")
_GENERIC_SECRET = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:key|secret|token|password|passwd|api[_-]?key)(?![A-Za-z0-9])"
    r"\s*[:=]\s*['\"]?([A-Za-z0-9+/_=-]{20,})['\"]?"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

_ENTROPY_THRESHOLD = 3.5


def _shannon_entropy(s: str) -> float:
    """Bits of entropy per character; callers only ever pass non-empty candidates."""
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    length = len(s)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


@dataclass(frozen=True)
class RedactionHit:
    """One detected secret: its kind and the (1-based) line it was found on."""

    kind: str
    line: int


@dataclass(frozen=True)
class RedactionResult:
    """The redacted text plus every hit that was replaced."""

    text: str
    hits: list[RedactionHit] = field(default_factory=list)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _redact_pattern(
    text: str, pattern: re.Pattern[str], kind: str, hits: list[RedactionHit]
) -> str:
    for match in pattern.finditer(text):
        hits.append(RedactionHit(kind=kind, line=_line_of(text, match.start())))
    return pattern.sub(_PLACEHOLDER.format(kind=kind), text)


def _redact_generic_secret(text: str, hits: list[RedactionHit]) -> str:
    def _replace(match: re.Match[str]) -> str:
        candidate = match.group(1)
        if _shannon_entropy(candidate) < _ENTROPY_THRESHOLD:
            return match.group(0)
        hits.append(RedactionHit(kind="generic_high_entropy", line=_line_of(text, match.start(1))))
        return match.group(0).replace(candidate, _PLACEHOLDER.format(kind="generic_high_entropy"))

    return _GENERIC_SECRET.sub(_replace, text)


def redact_text(text: str) -> RedactionResult:
    """Detect and replace secrets with «REDACTED:<kind>», in specificity order."""
    hits: list[RedactionHit] = []
    working = text
    working = _redact_pattern(working, _PEM_BLOCK, "pem_private_key", hits)
    working = _redact_pattern(working, _AWS_KEY, "aws_key", hits)
    working = _redact_pattern(working, _GITHUB_PAT, "github_pat", hits)
    working = _redact_pattern(working, _JWT, "jwt", hits)
    working = _redact_pattern(working, _CONNECTION_STRING, "connection_string", hits)
    working = _redact_pattern(working, _BEARER_TOKEN, "bearer_token", hits)
    working = _redact_generic_secret(working, hits)
    working = _redact_pattern(working, _EMAIL, "email", hits)
    return RedactionResult(text=working, hits=hits)


def redaction_finding(hit: RedactionHit, source: str) -> Finding:
    """Build the mandatory critical Finding (CWE-798) for a single redaction hit."""
    return Finding(
        agent="security",
        file=source,
        line_start=hit.line,
        line_end=hit.line,
        severity="critical",
        confidence=1.0,
        rule_id="CWE-798",
        title=f"Hardcoded credential detected ({hit.kind})",
        fact=(
            f"A {hit.kind} pattern was detected and redacted before this content left the process."
        ),
        assumption=None,
        impact=(
            "A leaked credential of this kind can be used to impersonate or access "
            "the associated service."
        ),
        recommendation="Remove the credential from source control and rotate it immediately.",
        evidence_quote=_PLACEHOLDER.format(kind=hit.kind),
    )


def redact_request(
    request: LLMRequest, *, source: str = "<prompt>"
) -> tuple[LLMRequest, list[Finding]]:
    """Redact both turns of a request; return a safe-to-send request plus any Findings."""
    system_result = redact_text(request.system)
    user_result = redact_text(request.user)
    findings = [redaction_finding(hit, source) for hit in (*system_result.hits, *user_result.hits)]
    safe_request = LLMRequest(
        system=system_result.text,
        user=user_result.text,
        max_output_tokens=request.max_output_tokens,
        temperature=request.temperature,
    )
    return safe_request, findings
