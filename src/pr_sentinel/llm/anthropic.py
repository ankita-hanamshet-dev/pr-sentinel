"""Default LLM provider (CLAUDE.md C1): Anthropic Messages API.

PR_SENTINEL_LLM_BASE_URL may point this at any endpoint that speaks the same
Messages-API wire format (e.g. an Azure/TCS front-end).
"""

from __future__ import annotations

import httpx

from pr_sentinel.llm.provider import LLMError, LLMRequest, LLMResponse, request_with_retry

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider:
    """Calls POST {base_url}/v1/messages with x-api-key auth."""

    name = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._client = client or httpx.Client(timeout=60.0)

    def complete(self, request: LLMRequest) -> LLMResponse:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        body: dict[str, object] = {
            "model": self._model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
        }
        # `temperature` is rejected outright (400 "deprecated for this model") by
        # current-generation models such as claude-sonnet-5 — confirmed against
        # the live API. Deliberately never sent; request.temperature is unused here.
        response = request_with_retry(
            self._client, "POST", f"{self._base_url}/v1/messages", headers=headers, json_body=body
        )
        payload = response.json()
        try:
            content_blocks = payload["content"]
            text = "".join(block["text"] for block in content_blocks if block.get("type") == "text")
            usage = payload["usage"]
            tokens_in = int(usage["input_tokens"])
            tokens_out = int(usage["output_tokens"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected Anthropic response shape: {exc}") from exc

        return LLMResponse(
            text=text, tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=0, model=self._model
        )
