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

from pr_sentinel.settings import Settings

logger = structlog.get_logger()

BudgetReason = Literal["rpm", "rpd", "concurrency", "max_calls_per_run"]

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


class BudgetGovernor:
    """Enforces settings.rpm / rpd / max_concurrency / max_llm_calls_per_run."""

    def __init__(self, settings: Settings) -> None:
        self._rpm = settings.rpm
        self._rpd = settings.rpd
        self._max_concurrency = settings.max_concurrency
        self._max_calls_per_run = settings.max_llm_calls_per_run
        self._ledger = _Ledger()

    def reserve(self) -> None:
        """Claim a call slot or raise BudgetExhausted; call before the HTTP request."""
        now = time.time()
        self._evict_stale(now)

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

    def record(self, *, tokens_in: int, tokens_out: int, latency_ms: int, cache_hit: bool) -> None:
        """Mark a reserved call as finished and log its accounting."""
        if self._ledger.in_flight > 0:
            self._ledger.in_flight -= 1
        logger.info(
            "llm_call_recorded",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            calls_used=self._ledger.calls_used,
        )

    def snapshot(self) -> dict[str, int]:
        """Return current usage counters for reporting (e.g. the smoke command)."""
        now = time.time()
        return {
            "calls_used": self._ledger.calls_used,
            "calls_remaining": max(0, self._max_calls_per_run - self._ledger.calls_used),
            "rpm_used": self._count_within(now, _RPM_WINDOW_S),
            "rpd_used": self._count_within(now, _RPD_WINDOW_S),
        }

    def _evict_stale(self, now: float) -> None:
        timestamps = self._ledger.call_timestamps
        while timestamps and now - timestamps[0] > _RPD_WINDOW_S:
            timestamps.popleft()

    def _count_within(self, now: float, window_s: float) -> int:
        return sum(1 for ts in self._ledger.call_timestamps if now - ts < window_s)
