"""Independent Phase 5 red-team verification, focused on the grounding filter.

Does NOT reuse tests/*.py's assertions. One real, exploitable flaw was found and
fixed in src/pr_sentinel/core/grounding.py while building STEP 1 below: evidence_quote
and line_start/line_end were checked independently against the whole hunk, so a
finding could cite real text from one line while claiming it belonged to a
different line. Fixed by cross-validating evidence against the SPECIFIC claimed
line range. That fix then caught a genuine off-by-one in this repo's own
fixtures/synthetic_prs/py_sqli fixture (see the project notes for detail).

Usage:
    uv run python scripts/verify_phase5.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from rapidfuzz import fuzz

from pr_sentinel.agents.tools import (
    ToolContext,
    ToolMisuseError,
    get_ci_result,
    get_file_context,
    lookup_rule,
    search_team_conventions,
)
from pr_sentinel.audit import AuditLog
from pr_sentinel.core.dedup import dedup_findings
from pr_sentinel.core.grounding import ground_findings
from pr_sentinel.core.scoring import SPECIALIST_AGENTS, file_score, pr_score
from pr_sentinel.gh.diff import parse_diff
from pr_sentinel.guardrails.policy import check_comment_tone
from pr_sentinel.llm.anthropic import AnthropicProvider
from pr_sentinel.llm.cache import LLMCache
from pr_sentinel.llm.provider import LLMRequest, call_llm
from pr_sentinel.models import Finding, Hunk
from pr_sentinel.prompts import load_prompt

PY_SQLI_DIR = Path("fixtures/synthetic_prs/py_sqli")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# The real vulnerable line from fixtures/synthetic_prs/py_sqli/diff.patch, line 12.
QUERY_LINE = 'query = "SELECT * FROM users WHERE email LIKE \'%" + email_fragment + "%\'"'


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "agent": "security",
        "file": "app/db.py",
        "line_start": 12,
        "line_end": 12,
        "severity": "critical",
        "confidence": 0.9,
        "rule_id": "CWE-89",
        "title": "Parameterize the concatenated SQL query",
        "fact": "x",
        "assumption": None,
        "impact": "x",
        "recommendation": "x",
        "evidence_quote": QUERY_LINE,
    }
    base.update(overrides)
    return Finding.model_validate(base)


def _real_hunk() -> Hunk:
    diff_text = (PY_SQLI_DIR / "diff.patch").read_text(encoding="utf-8")
    return parse_diff(diff_text)[0].hunks[0]


# ---------------------------------------------------------------------------
# STEP 1 - Grounding, adversarial
# ---------------------------------------------------------------------------
def step1_grounding_adversarial() -> bool:
    print("=" * 100)
    print("STEP 1 - Grounding, adversarial: hand-construct Findings that SHOULD be dropped")
    print("=" * 100)
    hunk = _real_hunk()  # real diff: line 12 = vulnerable query build, line 13 = execute
    ok = True

    cases: list[tuple[str, Finding, bool]] = [
        (
            "evidence_quote appears nowhere in the diff",
            _finding(evidence_quote="this string was never in the diff at all"),
            False,
        ),
        (
            "evidence_quote real, but on an UNCHANGED (different) line than claimed",
            _finding(line_start=17, line_end=17, evidence_quote=QUERY_LINE),
            False,
        ),
        (
            "line_start outside the changed range (way beyond the hunk)",
            _finding(line_start=500, line_end=500),
            False,
        ),
        (
            "line numbers point at a different file entirely",
            _finding(file="app/unrelated.py"),
            False,
        ),
        (
            "rule_id not in any known taxonomy",
            _finding(rule_id="TOTALLY-MADE-UP-999"),
            False,
        ),
        (
            "evidence_quote differs only in whitespace (should SURVIVE -- normalized)",
            _finding(evidence_quote=QUERY_LINE.replace(" ", "   ")),
            True,
        ),
    ]

    print(f"{'case':<68} {'expected':>9} {'actual':>7}  pass")
    print("-" * 100)
    for label, finding, expected_kept in cases:
        kept, rejects = ground_findings([finding], [hunk])
        actual_kept = len(kept) == 1
        passed = actual_kept == expected_kept
        ok = ok and passed
        note = "KEPT" if actual_kept else f"DROPPED ({rejects[0].reason})"
        status = "PASS" if passed else "FAIL"
        print(f"{label:<68} {str(expected_kept):>9} {str(actual_kept):>7}  {status}")
        print(f"    -> {note}")

    print(f"\nSTEP 1 result: {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# STEP 2 - Grounding, false rejections
# ---------------------------------------------------------------------------
def step2_grounding_false_rejections() -> bool:
    print("\n" + "=" * 100)
    print("STEP 2 - Grounding, false rejections: genuine findings must never be dropped")
    print("=" * 100)
    hunk = _real_hunk()

    # The actual finding a replayed run of this fixture produces (see STEP 8), plus
    # a batch of additional plausible-genuine findings spanning every rule_id
    # taxonomy and both added/context lines, to get a meaningful sample size.
    genuine: list[Finding] = [
        _finding(),  # the real security finding, line 12
        _finding(
            agent="security",
            rule_id="OWASP-A03:2021",
            line_start=13,
            line_end=13,
            evidence_quote="cursor.execute(query)",
            title="Executes an unparameterized query",
        ),
        _finding(
            agent="improvement",
            rule_id="SENTINEL-IMPROVEMENT-001",
            line_start=11,
            line_end=11,
            evidence_quote="cursor = conn.cursor()",
            title="Use a context manager for the cursor",
        ),
        _finding(
            agent="style",
            rule_id="SENTINEL-STYLE-001",
            line_start=10,
            line_end=10,
            evidence_quote=(
                "def search_users_by_email(conn: sqlite3.Connection, email_fragment: str) "
                "-> list[sqlite3.Row]:"
            ),
            title="Add a docstring to this public function",
        ),
        _finding(
            agent="style",
            rule_id="PEP8-E501",
            line_start=10,
            line_end=10,
            evidence_quote="email_fragment: str",
            title="Line exceeds the configured length limit",
        ),
        _finding(
            agent="bug",
            rule_id="SENTINEL-BUG-014",
            line_start=17,
            line_end=17,
            evidence_quote="def list_active_sessions(conn: sqlite3.Connection)",
            title="Consider a return type alias shared with the sibling function",
        ),
        _finding(
            agent="security",
            rule_id="CWE-89",
            line_start=12,
            line_end=13,
            evidence_quote=f"{QUERY_LINE}\n    cursor.execute(query)",
            title="SQL built via concatenation then executed",
        ),
    ]

    kept, rejects = ground_findings(genuine, [hunk])
    reject_rate = len(rejects) / len(genuine)

    print(f"sample size: {len(genuine)}")
    print(f"{'#':>2} {'rule_id':<16} {'line':>5}  kept?")
    print("-" * 60)
    for i, f in enumerate(genuine, 1):
        was_kept = f in kept
        print(f"{i:>2} {f.rule_id:<16} {f.line_start:>5}  {'yes' if was_kept else 'NO'}")

    print(f"\nreject rate: {len(rejects)}/{len(genuine)} = {reject_rate:.0%}")
    if rejects:
        print("reject reasons:")
        for r in rejects:
            print(f"  {r.finding_id}: {r.reason}")

    ok = len(rejects) == 0
    print(f"\nSTEP 2 result: {'PASS' if ok else 'FAIL'} (pass = zero false rejections)")
    return ok


# ---------------------------------------------------------------------------
# STEP 3 - Scoring
# ---------------------------------------------------------------------------
def step3_scoring() -> bool:
    print("\n" + "=" * 100)
    print("STEP 3 - Scoring formula, and the all-agents-failed -> 0 case")
    print("=" * 100)
    ok = True

    cases = [
        (["critical"], 100 - 20),
        (["high"], 100 - 10),
        (["medium"], 100 - 5),
        (["low"], 100 - 1),
        (["critical", "high", "medium", "low"], 100 - (20 + 10 + 5 + 1)),
        (["critical"] * 10, 0),  # floored at 0, not negative
    ]
    print(f"{'severities':<45} {'expected':>9} {'actual':>7}  pass")
    print("-" * 80)
    for severities, expected in cases:
        findings = [_finding(severity=s, rule_id=f"CWE-{89 + i}") for i, s in enumerate(severities)]
        actual = file_score(findings)
        passed = actual == expected
        ok = ok and passed
        print(f"{str(severities):<45} {expected:>9} {actual:>7.0f}  {'PASS' if passed else 'FAIL'}")

    print()
    all_failed = {"bug": "x", "security": "x", "style": "x", "improvement": "x"}
    score, _per_file = pr_score([_finding(severity="low")], {"app/db.py": 10}, all_failed)
    banner_would_show = SPECIALIST_AGENTS <= set(all_failed)
    print(f"all 4 specialist agents errored -> score = {score} (expect 0.0, NOT 100.0)")
    print(f"failure banner condition true: {banner_would_show}")
    ok = ok and score == 0.0 and banner_would_show

    partial_failed = {"bug": "x"}
    score2, _ = pr_score([], {"app/db.py": 10}, partial_failed)
    print(f"only 1/4 agents errored -> score = {score2} (expect 100.0 -- not falsely zeroed)")
    ok = ok and score2 == 100.0

    print(f"\nSTEP 3 result: {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# STEP 4 - Dedup
# ---------------------------------------------------------------------------
def step4_dedup() -> bool:
    print("\n" + "=" * 100)
    print("STEP 4 - Dedup: near-duplicates at ~70%, ~76%, ~95% similarity")
    print("=" * 100)
    ok = True

    variants = [
        ("~70% (below threshold)", "Untrusted input flows into a raw database query string", False),
        (
            "~76% (just above threshold)",
            "Query string built via concatenation, not parameters",
            True,
        ),
        ("~95% (well above)", "Parameterize the concatenated SQL statement", True),
    ]

    print(f"{'variant':<32} {'measured_sim':>12} {'expect_merge':>12} {'actual_merge':>12}  pass")
    print("-" * 100)
    for label, other_title, expect_merge in variants:
        a = _finding(agent="security", severity="high", confidence=0.7)
        b = _finding(
            agent="bug", severity="critical", confidence=0.9, title=other_title, rule_id="CWE-89"
        )
        sim = fuzz.token_sort_ratio(
            f"{a.title} {a.evidence_quote}", f"{b.title} {b.evidence_quote}"
        )
        merged, corroboration = dedup_findings([a, b])
        actual_merge = len(merged) == 1
        passed = actual_merge == expect_merge
        ok = ok and passed
        status = "PASS" if passed else "FAIL"
        print(
            f"{label:<32} {sim:>11.1f}% {str(expect_merge):>12} {str(actual_merge):>12}  {status}"
        )
        if actual_merge:
            winner = merged[0]
            higher_severity_won = winner.severity == "critical"
            both_corroborate = corroboration[winner.id] == ["bug", "security"]
            ok = ok and higher_severity_won and both_corroborate
            agents = corroboration[winner.id]
            print(
                f"    winner_severity={winner.severity}  higher_severity_wins={higher_severity_won}"
            )
            print(f"    corroborating_agents={agents}  both_recorded={both_corroborate}")

    print(f"\nSTEP 4 result: {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# STEP 5 - Prompt versioning feeds the cache key
# ---------------------------------------------------------------------------
def step5_prompt_versioning() -> bool:
    print("\n" + "=" * 100)
    print("STEP 5 - Prompt versioning: a version bump must miss the cache")
    print("=" * 100)
    ok = True

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        real_bug_yml = Path("prompts/bug.prompt.yml").read_text(encoding="utf-8")
        prompt_copy = tmp_path / "bug.prompt.yml"
        prompt_copy.write_text(real_bug_yml)

        spec_v1 = load_prompt("bug", base_dir=tmp_path)
        print(f"loaded version: {spec_v1.version!r}")

        call_count = 0

        def handler(sent: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        provider = AnthropicProvider(model="claude-sonnet-5", api_key="sk-verify", client=client)
        cache = LLMCache(path=tmp_path / "cache.sqlite", ttl_days=14)
        request = LLMRequest(
            system="sys", user="identical text, never changes", max_output_tokens=16
        )

        call_llm(
            provider,
            request,
            cache=cache,
            governor=None,
            provider_name="anthropic",
            model="claude-sonnet-5",
            prompt_version=spec_v1.version,
            agent="bug",
        )
        call_llm(
            provider,
            request,
            cache=cache,
            governor=None,
            provider_name="anthropic",
            model="claude-sonnet-5",
            prompt_version=spec_v1.version,
            agent="bug",
        )
        print(
            f"same version, called twice -> HTTP calls = {call_count} (expect 1, cache hit on 2nd)"
        )
        ok = ok and call_count == 1

        # Now bump the version field in the copy -- request text is otherwise identical.
        bumped = real_bug_yml.replace('version: "1"', 'version: "2"')
        assert bumped != real_bug_yml, (
            "version replacement did not match -- check prompt file format"
        )
        prompt_copy.write_text(bumped)
        spec_v2 = load_prompt("bug", base_dir=tmp_path)
        print(f"bumped version: {spec_v2.version!r}")

        call_llm(
            provider,
            request,
            cache=cache,
            governor=None,
            provider_name="anthropic",
            model="claude-sonnet-5",
            prompt_version=spec_v2.version,
            agent="bug",
        )
        print(
            f"version bumped, identical request -> HTTP calls = {call_count} (expect 2, cache MISS)"
        )
        ok = ok and call_count == 2

    print(f"\nSTEP 5 result: {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# STEP 6 - Tool allowlist
# ---------------------------------------------------------------------------
def step6_tool_allowlist() -> bool:
    print("\n" + "=" * 100)
    print("STEP 6 - Tool allowlist: out-of-scope arguments across all four tools")
    print("=" * 100)
    ok = True

    with tempfile.TemporaryDirectory() as tmp:
        audit_path = Path(tmp) / "audit.jsonl"
        audit = AuditLog(audit_path)

        def ctx(agent: str) -> ToolContext:
            return ToolContext(
                agent=agent,
                changed_file_paths=frozenset({"app/db.py"}),
                run_id="verify",
                audit=audit,
            )

        attempts: list[tuple[str, str, bool]] = []  # (label, expect, is_refusal)

        try:
            get_file_context(ctx("bug"), "evil/outside_pr.py", line=1, radius=5, file_lines=None)
            attempts.append(("get_file_context: path not in PR", "refused", False))
        except ToolMisuseError:
            attempts.append(("get_file_context: path not in PR", "refused", True))

        try:
            get_file_context(ctx("bug"), "app/db.py", line=1, radius=41, file_lines=None)
            attempts.append(("get_file_context: radius=41 (>40)", "refused", False))
        except ToolMisuseError:
            attempts.append(("get_file_context: radius=41 (>40)", "refused", True))

        try:
            search_team_conventions(ctx("bug"), "query", ["doc"])  # bug not in {style, improvement}
            attempts.append(("search_team_conventions: called by 'bug'", "refused", False))
        except ToolMisuseError:
            attempts.append(("search_team_conventions: called by 'bug'", "refused", True))

        try:
            get_ci_result(ctx("security"), "test_x", {})  # security not in {bug}
            attempts.append(("get_ci_result: called by 'security'", "refused", False))
        except ToolMisuseError:
            attempts.append(("get_ci_result: called by 'security'", "refused", True))

        # lookup_rule is allowlisted for every agent; a URL/network-shaped rule_id is
        # not a scope violation for it, but it must never attempt any network call
        # and must degrade to None instead of crashing.
        network_shaped = "http://169.254.169.254/latest/meta-data/../../CWE-89"

        def _boom(*args: object, **kwargs: object) -> httpx.Response:
            raise RuntimeError("lookup_rule attempted a network call!")

        import unittest.mock as mock

        with mock.patch("httpx.Client.send", side_effect=_boom):
            result = lookup_rule(ctx("bug"), network_shaped)
        network_safe = result is None
        print(
            f"lookup_rule('{network_shaped}') -> {result!r}  "
            f"(no network call attempted, degrades to None: {network_safe})"
        )
        ok = ok and network_safe

        print(f"\n{'attempt':<45} {'expected':>10} {'actual':>10}  pass")
        print("-" * 100)
        for label, expected, was_refused in attempts:
            actual = "refused" if was_refused else "ALLOWED"
            passed = was_refused == (expected == "refused")
            ok = ok and passed
            print(f"{label:<45} {expected:>10} {actual:>10}  {'PASS' if passed else 'FAIL'}")

        audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
        print(
            f"\naudit records written: {len(audit_lines)} (expect {len(attempts)}, one per refusal)"
        )
        ok = ok and len(audit_lines) == len(attempts)
        for line in audit_lines:
            record = json.loads(line)
            action, actor, decision = record["action"], record["actor"], record["decision"]
            print(f"  action={action:<28} actor={actor:<10} decision={decision}")

    print(f"\nSTEP 6 result: {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# STEP 7 - Tone filter
# ---------------------------------------------------------------------------
def step7_tone_filter() -> bool:
    print("\n" + "=" * 100)
    print("STEP 7 - Tone post-filter: banned phrases, author references, exclamation marks")
    print("=" * 100)
    ok = True

    cases: list[tuple[str, str, str | None, bool]] = [
        ("banned phrase", "Good job fixing this bug", None, True),
        ("banned phrase (you forgot)", "You forgot to close the connection", None, True),
        ("exclamation mark", "This will crash in production!", None, True),
        ("author reference", "alice's change introduces a race condition", "alice", True),
        ("author reference, different case", "Alice broke the build here", "alice", True),
        ("clean, no author given", "The query is not parameterized", None, False),
        ("clean, author given but not mentioned", "The query is not parameterized", "alice", False),
    ]

    print(f"{'case':<38} {'body':<45} {'expect_flag':>11} {'actual_flag':>11}  pass")
    print("-" * 120)
    for label, body, author, expect_flag in cases:
        violations = check_comment_tone(body, author=author) if author else check_comment_tone(body)
        actual_flag = len(violations) > 0
        passed = actual_flag == expect_flag
        ok = ok and passed
        status = "PASS" if passed else "FAIL"
        print(
            f"{label:<38} {body[:43]:<45} {str(expect_flag):>11} {str(actual_flag):>11}  {status}"
        )
        if violations:
            print(f"    -> {violations}")

    print(f"\nSTEP 7 result: {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# STEP 8 - End to end on replay
# ---------------------------------------------------------------------------
def step8_end_to_end() -> bool:
    print("\n" + "=" * 100)
    print("STEP 8 - End to end: uv run pr-sentinel local --path fixtures/synthetic_prs/py_sqli/")
    print("=" * 100)
    result = subprocess.run(
        ["uv", "run", "pr-sentinel", "local", "--path", str(PY_SQLI_DIR), "--provider", "replay"],
        capture_output=True,
        text=True,
        check=False,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
    ok = result.returncode == 0 and "grounding_rejects=0" in result.stdout
    print(f"STEP 8 result: {'PASS' if ok else 'FAIL'}")
    return ok


def run() -> int:
    results = {
        "1: grounding adversarial": step1_grounding_adversarial(),
        "2: grounding false rejections": step2_grounding_false_rejections(),
        "3: scoring": step3_scoring(),
        "4: dedup": step4_dedup(),
        "5: prompt versioning / cache": step5_prompt_versioning(),
        "6: tool allowlist": step6_tool_allowlist(),
        "7: tone filter": step7_tone_filter(),
        "8: end to end": step8_end_to_end(),
    }
    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    for name, ok in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(run())
