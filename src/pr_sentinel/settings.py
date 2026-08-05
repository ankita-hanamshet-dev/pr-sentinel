"""Central configuration for PR Sentinel, sourced from PR_SENTINEL_* env vars."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration; every knob is a PR_SENTINEL_* env var with a default."""

    model_config = SettingsConfigDict(
        env_prefix="PR_SENTINEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM provider — configurable, default Anthropic (CLAUDE.md C1).
    llm_provider: str = "anthropic"
    model: str = "claude-sonnet-5"
    llm_api_key: str | None = None
    llm_base_url: str | None = None

    # GitHub REST access — unprefixed env var, never the model key.
    github_token: str | None = Field(default=None, validation_alias="GITHUB_TOKEN")

    # Budget / token ceilings (CLAUDE.md C2, C3).
    max_input_tokens: int = 8000
    max_output_tokens: int = 4000
    max_llm_calls_per_run: int = 12
    rpm: int = 15
    rpd: int = 150
    max_concurrency: int = 5

    # Review shaping.
    max_comments: int = 25
    max_diff_lines: int = 5000
    max_file_bytes: int = 1_000_000
    confidence_floor: float = 0.55
    context_radius: int = 8

    # Caching / kill switch.
    cache_ttl_days: int = 14
    disabled: bool = False


@lru_cache
def get_settings() -> Settings:
    """Return a process-cached Settings instance loaded from the environment."""
    return Settings()
