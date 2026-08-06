"""Team-conventions mining: corpus file, BM25 top-k retrieval, empty-corpus safety."""

from __future__ import annotations

from pathlib import Path

from pytest_httpx import HTTPXMock

from pr_sentinel.gh.client import GitHubClient
from pr_sentinel.gh.history import TeamConventions


def test_from_github_writes_corpus_and_retrieves_relevant(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    comments = [
        {"body": "Use parameterized queries, never string concatenation for SQL."},
        {"body": "Prefer pathlib over os.path for filesystem work."},
        {"body": "Log with structlog, never print statements."},
    ]
    httpx_mock.add_response(method="GET", json=comments)  # matches the single comments page
    out = tmp_path / "team_conventions.md"

    conventions = TeamConventions.from_github(
        GitHubClient("tok", cache=None), "o", "r", out_path=out
    )
    assert out.exists()
    assert "Distilled from 3" in out.read_text()

    hits = conventions.search("sql injection string concatenation", k=3)
    assert hits
    assert "parameterized queries" in hits[0]


def test_skips_non_dict_and_empty_comments(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    # Five items, three valid: the empty body and the non-dict are skipped. (BM25 IDF is
    # only positive once a term is rare across >=3 documents, so keep three real comments.)
    comments = [
        {"body": ""},
        "not-a-dict",
        {"body": "real convention: prefer pathlib"},
        {"body": "log with structlog, never print"},
        {"body": "use type hints on public functions"},
    ]
    httpx_mock.add_response(method="GET", json=comments)
    out = tmp_path / "c.md"
    conventions = TeamConventions.from_github(
        GitHubClient("tok", cache=None), "o", "r", out_path=out
    )

    assert "Distilled from 3" in out.read_text()  # only the three valid comments kept
    assert "pathlib" in conventions.search("pathlib")[0]


def test_empty_corpus_search_returns_nothing() -> None:
    assert TeamConventions([]).search("anything") == []


def test_search_ignores_zero_score_matches() -> None:
    conventions = TeamConventions(["use pathlib for paths"])
    assert conventions.search("completely unrelated tokens") == []
