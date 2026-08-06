"""Diff parser tests against REAL git-generated fixtures.

Fixtures under fixtures/diffs/ are produced by `git diff` over real files and
independently cross-checked with `git apply`, so these expectations are not a
shared assumption between the parser and hand-written diffs.
"""

from __future__ import annotations

from pathlib import Path

from pr_sentinel.gh.diff import added_lines, iter_hunks, parse_diff

DIFFS = Path(__file__).parent.parent / "fixtures" / "diffs"


def _load(name: str) -> str:
    # read_bytes (not read_text) so CRLF fixtures keep their \r, as a real diff
    # fetched from the GitHub API would.
    return (DIFFS / name).read_bytes().decode("utf-8")


def _added(name: str, hunk_index: int = 0) -> list[tuple[int, str]]:
    hunk = parse_diff(_load(name))[0].hunks[hunk_index]
    return [(a.line_no, a.content) for a in added_lines(hunk)]


def test_interleaved_added_lines_map_to_exact_post_image_numbers() -> None:
    # The critical fidelity case: interleaved add/delete/context in one hunk.
    assert _added("interleaved.diff") == [(2, "ADD_1"), (4, "ADD_2")]


def test_multi_hunk_line_numbers_account_for_earlier_insertions() -> None:
    files = parse_diff(_load("multi_hunk.diff"))
    hunks = files[0].hunks
    assert len(hunks) == 2
    assert [(a.line_no, a.content) for a in added_lines(hunks[0])] == [(4, "INSERTED_A")]
    # CHANGED_25 lands on post-image line 26 because INSERTED_A shifted everything down.
    assert [(a.line_no, a.content) for a in added_lines(hunks[1])] == [(26, "CHANGED_25")]


def test_new_file() -> None:
    files = parse_diff(_load("new_file.diff"))
    assert files[0].is_new is True
    assert files[0].path == "newmod.py"
    assert _added("new_file.diff") == [(1, 'print("hi")'), (2, 'print("bye")')]


def test_deleted_file_has_no_added_lines() -> None:
    files = parse_diff(_load("deleted_file.diff"))
    assert files[0].is_deleted is True
    assert files[0].path == "old.py"
    assert added_lines(files[0].hunks[0]) == []


def test_pure_rename_has_paths_and_no_hunks() -> None:
    files = parse_diff(_load("rename_pure.diff"))
    assert files[0].is_rename is True
    assert files[0].old_path == "oldname.py"
    assert files[0].new_path == "newname.py"
    assert files[0].path == "newname.py"
    assert files[0].hunks == []


def test_rename_with_modification() -> None:
    files = parse_diff(_load("rename_modify.diff"))
    assert files[0].is_rename is True
    assert files[0].new_path == "mod_new.py"
    assert _added("rename_modify.diff") == [(2, "b = 20")]


def test_no_newline_marker_is_ignored() -> None:
    hunk = parse_diff(_load("no_newline.diff"))[0].hunks[0]
    assert all(not line.startswith("\\") for line in hunk.lines)
    assert [(a.line_no, a.content) for a in added_lines(hunk)] == [(2, "second_changed")]


def test_crlf_content_is_normalized() -> None:
    # Parser splits on line boundaries and drops the trailing \r (Windows-safe).
    assert _added("crlf.diff") == [(2, "BETA_CHANGED")]


def test_single_line_new_file() -> None:
    files = parse_diff(_load("single_line.diff"))
    assert files[0].is_new is True
    assert _added("single_line.diff") == [(1, "only_line = True")]


def test_only_deletions_have_no_added_lines() -> None:
    assert _added("only_deletions.diff") == []


def test_binary_file() -> None:
    files = parse_diff(_load("binary.diff"))
    assert files[0].is_binary is True
    assert files[0].hunks == []


def test_multi_file() -> None:
    files = parse_diff(_load("multi_file.diff"))
    assert [f.path for f in files] == ["a.py", "b.py"]
    assert len(iter_hunks(files)) == 2
    assert _added("multi_file.diff", 0) == [(2, "beta")]


def test_plain_unified_without_git_header() -> None:
    files = parse_diff(_load("plain_unified.diff"))
    assert files[0].path == "plain.py"
    assert _added("plain_unified.diff") == [(2, "two")]


def test_leading_junk_and_unprefixed_paths() -> None:
    text = "junk preamble\n--- foo.py\n+++ bar.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    files = parse_diff(text)
    assert files[0].old_path == "foo.py"
    assert files[0].new_path == "bar.py"
    assert [(a.line_no, a.content) for a in added_lines(files[0].hunks[0])] == [(1, "y")]


def test_empty_diff() -> None:
    assert parse_diff(_load("empty.diff")) == []
    assert parse_diff("") == []


def test_crlf_and_lf_agree_on_line_numbers() -> None:
    lf = _load("interleaved.diff")
    crlf = lf.replace("\n", "\r\n")
    hunk = parse_diff(crlf)[0].hunks[0]
    assert [(a.line_no, a.content) for a in added_lines(hunk)] == [(2, "ADD_1"), (4, "ADD_2")]


def test_line_ending_lf() -> None:
    file = parse_diff(_load("interleaved.diff"))[0]
    assert file.hunks[0].line_ending == "lf"
    assert file.line_ending == "lf"


def test_line_ending_crlf() -> None:
    file = parse_diff(_load("crlf.diff"))[0]
    assert file.hunks[0].line_ending == "crlf"
    assert file.line_ending == "crlf"


def test_line_ending_mixed() -> None:
    text = "--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,2 @@\n one\r\n-two\n+TWO\n"
    file = parse_diff(text)[0]
    assert file.hunks[0].line_ending == "mixed"
    assert file.line_ending == "mixed"


def test_line_ending_none_without_hunks() -> None:
    assert parse_diff(_load("rename_pure.diff"))[0].line_ending == "none"
    assert parse_diff(_load("binary.diff"))[0].line_ending == "none"


def test_line_ending_none_for_empty_hunk() -> None:
    # A hunk with a header but no body lines classifies as "none".
    file = parse_diff("--- a/x.py\n+++ b/x.py\n@@ -0,0 +0,0 @@\n")[0]
    assert file.hunks[0].line_ending == "none"


def test_file_line_ending_mixed_across_hunks() -> None:
    text = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n a\n+b\n@@ -5,1 +6,2 @@\n c\r\n+d\r\n"
    file = parse_diff(text)[0]
    assert file.hunks[0].line_ending == "lf"
    assert file.hunks[1].line_ending == "crlf"
    assert file.line_ending == "mixed"
