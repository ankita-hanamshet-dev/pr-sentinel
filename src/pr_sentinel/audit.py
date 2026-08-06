"""Append-only audit log: one JSON line per privileged action (CLAUDE.md §Security).

Covers every comment posted, Check Run conclusion, policy refusal, tool call, budget
denial, and command authorization decision.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_AUDIT_PATH = Path(".sentinel/audit.jsonl")


@dataclass(frozen=True)
class AuditRecord:
    """One privileged-action record."""

    ts: str
    run_id: str
    actor: str
    action: str
    target: str
    decision: str
    reason: str


class AuditLog:
    """Appends AuditRecords to a JSONL file; never truncates or rewrites."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self, *, run_id: str, actor: str, action: str, target: str, decision: str, reason: str
    ) -> AuditRecord:
        """Append one record and return it."""
        entry = AuditRecord(
            ts=datetime.now(UTC).isoformat(),
            run_id=run_id,
            actor=actor,
            action=action,
            target=target,
            decision=decision,
            reason=reason,
        )
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry)) + "\n")
        return entry
