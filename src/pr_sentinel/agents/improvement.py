"""Improvement agent (CLAUDE.md roles table): modern-idiom suggestions, high risk only."""

from __future__ import annotations

from pr_sentinel.agents.base import ChunkAgent


class ImprovementAgent(ChunkAgent):
    """Stdlib over hand-rolled, comprehensions, pathlib, context managers, missing tests."""

    role = "improvement"
    temperature = 0.2
    prompt_name = "improvement"
