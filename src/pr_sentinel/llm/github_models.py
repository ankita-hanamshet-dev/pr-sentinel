"""LEGACY adapter (CLAUDE.md C1): GitHub Models, retired 2026-07-30.

Kept for reference only — not the default and not required. Uses the
OpenAI-compatible chat-completions shape that models.github.ai served.
"""

from __future__ import annotations

import httpx

from pr_sentinel.llm.provider import LLMError, LLMRequest, LLMResponse, request_with_retry

_ENDPOINT = "https://models.github.ai/inference/chat/completions"


class GitHubModelsProvider:
    """Calls POST {endpoint} with Bearer GITHUB_TOKEN auth."""

    name = "github_models"

    def __init__(self, model: str, github_token: str, client: httpx.Client | None = None) -> None:
        self._model = model
        self._github_token = github_token
        self._client = client or httpx.Client(timeout=60.0)

    def complete(self, request: LLMRequest) -> LLMResponse:
        headers = {
            "Authorization": f"Bearer {self._github_token}",
            "content-type": "application/json",
        }
        body: dict[str, object] = {
            "model": self._model,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        response = request_with_retry(
            self._client, "POST", _ENDPOINT, headers=headers, json_body=body
        )
        payload = response.json()
        try:
            text = payload["choices"][0]["message"]["content"]
            usage = payload["usage"]
            tokens_in = int(usage["prompt_tokens"])
            tokens_out = int(usage["completion_tokens"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected GitHub Models response shape: {exc}") from exc

        return LLMResponse(
            text=text, tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=0, model=self._model
        )
