"""The autonomy allowlist, enforced as code (CLAUDE.md: not a docstring).

A single check_action(action, target) -> Decision entrypoint. `target` is interpreted
per-action: a branch name for push-type actions, a file path for `send_to_model`, a
slash-command string for the two human-gated actions, or a stringified count for
`exceed_max_comments`. Unknown actions default-deny (fail closed).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pr_sentinel.settings import get_settings

Verdict = Literal["allow", "deny", "requires_human"]


@dataclass(frozen=True)
class Decision:
    """The outcome of a policy check: what's allowed, and why."""

    verdict: Verdict
    reason: str


_ALWAYS_ALLOWED = frozenset(
    {
        "read_pr_diff",
        "read_pr_metadata",
        "read_ci_checks",
        "call_model",
        "post_review_comment",
        "post_summary_comment",
        "update_summary_comment",
        "create_check_run",
        "update_check_run",
        "upload_artifact",
    }
)

_ALWAYS_HUMAN = frozenset({"apply_suggestion", "merge_pr", "close_pr"})

_HUMAN_GATED_COMMANDS: dict[str, str] = {
    "create_fix_branch": "/sentinel fix",
    "rerun_deep_model": "/sentinel deep",
}

_ALWAYS_DENIED = frozenset(
    {
        "push_to_head_branch",
        "force_push",
        "modify_workflow_file",
        "delete_branch",
        "submit_approve_review",
    }
)

DEFAULT_AIREVIEWIGNORE_PATH = Path(".aireviewignore")


def _load_ignore_patterns(ignore_file: Path) -> list[str]:
    if not ignore_file.exists():
        return []
    patterns: list[str] = []
    for line in ignore_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            patterns.append(stripped)
    return patterns


def _matches_ignore_pattern(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        prefix = pattern.rstrip("/")
        return path == prefix or path.startswith(f"{prefix}/") or f"/{prefix}/" in f"/{path}"
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern)


def is_ignored_path(path: str, *, ignore_file: Path = DEFAULT_AIREVIEWIGNORE_PATH) -> bool:
    """Return True if `path` matches a pattern in `ignore_file` (default: .aireviewignore)."""
    patterns = _load_ignore_patterns(ignore_file)
    return any(_matches_ignore_pattern(path, pattern) for pattern in patterns)


def check_action(action: str, target: str) -> Decision:
    """The single enforced entrypoint for every action an agent or workflow might take."""
    if action in _ALWAYS_DENIED:
        return Decision("deny", f"{action} is never permitted (CLAUDE.md autonomy boundary)")

    if action == "exceed_max_comments":
        count = int(target)
        max_comments = get_settings().max_comments
        if count > max_comments:
            return Decision("deny", f"{count} comments exceeds MAX_COMMENTS={max_comments}")
        return Decision("allow", f"{count} comments is within MAX_COMMENTS={max_comments}")

    if action == "send_to_model":
        if is_ignored_path(target):
            return Decision("deny", f"{target} matches a .aireviewignore pattern")
        return Decision("allow", f"{target} is not ignored")

    if action in _ALWAYS_HUMAN:
        return Decision("requires_human", f"{action} always requires a human to act")

    if action in _HUMAN_GATED_COMMANDS:
        required = _HUMAN_GATED_COMMANDS[action]
        if target == required:
            return Decision("requires_human", f"{action} requires the human-triggered {required}")
        return Decision("deny", f"{action} is only permitted via {required}, got {target!r}")

    if action in _ALWAYS_ALLOWED:
        return Decision("allow", f"{action} is in the always-allowed set")

    return Decision("deny", f"unknown action {action!r}; default-deny")


BANNED_PHRASES: tuple[str, ...] = (
    "you forgot",
    "you should have",
    "why didn't you",
    "good job",
    "great job",
    "nice work",
    "well done",
    "as an ai",
)


def check_comment_tone(body: str, *, author: str | None = None) -> list[str]:
    """Flag banned phrases, exclamation marks, and (if given) the PR author's name.

    CLAUDE.md: critique the code, never the author. `author` is optional because
    it isn't always known at the point a comment is drafted; pass it whenever the
    PR author's username/display name is available so a direct reference is caught
    deterministically rather than relying on prompt instructions alone.
    """
    violations: list[str] = []
    lowered = body.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            violations.append(f"banned phrase: {phrase!r}")
    if "!" in body:
        violations.append("exclamation marks are not allowed")
    if author and author.lower() in lowered:
        violations.append(f"references the PR author ({author!r}) -- critique code, not authors")
    return violations


_UNSAFE_PATH_PREFIXES = (".github/workflows/", ".git/")
_UNSAFE_PATH_SUFFIXES = (".pem", ".key")
_DEPENDENCY_MANIFESTS = frozenset(
    {
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
    }
)
_UNSAFE_CODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\beval\("),
    re.compile(r"\bexec\("),
    re.compile(r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"),
    re.compile(r"pickle\.loads\("),
)


def check_patch_safety(file_path: str, patch_text: str) -> list[str]:
    """The Fixer's unsafe-patch filter: refuse workflow/git/key/manifest paths and unsafe calls."""
    reasons: list[str] = []
    if any(file_path.startswith(prefix) for prefix in _UNSAFE_PATH_PREFIXES):
        reasons.append(f"refuses to patch {file_path}: workflow/git-internal path")
    if any(file_path.endswith(suffix) for suffix in _UNSAFE_PATH_SUFFIXES):
        reasons.append(f"refuses to patch {file_path}: key material path")
    if Path(file_path).name in _DEPENDENCY_MANIFESTS:
        reasons.append(f"refuses to patch {file_path}: dependency manifest")
    for pattern in _UNSAFE_CODE_PATTERNS:
        if pattern.search(patch_text):
            reasons.append(f"patch introduces an unsafe call matching {pattern.pattern!r}")
    return reasons
