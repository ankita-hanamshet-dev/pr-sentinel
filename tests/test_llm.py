"""Tests for the LLM layer: providers, retry/backoff, budget, cache, JSON mode, replay.

All offline — pytest-httpx intercepts every HTTP call, so an accidental real
request (e.g. a cache hit that still called out) fails the test immediately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from pytest_httpx import HTTPXMock

from pr_sentinel.llm.anthropic import AnthropicProvider
from pr_sentinel.llm.budget import BudgetExhausted, BudgetGovernor
from pr_sentinel.llm.cache import LLMCache, cache_key
from pr_sentinel.llm.github_models import GitHubModelsProvider
from pr_sentinel.llm.json_mode import JsonModeGiveUp, JsonModeSuccess, complete_json
from pr_sentinel.llm.provider import LLMRequest, LLMResponse, call_llm
from pr_sentinel.llm.replay import ReplayNotFound, ReplayProvider, replay_key
from pr_sentinel.settings import Settings

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"


def _anthropic_json(text: str, tokens_in: int, tokens_out: int) -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }


def test_anthropic_marks_system_cacheable_only_when_requested(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=ANTHROPIC_URL, json=_anthropic_json("a", 1, 1))
    httpx_mock.add_response(url=ANTHROPIC_URL, json=_anthropic_json("b", 1, 1))
    provider = AnthropicProvider(model="claude-sonnet-5", api_key="sk")

    provider.complete(LLMRequest(system="rules", user="u", max_output_tokens=8, cache_system=True))
    provider.complete(LLMRequest(system="rules", user="u", max_output_tokens=8))
    reqs = httpx_mock.get_requests()
    cached_body = json.loads(reqs[0].content)
    plain_body = json.loads(reqs[1].content)
    assert cached_body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert plain_body["system"] == "rules"  # no cache_control on single-call agents


def test_anthropic_reports_cache_tokens(httpx_mock: HTTPXMock) -> None:
    payload = _anthropic_json("pong", 3, 2)
    usage = payload["usage"]
    assert isinstance(usage, dict)
    usage["cache_read_input_tokens"] = 1800
    usage["cache_creation_input_tokens"] = 0
    httpx_mock.add_response(url=ANTHROPIC_URL, json=payload)
    provider = AnthropicProvider(model="claude-sonnet-5", api_key="sk")

    req = LLMRequest(system="s", user="u", max_output_tokens=8, cache_system=True)
    resp = provider.complete(req)
    assert resp.cache_read_tokens == 1800
    assert resp.cache_write_tokens == 0


def test_governor_counts_a_cache_hit(httpx_mock: HTTPXMock) -> None:
    payload = _anthropic_json("pong", 3, 2)
    usage = payload["usage"]
    assert isinstance(usage, dict)
    usage["cache_read_input_tokens"] = 1800
    httpx_mock.add_response(url=ANTHROPIC_URL, json=payload)
    settings = Settings(llm_provider="anthropic", model="claude-sonnet-5", llm_api_key="sk")
    governor = BudgetGovernor(settings)
    call_llm(
        AnthropicProvider(model="claude-sonnet-5", api_key="sk"),
        LLMRequest(system="s", user="u", max_output_tokens=8, cache_system=True),
        cache=None, governor=governor, provider_name="anthropic",
        model="claude-sonnet-5", prompt_version="v1", agent="bug",
    )
    snap = governor.snapshot()
    assert snap["cache_hits"] == 1
    assert snap["cache_writes"] == 0


def test_anthropic_retries_on_429_then_succeeds(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=ANTHROPIC_URL, status_code=429, headers={"retry-after": "0"})
    httpx_mock.add_response(url=ANTHROPIC_URL, json=_anthropic_json("pong", 5, 2))

    provider = AnthropicProvider(model="claude-sonnet-5", api_key="sk-test")
    response = provider.complete(LLMRequest(system="sys", user="ping", max_output_tokens=16))

    assert response.text == "pong"
    assert response.tokens_in == 5
    assert response.tokens_out == 2


def test_github_models_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=GITHUB_MODELS_URL,
        json={
            "choices": [{"message": {"content": "pong"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        },
    )

    provider = GitHubModelsProvider(model="openai/gpt-4o-mini", github_token="ghp_test")
    response = provider.complete(LLMRequest(system="sys", user="ping", max_output_tokens=16))

    assert response.text == "pong"
    assert response.tokens_in == 5
    assert response.tokens_out == 2


def test_budget_refuses_call_n_plus_1_before_any_http_call(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=ANTHROPIC_URL, json=_anthropic_json("pong", 1, 1))
    httpx_mock.add_response(url=ANTHROPIC_URL, json=_anthropic_json("pong", 1, 1))

    settings = Settings(
        llm_provider="anthropic",
        model="claude-sonnet-5",
        llm_api_key="sk-test",
        max_llm_calls_per_run=2,
    )
    provider = AnthropicProvider(model=settings.model, api_key="sk-test")
    governor = BudgetGovernor(settings)
    request = LLMRequest(system="s", user="u", max_output_tokens=8)

    def _call() -> LLMResponse:
        return call_llm(
            provider,
            request,
            cache=None,
            governor=governor,
            provider_name="anthropic",
            model=settings.model,
            prompt_version="v1",
            agent="test",
        )

    _call()
    _call()
    with pytest.raises(BudgetExhausted) as exc_info:
        _call()
    assert exc_info.value.reason == "max_calls_per_run"


def test_budget_dollar_cap_is_the_hard_stop(httpx_mock: HTTPXMock) -> None:
    # One call bills 1000 output tokens = $0.015 at sonnet-5's $15/Mtok, which
    # already exceeds the $0.001 cap, so the SECOND reserve() is refused on dollars
    # even though the calls cap (12) has plenty of room left.
    httpx_mock.add_response(url=ANTHROPIC_URL, json=_anthropic_json("pong", 10, 1000))

    settings = Settings(
        llm_provider="anthropic",
        model="claude-sonnet-5",
        llm_api_key="sk-test",
        max_usd_per_run=0.001,
    )
    provider = AnthropicProvider(model=settings.model, api_key="sk-test")
    governor = BudgetGovernor(settings)
    request = LLMRequest(system="s", user="u", max_output_tokens=8)

    call_llm(
        provider,
        request,
        cache=None,
        governor=governor,
        provider_name="anthropic",
        model=settings.model,
        prompt_version="v1",
        agent="test",
    )
    # 1000 out * $15/Mtok + 10 in * $3/Mtok = 0.015 + 0.00003.
    assert governor.cost_used == pytest.approx(0.01503)
    with pytest.raises(BudgetExhausted) as exc_info:
        governor.reserve()
    assert exc_info.value.reason == "max_usd_per_run"


def test_cache_hit_avoids_http_call_entirely(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    # No httpx_mock.add_response registered: if the provider were ever called,
    # pytest-httpx raises on the unmatched request and this test fails.
    cache = LLMCache(path=tmp_path / "cache.sqlite", ttl_days=14)
    provider = AnthropicProvider(model="claude-sonnet-5", api_key="sk-test")
    request = LLMRequest(system="s", user="u", max_output_tokens=8)
    key = cache_key(
        "anthropic", "claude-sonnet-5", "v1", "test", request.system + "\x00" + request.user
    )
    cached = LLMResponse(
        text="cached", tokens_in=1, tokens_out=1, latency_ms=5, model="claude-sonnet-5"
    )
    cache.put(key, cached)

    response = call_llm(
        provider,
        request,
        cache=cache,
        governor=None,
        provider_name="anthropic",
        model="claude-sonnet-5",
        prompt_version="v1",
        agent="test",
    )

    assert response == cached


def test_cache_round_trip_and_ttl_expiry(tmp_path: Path) -> None:
    cache = LLMCache(path=tmp_path / "cache.sqlite", ttl_days=14)
    response = LLMResponse(text="hi", tokens_in=1, tokens_out=1, latency_ms=1, model="m")
    cache.put("k", response)
    assert cache.get("k") == response

    expired_cache = LLMCache(path=tmp_path / "expired.sqlite", ttl_days=0)
    expired_cache.put("k", response)
    assert expired_cache.get("k") is None


class _Widget(BaseModel):
    name: str
    count: int


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)

    def complete(self, request: LLMRequest) -> LLMResponse:
        text = self._texts.pop(0)
        return LLMResponse(text=text, tokens_in=1, tokens_out=1, latency_ms=0, model="test")


def test_json_mode_repairs_on_first_failure() -> None:
    provider = _ScriptedProvider(["not json", '{"name": "a", "count": 1}'])
    result, _response = complete_json(provider, _Widget, system="s", user="u", max_output_tokens=32)
    assert isinstance(result, JsonModeSuccess)
    assert result.attempts == 2
    assert result.value == _Widget(name="a", count=1)


def test_json_mode_succeeds_on_first_attempt() -> None:
    provider = _ScriptedProvider(['{"name": "a", "count": 1}'])
    result, _response = complete_json(provider, _Widget, system="s", user="u", max_output_tokens=32)
    assert isinstance(result, JsonModeSuccess)
    assert result.attempts == 1


def test_json_mode_gives_up_after_two_failures() -> None:
    provider = _ScriptedProvider(["not json", "still not json"])
    result, _response = complete_json(provider, _Widget, system="s", user="u", max_output_tokens=32)
    assert isinstance(result, JsonModeGiveUp)
    assert result.attempts == 2


def test_replay_provider_is_deterministic(tmp_path: Path) -> None:
    request = LLMRequest(system="s", user="u", max_output_tokens=8)
    key = replay_key(request)
    fixture = tmp_path / f"{key}.json"
    fixture.write_text(
        json.dumps({"text": "pong", "tokens_in": 3, "tokens_out": 1, "model": "replay-model"})
    )
    provider = ReplayProvider(base_dir=tmp_path)

    first = provider.complete(request)
    second = provider.complete(request)

    expected = LLMResponse(
        text="pong", tokens_in=3, tokens_out=1, latency_ms=0, model="replay-model"
    )
    assert first == expected
    assert second == expected


def test_replay_provider_missing_fixture_raises(tmp_path: Path) -> None:
    provider = ReplayProvider(base_dir=tmp_path)
    request = LLMRequest(system="s", user="u", max_output_tokens=8)
    with pytest.raises(ReplayNotFound):
        provider.complete(request)
