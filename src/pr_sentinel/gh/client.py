"""httpx REST client for the GitHub API: ETag-aware caching, pagination, backoff.

CLAUDE.md: the bot never writes to repo content and never merges/deletes anything.
This client is deliberately narrow -- get/post/patch/paginate only. There is no
delete, no merge, no branch-management method anywhere in this class.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger()

DEFAULT_BASE_URL = "https://api.github.com"
DEFAULT_ETAG_CACHE_PATH = Path(".sentinel/http_cache.sqlite")

# GitHub query-string params: string or integer values (e.g. {"per_page": 100}).
QueryParams = dict[str, str | int]

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS http_cache (
    cache_key TEXT PRIMARY KEY,
    etag TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    body TEXT NOT NULL,
    headers TEXT NOT NULL
)
"""


class GitHubError(Exception):
    """Raised on an unrecoverable GitHub API error (4xx, or 5xx after retries)."""


class ETagCache:
    """SQLite-backed ETag cache: key -> (etag, status_code, body, headers)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    def get(self, cache_key: str) -> tuple[str, int, str, dict[str, str]] | None:
        row = self._conn.execute(
            "SELECT etag, status_code, body, headers FROM http_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        etag, status_code, body, headers_json = row
        return etag, status_code, body, json.loads(headers_json)

    def put(
        self, cache_key: str, etag: str, status_code: int, body: str, headers: dict[str, str]
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO http_cache (cache_key, etag, status_code, body, headers) "
            "VALUES (?, ?, ?, ?, ?)",
            (cache_key, etag, status_code, body, json.dumps(headers)),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


@dataclass(frozen=True)
class GitHubResponse:
    """A parsed GitHub API response."""

    status_code: int
    json_body: object
    headers: dict[str, str]
    from_cache: bool


def _parse_link_header(link_header: str | None) -> dict[str, str]:
    """Parse a GitHub `Link` header into {rel: url}."""
    links: dict[str, str] = {}
    if not link_header:
        return links
    for part in link_header.split(","):
        segments = part.split(";")
        url_part = segments[0].strip()
        if not (url_part.startswith("<") and url_part.endswith(">")):
            continue
        url = url_part[1:-1]
        for raw_seg in segments[1:]:
            seg = raw_seg.strip()
            if seg.startswith("rel="):
                links[seg[4:].strip('"')] = url
    return links


def _is_retryable(response: httpx.Response) -> bool:
    if response.status_code >= 500:
        return True
    if response.status_code in (403, 429):
        if "retry-after" in response.headers:
            return True
        if response.headers.get("x-ratelimit-remaining") == "0":
            return True
    return False


class GitHubClient:
    """A narrow, mostly-read-only REST client: get/post/patch/paginate. No
    delete, merge, or branch-management method exists on this class.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        cache: ETagCache | None = None,
        max_attempts: int = 5,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0)
        self._cache = cache
        self._max_attempts = max_attempts
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _cache_key(self, method: str, url: str, params: QueryParams | None) -> str:
        return f"{method}:{url}:{json.dumps(params or {}, sort_keys=True)}"

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: QueryParams | None = None,
        json_body: object = None,
    ) -> httpx.Response:
        base_delay, cap_delay = 1.0, 20.0
        last_response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            try:
                response = self._client.request(
                    method, url, headers=headers, params=params, json=json_body
                )
            except httpx.TransportError:
                if attempt == self._max_attempts - 1:
                    raise
                time.sleep(min(cap_delay, base_delay * (2**attempt)))
                continue

            if not _is_retryable(response):
                return response

            last_response = response
            if attempt == self._max_attempts - 1:
                break

            retry_after = response.headers.get("retry-after")
            reset_at = response.headers.get("x-ratelimit-reset")
            if retry_after is not None:
                delay = float(retry_after)
            elif reset_at is not None:
                delay = max(0.0, float(reset_at) - time.time())
            else:
                delay = min(cap_delay, base_delay * (2**attempt)) * (0.5 + random.random() * 0.5)
            logger.info(
                "github_retry", attempt=attempt, delay_s=delay, url=url, status=response.status_code
            )
            time.sleep(delay)

        assert last_response is not None
        return last_response

    def _fetch(
        self,
        method: str,
        url: str,
        *,
        params: QueryParams | None = None,
        json_body: object = None,
        cache_key: str | None = None,
    ) -> GitHubResponse:
        headers = dict(self._headers)
        cached = self._cache.get(cache_key) if (self._cache and cache_key) else None
        if cached is not None:
            headers["If-None-Match"] = cached[0]

        response = self._request_with_retry(
            method, url, headers=headers, params=params, json_body=json_body
        )

        if response.status_code == 304 and cached is not None:
            _etag, status_code, body, resp_headers = cached
            return GitHubResponse(status_code, json.loads(body), resp_headers, from_cache=True)

        if response.status_code >= 400:
            snippet = response.text[:200]
            raise GitHubError(f"{method} {url} failed: {response.status_code} {snippet}")

        if method == "GET" and cache_key is not None and self._cache is not None:
            etag = response.headers.get("etag")
            if etag is not None:
                self._cache.put(
                    cache_key, etag, response.status_code, response.text, dict(response.headers)
                )

        return GitHubResponse(
            response.status_code, response.json(), dict(response.headers), from_cache=False
        )

    def get(self, path: str, params: QueryParams | None = None) -> GitHubResponse:
        url = f"{self._base_url}{path}"
        return self._fetch("GET", url, params=params, cache_key=self._cache_key("GET", url, params))

    def get_text(self, path: str, *, accept: str) -> str:
        """GET raw text under a custom Accept (e.g. the unified-diff media type).

        Bypasses the ETag/JSON cache path -- used to fetch a PR's raw diff, which is
        text, not JSON, and which we feed straight into the verified diff parser.
        """
        headers = dict(self._headers)
        headers["Accept"] = accept
        response = self._request_with_retry("GET", f"{self._base_url}{path}", headers=headers)
        if response.status_code >= 400:
            raise GitHubError(f"GET {path} failed: {response.status_code} {response.text[:200]}")
        return response.text

    def post(self, path: str, json_body: object) -> GitHubResponse:
        return self._fetch("POST", f"{self._base_url}{path}", json_body=json_body)

    def patch(self, path: str, json_body: object) -> GitHubResponse:
        return self._fetch("PATCH", f"{self._base_url}{path}", json_body=json_body)

    def paginate(self, path: str, params: QueryParams | None = None) -> Iterator[object]:
        """Yield every item across all pages, following the `Link: rel="next"` header."""
        url: str | None = f"{self._base_url}{path}"
        current_params: QueryParams | None = {"per_page": 100, **(params or {})}
        while url is not None:
            cache_key = self._cache_key("GET", url, current_params)
            response = self._fetch("GET", url, params=current_params, cache_key=cache_key)
            if not isinstance(response.json_body, list):
                raise GitHubError(f"paginate() expected a JSON array from {url}")
            yield from response.json_body
            url = _parse_link_header(response.headers.get("link")).get("next")
            current_params = None  # the "next" URL already carries its own query string
