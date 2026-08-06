"""Bug agent (CLAUDE.md roles table): correctness defects in high/medium-risk hunks."""

from __future__ import annotations

from pr_sentinel.agents.base import ChunkAgent


class BugAgent(ChunkAgent):
    """Null derefs, off-by-ones, resource leaks, races, wrong/swallowed error handling."""

    role = "bug"
    temperature = 0.1
    prompt_name = "bug"
