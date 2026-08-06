"""GitHubClient: ETag 304 caching, Link pagination, raw-text GET, retry on 5xx."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from pr_sentinel.gh.client import ETagCache, GitHubClient, GitHubError

BASE = "https://api.github.com"


def test_etag_304_serves_cached_body(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    url = f"{BASE}/repos/o/r/pulls/1"
    httpx_mock.add_response(method="GET", url=url, json={"n": 1}, headers={"etag": 'W/"abc"'})
    httpx_mock.add_response(method="GET", url=url, status_code=304)
    client = GitHubClient("tok", cache=ETagCache(tmp_path / "c.sqlite"))

    first = client.get("/repos/o/r/pulls/1")
    assert first.from_cache is False
    second = client.get("/repos/o/r/pulls/1")
    assert second.from_cache is True
    assert second.json_body == {"n": 1}

    # The conditional request carried the stored ETag.
    assert httpx_mock.get_requests()[1].headers["If-None-Match"] == 'W/"abc"'


def test_paginate_follows_link_next(httpx_mock: HTTPXMock) -> None:
    page1 = f"{BASE}/repos/o/r/items?per_page=100"
    page2 = f"{BASE}/repos/o/r/items?page=2"
    httpx_mock.add_response(
        method="GET", url=page1, json=[1, 2], headers={"link": f'<{page2}>; rel="next"'}
    )
    httpx_mock.add_response(method="GET", url=page2, json=[3])
    client = GitHubClient("tok", cache=None)

    assert list(client.paginate("/repos/o/r/items")) == [1, 2, 3]


def test_get_text_uses_custom_accept(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=f"{BASE}/repos/o/r/pulls/1", text="raw diff body")
    client = GitHubClient("tok", cache=None)

    text = client.get_text("/repos/o/r/pulls/1", accept="application/vnd.github.diff")
    assert text == "raw diff body"
    assert httpx_mock.get_requests()[0].headers["Accept"] == "application/vnd.github.diff"


def test_retry_on_503_then_success(httpx_mock: HTTPXMock) -> None:
    url = f"{BASE}/repos/o/r/x"
    # retry-after: 0 keeps the test instant while still exercising the retry path.
    httpx_mock.add_response(method="GET", url=url, status_code=503, headers={"retry-after": "0"})
    httpx_mock.add_response(method="GET", url=url, json={"ok": True})
    client = GitHubClient("tok", cache=None)

    assert client.get("/repos/o/r/x").json_body == {"ok": True}
    assert len(httpx_mock.get_requests()) == 2


def test_paginate_rejects_non_list(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/repos/o/r/items?per_page=100", json={"not": "a list"}
    )
    client = GitHubClient("tok", cache=None)
    with pytest.raises(GitHubError):
        list(client.paginate("/repos/o/r/items"))


def test_get_raises_on_404(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/repos/o/r/missing", status_code=404, text="no"
    )
    client = GitHubClient("tok", cache=None)
    with pytest.raises(GitHubError):
        client.get("/repos/o/r/missing")


def test_retry_exhausts_then_raises(httpx_mock: HTTPXMock) -> None:
    url = f"{BASE}/repos/o/r/x"
    for _ in range(5):  # max_attempts default is 5; all fail -> give up -> raise
        httpx_mock.add_response(
            method="GET", url=url, status_code=503, headers={"retry-after": "0"}
        )
    client = GitHubClient("tok", cache=None)
    with pytest.raises(GitHubError):
        client.get("/repos/o/r/x")


def test_retry_honors_ratelimit_reset(httpx_mock: HTTPXMock) -> None:
    url = f"{BASE}/repos/o/r/x"
    # 403 with remaining=0 is retryable; reset in the past -> zero delay, keeps test fast.
    httpx_mock.add_response(
        method="GET",
        url=url,
        status_code=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"},
    )
    httpx_mock.add_response(method="GET", url=url, json={"ok": True})
    client = GitHubClient("tok", cache=None)
    assert client.get("/repos/o/r/x").json_body == {"ok": True}
