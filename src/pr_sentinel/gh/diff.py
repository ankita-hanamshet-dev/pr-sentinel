"""Parse unified diffs into Hunk objects with true post-image line numbers.

Line-number fidelity is the single most important property here: inline review
comments are worthless if an added line maps to the wrong post-image line.

Normalization contract
----------------------
The parser normalizes line endings: every line is split on ``\\n`` and any
trailing ``\\r`` is stripped, so ``Hunk.lines`` and every ``AddedLine.content``
are LF-normalized text. All downstream analysis (chunking, agents, grounding)
operates on this normalized text. **Any comparison of analysis output against
raw file bytes must normalize both sides** — otherwise a CRLF file's line
``"foo\\r"`` will never equal the parser's ``"foo"``.

The original line ending is not discarded: it is recorded as ``line_ending``
(``lf`` | ``crlf`` | ``mixed`` | ``none``) on every ``Hunk`` and on the
``FileDiff``, so downstream can restore it.

Downstream consequence (Fixer): when the Phase 5 Fixer emits a GitHub
```suggestion``` block, it MUST re-apply the file's original ``line_ending`` to
the suggested text. Emitting LF into a CRLF file makes "Apply suggestion"
introduce spurious whitespace-only changes on every touched line.
TODO(fixer): honor Hunk.line_ending when rendering suggestions in
src/pr_sentinel/agents/fixer.py (Phase 5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pr_sentinel.models import Hunk, LineEnding

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")


@dataclass
class AddedLine:
    """A single added line with its post-image (new-file) line number."""

    line_no: int
    content: str


@dataclass
class FileDiff:
    """All hunks and file-level metadata for one file in a diff."""

    old_path: str | None = None
    new_path: str | None = None
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False
    is_binary: bool = False
    line_ending: LineEnding = "none"
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        """Review path: the post-image path when present, else the old path."""
        return self.new_path or self.old_path or ""


def _strip_ab_prefix(path: str) -> str:
    """Drop a leading ``a/`` or ``b/`` and any trailing ``\\t<timestamp>``."""
    path = path.split("\t", 1)[0]
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _classify_endings(flags: list[bool]) -> LineEnding:
    """Reduce per-line CRLF flags to a single line-ending classification."""
    if not flags:
        return "none"
    if all(flags):
        return "crlf"
    if not any(flags):
        return "lf"
    return "mixed"


def _combine_endings(endings: list[LineEnding]) -> LineEnding:
    """Combine per-hunk line endings into one file-level classification."""
    seen = {e for e in endings if e != "none"}
    if not seen:
        return "none"
    if len(seen) == 1:
        return next(iter(seen))
    return "mixed"


def parse_diff(text: str) -> list[FileDiff]:
    """Parse a unified diff into a list of FileDiff objects (one per file)."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    cr_flags: dict[int, list[bool]] = {}
    # Per-hunk [new_consumed, old_consumed], so an empty body line can be recognised
    # as a blank CONTEXT line (git apply's behaviour) only while the hunk still
    # expects lines -- and ignored once the hunk is full (e.g. a trailing "" from the
    # final newline split).
    fill: dict[int, list[int]] = {}

    for physical in text.split("\n"):  # split on LF; strip \r ourselves
        had_cr = physical.endswith("\r")
        raw = physical[:-1] if had_cr else physical

        git = _GIT_HEADER.match(raw)
        if git:
            current = FileDiff(old_path=git.group(1), new_path=git.group(2))
            files.append(current)
            hunk = None
            continue

        if raw.startswith("--- ") and current is None:
            # Plain unified diff with no "diff --git" header.
            current = FileDiff()
            files.append(current)
            hunk = None

        if current is None:
            continue

        if raw.startswith("new file mode"):
            current.is_new = True
        elif raw.startswith("deleted file mode"):
            current.is_deleted = True
        elif raw.startswith("rename from "):
            current.is_rename = True
            current.old_path = raw[len("rename from ") :]
        elif raw.startswith("rename to "):
            current.is_rename = True
            current.new_path = raw[len("rename to ") :]
        elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            current.is_binary = True
        elif raw.startswith("--- "):
            value = raw[4:]
            if value == "/dev/null":
                current.old_path = None
                current.is_new = True
            else:
                current.old_path = _strip_ab_prefix(value)
        elif raw.startswith("+++ "):
            value = raw[4:]
            if value == "/dev/null":
                current.new_path = None
                current.is_deleted = True
            else:
                current.new_path = _strip_ab_prefix(value)
        else:
            header = _HUNK_HEADER.match(raw)
            if header:
                hunk = Hunk(
                    file=current.path,
                    old_start=int(header.group(1)),
                    old_len=int(header.group(2)) if header.group(2) is not None else 1,
                    new_start=int(header.group(3)),
                    new_len=int(header.group(4)) if header.group(4) is not None else 1,
                    lines=[],
                )
                current.hunks.append(hunk)
                cr_flags[id(hunk)] = []
                fill[id(hunk)] = [0, 0]
            elif hunk is not None and not raw.startswith("\\"):
                # A "\ No newline at end of file" marker is metadata, not content.
                tag = raw[:1]
                new_c, old_c = fill[id(hunk)]
                if tag == "" and (new_c < hunk.new_len or old_c < hunk.old_len):
                    # A blank context line emitted without its leading space; git apply
                    # treats a bare empty line in the hunk body as context. Normalise to
                    # a space so post-image numbering stays correct.
                    hunk.lines.append(" ")
                    cr_flags[id(hunk)].append(had_cr)
                    fill[id(hunk)] = [new_c + 1, old_c + 1]
                elif tag in (" ", "+", "-"):
                    hunk.lines.append(raw)
                    cr_flags[id(hunk)].append(had_cr)
                    fill[id(hunk)] = [
                        new_c + (tag != "-"),
                        old_c + (tag != "+"),
                    ]

    for file in files:
        for hunk in file.hunks:
            hunk.line_ending = _classify_endings(cr_flags.get(id(hunk), []))
        file.line_ending = _combine_endings([h.line_ending for h in file.hunks])
    return files


def iter_hunks(files: list[FileDiff]) -> list[Hunk]:
    """Flatten a list of FileDiff objects into their hunks, in order."""
    return [hunk for file in files for hunk in file.hunks]


def added_lines(hunk: Hunk) -> list[AddedLine]:
    """Map every added line in a hunk to its exact post-image line number."""
    result: list[AddedLine] = []
    new_line = hunk.new_start
    for raw in hunk.lines:
        tag, content = raw[:1], raw[1:]
        if tag == "+":
            result.append(AddedLine(line_no=new_line, content=content))
            new_line += 1
        elif tag == " ":
            new_line += 1
        # A "-" (removed) line is absent from the post-image; do not advance.
    return result
