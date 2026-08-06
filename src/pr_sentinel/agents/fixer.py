"""Fixer agent (CLAUDE.md roles table): opt-in, per-finding minimal patch generation.

Never wired into the default `local` pipeline -- invoked per finding-id via
/sentinel fix (Phase 7), critical/high severity only, max 5 per run (enforced by
that caller, not here). Refuses via guardrails/policy.py's check_patch_safety
before ever emitting a patch, and restores the original file's line ending so
"Apply suggestion" on a CRLF file doesn't inject spurious whitespace-only changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from pr_sentinel.agents.base import Agent
from pr_sentinel.guardrails.policy import check_patch_safety
from pr_sentinel.llm.json_mode import JsonModeGiveUp
from pr_sentinel.models import Finding, LineEnding
from pr_sentinel.prompts import render

ELIGIBLE_SEVERITIES = frozenset({"critical", "high"})


class FixerOutput(BaseModel):
    """The Fixer's schema-constrained response shape."""

    patch: str


@dataclass
class FixResult:
    """What one Fixer invocation produced."""

    suggestion: str | None
    refused_reasons: list[str] = field(default_factory=list)
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


def _restore_line_ending(text: str, line_ending: LineEnding) -> str:
    """Undo LF-normalization for a suggestion block (CRLF files need \\r\\n back)."""
    if line_ending == "crlf":
        return text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text


def _render_suggestion_block(patch: str, line_ending: LineEnding) -> str:
    restored = _restore_line_ending(patch, line_ending)
    return f"```suggestion\n{restored}\n```"


class FixerAgent(Agent):
    """Given one Finding + its diff context, emits a minimal GitHub suggestion block."""

    role = "fixer"
    temperature = 0.1
    prompt_name = "fixer"

    def fix(self, finding: Finding, diff_text: str, *, line_ending: LineEnding = "lf") -> FixResult:
        if finding.severity not in ELIGIBLE_SEVERITIES:
            return FixResult(
                suggestion=None,
                refused_reasons=[
                    f"severity {finding.severity!r} is not critical/high -- "
                    "the Fixer is opt-in for those only"
                ],
            )

        template_vars = {"finding_json": finding.model_dump_json(), "diff_text": diff_text}
        system = render(self._prompt.system_template, template_vars)
        user = render(self._prompt.user_template, template_vars)
        result, response = self._call_json(FixerOutput, system, user)
        llm_calls, tokens_in, tokens_out = result.attempts, response.tokens_in, response.tokens_out

        if isinstance(result, JsonModeGiveUp):
            return FixResult(
                suggestion=None,
                llm_calls=llm_calls,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                error=f"fixer gave up: {result.last_error}",
            )

        patch = result.value.patch
        unsafe_reasons = check_patch_safety(finding.file, patch)
        if unsafe_reasons:
            return FixResult(
                suggestion=None,
                refused_reasons=unsafe_reasons,
                llm_calls=llm_calls,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        if not patch.strip():
            return FixResult(
                suggestion=None,
                refused_reasons=["model returned an empty patch"],
                llm_calls=llm_calls,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        return FixResult(
            suggestion=_render_suggestion_block(patch, line_ending),
            llm_calls=llm_calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
