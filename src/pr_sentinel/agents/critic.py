"""Critic agent (CLAUDE.md roles table): prunes false positives, downgrades over-severe findings.

Only ever narrows or adjusts severity on the findings it was handed -- it can never
fabricate a new finding, and any field other than severity that the model tries to
alter on a kept finding is silently reverted to the original. This is enforced in
code (_validate_critic_output), not just prompted, since a finding-pruning agent
getting fooled is a real security-relevant failure mode.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from pr_sentinel.agents.base import Agent
from pr_sentinel.llm.json_mode import JsonModeGiveUp
from pr_sentinel.models import Finding
from pr_sentinel.prompts import render


class CriticDrop(BaseModel):
    """One finding the Critic removed, and why (logged, never posted)."""

    finding_id: str
    reason: str


class CriticOutput(BaseModel):
    """The Critic's schema-constrained response shape."""

    findings: list[Finding] = Field(default_factory=list)
    drops: list[CriticDrop] = Field(default_factory=list)


@dataclass
class CriticOutcome:
    """What one Critic pass over a finding set produced."""

    findings: list[Finding] = field(default_factory=list)
    drops: list[CriticDrop] = field(default_factory=list)
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


def _validate_critic_output(output: CriticOutput, original: list[Finding]) -> list[Finding]:
    """Accept only findings whose id was in the original set; only severity may change."""
    by_id = {f.id: f for f in original}
    validated: list[Finding] = []
    for candidate in output.findings:
        source = by_id.get(candidate.id)
        if source is None:
            continue  # fabricated or mangled beyond recognition -- never trust it
        if candidate.severity != source.severity:
            validated.append(source.model_copy(update={"severity": candidate.severity}))
        else:
            validated.append(source)
    return validated


class CriticAgent(Agent):
    """Semantic quality control over the merged, grounded finding set."""

    role = "critic"
    temperature = 0.0
    prompt_name = "critic"

    def review(self, findings: list[Finding], diff_text: str) -> CriticOutcome:
        if not findings:
            return CriticOutcome()

        template_vars = {
            "diff_text": diff_text,
            "findings_json": json.dumps([f.model_dump(mode="json") for f in findings]),
        }
        system = render(self._prompt.system_template, template_vars)
        user = render(self._prompt.user_template, template_vars)
        result, response = self._call_json(CriticOutput, system, user)
        outcome = CriticOutcome(
            llm_calls=result.attempts, tokens_in=response.tokens_in, tokens_out=response.tokens_out
        )

        if isinstance(result, JsonModeGiveUp):
            # Fail safe: keep every original finding unfiltered rather than losing them.
            outcome.error = f"critic gave up: {result.last_error}"
            outcome.findings = findings
            return outcome

        outcome.findings = _validate_critic_output(result.value, findings)
        outcome.drops = result.value.drops
        return outcome
