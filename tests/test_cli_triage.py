"""CLI extract (heuristic, no LLM) and refine (strategy-aware) commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pr_sentinel.cli as cli
from pr_sentinel.gh.context import PRContext
from pr_sentinel.gh.diff import FileDiff
from pr_sentinel.llm.provider import LLMRequest, LLMResponse
from pr_sentinel.models import Hunk
from pr_sentinel.settings import get_settings


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=self._texts.pop(0), tokens_in=1, tokens_out=1, latency_ms=0, model="scripted"
        )


def _hunk(file: str) -> Hunk:
    return Hunk(
        file=file, old_start=1, old_len=1, new_start=1, new_len=2, lines=[" a", "+b"]
    )


def _file(path: str) -> FileDiff:
    return FileDiff(new_path=path, is_new=True, hunks=[_hunk(path)])


def _fake_context() -> PRContext:
    return PRContext(
        owner="o",
        repo="r",
        number=7,
        head_sha="abc",
        title="t",
        body="",
        author="a",
        base_ref="main",
        head_ref="feat",
        diff_text="d",
        file_diffs=[_file("app/auth/login.py"), _file("data/blob.xyz")],
        languages={"app/auth/login.py": "python", "data/blob.xyz": "unknown"},
    )


def test_extract_writes_heuristic_plan_without_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    # No provider is built at all: get_provider would raise without an LLM key.
    monkeypatch.setattr(cli, "GitHubClient", lambda token: object())
    monkeypatch.setattr(cli, "fetch_pr_context", lambda client, o, r, n: _fake_context())
    get_settings.cache_clear()
    try:
        cli.extract(repo="o/r", pr=7, out="context.json")
    finally:
        get_settings.cache_clear()

    data = json.loads(Path("context.json").read_text())
    risks = {fp["file"]: fp["risk"] for fp in data["triage"]}
    assert risks == {"app/auth/login.py": "high", "data/blob.xyz": "unknown"}
    assert data["prompt_versions"]["triage"] == "heuristic"


def test_refine_hybrid_escalates_only_unknown_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PR_SENTINEL_TRIAGE_STRATEGY", "hybrid")
    monkeypatch.setenv("PR_SENTINEL_LLM_API_KEY", "sk-test")
    # The LLM reclassifies the one unknown file as medium; the high file is untouched.
    refined = json.dumps(
        {
            "files": [
                {
                    "file": "data/blob.xyz",
                    "language": "json",
                    "risk": "medium",
                    "agents_to_run": ["bug", "security", "style"],
                    "skip_reason": None,
                }
            ],
            "llm_call_budget": 12,
        }
    )
    monkeypatch.setattr(cli, "get_provider", lambda settings: _ScriptedProvider([refined]))
    (tmp_path / "prompts").symlink_to(Path(__file__).resolve().parent.parent / "prompts")

    context = {
        "pr_number": 7,
        "head_sha": "abc",
        "model": "m",
        "diff_text": "d",
        "hunks": [_hunk("app/auth/login.py").model_dump(mode="json")],
        "changed_lines_by_file": {"app/auth/login.py": 1, "data/blob.xyz": 1},
        "languages": {"app/auth/login.py": "python", "data/blob.xyz": "unknown"},
        "triage": [
            {
                "file": "app/auth/login.py",
                "language": "python",
                "risk": "high",
                "agents_to_run": ["bug", "security", "style", "improvement"],
                "skip_reason": None,
            },
            {
                "file": "data/blob.xyz",
                "language": "unknown",
                "risk": "unknown",
                "agents_to_run": ["bug", "security", "style"],
                "skip_reason": None,
            },
        ],
        "prompt_versions": {"triage": "heuristic"},
        "max_diff_lines": 5000,
        "diff_lines": 2,
        "confidence_floor": 0.55,
    }
    Path("context.json").write_text(json.dumps(context))
    get_settings.cache_clear()
    try:
        cli.refine(context="context.json", provider="", out="")
    finally:
        get_settings.cache_clear()

    data = json.loads(Path("context.json").read_text())
    risks = {fp["file"]: fp["risk"] for fp in data["triage"]}
    assert risks["data/blob.xyz"] == "medium"  # escalated
    assert risks["app/auth/login.py"] == "high"  # untouched


def test_refine_heuristic_strategy_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PR_SENTINEL_TRIAGE_STRATEGY", "heuristic")

    def _boom(settings: object) -> object:
        raise AssertionError("heuristic strategy must not build a provider")

    monkeypatch.setattr(cli, "get_provider", _boom)
    context = {
        "triage": [
            {
                "file": "data/blob.xyz",
                "language": "unknown",
                "risk": "unknown",
                "agents_to_run": ["bug", "security", "style"],
                "skip_reason": None,
            }
        ],
    }
    Path("context.json").write_text(json.dumps(context))
    get_settings.cache_clear()
    try:
        cli.refine(context="context.json", provider="", out="")
    finally:
        get_settings.cache_clear()

    data = json.loads(Path("context.json").read_text())
    assert data["triage"][0]["risk"] == "unknown"  # unchanged, no LLM call
