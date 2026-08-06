# Replay fixtures

Used by `ReplayProvider` (`src/pr_sentinel/llm/replay.py`) when
`PR_SENTINEL_LLM_PROVIDER=replay`, so tests and CI never make a real LLM call.

## Naming

Each fixture is named `<key>.json`, where `key = sha256(request.system + "\x00"
+ request.user)[:16]` (see `replay_key()`). The key depends only on the prompt
content, not on agent name or prompt version, so every provider adapter shares
one `complete(request) -> LLMResponse` shape.

## Format

```json
{
  "text": "the completion text",
  "tokens_in": 123,
  "tokens_out": 45,
  "model": "claude-sonnet-5"
}
```

## Recording

Phase 8's `pr-sentinel eval --suite golden --record` mode writes live provider
responses into this directory using the same naming scheme, so recording never
collides with hand-authored fixtures.
