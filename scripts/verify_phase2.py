"""Independent Phase 2 verification: ground-truth via `git apply`, not the tests.

For each edge case we build a REAL git patch (git diff over real files), then
materialize the post-image with `git apply` in a clean temp dir and compare every
added line the parser reports against the materialized file, byte-for-byte.

Usage:
    uv run python scripts/verify_phase2.py           # verify only (CI mode)
    uv run python scripts/verify_phase2.py --write    # also (re)write fixtures

Exit code is non-zero if any real (non-CRLF-normalized) mismatch is found, so this
is safe to wire into the CI Validation workflow (see docs/BUILD_PLAN.md Phase 7).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pr_sentinel.core.chunking import (
    DEFAULT_PROMPT_OVERHEAD,
    budget_from_settings,
    chunk_hunks,
)
from pr_sentinel.core.language import detect_language_verbose
from pr_sentinel.gh.diff import added_lines, iter_hunks, parse_diff
from pr_sentinel.models import Hunk

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures" / "diffs"
LANGS = REPO_ROOT / "fixtures" / "langs"


def git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    if proc.returncode not in (0, 1):  # git diff exits 1 when differences exist
        raise RuntimeError(f"git {args} failed: {proc.stderr.decode()}")
    return proc.stdout.decode("utf-8", errors="surrogateescape")


def init_repo(path: Path) -> None:
    git(["init", "-q"], path)
    git(["config", "user.email", "v@x"], path)
    git(["config", "user.name", "v"], path)
    git(["config", "core.autocrlf", "false"], path)
    git(["config", "commit.gpgsign", "false"], path)


def make_patch(
    pre: dict[str, bytes],
    post: dict[str, bytes],
    rename: tuple[str, str] | None,
) -> str:
    repo = Path(tempfile.mkdtemp())
    try:
        init_repo(repo)
        for rel, data in pre.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_bytes(data)
        git(["add", "-A"], repo)
        git(["commit", "-q", "-m", "base", "--allow-empty"], repo)
        if rename is not None:
            old, new = rename
            git(["mv", old, new], repo)
            if new in post:
                (repo / new).write_bytes(post[new])
        else:
            for rel in pre:
                if rel not in post:
                    (repo / rel).unlink()
            for rel, data in post.items():
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_bytes(data)
        git(["add", "-A"], repo)
        return git(["diff", "--cached", "-M", "--unified=3"], repo)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def materialize(pre: dict[str, bytes], patch: str) -> dict[str, bytes]:
    work = Path(tempfile.mkdtemp())
    try:
        init_repo(work)
        for rel, data in pre.items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_bytes(data)
        patch_file = work / "_patch.diff"
        patch_file.write_bytes(patch.encode("utf-8", errors="surrogateescape"))
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file)],
            cwd=work,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git apply failed: {proc.stderr.decode()}")
        result: dict[str, bytes] = {}
        for fp in work.rglob("*"):
            if fp.is_file() and fp.name != "_patch.diff" and ".git" not in fp.parts:
                result[str(fp.relative_to(work))] = fp.read_bytes()
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def lines_of(data: bytes) -> list[bytes]:
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    return parts


def numbered(n: int, prefix: str) -> bytes:
    return ("".join(f"{prefix}_{i}\n" for i in range(1, n + 1))).encode()


# (name, pre, post, rename)
CASES: list[tuple[str, dict[str, bytes], dict[str, bytes], tuple[str, str] | None]] = []
_pre_multi = numbered(30, "row")
_post_multi = _pre_multi.replace(b"row_3\n", b"row_3\nINSERTED_A\n").replace(
    b"row_25\n", b"CHANGED_25\n"
)
CASES.append(("multi_hunk", {"src/util.py": _pre_multi}, {"src/util.py": _post_multi}, None))
CASES.append(
    (
        "interleaved",
        {"app/calc.py": b"ctx_a\nkill_1\nctx_b\nkill_2\nctx_c\n"},
        {"app/calc.py": b"ctx_a\nADD_1\nctx_b\nADD_2\nctx_c\n"},
        None,
    )
)
CASES.append(("new_file", {}, {"newmod.py": b'print("hi")\nprint("bye")\n'}, None))
CASES.append(("deleted_file", {"old.py": b"print('gone')\nprint('removed')\n"}, {}, None))
_rc = b"def g():\n    return 42\n"
CASES.append(
    ("rename_pure", {"oldname.py": _rc}, {"newname.py": _rc}, ("oldname.py", "newname.py"))
)
CASES.append(
    (
        "rename_modify",
        {"mod_old.py": b"a = 1\nb = 2\nc = 3\n"},
        {"mod_new.py": b"a = 1\nb = 20\nc = 3\n"},
        ("mod_old.py", "mod_new.py"),
    )
)
CASES.append(
    ("no_newline", {"tail.py": b"first\nsecond\n"}, {"tail.py": b"first\nsecond_changed"}, None)
)
CASES.append(
    (
        "crlf",
        {"win.py": b"alpha\r\nbeta\r\ngamma\r\n"},
        {"win.py": b"alpha\r\nBETA_CHANGED\r\ngamma\r\n"},
        None,
    )
)
CASES.append(("single_line", {}, {"one.py": b"only_line = True"}, None))
CASES.append(("only_deletions", {"del.py": numbered(5, "d")}, {"del.py": b"d_1\nd_4\nd_5\n"}, None))
CASES.append(("empty", {"same.py": b"unchanged\n"}, {"same.py": b"unchanged\n"}, None))
CASES.append(
    (
        "multi_file",
        {"a.py": b"alpha\n", "b.py": b"gamma\n"},
        {"a.py": b"alpha\nbeta\n", "b.py": b"gamma\ndelta\n"},
        None,
    )
)
CASES.append(
    ("binary", {}, {"logo.png": b"\x89PNG\r\n\x1a\n\x00\x10\x20\x30BIN\xff\xfe\x00end"}, None)
)

REQUIRED_EDGE_CASES = {
    "multi-hunk file": "multi_hunk",
    "new file": "new_file",
    "deleted file": "deleted_file",
    "rename": "rename_pure",
    "rename+modify": "rename_modify",
    "no-newline-at-EOF": "no_newline",
    "CRLF line endings": "crlf",
    "empty diff": "empty",
    "single-line file": "single_line",
    "file with only deletions": "only_deletions",
}


def run(write: bool) -> int:
    if write:
        FIXTURES.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("STEP 1 - Parser added-lines vs git-apply materialized post-image")
    print("=" * 78)
    print(f"{'fixture':<16} {'added':>7} {'mismatch':>9}  notes")
    print("-" * 60)

    total_mismatches = 0
    hunks_by_fixture: dict[str, list[Hunk]] = {}
    for name, pre, post, rename in CASES:
        patch = make_patch(pre, post, rename)
        if write:
            (FIXTURES / f"{name}.diff").write_text(patch, encoding="utf-8")
        parsed = parse_diff(patch)
        hunks_by_fixture[name] = iter_hunks(parsed)
        materialized = materialize(pre, patch) if any(f.hunks for f in parsed) else {}

        added = mism = 0
        notes: list[str] = []
        for fd in parsed:
            file_bytes = materialized.get(fd.path)
            for hunk in fd.hunks:
                for al in added_lines(hunk):
                    added += 1
                    post_lines = lines_of(file_bytes) if file_bytes is not None else []
                    if not 1 <= al.line_no <= len(post_lines):
                        mism += 1
                        notes.append(f"{fd.path}:{al.line_no} out-of-range")
                        continue
                    actual = post_lines[al.line_no - 1]
                    parsed_bytes = al.content.encode("utf-8", errors="surrogateescape")
                    if parsed_bytes == actual:
                        continue
                    if parsed_bytes == actual.rstrip(b"\r"):
                        notes.append(f"{fd.path}:{al.line_no} CRLF-normalized")
                        continue
                    mism += 1
                    notes.append(f"{fd.path}:{al.line_no} MISMATCH {parsed_bytes!r}!={actual!r}")
        total_mismatches += mism
        note = "; ".join(dict.fromkeys(notes)) if notes else "ok"
        print(f"{name:<16} {added:>7} {mism:>9}  {note}")

    print("-" * 60)
    print(f"TOTAL real mismatches (excluding CRLF-normalized): {total_mismatches}")

    print("\n" + "=" * 78)
    print("STEP 2 - Edge-case coverage")
    print("=" * 78)
    case_names = {name for name, *_ in CASES}
    for label, fixture in REQUIRED_EDGE_CASES.items():
        present = fixture in case_names
        print(f"  [{'x' if present else ' '}] {label:<26} -> {fixture}.diff")

    print("\n" + "=" * 78)
    print("STEP 4 - Chunker invariants over every fixture")
    print("=" * 78)
    budget = budget_from_settings()
    print(f"budget = {budget} (MAX_INPUT_TOKENS - PROMPT_OVERHEAD {DEFAULT_PROMPT_OVERHEAD})")
    counts: list[int] = []
    over_budget = split = 0
    for hunks in hunks_by_fixture.values():
        if not hunks:
            continue
        chunks = chunk_hunks(hunks, max_tokens=budget)
        if sorted(id(h) for h in hunks) != sorted(id(h) for c in chunks for h in c.hunks):
            split += 1
        for c in chunks:
            counts.append(c.token_count)
            if c.token_count > budget and len(c.hunks) > 1:
                over_budget += 1
    if counts:
        mean = sum(counts) / len(counts)
        print(f"chunks={len(counts)}  min={min(counts)}  max={max(counts)}  mean={mean:.1f}")
    print(f"over-budget multi-hunk chunks: {over_budget}")
    print(f"hunk-split violations: {split}")

    print("\n" + "=" * 78)
    print("STEP 5 - Language detection per fixture (fixtures/langs)")
    print("=" * 78)
    print(f"{'file':<16} {'language':<12} {'layer'}")
    print("-" * 44)
    for fp in sorted(LANGS.glob("*")):
        if fp.is_file():
            lang, layer = detect_language_verbose(fp.name, fp.read_text(encoding="utf-8"))
            flag = "  <- fallible" if layer in ("keyword", "keyword-ambiguous", "shebang") else ""
            print(f"{fp.name:<16} {lang:<12} {layer}{flag}")

    return 1 if total_mismatches else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent Phase 2 verification.")
    parser.add_argument("--write", action="store_true", help="(re)write fixtures/diffs from cases")
    args = parser.parse_args()
    return run(write=args.write)


if __name__ == "__main__":
    sys.exit(main())
