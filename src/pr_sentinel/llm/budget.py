"""Hard LLM call budget governor (CLAUDE.md C2): RPM, RPD, concurrency, per-run cap.

In-memory and per-process only — CLAUDE.md's Storage section lists no persisted
budget ledger, so this does not survive across separate GitHub Actions jobs.
Raises BudgetExhausted before any HTTP call is attempted; callers treat that as a
partial-result path, never a crash.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

import structlog

from pr_sentinel.settings import ModelPricing, Settings

logger = structlog.get_logger()

BudgetReason = Literal["rpm", "rpd", "concurrency", "max_calls_per_run", "max_usd_per_run"]

_RPM_WINDOW_S = 60.0
_RPD_WINDOW_S = 86400.0


class BudgetExhausted(Exception):
    """Raised by BudgetGovernor.reserve() when no room remains under any limit."""

    def __init__(self, reason: BudgetReason) -> None:
        self.reason = reason
        super().__init__(f"LLM budget exhausted: {reason}")


@dataclass
class _Ledger:
    calls_used: int = 0
    call_timestamps: deque[float] = field(default_factory=deque)
    in_flight: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_used: float = 0.0
    cache_hits: int = 0  # calls that read a cached prefix
    cache_writes: int = 0  # calls that wrote a cache entry


class BudgetGovernor:
    """Hard dollar stop (primary) plus calls/rpm/rpd/concurrency caps (secondary).

    The dollar cap (CLAUDE.md C2) is the real quota now that GitHub Models' free
    request quota is gone: reserve() refuses the next call once accumulated spend
    reaches settings.max_usd_per_run, and record() accrues cost from the token
    ledger at the current model's published rates.
    """

    def __init__(self, settings: Settings) -> None:
        self._rpm = settings.rpm
        self._rpd = settings.rpd
        self._max_concurrency = settings.max_concurrency
        self._max_calls_per_run = settings.max_llm_calls_per_run
        self._max_usd = settings.max_usd_per_run
        self._pricing: ModelPricing | None = settings.model_prices.get(settings.model)
        if self._pricing is None:
            logger.warning(
                "budget_no_pricing_for_model",
                model=settings.model,
                detail="dollar cap disabled for this model; calls cap still applies",
            )
        self._ledger = _Ledger()

    def reserve(self) -> None:
        """Claim a call slot or raise BudgetExhausted; call before the HTTP request."""
        now = time.time()
        self._evict_stale(now)

        if self._pricing is not None and self._ledger.cost_used >= self._max_usd:
            raise BudgetExhausted("max_usd_per_run")
        if self._ledger.calls_used >= self._max_calls_per_run:
            raise BudgetExhausted("max_calls_per_run")
        if self._ledger.in_flight >= self._max_concurrency:
            raise BudgetExhausted("concurrency")
        if self._count_within(now, _RPM_WINDOW_S) >= self._rpm:
            raise BudgetExhausted("rpm")
        if self._count_within(now, _RPD_WINDOW_S) >= self._rpd:
            raise BudgetExhausted("rpd")

        self._ledger.calls_used += 1
        self._ledger.call_timestamps.append(now)
        self._ledger.in_flight += 1

    def record(
        self,
        *,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        cache_hit: bool,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Mark a reserved call as finished, accrue its USD cost, and log accounting."""
        if self._ledger.in_flight > 0:
            self._ledger.in_flight -= 1
        self._ledger.tokens_in += tokens_in
        self._ledger.tokens_out += tokens_out
        if cache_read_tokens > 0:
            self._ledger.cache_hits += 1
        if cache_write_tokens > 0:
            self._ledger.cache_writes += 1
        cost = self._cost(tokens_in, tokens_out, cache_read_tokens, cache_write_tokens)
        self._ledger.cost_used += cost
        logger.info(
            "llm_call_recorded",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            calls_used=self._ledger.calls_used,
            call_cost_usd=round(cost, 6),
            cost_used_usd=round(self._ledger.cost_used, 6),
        )

    def _cost(
        self, tokens_in: int, tokens_out: int, cache_read_tokens: int, cache_write_tokens: int
    ) -> float:
        """USD for one call from the token split at the model's published rates."""
        p = self._pricing
        if p is None:
            return 0.0
        return (
            tokens_in * p.input_per_mtok
            + tokens_out * p.output_per_mtok
            + cache_read_tokens * p.cache_read_per_mtok
            + cache_write_tokens * p.cache_write_per_mtok
        ) / 1_000_000

    @property
    def cost_used(self) -> float:
        """Accumulated USD spend on this run (0.0 when the model has no pricing)."""
        return self._ledger.cost_used

    def cost_snapshot(self) -> dict[str, float]:
        """USD accounting for the run summary (item 5): spent, remaining, tokens."""
        return {
            "cost_used_usd": round(self._ledger.cost_used, 6),
            "cost_remaining_usd": round(max(0.0, self._max_usd - self._ledger.cost_used), 6),
            "max_usd_per_run": self._max_usd,
            "tokens_in": float(self._ledger.tokens_in),
            "tokens_out": float(self._ledger.tokens_out),
        }

    def snapshot(self) -> dict[str, int]:
        """Return current call counters for reporting (e.g. the smoke command)."""
        now = time.time()
        return {
            "calls_used": self._ledger.calls_used,
            "calls_remaining": max(0, self._max_calls_per_run - self._ledger.calls_used),
            "rpm_used": self._count_within(now, _RPM_WINDOW_S),
            "rpd_used": self._count_within(now, _RPD_WINDOW_S),
            "tokens_in": self._ledger.tokens_in,
            "tokens_out": self._ledger.tokens_out,
            "cache_hits": self._ledger.cache_hits,
            "cache_writes": self._ledger.cache_writes,
        }

    def _evict_stale(self, now: float) -> None:
        timestamps = self._ledger.call_timestamps
        while timestamps and now - timestamps[0] > _RPD_WINDOW_S:
            timestamps.popleft()

    def _count_within(self, now: float, window_s: float) -> int:
        return sum(1 for ts in self._ledger.call_timestamps if now - ts < window_s)
