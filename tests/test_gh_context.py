"""Context assembly: PR metadata, parsed diff, CI results, file classification."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from pr_sentinel.gh.client import GitHubClient
from pr_sentinel.gh.context import fetch_pr_context

BASE = "https://api.github.com"
HEAD_SHA = "abc123sha"
DIFF = (
    "diff --git a/app/db.py b/app/db.py\n"
    "index 111..222 100644\n"
    "--- a/app/db.py\n"
    "+++ b/app/db.py\n"
    "@@ -1,2 +1,3 @@\n"
    " import os\n"
    "+import sys\n"
    " x = 1\n"
)


def test_fetch_pr_context_assembles_metadata_diff_and_ci(httpx_mock: HTTPXMock) -> None:
    meta = {
        "title": "Add sys import",
        "body": "why",
        "head": {"sha": HEAD_SHA, "ref": "feature"},
        "base": {"ref": "main"},
        "user": {"login": "octocat"},
    }
    httpx_mock.add_response(method="GET", url=f"{BASE}/repos/o/r/pulls/7", json=meta)
    httpx_mock.add_response(method="GET", url=f"{BASE}/repos/o/r/pulls/7", text=DIFF)
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/repos/o/r/commits/{HEAD_SHA}/check-runs",
        json={"check_runs": [{"name": "CI Validation", "conclusion": "success"}]},
    )

    ctx = fetch_pr_context(GitHubClient("tok", cache=None), "o", "r", 7)

    assert ctx.head_sha == HEAD_SHA
    assert ctx.author == "octocat"
    assert ctx.base_ref == "main"
    assert [f.path for f in ctx.file_diffs] == ["app/db.py"]
    assert ctx.languages["app/db.py"] == "python"
    assert ctx.ci_results[0].name == "CI Validation"
    assert ctx.ci_results[0].conclusion == "success"


def test_context_reviewable_and_changed_lines(httpx_mock: HTTPXMock) -> None:
    meta = {"head": {"sha": HEAD_SHA}, "base": {}, "user": {}}
    httpx_mock.add_response(method="GET", url=f"{BASE}/repos/o/r/pulls/7", json=meta)
    httpx_mock.add_response(method="GET", url=f"{BASE}/repos/o/r/pulls/7", text=DIFF)
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/repos/o/r/commits/{HEAD_SHA}/check-runs", json={"check_runs": []}
    )

    ctx = fetch_pr_context(GitHubClient("tok", cache=None), "o", "r", 7)
    assert [f.path for f in ctx.reviewable_files()] == ["app/db.py"]
    assert ctx.changed_lines_by_file() == {"app/db.py": 1}
    assert "app/db.py" in ctx.files_summary()
    assert ctx.ci_summary() == "(no CI check runs found)"


def test_non_dict_metadata_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/repos/o/r/pulls/7", json=["not", "a", "dict"]
    )
    with pytest.raises(TypeError):
        fetch_pr_context(GitHubClient("tok", cache=None), "o", "r", 7)


def test_ci_summary_lists_results(httpx_mock: HTTPXMock) -> None:
    meta = {"head": {"sha": HEAD_SHA}, "base": {}, "user": {}}
    httpx_mock.add_response(method="GET", url=f"{BASE}/repos/o/r/pulls/7", json=meta)
    httpx_mock.add_response(method="GET", url=f"{BASE}/repos/o/r/pulls/7", text=DIFF)
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/repos/o/r/commits/{HEAD_SHA}/check-runs",
        json={"check_runs": [{"name": "lint", "conclusion": None}, "ignored-non-dict"]},
    )
    ctx = fetch_pr_context(GitHubClient("tok", cache=None), "o", "r", 7)
    assert ctx.ci_summary() == "lint: pending"
