"""Agent base machinery: prompt loading, the guarded provider, retry ladder, reflection.

Composes Phase 3 (llm/provider.py's call_llm, llm/json_mode.py's complete_json) and
Phase 4 (guardrails/redaction.py, guardrails/injection.py) with zero changes to
either -- GuardedProvider only needs to satisfy the LLMProvider Protocol
(`complete(request) -> LLMResponse`) to be usable anywhere a raw provider is.

Retry ladder: complete_json() owns attempts 1-2 (initial + schema-repair). Attempt 3
("smaller chunk") lives here: on JsonModeGiveUp, a multi-hunk chunk is split in half
and each half retried independently; a single-hunk chunk that still fails is recorded
as a per-chunk error, never a whole-agent crash.

Reflection: exactly one extra call per specialist, re-prompting with the draft
findings and asking the model to re-quote or delete each one. A reflection call that
itself fails schema validation falls back to the pre-reflection findings rather than
discarding good output.

Note on tool-calling: agents/tools.py's four functions are real, validated, callable
Python code, but no model-driven tool-use loop is wired here -- Phase 3's
AnthropicProvider only extracts `type: "text"` content blocks, not `tool_use` blocks,
so there is no tool-invocation protocol for the model to drive yet. Concrete agents
may still call tools.py functions directly in their own template-variable assembly
(e.g. Style calling search_team_conventions to build {{team_conventions}}).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, TypeVar

from pydantic import BaseModel, Field

from pr_sentinel.agents.tools import ToolContext
from pr_sentinel.audit import AuditLog
from pr_sentinel.core.chunking import Chunk, render_hunk
from pr_sentinel.guardrails.injection import (
    detect_injection,
    injection_finding,
    wrap_untrusted,
)
from pr_sentinel.guardrails.redaction import redact_request
from pr_sentinel.llm.json_mode import JsonModeGiveUp, JsonModeResult, JsonModeSuccess, complete_json
from pr_sentinel.llm.provider import (
    CacheLike,
    GovernorLike,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    call_llm,
)
from pr_sentinel.models import Finding
from pr_sentinel.prompts import PromptSpec, load_prompt, render

T = TypeVar("T", bound=BaseModel)


class AgentFindings(BaseModel):
    """Wrapper schema every chunk-based specialist (Bug/Security/Style/Improvement) emits."""

    findings: list[Finding] = Field(default_factory=list)


class GuardedProvider:
    """Wraps a raw LLMProvider with redaction, then the Phase 3 cache/budget composition.

    Every .complete() call -- the initial attempt, the schema-repair attempt, and the
    reflection call -- independently goes through redaction, then
    cache-then-budget-then-provider. Satisfies the LLMProvider Protocol so it can be
    handed directly to complete_json().
    """

    name = "guarded"

    def __init__(
        self,
        provider: LLMProvider,
        *,
        cache: CacheLike | None,
        governor: GovernorLike | None,
        provider_name: str,
        model: str,
        prompt_version: str,
        agent: str,
        source: str = "<prompt>",
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._governor = governor
        self._provider_name = provider_name
        self._model = model
        self._prompt_version = prompt_version
        self._agent = agent
        self.source = source
        self.redaction_findings: list[Finding] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        safe_request, findings = redact_request(request, source=self.source)
        self.redaction_findings.extend(findings)
        return call_llm(
            self._provider,
            safe_request,
            cache=self._cache,
            governor=self._governor,
            provider_name=self._provider_name,
            model=self._model,
            prompt_version=self._prompt_version,
            agent=self._agent,
        )


class Agent:
    """Shared machinery: prompt loading, the guarded provider, tracked JSON calls."""

    role: ClassVar[str]
    temperature: ClassVar[float]
    prompt_name: ClassVar[str]

    def __init__(
        self,
        provider: LLMProvider,
        *,
        cache: CacheLike | None,
        governor: GovernorLike | None,
        provider_name: str,
        model: str,
        max_output_tokens: int,
        run_id: str,
    ) -> None:
        self._prompt: PromptSpec = load_prompt(self.prompt_name)
        self._max_output_tokens = max_output_tokens
        self._run_id = run_id
        self.guarded_provider = GuardedProvider(
            provider,
            cache=cache,
            governor=governor,
            provider_name=provider_name,
            model=model,
            prompt_version=self._prompt.version,
            agent=self.role,
        )

    def _call_json(
        self, schema: type[T], system: str, user: str
    ) -> tuple[JsonModeResult[T], LLMResponse]:
        return complete_json(
            self.guarded_provider,
            schema,
            system=system,
            user=user,
            max_output_tokens=self._max_output_tokens,
            temperature=self.temperature,
        )

    @property
    def prompt_version(self) -> str:
        """The version string of the .prompt.yml this agent loaded (feeds ReviewReport)."""
        return self._prompt.version

    def tool_context(
        self, changed_file_paths: frozenset[str], *, audit: AuditLog | None = None
    ) -> ToolContext:
        """Build the ToolContext this agent should use for every tools.py call it makes."""
        return ToolContext(
            agent=self.role, changed_file_paths=changed_file_paths, run_id=self._run_id, audit=audit
        )


@dataclass
class ChunkAgentOutcome:
    """Everything one run_on_chunk() call produced, across every split attempt."""

    findings: list[Finding] = field(default_factory=list)
    injection_findings: list[Finding] = field(default_factory=list)
    redaction_findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


def _render_chunk_diff(chunk: Chunk) -> str:
    return "\n".join(render_hunk(hunk) for hunk in chunk.hunks)


def _split_chunk(chunk: Chunk) -> list[Chunk]:
    mid = len(chunk.hunks) // 2
    halves = [chunk.hunks[:mid], chunk.hunks[mid:]]
    return [Chunk(file=chunk.file, hunks=half) for half in halves if half]


class ChunkAgent(Agent):
    """Shared run_on_chunk machinery for Bug, Security, Style, and Improvement."""

    def review(self, chunk: Chunk, *, language: str, **_ignored: object) -> ChunkAgentOutcome:
        """Convenience entrypoint: the standard {file, language} template vars.

        Accepts and ignores extra keyword arguments so callers can use one uniform
        `agent.review(chunk, language=..., **extras)` call across every specialist,
        even though only StyleAgent's override actually uses the extras.
        """
        return self.run_on_chunk(chunk, {"file": chunk.file, "language": language})

    def run_on_chunk(self, chunk: Chunk, template_vars: dict[str, str]) -> ChunkAgentOutcome:
        """Review one chunk: injection scan, complete+repair, reflect, split-retry on give-up."""
        injection_hits = detect_injection(_render_chunk_diff(chunk))
        outcome = ChunkAgentOutcome(
            injection_findings=[injection_finding(hit, chunk.file) for hit in injection_hits]
        )
        self.guarded_provider.source = chunk.file
        self._run_chunk_recursive(chunk, template_vars, outcome)
        outcome.redaction_findings = list(self.guarded_provider.redaction_findings)
        return outcome

    def _run_chunk_recursive(
        self, chunk: Chunk, template_vars: dict[str, str], outcome: ChunkAgentOutcome
    ) -> None:
        diff_text = wrap_untrusted(_render_chunk_diff(chunk))
        merged_vars = {**template_vars, "diff_text": diff_text}
        system = render(self._prompt.system_template, merged_vars)
        user = render(self._prompt.user_template, merged_vars)

        result, response = self._call_json(AgentFindings, system, user)
        outcome.llm_calls += result.attempts
        outcome.tokens_in += response.tokens_in
        outcome.tokens_out += response.tokens_out

        if isinstance(result, JsonModeGiveUp):
            if len(chunk.hunks) > 1:
                for half in _split_chunk(chunk):
                    self._run_chunk_recursive(half, template_vars, outcome)
            else:
                outcome.errors.append(
                    f"{chunk.file}: gave up after repair attempts: {result.last_error}"
                )
            return

        reflected = self._reflect(system, user, result.value, outcome)
        outcome.findings.extend(reflected.findings)

    def _reflect(
        self, system: str, original_user: str, draft: AgentFindings, outcome: ChunkAgentOutcome
    ) -> AgentFindings:
        """One self-review pass: re-quote or delete each finding. Falls back on failure."""
        if not draft.findings:
            return draft
        reflection_user = (
            f"{original_user}\n\nYour draft findings (JSON):\n"
            f"{draft.model_dump_json()}\n\n"
            "Self-review: for each finding, re-quote the exact evidence_quote from the diff "
            "above. If you cannot find that exact text in the diff, DELETE that finding. "
            "Re-emit only the findings that survive this check, in the same JSON shape."
        )
        result, response = self._call_json(AgentFindings, system, reflection_user)
        outcome.llm_calls += result.attempts
        outcome.tokens_in += response.tokens_in
        outcome.tokens_out += response.tokens_out
        if isinstance(result, JsonModeSuccess):
            return result.value
        return draft
