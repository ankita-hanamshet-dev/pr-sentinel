"""Tests for the concrete agents: role/temperature wiring, Triage, Critic guards."""

from __future__ import annotations

import json
from pathlib import Path

from pr_sentinel.agents.base import _render_chunk_diff
from pr_sentinel.agents.bug import BugAgent
from pr_sentinel.agents.critic import CriticAgent
from pr_sentinel.agents.improvement import ImprovementAgent
from pr_sentinel.agents.security import SecurityAgent
from pr_sentinel.agents.style import StyleAgent
from pr_sentinel.agents.triage import TriageAgent
from pr_sentinel.core.chunking import Chunk
from pr_sentinel.guardrails.injection import wrap_untrusted
from pr_sentinel.llm.provider import LLMRequest, LLMResponse
from pr_sentinel.llm.replay import ReplayProvider, replay_key
from pr_sentinel.models import Finding, Hunk
from pr_sentinel.prompts import load_prompt, render


def _hunk(**overrides: object) -> Hunk:
    base: dict[str, object] = {
        "file": "app/db.py",
        "old_start": 4,
        "old_len": 1,
        "new_start": 4,
        "new_len": 2,
        "lines": [" ctx", "+x.value"],
    }
    base.update(overrides)
    return Hunk.model_validate(base)


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "agent": "bug",
        "file": "app/db.py",
        "line_start": 5,
        "line_end": 5,
        "severity": "medium",
        "confidence": 0.8,
        "rule_id": "CWE-476",
        "title": "Possible null dereference",
        "fact": "x may be None.",
        "assumption": None,
        "impact": "Crash.",
        "recommendation": "Add a check.",
        "evidence_quote": "x.value",
    }
    base.update(overrides)
    return Finding.model_validate(base)


class _SpyProvider:
    name = "spy"

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        text = self._texts.pop(0)
        return LLMResponse(text=text, tokens_in=1, tokens_out=1, latency_ms=0, model="spy")


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "cache": None,
        "governor": None,
        "provider_name": "spy",
        "model": "test-model",
        "max_output_tokens": 512,
        "run_id": "run-1",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Role / temperature wiring (CLAUDE.md roles table, matched exactly)
# ---------------------------------------------------------------------------


def test_bug_agent_role_and_temperature() -> None:
    agent = BugAgent(_SpyProvider([]), **_kwargs())  # type: ignore[arg-type]
    assert agent.role == "bug"
    assert agent.temperature == 0.1


def test_security_agent_role_and_temperature() -> None:
    agent = SecurityAgent(_SpyProvider([]), **_kwargs())  # type: ignore[arg-type]
    assert agent.role == "security"
    assert agent.temperature == 0.0


def test_improvement_agent_role_and_temperature() -> None:
    agent = ImprovementAgent(_SpyProvider([]), **_kwargs())  # type: ignore[arg-type]
    assert agent.role == "improvement"
    assert agent.temperature == 0.2


def test_style_agent_role_and_temperature() -> None:
    agent = StyleAgent(_SpyProvider([]), **_kwargs())  # type: ignore[arg-type]
    assert agent.role == "style"
    assert agent.temperature == 0.1


def test_triage_agent_role_and_temperature() -> None:
    agent = TriageAgent(_SpyProvider([]), **_kwargs())  # type: ignore[arg-type]
    assert agent.role == "triage"
    assert agent.temperature == 0.0


def test_critic_agent_role_and_temperature() -> None:
    agent = CriticAgent(_SpyProvider([]), **_kwargs())  # type: ignore[arg-type]
    assert agent.role == "critic"
    assert agent.temperature == 0.0


# ---------------------------------------------------------------------------
# End-to-end through the real replay provider (no findings -> no reflection call,
# so a single fixture covers the whole run)
# ---------------------------------------------------------------------------


