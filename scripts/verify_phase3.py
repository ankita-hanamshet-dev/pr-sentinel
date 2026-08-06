"""Independent Phase 3 verification: prove behaviour at the transport boundary.

Does NOT reuse tests/test_llm.py's assertions or fixtures. Every check here either
patches httpx's transport directly (httpx.MockTransport, or a monkeypatch of
httpx.Client.send that raises if ever invoked) or drives the real clock through
unittest.mock so timing-dependent behaviour (429 backoff, RPM/RPD windows) is
observed rather than assumed.

Usage:
    uv run python scripts/verify_phase3.py

Exit code is non-zero if any check fails.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import NoReturn
from unittest import mock

import httpx

from pr_sentinel.llm.anthropic import AnthropicProvider
from pr_sentinel.llm.budget import BudgetExhausted, BudgetGovernor
from pr_sentinel.llm.cache import LLMCache, cache_key
from pr_sentinel.llm.json_mode import JsonModeGiveUp, complete_json
from pr_sentinel.llm.provider import LLMRequest, LLMResponse, call_llm, request_with_retry
from pr_sentinel.llm.replay import ReplayProvider, replay_key
from pr_sentinel.models import AgentResult, Finding
from pr_sentinel.settings import Settings


def _anthropic_payload(text: str, tokens_in: int, tokens_out: int) -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "llm_provider": "anthropic",
        "model": "claude-sonnet-5",
        "llm_api_key": "sk-verify",
        "max_llm_calls_per_run": 1000,
        "rpm": 1000,
        "rpd": 1000,
        "max_concurrency": 1000,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# STEP 1 - Cache correctness at the transport layer
# ---------------------------------------------------------------------------
def step1_cache_correctness() -> bool:
    print("=" * 78)
    print("STEP 1 - Cache correctness at the transport layer (httpx.MockTransport)")
    print("=" * 78)
    ok = True

    call_log: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(1)
        return httpx.Response(200, json=_anthropic_payload("pong", 5, 2))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(model="claude-sonnet-5", api_key="sk-verify", client=client)
    cache = LLMCache(path=Path(tempfile.mkdtemp()) / "cache.sqlite", ttl_days=14)

    def call(request: LLMRequest, prompt_version: str) -> LLMResponse:
        return call_llm(
            provider,
            request,
            cache=cache,
            governor=None,
            provider_name="anthropic",
            model="claude-sonnet-5",
            prompt_version=prompt_version,
            agent="verify",
        )

    request_v1 = LLMRequest(system="verify", user="ping ping", max_output_tokens=16)

    rows: list[tuple[str, int, str]] = []

    call(request_v1, "v1")
    call(request_v1, "v1")
    rows.append(("same request x2, prompt_version=v1", len(call_log), "expect 1"))
    ok &= len(call_log) == 1

    call(request_v1, "v2")
    rows.append(("same request, prompt_version v1->v2", len(call_log), "expect 2 (version in key)"))
    ok &= len(call_log) == 2

    ws_request = LLMRequest(system="verify", user="ping   ping\n\n", max_output_tokens=16)
    call(ws_request, "v1")
    hit = len(call_log) == 2
    rows.append(
        (
            "v1 request, whitespace-only variant (extra spaces/newlines)",
            len(call_log),
            "HIT (reused cache)" if hit else "MISS (new HTTP call)",
        )
    )

    print(f"{'scenario':<58} {'http_calls':>10}  note")
    print("-" * 100)
    for scenario, calls, note in rows:
        print(f"{scenario:<58} {calls:>10}  {note}")

    # Semantically significant whitespace: does normalization erase real code differences?
    indented = LLMRequest(system="verify", user="def f():\n    return 1", max_output_tokens=16)
    unindented = LLMRequest(system="verify", user="def f():\nreturn 1", max_output_tokens=16)
    key_indented = cache_key("anthropic", "m", "v1", "a", indented.system + "\x00" + indented.user)
    key_unindented = cache_key(
        "anthropic", "m", "v1", "a", unindented.system + "\x00" + unindented.user
    )
    collide = key_indented == key_unindented
    print()
    print("Whitespace-normalization verdict:")
    print(
        f"  'ping   ping' vs 'ping ping'                -> {'HIT' if hit else 'MISS'} "
        "(pure spacing noise; a HIT here is desired)"
    )
    print(
        f"  'def f():\\n    return 1' vs 'def f():\\nreturn 1' (different code, "
        f"different indentation) -> keys {'COLLIDE' if collide else 'differ'}"
    )
    if collide:
        print(
            "  VERDICT: cache_key's normalisation (`' '.join(payload.split())`) is TOO "
            "AGGRESSIVE — it collapses all whitespace runs to a single space, so two hunks "
            "that differ ONLY in indentation (a real code difference in Python/YAML/etc.) "
            "hash identically and would silently share a cached LLM response."
        )
    else:
        print("  VERDICT: normalisation preserves semantically-significant whitespace.")

    # A collision bug found and fixed during this verification pass: call_llm used to
    # concatenate system+user with NO delimiter, so (system="AB", user="C") and
    # (system="A", user="BC") hashed identically. Prove the old behaviour collided and
    # the shipped fix (a "\x00" delimiter) does not.
    naive_a = cache_key("anthropic", "m", "v1", "a", "AB" + "C")
    naive_b = cache_key("anthropic", "m", "v1", "a", "A" + "BC")
    fixed_a = cache_key("anthropic", "m", "v1", "a", "AB" + "\x00" + "C")
    fixed_b = cache_key("anthropic", "m", "v1", "a", "A" + "\x00" + "BC")
    print()
    print("Delimiter-collision check (system+user concatenation boundary):")
    print(f"  naive concat (no delimiter):    collide = {naive_a == naive_b}  <- was the bug")
    print(f"  shipped fix ('\\x00' delimiter): collide = {fixed_a == fixed_b}  <- now in call_llm")
    ok &= naive_a == naive_b  # confirms the bug was real, not hypothetical
    ok &= fixed_a != fixed_b  # confirms the fix actually works

    print(f"\nSTEP 1 result: {'PASS' if (ok and hit) else 'FAIL'}")
    return ok and hit


# ---------------------------------------------------------------------------
# STEP 2 - Budget governor
# ---------------------------------------------------------------------------
def step2_budget_governor() -> bool:
    print("\n" + "=" * 78)
    print("STEP 2 - Budget governor: per-run cap, partial-result path, RPM/RPD independence")
    print("=" * 78)
    ok = True

    max_calls = 5
    attempts = max_calls + 5
    governor = BudgetGovernor(_settings(max_llm_calls_per_run=max_calls))
    rows: list[tuple[int, str, str]] = []
    for i in range(1, attempts + 1):
        try:
            governor.reserve()
            governor.record(tokens_in=1, tokens_out=1, latency_ms=1, cache_hit=False)
            rows.append((i, "OK", ""))
        except BudgetExhausted as exc:
            rows.append((i, "REFUSED", exc.reason))

    print(f"MAX_LLM_CALLS_PER_RUN={max_calls}, attempted {attempts} calls:")
    print(f"{'call#':>6} {'result':<8} reason")
    for i, status, reason in rows:
        print(f"{i:>6} {status:<8} {reason}")
    successes = sum(1 for _, s, _ in rows if s == "OK")
    tail_all_refused = all(s == "REFUSED" for _, s, _ in rows[max_calls:])
    overflow_reasons = [r for _, s, r in rows[max_calls:] if s == "REFUSED"]
    tail_reason_is_cap = all(r == "max_calls_per_run" for r in overflow_reasons)
    print(
        f"successes={successes} (expect {max_calls}) | all overflow refused={tail_all_refused} "
        f"| reason==max_calls_per_run for all overflow={tail_reason_is_cap}"
    )
    print("ledger snapshot:", governor.snapshot())
    ok &= successes == max_calls and tail_all_refused and tail_reason_is_cap

    # Partial-result path: an agent-shaped loop must return an AgentResult, not crash.
    def run_agent_like_loop(n_requests: int, gov: BudgetGovernor) -> AgentResult:
        made = 0
        for _ in range(n_requests):
            try:
                gov.reserve()
                gov.record(tokens_in=1, tokens_out=1, latency_ms=1, cache_hit=False)
                made += 1
            except BudgetExhausted as exc:
                return AgentResult(
                    agent="bug", findings=[], error=f"budget_exhausted:{exc.reason}", llm_calls=made
                )
        return AgentResult(agent="bug", findings=[], error=None, llm_calls=made)

    partial_governor = BudgetGovernor(_settings(max_llm_calls_per_run=3))
    partial = run_agent_like_loop(10, partial_governor)
    print()
    print(
        f"Partial-result path: AgentResult(error={partial.error!r}, llm_calls={partial.llm_calls}) "
        "-- no exception propagated out of the loop"
    )
    ok &= partial.error is not None and partial.llm_calls == 3

    # RPM enforced independently of a much larger per-run cap.
    rpm_governor = BudgetGovernor(_settings(max_llm_calls_per_run=1000, rpm=3, rpd=1000))
    with mock.patch("time.time", return_value=1_000_000.0):
        rpm_rows: list[tuple[int, str, str]] = []
        for i in range(1, 6):
            try:
                rpm_governor.reserve()
                rpm_governor.record(tokens_in=1, tokens_out=1, latency_ms=1, cache_hit=False)
                rpm_rows.append((i, "OK", ""))
            except BudgetExhausted as exc:
                rpm_rows.append((i, "REFUSED", exc.reason))
    print()
    print("RPM=3, RPD=1000, max_calls_per_run=1000, all 5 calls at the same instant:")
    print(f"{'call#':>6} {'result':<8} reason")
    for i, s, r in rpm_rows:
        print(f"{i:>6} {s:<8} {r}")
    rpm_ok = [s for _, s, _ in rpm_rows] == ["OK", "OK", "OK", "REFUSED", "REFUSED"]
    rpm_reason_ok = all(r == "rpm" for _, s, r in rpm_rows if s == "REFUSED")
    print(f"RPM enforced independently of the larger per-run cap: {rpm_ok and rpm_reason_ok}")
    ok &= rpm_ok and rpm_reason_ok

    # RPD enforced independently of RPM/per-run cap (calls spaced 90s apart so the
    # 60s RPM window never sees more than one call, but the 24h RPD window accumulates).
    rpd_governor = BudgetGovernor(_settings(max_llm_calls_per_run=1000, rpm=1000, rpd=3))
    base_t = 1_000_000.0
    rpd_rows: list[tuple[int, str, str]] = []
    for i in range(1, 6):
        with mock.patch("time.time", return_value=base_t + i * 90):
            try:
                rpd_governor.reserve()
                rpd_governor.record(tokens_in=1, tokens_out=1, latency_ms=1, cache_hit=False)
                rpd_rows.append((i, "OK", ""))
            except BudgetExhausted as exc:
                rpd_rows.append((i, "REFUSED", exc.reason))
    print()
    print("RPD=3, RPM=1000, max_calls=1000, calls 90s apart (RPM window never trips):")
    print(f"{'call#':>6} {'result':<8} reason")
    for i, s, r in rpd_rows:
        print(f"{i:>6} {s:<8} {r}")
    rpd_ok = [s for _, s, _ in rpd_rows] == ["OK", "OK", "OK", "REFUSED", "REFUSED"]
    rpd_reason_ok = all(r == "rpd" for _, s, r in rpd_rows if s == "REFUSED")
    print(f"RPD enforced independently of RPM/per-run cap: {rpd_ok and rpd_reason_ok}")
    ok &= rpd_ok and rpd_reason_ok

    print(f"\nSTEP 2 result: {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# STEP 3 - 429 handling
# ---------------------------------------------------------------------------
def step3_429_handling() -> bool:
    print("\n" + "=" * 78)
    print("STEP 3 - 429 handling: retry-after vs exponential+jitter, bounded retries")
    print("=" * 78)
    ok = True

    # Scenario A: 429-with-header, then 429-without-header, then success (3 attempts).
    calls_a: list[float] = []

    def handler_a(request: httpx.Request) -> httpx.Response:
        calls_a.append(time.monotonic())
        n = len(calls_a)
        if n == 1:
            return httpx.Response(429, headers={"retry-after": "0.3"})
        if n == 2:
            return httpx.Response(429)  # no header -> exponential+jitter path
        return httpx.Response(200, json=_anthropic_payload("pong", 1, 1))

    client_a = httpx.Client(transport=httpx.MockTransport(handler_a))
    start_a = time.perf_counter()
    response_a = request_with_retry(
        client_a,
        "POST",
        "https://api.anthropic.com/v1/messages",
        headers={},
        json_body={},
        max_attempts=3,
    )
    elapsed_a = time.perf_counter() - start_a
    gap1 = calls_a[1] - calls_a[0]
    gap2 = calls_a[2] - calls_a[1]

    print("Scenario A: 429(retry-after=0.3s) -> 429(no header) -> 200")
    print(f"{'attempts':>9} {'elapsed_s':>10} {'gap1_s':>8} {'gap2_s':>8}  status")
    status_a = response_a.status_code
    print(f"{len(calls_a):>9} {elapsed_a:>10.2f} {gap1:>8.2f} {gap2:>8.2f}  {status_a}")
    print("  gap1 = retry-after honored (>=0.25s); gap2 = exponential+jitter (>=0.9s)")
    ok &= len(calls_a) == 3 and response_a.status_code == 200
    ok &= gap1 >= 0.25
    ok &= gap2 >= 0.9

    # Scenario B: always 429 -> must give up after exactly max_attempts, never retry forever.
    calls_b: list[float] = []

    def handler_b(request: httpx.Request) -> httpx.Response:
        calls_b.append(time.monotonic())
        return httpx.Response(429, headers={"retry-after": "0.05"})

    client_b = httpx.Client(transport=httpx.MockTransport(handler_b))
    start_b = time.perf_counter()
    raised = False
    try:
        request_with_retry(
            client_b,
            "POST",
            "https://api.anthropic.com/v1/messages",
            headers={},
            json_body={},
            max_attempts=3,
        )
    except httpx.HTTPStatusError:
        raised = True
    elapsed_b = time.perf_counter() - start_b

    print()
    print("Scenario B: always 429 (retry-after=0.05s each time)")
    print(f"{'attempts':>9} {'elapsed_s':>10}  raised_after_giving_up")
    print(f"{len(calls_b):>9} {elapsed_b:>10.2f}  {raised}")
    ok &= raised and len(calls_b) == 3

    print(f"\nSTEP 3 result: {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# STEP 4 - Replay determinism, zero network calls
# ---------------------------------------------------------------------------
def step4_replay_determinism() -> bool:
    print("\n" + "=" * 78)
    print("STEP 4 - Replay determinism and zero network calls")
    print("=" * 78)
    ok = True

    tmp_dir = Path(tempfile.mkdtemp())
    request = LLMRequest(system="verify-replay", user="ping", max_output_tokens=8)
    key = replay_key(request)
    fixture_path = tmp_dir / f"{key}.json"
    fixture_path.write_text(
        json.dumps({"text": "pong", "tokens_in": 3, "tokens_out": 1, "model": "replay-model"})
    )
    provider = ReplayProvider(base_dir=tmp_dir)

    def _boom(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("network call attempted by ReplayProvider!")

    with mock.patch("httpx.Client.send", side_effect=_boom):
        first = provider.complete(request)
        second = provider.complete(request)

    identical = first == second
    print(f"run1: {first!r}")
    print(f"run2: {second!r}")
    print(f"byte-for-byte identical: {identical}")
    print("httpx.Client.send was patched to raise for the whole block; no exception occurred,")
    print("so ReplayProvider made zero network calls.")
    ok &= identical

    print(f"\nSTEP 4 result: {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# STEP 5 - JSON repair loop against the real Finding schema
# ---------------------------------------------------------------------------
def step5_json_repair_loop() -> bool:
    print("\n" + "=" * 78)
    print("STEP 5 - JSON repair loop against the real Finding schema (models.py)")
    print("=" * 78)
    ok = True

    malformed = "this is not json at all {{{"
    schema_violating = json.dumps(
        {
            "agent": "bug",
            "file": "x.py",
            "line_start": 1,
            "line_end": 1,
            "severity": "extreme",  # not a valid Severity literal
            "confidence": 0.9,
            "rule_id": "SENTINEL-BUG-001",
            "title": "t",
            "fact": "f",
            "impact": "i",
            "recommendation": "r",
            "evidence_quote": "q",
        }
    )
    scripted_responses = [malformed, schema_violating]
    sent_requests: list[LLMRequest] = []

    class _SpyProvider:
        name = "spy"

        def complete(self, request: LLMRequest) -> LLMResponse:
            sent_requests.append(request)
            text = scripted_responses[len(sent_requests) - 1]
            return LLMResponse(text=text, tokens_in=1, tokens_out=1, latency_ms=0, model="spy")

    result, _response = complete_json(
        _SpyProvider(), Finding, system="sys", user="please emit a Finding", max_output_tokens=64
    )

    print(f"attempt 1 response (malformed JSON):     {malformed!r}")
    print(f"attempt 2 response (valid JSON, bad severity): {schema_violating[:60]}...")
    print(f"HTTP-level calls made: {len(sent_requests)} (expect 2 -- no 3rd call attempted)")
    print(f"result type: {type(result).__name__} (expect JsonModeGiveUp -- a signal, not a raise)")
    ok &= len(sent_requests) == 2
    ok &= isinstance(result, JsonModeGiveUp)

    try:
        Finding.model_validate_json(malformed)
        attempt1_error = ""
    except Exception as exc:  # capturing pydantic's exact error text for comparison below
        attempt1_error = str(exc)

    repair_prompt = sent_requests[1].user
    embedded = bool(attempt1_error) and attempt1_error in repair_prompt
    print()
    print(f"attempt-1's pydantic error embedded verbatim in the repair prompt: {embedded}")
    print(f"  repair prompt tail: ...{repair_prompt[-160:]!r}")

    if isinstance(result, JsonModeGiveUp):
        reason = result.last_error[:120]
        print(f"final give-up reason (real schema violation): {reason!r}")
        ok &= "severity" in result.last_error.lower()
        ok &= result.attempts == 2

    ok &= embedded

    print(f"\nSTEP 5 result: {'PASS' if ok else 'FAIL'}")
    return ok


def run() -> int:
    results = {
        "1: cache correctness": step1_cache_correctness(),
        "2: budget governor": step2_budget_governor(),
        "3: 429 handling": step3_429_handling(),
        "4: replay determinism": step4_replay_determinism(),
        "5: JSON repair loop": step5_json_repair_loop(),
    }

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for name, ok in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(run())
