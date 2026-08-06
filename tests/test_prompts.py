"""Tests for the prompts/*.prompt.yml loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from pr_sentinel.prompts import PromptError, load_prompt, render

REPO_PROMPTS_DIR = Path("prompts")

EXPECTED_TEMPERATURES = {
    "triage": 0.0,
    "bug": 0.1,
    "security": 0.0,
    "style": 0.1,
    "improvement": 0.2,
    "critic": 0.0,
    "fixer": 0.1,
}


@pytest.mark.parametrize("name", sorted(EXPECTED_TEMPERATURES))
def test_load_each_repo_prompt(name: str) -> None:
    spec = load_prompt(name, base_dir=REPO_PROMPTS_DIR)
    assert spec.name == name
    assert spec.version == "1"
    assert spec.temperature == EXPECTED_TEMPERATURES[name]
    assert spec.system_template.strip() != ""
    assert spec.user_template.strip() != ""


def test_render_substitutes_variables() -> None:
    assert render("hello {{name}}", {"name": "world"}) == "hello world"


def test_render_raises_on_unresolved_variable() -> None:
    with pytest.raises(PromptError):
        render("hello {{name}}", {})


def test_load_prompt_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PromptError):
        load_prompt("does_not_exist", base_dir=tmp_path)


def test_load_prompt_missing_version(tmp_path: Path) -> None:
    (tmp_path / "bad.prompt.yml").write_text(
        "messages:\n  - role: system\n    content: hi\n  - role: user\n    content: hi\n"
    )
    with pytest.raises(PromptError):
        load_prompt("bad", base_dir=tmp_path)


def test_load_prompt_missing_messages(tmp_path: Path) -> None:
    (tmp_path / "bad.prompt.yml").write_text("version: '1'\n")
    with pytest.raises(PromptError):
        load_prompt("bad", base_dir=tmp_path)


def test_load_prompt_missing_role(tmp_path: Path) -> None:
    (tmp_path / "bad.prompt.yml").write_text(
        "version: '1'\nmessages:\n  - role: system\n    content: hi\n"
    )
    with pytest.raises(PromptError):
        load_prompt("bad", base_dir=tmp_path)


def test_load_prompt_default_temperature(tmp_path: Path) -> None:
    (tmp_path / "notemp.prompt.yml").write_text(
        "version: '1'\nmessages:\n  - role: system\n    content: sys\n"
        "  - role: user\n    content: usr\n"
    )
    spec = load_prompt("notemp", base_dir=tmp_path)
    assert spec.temperature == 0.0
