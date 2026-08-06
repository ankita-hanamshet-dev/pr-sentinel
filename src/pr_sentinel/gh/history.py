"""Mine a repo's past review comments into a team-conventions corpus with BM25.

CLAUDE.md Knowledge layer: distil the last 90 days of review comments into
.sentinel/team_conventions.md, retrieve the top-3 relevant past comments per query
with in-process BM25 (rank_bm25) -- no vector DB. Retrieval provenance is logged.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from rank_bm25 import BM25Okapi

from pr_sentinel.gh.client import GitHubClient

logger = structlog.get_logger()

DEFAULT_CONVENTIONS_PATH = Path(".sentinel/team_conventions.md")
_TOKEN = re.compile(r"[a-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _write_conventions(path: Path, comments: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Team review conventions",
        "",
        f"Distilled from {len(comments)} past review comments.",
        "",
    ]
    for comment in comments:
        first = comment.strip().splitlines()[0] if comment.strip() else ""
        lines.append(f"- {first}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TeamConventions:
    """In-process BM25 over past review comments; top-k retrieval for prompts."""

    def __init__(self, comments: list[str]) -> None:
        self._comments = comments
        self._tokenized = [_tokenize(c) for c in comments]
        # BM25Okapi rejects an empty corpus; guard so a fresh repo is not an error.
        self._bm25 = BM25Okapi(self._tokenized) if self._tokenized else None

    @classmethod
    def from_github(
        cls,
        client: GitHubClient,
        owner: str,
        repo: str,
        *,
        since_days: int = 90,
        out_path: Path = DEFAULT_CONVENTIONS_PATH,
    ) -> TeamConventions:
        """Fetch <=since_days of review comments, write the corpus file, return the index."""
        since = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
        comments: list[str] = []
        for item in client.paginate(
            f"/repos/{owner}/{repo}/pulls/comments", params={"since": since}
        ):
            if isinstance(item, dict):
                body = str(item.get("body", "")).strip()
                if body:
                    comments.append(body)
        _write_conventions(out_path, comments)
        logger.info("team_conventions_built", count=len(comments), path=str(out_path))
        return cls(comments)

    def search(self, query: str, k: int = 3) -> list[str]:
        """Return up to k past comments most relevant to `query` (BM25 score > 0)."""
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(range(len(scores)), key=lambda i: float(scores[i]), reverse=True)
        chosen = [i for i in ranked if float(scores[i]) > 0.0][:k]
        logger.info(
            "team_conventions_retrieval",
            query=query[:80],
            hits=[{"idx": i, "score": round(float(scores[i]), 3)} for i in chosen],
        )
        return [self._comments[i] for i in chosen]
