"""Language detection across extension, shebang, and keyword-scoring layers."""

from __future__ import annotations

from pathlib import Path

import pytest

from pr_sentinel.core.language import detect_language, detect_language_verbose

LANGS = Path(__file__).parent.parent / "fixtures" / "langs"

EXTENSION_CASES = [
    ("a.py", "python"),
    ("a.js", "javascript"),
    ("a.mjs", "javascript"),
    ("a.jsx", "javascript"),
    ("a.ts", "typescript"),
    ("a.tsx", "typescript"),
    ("a.go", "go"),
    ("A.java", "java"),
    ("a.c", "c"),
    ("a.h", "c"),
    ("a.cpp", "cpp"),
    ("a.cc", "cpp"),
    ("a.hpp", "cpp"),
    ("a.cs", "csharp"),
    ("a.rb", "ruby"),
    ("a.rs", "rust"),
    ("a.php", "php"),
    ("a.swift", "swift"),
    ("a.kt", "kotlin"),
    ("a.scala", "scala"),
    ("a.sh", "shell"),
    ("a.bash", "shell"),
    ("a.ps1", "powershell"),
    ("a.pl", "perl"),
    ("a.lua", "lua"),
    ("a.r", "r"),
    ("a.dart", "dart"),
    ("a.ex", "elixir"),
    ("a.erl", "erlang"),
    ("a.hs", "haskell"),
    ("a.clj", "clojure"),
    ("a.m", "objective-c"),
    ("a.yaml", "yaml"),
    ("a.json", "json"),
    ("a.toml", "toml"),
    ("a.html", "html"),
    ("a.css", "css"),
    ("a.sql", "sql"),
    ("a.md", "markdown"),
    ("a.tf", "terraform"),
]


@pytest.mark.parametrize(("path", "expected"), EXTENSION_CASES)
def test_extension_detection(path: str, expected: str) -> None:
    assert detect_language(path) == expected


def test_extension_wins_over_nested_path() -> None:
    assert detect_language("src/pkg/module.go") == "go"


def test_filename_detection() -> None:
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language("Makefile") == "makefile"
    assert detect_language("services/api/Dockerfile") == "dockerfile"


def test_shebang_detection() -> None:
    assert detect_language("run", "#!/usr/bin/env python3\nprint(1)\n") == "python"
    assert detect_language("run", "#!/bin/bash\necho hi\n") == "shell"
    assert detect_language("run", "#!/usr/bin/node\nconsole.log(1)\n") == "javascript"


def test_keyword_scoring() -> None:
    go_code = "package main\nfunc main() {\n\tch := make(chan int)\n\tdefer close(ch)\n}\n"
    assert detect_language("mystery", go_code) == "go"
    py_code = "import os\ndef main():\n    self_val = None\n    print(os)\n"
    assert detect_language("mystery", py_code) == "python"


def test_shebang_without_known_interpreter_falls_through() -> None:
    assert detect_language("script", "#!/usr/bin/env awk\nBEGIN {}\n") == "unknown"


# (fixture filename, expected language, expected resolving layer)
FIXTURE_CASES = [
    ("deploy", "shell", "shebang"),
    ("pyscript", "python", "keyword"),
    ("goservice", "go", "keyword"),
    ("Dockerfile", "dockerfile", "filename"),
    ("Makefile", "makefile", "filename"),
    ("matrix.h", "cpp", "keyword-ambiguous"),
]


@pytest.mark.parametrize(("fixture", "language", "layer"), FIXTURE_CASES)
def test_language_fixtures(fixture: str, language: str, layer: str) -> None:
    content = (LANGS / fixture).read_text(encoding="utf-8")
    detected, resolved_layer = detect_language_verbose(fixture, content)
    assert detected == language
    assert resolved_layer == layer


def test_ambiguous_extension_without_content_uses_default() -> None:
    lang, layer = detect_language_verbose("header.h")
    assert lang == "c"
    assert layer == "extension-default"


def test_ambiguous_extension_with_undecidable_content_uses_default() -> None:
    lang, layer = detect_language_verbose("header.h", "int x = 1;\n")
    assert lang == "c"
    assert layer == "extension-default"


def test_unknown_when_nothing_matches() -> None:
    assert detect_language("file.xyzzy") == "unknown"
    assert detect_language("file.xyzzy", "!!! ??? %%%\n") == "unknown"
    assert detect_language("noext") == "unknown"
