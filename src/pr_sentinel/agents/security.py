"""Security agent (CLAUDE.md roles table): OWASP/CWE review of ALL changed hunks.

Never skipped -- enforced in agents/triage.py, not here (this class just reviews
whatever chunk it's handed).
"""

from __future__ import annotations

from pr_sentinel.agents.base import ChunkAgent


class SecurityAgent(ChunkAgent):
    """Injection, insecure deserialization, path traversal, SSRF, secrets, weak crypto."""

    role = "security"
    temperature = 0.0
    prompt_name = "security"