def test_bug_agent_end_to_end_with_replay_provider(tmp_path: Path) -> None:
    chunk = Chunk(file="app/db.py", hunks=[_hunk()])
    template_vars = {"file": chunk.file, "language": "python", "diff_text": ""}
    prompt = load_prompt("bug")
    merged_vars = {**template_vars, "diff_text": wrap_untrusted(_render_chunk_diff(chunk))}
    system = render(prompt.system_template, merged_vars)
    user = render(prompt.user_template, merged_vars)
    request = LLMRequest(system=system, user=user, max_output_tokens=512)

    key = replay_key(request)
    (tmp_path / f"{key}.json").write_text(
        json.dumps({"text": '{"findings": []}', "tokens_in": 10, "tokens_out": 4, "model": "r"})
    )

    provider = ReplayProvider(base_dir=tmp_path)
    agent = BugAgent(provider, **_kwargs(provider_name="replay"))  # type: ignore[arg-type]
    outcome = agent.review(chunk, language="python")

    assert outcome.findings == []
    assert outcome.errors == []
    assert outcome.llm_calls == 1  # no draft findings -> reflection skipped


def test_style_agent_injects_style_guide_and_conventions_into_prompt() -> None:
    spy = _SpyProvider(['{"findings": []}'])
    agent = StyleAgent(spy, **_kwargs())  # type: ignore[arg-type]
    chunk = Chunk(file="app/db.py", hunks=[_hunk()])

    agent.review(
        chunk,
        language="python",
        conventions_corpus=["in python code, prefer pathlib over os.path in this repo"],
        changed_file_paths=frozenset({"app/db.py"}),
    )

    assert len(spy.requests) == 1
    assert "PEP 8" in spy.requests[0].system
    assert "pathlib" in spy.requests[0].system


def test_improvement_agent_uses_default_style_when_language_unknown() -> None:
    # Improvement doesn't take a style guide at all -- just confirm it renders cleanly
    # for a language with no dedicated prompt customization.
    spy = _SpyProvider(['{"findings": []}'])
    agent = ImprovementAgent(spy, **_kwargs())  # type: ignore[arg-type]
    chunk = Chunk(file="app/db.py", hunks=[_hunk()])
    outcome = agent.review(chunk, language="cobol")
    assert outcome.findings == []
    assert "cobol" in spy.requests[0].user


# ---------------------------------------------------------------------------
# TriageAgent
# ---------------------------------------------------------------------------


def _triage_plan_json(*files: dict[str, object]) -> str:
    return json.dumps({"files": list(files), "llm_call_budget": 12})


def test_triage_forces_security_into_agents_to_run() -> None:
    plan_json = _triage_plan_json(
        {
            "file": "app/db.py",
            "language": "python",
            "risk": "high",
            "agents_to_run": ["bug"],
            "skip_reason": None,
        }
    )
    spy = _SpyProvider([plan_json])
    agent = TriageAgent(spy, **_kwargs())  # type: ignore[arg-type]
    plan = agent.plan(
        call_budget=12,
        files_summary="app/db.py",
        pr_metadata="",
        ci_results="",
        file_paths=["app/db.py"],
        languages={"app/db.py": "python"},
    )
    assert plan.files[0].agents_to_run == ["bug", "security"]


def test_triage_does_not_duplicate_security() -> None:
    plan_json = _triage_plan_json(
        {
            "file": "app/db.py",
            "language": "python",
            "risk": "high",
            "agents_to_run": ["security", "bug"],
            "skip_reason": None,
        }
    )
    spy = _SpyProvider([plan_json])
    agent = TriageAgent(spy, **_kwargs())  # type: ignore[arg-type]
    plan = agent.plan(
        call_budget=12,
        files_summary="app/db.py",
        pr_metadata="",
        ci_results="",
        file_paths=["app/db.py"],
        languages={"app/db.py": "python"},
    )
    assert plan.files[0].agents_to_run == ["security", "bug"]


def test_triage_skipped_file_is_not_given_security() -> None:
    plan_json = _triage_plan_json(
        {
            "file": "vendor/lib.py",
            "language": "python",
            "risk": "low",
            "agents_to_run": [],
            "skip_reason": "vendored code",
        }
    )
    spy = _SpyProvider([plan_json])
    agent = TriageAgent(spy, **_kwargs())  # type: ignore[arg-type]
    plan = agent.plan(
        call_budget=12,
        files_summary="vendor/lib.py",
        pr_metadata="",
        ci_results="",
        file_paths=["vendor/lib.py"],
        languages={"vendor/lib.py": "python"},
    )
    assert plan.files[0].agents_to_run == []
    assert plan.files[0].skip_reason == "vendored code"


