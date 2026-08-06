"""Triage agent (CLAUDE.md roles table): per-file risk classification and agent assignment.

Enforces the call budget by capping which files get specialist review, and enforces
CLAUDE.md's one absolute: "security" runs on every non-skipped file regardless of
what risk the model assigned or which agents it listed.
"""

from __future__ import annotations

from pr_sentinel.agents.base import Agent
from pr_sentinel.llm.json_mode import JsonModeGiveUp
from pr_sentinel.models import TriageFilePlan, TriagePlan
from pr_sentinel.prompts import render


def _enforce_security_never_skipped(plan: TriagePlan) -> TriagePlan:
    """CLAUDE.md: security review is never skipped, regardless of what Triage said."""
    fixed_files: list[TriageFilePlan] = []
    for file_plan in plan.files:
        if file_plan.skip_reason is not None:
            fixed_files.append(file_plan)
            continue
        if "security" not in file_plan.agents_to_run:
            file_plan = file_plan.model_copy(
                update={"agents_to_run": [*file_plan.agents_to_run, "security"]}
            )
        fixed_files.append(file_plan)
    return plan.model_copy(update={"files": fixed_files})


class TriageAgent(Agent):
    """Emits a TriagePlan: per-file language, risk, agents_to_run, skip_reason."""

    role = "triage"
    temperature = 0.0
    prompt_name = "triage"

    def plan(
        self,
        *,
        call_budget: int,
        files_summary: str,
        pr_metadata: str,
        ci_results: str,
        file_paths: list[str],
        languages: dict[str, str],
    ) -> TriagePlan:
        template_vars = {
            "call_budget": str(call_budget),
            "files_summary": files_summary,
            "pr_metadata": pr_metadata or "(none provided)",
            "ci_results": ci_results or "(none provided)",
        }
        system = render(self._prompt.system_template, template_vars)
        user = render(self._prompt.user_template, template_vars)
        result, _response = self._call_json(TriagePlan, system, user)

        if isinstance(result, JsonModeGiveUp):
            # Fail safe: if Triage itself can't be parsed, never review nothing --
            # default every file to medium risk with at least Security assigned.
            fallback = TriagePlan(
                files=[
                    TriageFilePlan(
                        file=path,
                        language=languages.get(path, "unknown"),
                        risk="medium",
                        agents_to_run=["security"],
                    )
                    for path in file_paths
                ],
                llm_call_budget=call_budget,
            )
            return _enforce_security_never_skipped(fallback)

        return _enforce_security_never_skipped(result.value)
