"""Loader for prompts/*.prompt.yml (GitHub Models .prompt.yml shape).

Every prompt's `version` field feeds the Phase 3 cache key (call_llm's prompt_version) —
editing a prompt's wording without bumping `version` silently reuses stale cache entries
for the old wording.
"""

from __future__ import annotations

import importlib.resources
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_PROMPTS_ENV = "PR_SENTINEL_PROMPTS_DIR"
_CWD_PROMPTS_DIR = Path("prompts")
_PACKAGE = "pr_sentinel"
_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


class PromptError(Exception):
    """Raised on a malformed, missing, or under-substituted prompt template."""


@dataclass(frozen=True)
class PromptSpec:
    """A parsed .prompt.yml: version, temperature, and the two message templates."""

    name: str
    version: str
    model: str | None
    temperature: float
    system_template: str
    user_template: str


def render(template: str, variables: dict[str, str]) -> str:
    """Substitute every {{var}} in `template`; raises PromptError on an unresolved var."""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise PromptError(f"template variable {{{{{key}}}}} has no value supplied")
        return variables[key]

    return _VAR_PATTERN.sub(_sub, template)


def _message_content(messages: list[object], role: str, source: str) -> str:
    for message in messages:
        if isinstance(message, dict) and message.get("role") == role:
            content = message.get("content")
            if not isinstance(content, str):
                raise PromptError(f"{source}: message role={role!r} has non-string content")
            return content
    raise PromptError(f"{source}: no message with role={role!r} found")


def _read_prompt(name: str, base_dir: Path | None) -> tuple[str, str]:
    """Resolve a prompt to (source_label, text). Resolution order when base_dir is None:

    1. the PR_SENTINEL_PROMPTS_DIR env var, if set;
    2. a ./prompts/ directory in CWD (preserves the checkout-based workflows);
    3. the packaged prompts (importlib.resources) -- the uvx / wheel-install case.

    An explicit base_dir bypasses the search (used by tests and callers with their own dir).
    """
    filename = f"{name}.prompt.yml"

    if base_dir is not None:
        path = base_dir / filename
        if not path.is_file():
            raise PromptError(f"no such prompt file: {path}")
        return str(path), path.read_text(encoding="utf-8")

    env_dir = os.environ.get(_PROMPTS_ENV)
    if env_dir:
        path = Path(env_dir) / filename
        if not path.is_file():
            raise PromptError(f"{_PROMPTS_ENV}={env_dir} is set but {path} does not exist")
        return str(path), path.read_text(encoding="utf-8")

    cwd_path = _CWD_PROMPTS_DIR / filename
    if cwd_path.is_file():
        return str(cwd_path), cwd_path.read_text(encoding="utf-8")

    resource = importlib.resources.files(_PACKAGE).joinpath("prompts", filename)
    if resource.is_file():
        return f"<pkg:{_PACKAGE}/prompts/{filename}>", resource.read_text(encoding="utf-8")

    raise PromptError(
        f"prompt {name!r} not found: {_PROMPTS_ENV} unset, no ./{_CWD_PROMPTS_DIR}/{filename}, "
        f"and not packaged under {_PACKAGE}/prompts/"
    )


def load_prompt(name: str, *, base_dir: Path | None = None) -> PromptSpec:
    """Load and parse `<name>.prompt.yml` (see _read_prompt for the resolution order)."""
    source, text = _read_prompt(name, base_dir)
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise PromptError(f"{source} did not parse to a mapping")

    if "version" not in data:
        raise PromptError(f"{source} missing required key: 'version'")
    if "messages" not in data:
        raise PromptError(f"{source} missing required key: 'messages'")
    messages = data["messages"]
    if not isinstance(messages, list):
        raise PromptError(f"{source}: 'messages' must be a list")

    model_parameters = data.get("modelParameters", {})
    temperature = (
        float(model_parameters.get("temperature", 0.0))
        if isinstance(model_parameters, dict)
        else 0.0
    )

    return PromptSpec(
        name=name,
        version=str(data["version"]),
        model=data.get("model"),
        temperature=temperature,
        system_template=_message_content(messages, "system", source),
        user_template=_message_content(messages, "user", source),
    )