def test_triage_fallback_on_giveup_defaults_to_medium_with_security() -> None:
    spy = _SpyProvider(["not json", "still not json"])
    agent = TriageAgent(spy, **_kwargs())  # type: ignore[arg-type]
    plan = agent.plan(
        call_budget=12,
        files_summary="app/db.py",
        pr_metadata="",
        ci_results="",
        file_paths=["app/db.py", "app/other.py"],
        languages={"app/db.py": "python"},
    )
    assert len(plan.files) == 2
    assert all(f.risk == "medium" for f in plan.files)
    assert all(f.agents_to_run == ["security"] for f in plan.files)
    assert plan.files[1].language == "unknown"  # not in the languages map


# ---------------------------------------------------------------------------
# CriticAgent
# ---------------------------------------------------------------------------


def test_critic_empty_input_short_circuits_without_llm_call() -> None:
    spy = _SpyProvider([])
    agent = CriticAgent(spy, **_kwargs())  # type: ignore[arg-type]
    outcome = agent.review([], diff_text="")
    assert outcome.findings == []
    assert outcome.llm_calls == 0
    assert spy.requests == []


def test_critic_keeps_finding_and_downgrades_severity() -> None:
    original = _finding(severity="critical")
    kept = original.model_dump(mode="json")
    kept["severity"] = "medium"
    output_json = json.dumps({"findings": [kept], "drops": []})
    spy = _SpyProvider([output_json])
    agent = CriticAgent(spy, **_kwargs())  # type: ignore[arg-type]

    outcome = agent.review([original], diff_text="x.value")

    assert len(outcome.findings) == 1
    assert outcome.findings[0].severity == "medium"
    assert outcome.findings[0].id == original.id


def test_critic_drops_a_finding_and_records_reason() -> None:
    original = _finding()
    output_json = json.dumps(
        {"findings": [], "drops": [{"finding_id": original.id, "reason": "not a real issue"}]}
    )
    spy = _SpyProvider([output_json])
    agent = CriticAgent(spy, **_kwargs())  # type: ignore[arg-type]

    outcome = agent.review([original], diff_text="x.value")

    assert outcome.findings == []
    assert outcome.drops[0].finding_id == original.id


def test_critic_rejects_a_fabricated_finding() -> None:
    original = _finding()
    fabricated = _finding(title="Something the model invented", rule_id="CWE-999").model_dump(
        mode="json"
    )
    output_json = json.dumps({"findings": [fabricated], "drops": []})
    spy = _SpyProvider([output_json])
    agent = CriticAgent(spy, **_kwargs())  # type: ignore[arg-type]

    outcome = agent.review([original], diff_text="x.value")

    assert outcome.findings == []  # fabricated finding never accepted


def test_critic_ignores_tampering_with_fields_other_than_severity() -> None:
    original = _finding()
    tampered = original.model_dump(mode="json")
    tampered["evidence_quote"] = "something the model rewrote"
    tampered["recommendation"] = "a different recommendation"
    output_json = json.dumps({"findings": [tampered], "drops": []})
    spy = _SpyProvider([output_json])
    agent = CriticAgent(spy, **_kwargs())  # type: ignore[arg-type]

    outcome = agent.review([original], diff_text="x.value")

    assert len(outcome.findings) == 1
    assert outcome.findings[0].evidence_quote == original.evidence_quote
    assert outcome.findings[0].recommendation == original.recommendation


def test_critic_fallback_on_giveup_keeps_original_findings() -> None:
    original = _finding()
    spy = _SpyProvider(["not json", "still not json"])
    agent = CriticAgent(spy, **_kwargs())  # type: ignore[arg-type]

    outcome = agent.review([original], diff_text="x.value")

    assert outcome.findings == [original]
    assert outcome.error is not None
