"""
Application settings loaded from environment variables.
All configuration is centralized here for easy management.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()


def _parse_list(raw: str, sep: str = ",") -> List[str]:
    """Parse a comma-separated env string into a list of stripped strings."""
    return [item.strip() for item in raw.split(sep) if item.strip()]


def _parse_aliases(raw: str) -> Dict[str, str]:
    """Parse 'alias:model,alias2:model2' into a dict."""
    aliases: Dict[str, str] = {}
    for pair in _parse_list(raw):
        if ":" in pair:
            alias, model = pair.split(":", 1)
            aliases[alias.strip().lower()] = model.strip()
    return aliases


def _parse_optional_int(raw: str) -> Optional[int]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class Settings:
    """Immutable application settings loaded once at startup."""

    # Discord
    discord_token: str = field(default_factory=lambda: os.getenv("DISCORD_TOKEN", ""))
    application_id: Optional[int] = field(
        default_factory=lambda: _parse_optional_int(os.getenv("DISCORD_APPLICATION_ID", ""))
    )

    # MegaLLM
    megallm_api_key: str = field(
        default_factory=lambda: os.getenv("MEGALLM_API_KEY", "")
    )
    megallm_base_url: str = field(
        default_factory=lambda: os.getenv(
            "MEGALLM_BASE_URL", "https://ai.megallm.io/v1"
        )
    )

    # Models
    available_models: List[str] = field(
        default_factory=lambda: _parse_list(
            os.getenv(
                "AVAILABLE_MODELS",
                "gpt-4,gpt-3.5-turbo,claude-3-opus,claude-3-sonnet",
            )
        )
    )
    default_model: str = field(
        default_factory=lambda: os.getenv("DEFAULT_MODEL", "gpt-4")
    )
    model_aliases: Dict[str, str] = field(
        default_factory=lambda: _parse_aliases(
            os.getenv("MODEL_ALIASES", "gpt4:gpt-4,claude:claude-3-opus")
        )
    )

    # Bot
    owner_ids: List[int] = field(
        default_factory=lambda: [
            int(i) for i in _parse_list(os.getenv("OWNER_IDS", ""))
            if i.isdigit()
        ]
    )
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # Rate limiting
    rate_limit_per_user: int = field(
        default_factory=lambda: int(os.getenv("RATE_LIMIT_PER_USER", "10"))
    )
    rate_limit_global: int = field(
        default_factory=lambda: int(os.getenv("RATE_LIMIT_GLOBAL", "200"))
    )
    daily_token_limit_user: int = field(
        default_factory=lambda: int(os.getenv("DAILY_TOKEN_LIMIT_USER", "50000"))
    )
    daily_token_limit_server: int = field(
        default_factory=lambda: int(os.getenv("DAILY_TOKEN_LIMIT_SERVER", "500000"))
    )

    # File uploads
    max_file_size_mb: int = field(
        default_factory=lambda: int(os.getenv("MAX_FILE_SIZE_MB", "10"))
    )
    allowed_file_types: List[str] = field(
        default_factory=lambda: _parse_list(
            os.getenv("ALLOWED_FILE_TYPES", ".txt,.pdf,.docx,.md")
        )
    )

    # Railway
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8080")))

    # ── Helpers ──────────────────────────────────────────────────────
    def resolve_model(self, name: str) -> str:
        """Resolve a model alias or name to its canonical form."""
        lower = name.strip().lower()
        resolved = self.model_aliases.get(lower, name.strip())
        if resolved in self.available_models:
            return resolved
        return self.default_model

    def validate(self) -> List[str]:
        """Return a list of validation errors (empty = OK)."""
        errors: List[str] = []
        if not self.discord_token:
            errors.append("DISCORD_TOKEN is required")
        if not self.megallm_api_key:
            errors.append("MEGALLM_API_KEY is required")
        if not self.available_models:
            errors.append("AVAILABLE_MODELS must contain at least one model")
        return errors
