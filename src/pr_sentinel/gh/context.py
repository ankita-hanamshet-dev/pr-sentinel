"""Assemble the read-only context a review needs: PR metadata, the parsed diff,
CI check-run results, and per-file language/classification.

Everything here is a GET. This module never writes to the repo (CLAUDE.md C6).
The diff is fetched with the `application/vnd.github.diff` media type and parsed by
the verified parser in gh/diff.py, rather than reconstructed from the files API.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pr_sentinel.core.language import detect_language
from pr_sentinel.gh.client import GitHubClient
from pr_sentinel.gh.diff import FileDiff, added_lines, parse_diff
from pr_sentinel.guardrails.policy import is_ignored_path
from pr_sentinel.models import Hunk

DIFF_MEDIA_TYPE = "application/vnd.github.diff"


@dataclass(frozen=True)
class CheckResult:
    """One CI check run: its name and conclusion (or None if still running)."""

    name: str
    conclusion: str | None


@dataclass
class PRContext:
    """The read-only inputs to a review of one pull request."""

    owner: str
    repo: str
    number: int
    head_sha: str
    title: str
    body: str
    author: str
    base_ref: str
    head_ref: str
    diff_text: str
    file_diffs: list[FileDiff] = field(default_factory=list)
    languages: dict[str, str] = field(default_factory=dict)
    ci_results: list[CheckResult] = field(default_factory=list)

    def reviewable_files(self) -> list[FileDiff]:
        """Files a specialist should see: not binary, has hunks, not .aireviewignore'd."""
        return [
            f
            for f in self.file_diffs
            if f.hunks and not f.is_binary and not is_ignored_path(f.path)
        ]

    def hunks_by_file(self) -> dict[str, list[Hunk]]:
        return {f.path: f.hunks for f in self.reviewable_files()}

    def changed_lines_by_file(self) -> dict[str, int]:
        return {
            f.path: sum(len(added_lines(h)) for h in f.hunks) for f in self.reviewable_files()
        }

    def ci_summary(self) -> str:
        """A compact text block of CI results, for the Triage/Bug prompts."""
        if not self.ci_results:
            return "(no CI check runs found)"
        return "\n".join(f"{c.name}: {c.conclusion or 'pending'}" for c in self.ci_results)

    def files_summary(self) -> str:
        lines = [
            f"{f.path} ({self.languages.get(f.path, 'unknown')}, {len(f.hunks)} hunks, "
            f"{sum(len(added_lines(h)) for h in f.hunks)} added lines)"
            for f in self.reviewable_files()
        ]
        return "\n".join(lines) or "(no reviewable files)"


def _post_image_snippet(hunks: list[Hunk]) -> str:
    lines: list[str] = []
    for hunk in hunks:
        lines.extend(raw[1:] for raw in hunk.lines if raw[:1] in (" ", "+"))
    return "\n".join(lines)


def fetch_pr_context(client: GitHubClient, owner: str, repo: str, number: int) -> PRContext:
    """Fetch and assemble everything a review needs for one PR. All reads, no writes."""
    meta = client.get(f"/repos/{owner}/{repo}/pulls/{number}").json_body
    if not isinstance(meta, dict):
        raise TypeError("unexpected PR metadata shape from GitHub")
    head = meta.get("head") or {}
    base = meta.get("base") or {}
    user = meta.get("user") or {}
    head_sha = str(head.get("sha", ""))

    diff_text = client.get_text(f"/repos/{owner}/{repo}/pulls/{number}", accept=DIFF_MEDIA_TYPE)
    file_diffs = parse_diff(diff_text)
    languages = {
        f.path: detect_language(f.path, _post_image_snippet(f.hunks) or None) for f in file_diffs
    }

    ci_results: list[CheckResult] = []
    if head_sha:
        check_body = client.get(
            f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs"
        ).json_body
        if isinstance(check_body, dict):
            for run in check_body.get("check_runs", []):
                if isinstance(run, dict):
                    ci_results.append(
                        CheckResult(name=str(run.get("name", "")), conclusion=run.get("conclusion"))
                    )

    return PRContext(
        owner=owner,
        repo=repo,
        number=number,
        head_sha=head_sha,
        title=str(meta.get("title", "")),
        body=str(meta.get("body") or ""),
        author=str(user.get("login", "")),
        base_ref=str(base.get("ref", "")),
        head_ref=str(head.get("ref", "")),
        diff_text=diff_text,
        file_diffs=file_diffs,
        languages=languages,
        ci_results=ci_results,
    )
