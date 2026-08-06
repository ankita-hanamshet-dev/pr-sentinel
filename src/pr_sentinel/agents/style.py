"""Style agent (CLAUDE.md roles table): convention review, deferring to team history."""

from __future__ import annotations

from collections.abc import Sequence

from pr_sentinel.agents.base import ChunkAgent, ChunkAgentOutcome
from pr_sentinel.agents.tools import search_team_conventions
from pr_sentinel.core.chunking import Chunk

STYLE_GUIDES: dict[str, str] = {
    "python": "PEP 8",
    "go": "Effective Go",
    "java": "Google Java Style",
    "javascript": "ESLint-Airbnb",
    "typescript": "ESLint-Airbnb",
    "rust": "Rust API Guidelines",
}
DEFAULT_STYLE_GUIDE = "general community conventions for the language"


class StyleAgent(ChunkAgent):
    """Bare excepts, magic numbers, dead code, naming, complexity, missing docstrings."""

    role = "style"
    temperature = 0.1
    prompt_name = "style"

    def review(  # type: ignore[override]  # deliberately widens with extra optional kwargs
        self,
        chunk: Chunk,
        *,
        language: str,
        conventions_corpus: Sequence[str] = (),
        changed_file_paths: frozenset[str] = frozenset(),
        **_ignored: object,
    ) -> ChunkAgentOutcome:
        query = f"{language} {chunk.file}"
        conventions = search_team_conventions(
            self.tool_context(changed_file_paths), query, conventions_corpus
        )
        template_vars = {
            "file": chunk.file,
            "language": language,
            "style_guide": STYLE_GUIDES.get(language, DEFAULT_STYLE_GUIDE),
            "team_conventions": "\n".join(conventions) if conventions else "(none found)",
        }
        return self.run_on_chunk(chunk, template_vars)
