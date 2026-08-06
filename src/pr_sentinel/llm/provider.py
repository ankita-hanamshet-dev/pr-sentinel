"""LLMProvider protocol, request/response types, retry helper, registry, and call_llm.

Per CLAUDE.md C1: the provider is configurable via PR_SENTINEL_LLM_PROVIDER and no
provider name may be hardcoded outside this module — callers (agents, the CLI) only
ever see `LLMProvider`, `get_provider`, and `call_llm`.
"""

from __future__ import annotations

import importlib
import random
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

from pr_sentinel.settings import Settings

logger = structlog.get_logger()

_PROVIDER_CLASSES: dict[str, tuple[str, str]] = {
    "anthropic": ("pr_sentinel.llm.anthropic", "AnthropicProvider"),
    "github_models": ("pr_sentinel.llm.github_models", "GitHubModelsProvider"),
    "replay": ("pr_sentinel.llm.replay", "ReplayProvider"),
}


class LLMError(Exception):
    """Base error for the model-access layer."""


@dataclass(frozen=True)
class LLMRequest:
    """A single completion request: system + user turns, capped output size."""

    system: str
    user: str
    max_output_tokens: int
    temperature: float = 0.0


@dataclass(frozen=True)
class LLMResponse:
    """A completion result with usage accounting.

    `tokens_in` is the uncached input count (Anthropic's `input_tokens`); cached
    prefix tokens are reported separately so the budget governor can price them at
    the cache read/write rates rather than the full input rate.
    """

    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    model: str
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class LLMProvider(Protocol):
    """Structural interface every model adapter implements."""

    name: str

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Return a completion for `request`, raising LLMError on failure."""
        ...


class CacheLike(Protocol):
    """The subset of LLMCache that call_llm depends on."""

    def get(self, key: str) -> LLMResponse | None: ...
    def put(self, key: str, response: LLMResponse) -> None: ...


class GovernorLike(Protocol):
    """The subset of BudgetGovernor that call_llm depends on."""

    def reserve(self) -> None: ...
    def record(
        self,
        *,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        cache_hit: bool,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None: ...


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, object],
    max_attempts: int = 3,
) -> httpx.Response:
    """POST/GET with exponential-backoff-plus-jitter retry on HTTP 429.

    Honors a `Retry-After` header (seconds) when present; otherwise backs off
    `min(cap, base * 2**attempt)` seconds with jitter. Any non-429 HTTP error
    status or transport error propagates immediately.
    """
    base_delay = 1.0
    cap_delay = 20.0
    last_response: httpx.Response | None = None

    for attempt in range(max_attempts):
        try:
            response = client.request(method, url, headers=headers, json=json_body)
        except httpx.TransportError:
            if attempt == max_attempts - 1:
                raise
            time.sleep(min(cap_delay, base_delay * (2**attempt)))
            continue

        if response.status_code != 429:
            response.raise_for_status()
            return response

        last_response = response
        if attempt == max_attempts - 1:
            break

        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            delay = float(retry_after)
        else:
            delay = min(cap_delay, base_delay * (2**attempt)) * (0.5 + random.random() * 0.5)
        logger.info("llm_rate_limited", attempt=attempt, delay_s=delay, url=url)
        time.sleep(delay)

    assert last_response is not None
    last_response.raise_for_status()
    return last_response


def get_provider(settings: Settings) -> LLMProvider:
    """Instantiate the configured provider by name (CLAUDE.md C1)."""
    entry = _PROVIDER_CLASSES.get(settings.llm_provider)
    if entry is None:
        known = ", ".join(sorted(_PROVIDER_CLASSES))
        raise LLMError(f"unknown LLM provider {settings.llm_provider!r}; known providers: {known}")
    module_name, class_name = entry
    module = importlib.import_module(module_name)
    provider_class = getattr(module, class_name)
    if settings.llm_provider == "replay":
        provider: LLMProvider = provider_class()
    elif settings.llm_provider == "github_models":
        if settings.github_token is None:
            raise LLMError("PR_SENTINEL_LLM_PROVIDER=github_models requires GITHUB_TOKEN")
        provider = provider_class(model=settings.model, github_token=settings.github_token)
    else:
        if settings.llm_api_key is None:
            raise LLMError(
                f"PR_SENTINEL_LLM_PROVIDER={settings.llm_provider} requires PR_SENTINEL_LLM_API_KEY"
            )
        provider = provider_class(
            model=settings.model, api_key=settings.llm_api_key, base_url=settings.llm_base_url
        )
    return provider


def call_llm(
    provider: LLMProvider,
    request: LLMRequest,
    *,
    cache: CacheLike | None,
    governor: GovernorLike | None,
    provider_name: str,
    model: str,
    prompt_version: str,
    agent: str,
) -> LLMResponse:
    """Cache-then-budget-then-call composition every caller should use.

    A cache hit returns before the governor is ever touched (CLAUDE.md C2: this is
    what makes the daily quota survivable). Raises BudgetExhausted (from budget.py,
    duck-typed via GovernorLike) on a miss when the budget has no room.
    """
    from pr_sentinel.llm.cache import cache_key

    key = cache_key(
        provider_name, model, prompt_version, agent, request.system + "\x00" + request.user
    )

    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached

    if governor is not None:
        governor.reserve()

    start = time.monotonic()
    raw = provider.complete(request)
    latency_ms = int((time.monotonic() - start) * 1000)
    response = LLMResponse(
        text=raw.text,
        tokens_in=raw.tokens_in,
        tokens_out=raw.tokens_out,
        latency_ms=latency_ms,
        model=raw.model,
        cache_read_tokens=raw.cache_read_tokens,
        cache_write_tokens=raw.cache_write_tokens,
    )

    if governor is not None:
        governor.record(
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            latency_ms=response.latency_ms,
            cache_hit=False,
            cache_read_tokens=response.cache_read_tokens,
            cache_write_tokens=response.cache_write_tokens,
        )
    if cache is not None:
        cache.put(key, response)

    return response
