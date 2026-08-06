"""Schema-constrained LLM output: validate against a pydantic model, repair once.

Attempts 1 and 2 of CLAUDE.md's retry ladder live here (initial call, then a
repair re-prompt with the pydantic validation error appended). Attempt 3 —
retrying with a smaller chunk — is the caller's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from pr_sentinel.llm.provider import LLMProvider, LLMRequest, LLMResponse

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class JsonModeSuccess(Generic[T]):
    """A response that validated against `schema`, on attempt 1 or 2."""

    value: T
    attempts: int


@dataclass(frozen=True)
class JsonModeGiveUp:
    """Both attempts failed validation; the caller should retry smaller."""

    last_error: str
    attempts: int


JsonModeResult = JsonModeSuccess[T] | JsonModeGiveUp


def complete_json(
    provider: LLMProvider,
    schema: type[T],
    system: str,
    user: str,
    *,
    max_output_tokens: int,
    temperature: float = 0.0,
    cache_system: bool = False,
) -> tuple[JsonModeResult[T], LLMResponse]:
    """Call `provider`, validate the JSON output against `schema`, repair once."""
    request = LLMRequest(
        system=system,
        user=user,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        cache_system=cache_system,
    )
    response = provider.complete(request)
    try:
        value = schema.model_validate_json(response.text)
        return JsonModeSuccess(value=value, attempts=1), response
    except ValidationError as exc:
        first_error = str(exc)

    repair_user = (
        f"{user}\n\nYour previous response failed schema validation with this error:\n"
        f"{first_error}\n\nRe-emit ONLY valid JSON matching the required schema."
    )
    repair_request = LLMRequest(
        system=system,
        user=repair_user,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        cache_system=cache_system,
    )
    repair_response = provider.complete(repair_request)
    try:
        value = schema.model_validate_json(repair_response.text)
        return JsonModeSuccess(value=value, attempts=2), repair_response
    except ValidationError as exc:
        return JsonModeGiveUp(last_error=str(exc), attempts=2), repair_response
