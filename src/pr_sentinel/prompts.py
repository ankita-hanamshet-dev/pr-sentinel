"""Loader for prompts/*.prompt.yml (GitHub Models .prompt.yml shape).

Every prompt's `version` field feeds the Phase 3 cache key (call_llm's prompt_version) —
editing a prompt's wording without bumping `version` silently reuses stale cache entries
for the old wording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PROMPTS_DIR = Path("prompts")
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


def _message_content(messages: list[object], role: str, path: Path) -> str:
    for message in messages:
        if isinstance(message, dict) and message.get("role") == role:
            content = message.get("content")
            if not isinstance(content, str):
                raise PromptError(f"{path}: message role={role!r} has non-string content")
            return content
    raise PromptError(f"{path}: no message with role={role!r} found")


def load_prompt(name: str, *, base_dir: Path = DEFAULT_PROMPTS_DIR) -> PromptSpec:
    """Load and parse `<base_dir>/<name>.prompt.yml`."""
    path = base_dir / f"{name}.prompt.yml"
    if not path.exists():
        raise PromptError(f"no such prompt file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PromptError(f"{path} did not parse to a mapping")

    if "version" not in data:
        raise PromptError(f"{path} missing required key: 'version'")
    if "messages" not in data:
        raise PromptError(f"{path} missing required key: 'messages'")
    messages = data["messages"]
    if not isinstance(messages, list):
        raise PromptError(f"{path}: 'messages' must be a list")

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
        system_template=_message_content(messages, "system", path),
        user_template=_message_content(messages, "user", path),
    )
