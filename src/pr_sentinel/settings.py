"""Central configuration for PR Sentinel, sourced from PR_SENTINEL_* env vars."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

TriageStrategy = Literal["heuristic", "llm", "hybrid"]


class ModelPricing(BaseModel):
    """Per-model USD rates per 1M tokens (STANDARD published pricing, not promo).

    Cache rates are Anthropic's multipliers on the input rate: read ~=0.1x, write
    (5-min TTL) ~=1.25x. Used by the budget governor to turn the token ledger into a
    real dollar hard stop; documentation and demo material must quote these standard
    rates, since Sonnet's intro pricing expires 2026-08-31.
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


# STANDARD list prices per 1M tokens (Claude API, cached 2026-06-24). Intentionally NOT
# the promotional Sonnet rate ($2/$10), which lapses 2026-08-31.
DEFAULT_MODEL_PRICES: dict[str, ModelPricing] = {
    "claude-sonnet-5": ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "claude-opus-5": ModelPricing(
        input_per_mtok=5.00,
        output_per_mtok=25.00,
        cache_read_per_mtok=0.50,
        cache_write_per_mtok=6.25,
    ),
    "claude-haiku-4-5": ModelPricing(
        input_per_mtok=1.00,
        output_per_mtok=5.00,
        cache_read_per_mtok=0.10,
        cache_write_per_mtok=1.25,
    ),
}


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
    # Primary hard stop is now dollars, computed from the token ledger at the rates
    # below (CLAUDE.md C2). Calls remain a secondary cap; the retired GitHub Models
    # rpm/rpd knobs are kept for that legacy provider but are no longer the governor.
    max_usd_per_run: float = 1.00
    model_prices: dict[str, ModelPricing] = Field(
        default_factory=lambda: dict(DEFAULT_MODEL_PRICES)
    )
    max_llm_calls_per_run: int = 12
    rpm: int = 15
    rpd: int = 150
    max_concurrency: int = 5

    # Triage strategy (config flag; TriagePlan schema is identical either way):
    #   heuristic - no model calls; uncertain files get risk="unknown".
    #   llm       - always run the LLM triage agent.
    #   hybrid    - heuristic first, LLM refines ONLY the "unknown" files (default).
    triage_strategy: TriageStrategy = "hybrid"

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
