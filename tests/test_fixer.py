"""Tests for the standalone, opt-in Fixer agent."""

from __future__ import annotations

import json

from pr_sentinel.agents.fixer import FixerAgent
from pr_sentinel.llm.provider import LLMRequest, LLMResponse
from pr_sentinel.models import Finding


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        text = self._texts.pop(0)
        return LLMResponse(text=text, tokens_in=2, tokens_out=2, latency_ms=0, model="scripted")


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "agent": "security",
        "file": "app/db.py",
        "line_start": 10,
        "line_end": 10,
        "severity": "critical",
        "confidence": 0.9,
        "rule_id": "CWE-89",
        "title": "Parameterize the SQL query",
        "fact": "User input is concatenated into a SQL string.",
        "assumption": None,
        "impact": "Allows SQL injection.",
        "recommendation": "Use a parameterized query.",
        "evidence_quote": 'cursor.execute("SELECT * FROM t WHERE x=" + user_input)',
    }
    base.update(overrides)
    return Finding.model_validate(base)


def _agent(texts: list[str]) -> FixerAgent:
    return FixerAgent(
        _ScriptedProvider(texts),  # type: ignore[arg-type]
        cache=None,
        governor=None,
        provider_name="scripted",
        model="test-model",
        max_output_tokens=256,
        run_id="run-1",
    )


def test_fixer_role_and_temperature() -> None:
    agent = _agent([])
    assert agent.role == "fixer"
    assert agent.temperature == 0.1


def test_fixer_refuses_low_severity_without_calling_llm() -> None:
    agent = _agent([])
    finding = _finding(severity="medium")
    result = agent.fix(finding, diff_text="...")
    assert result.suggestion is None
    assert "not critical/high" in result.refused_reasons[0]
    assert result.llm_calls == 0


def test_fixer_produces_suggestion_for_high_severity() -> None:
    patch = 'cursor.execute("SELECT * FROM t WHERE x=%s", (user_input,))'
    patch_json = json.dumps({"patch": patch})
    agent = _agent([patch_json])
    finding = _finding(severity="high")
    result = agent.fix(finding, diff_text="...")
    assert result.suggestion is not None
    assert result.suggestion.startswith("```suggestion\n")
    assert result.suggestion.endswith("\n```")
    assert "%s" in result.suggestion
    assert result.refused_reasons == []


def test_fixer_restores_crlf_line_ending() -> None:
    patch_json = json.dumps({"patch": "line1\nline2"})
    agent = _agent([patch_json])
    finding = _finding(severity="critical")
    result = agent.fix(finding, diff_text="...", line_ending="crlf")
    assert result.suggestion is not None
    assert "line1\r\nline2" in result.suggestion


def test_fixer_refuses_unsafe_workflow_path() -> None:
    patch_json = json.dumps({"patch": "on: push"})
    agent = _agent([patch_json])
    finding = _finding(severity="critical", file=".github/workflows/ci.yml")
    result = agent.fix(finding, diff_text="...")
    assert result.suggestion is None
    assert any("workflow" in r for r in result.refused_reasons)


def test_fixer_refuses_unsafe_code_pattern() -> None:
    patch_json = json.dumps({"patch": "result = eval(user_input)"})
    agent = _agent([patch_json])
    finding = _finding(severity="critical")
    result = agent.fix(finding, diff_text="...")
    assert result.suggestion is None
    assert any("unsafe call" in r for r in result.refused_reasons)


def test_fixer_empty_patch_is_refused() -> None:
    patch_json = json.dumps({"patch": "   "})
    agent = _agent([patch_json])
    finding = _finding(severity="critical")
    result = agent.fix(finding, diff_text="...")
    assert result.suggestion is None
    assert "empty patch" in result.refused_reasons[0]


def test_fixer_giveup_records_error_not_crash() -> None:
    agent = _agent(["not json", "still not json"])
    finding = _finding(severity="critical")
    result = agent.fix(finding, diff_text="...")
    assert result.suggestion is None
    assert result.error is not None
    assert result.llm_calls == 2
