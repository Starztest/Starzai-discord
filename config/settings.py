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

    # ── LLM Provider API Keys ─────────────────────────────────────
    # At least ONE provider key is required for the bot to function.

    # MegaLLM (legacy — still supported as a provider)
    megallm_api_key: str = field(
        default_factory=lambda: os.getenv("MEGALLM_API_KEY", "")
    )
    megallm_base_url: str = field(
        default_factory=lambda: os.getenv(
            "MEGALLM_BASE_URL", "https://ai.megallm.io/v1"
        )
    )

    # Requesty.ai — unified LLM gateway, 400+ models
    requesty_api_key: str = field(
        default_factory=lambda: os.getenv("REQUESTY_API_KEY", "")
    )
    requesty_base_url: str = field(
        default_factory=lambda: os.getenv(
            "REQUESTY_BASE_URL", "https://router.requesty.ai/v1"
        )
    )

    # Featherless AI — open-source models
    featherless_api_key: str = field(
        default_factory=lambda: os.getenv("FEATHERLESS_API_KEY", "")
    )
    featherless_base_url: str = field(
        default_factory=lambda: os.getenv(
            "FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1"
        )
    )

    # ModelsLab — LLM + image generation
    modelslab_api_key: str = field(
        default_factory=lambda: os.getenv("MODELSLAB_API_KEY", "")
    )
    modelslab_base_url: str = field(
        default_factory=lambda: os.getenv(
            "MODELSLAB_BASE_URL", "https://modelslab.com/api/uncensored-chat/v1"
        )
    )

    # Chutes.ai — decentralized serverless AI
    chutes_api_key: str = field(
        default_factory=lambda: os.getenv("CHUTES_API_KEY", "")
    )
    chutes_base_url: str = field(
        default_factory=lambda: os.getenv(
            "CHUTES_BASE_URL", "https://llm.chutes.ai/v1"
        )
    )

    # Puter — free AI API (user-pays model, OpenAI-compatible)
    puter_auth_token: str = field(
        default_factory=lambda: os.getenv("PUTER_AUTH_TOKEN", "")
    )
    puter_base_url: str = field(
        default_factory=lambda: os.getenv(
            "PUTER_BASE_URL", "https://api.puter.com/puterai/openai/v1"
        )
    )

    # ── Provider Priority & Routing ───────────────────────────────
    # Comma-separated provider names in order of preference.
    # Only providers with valid API keys will be used.
    # Available: requesty, featherless, modelslab, chutes, puter, megallm
    llm_provider_priority: List[str] = field(
        default_factory=lambda: _parse_list(
            os.getenv(
                "LLM_PROVIDER_PRIORITY",
                "requesty,chutes,featherless,modelslab,puter,megallm",
            )
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

    # Database (Supabase PostgreSQL — use Session Pooler URL for IPv4 compat)
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "")
    )
    # Supabase region — used for auto-converting direct connection strings
    # to Session Pooler format.  Only needed if you use a direct DB URL;
    # ignored when you provide a pooler URL directly.
    supabase_region: str = field(
        default_factory=lambda: os.getenv("SUPABASE_REGION", "us-east-1")
    )

    # Music API mirrors (comma-separated JioSaavn mirrors)
    music_api_urls: List[str] = field(
        default_factory=lambda: _parse_list(
            os.getenv(
                "MUSIC_API_URLS",
                "https://jiosaavn-api2.vercel.app,"
                "https://jiosaavn-api-privatecvc2.vercel.app,"
                "https://saavn.dev/api,"
                "https://jiosaavn-api.vercel.app",
            )
        )
    )
    music_api_timeout: int = field(
        default_factory=lambda: int(os.getenv("MUSIC_API_TIMEOUT", "30"))
    )

    # Dodo Todo System — configured per-guild via /dodo setchannel (stored in DB)

    # Railway
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8080")))

    # ── Helpers ──────────────────────────────────────────────────────
    def resolve_model(self, name: str) -> str:
        """Resolve a model alias or name to its canonical form.

        Resolution order:
        1. Check the model registry (canonical names + aliases)
        2. Check env-var MODEL_ALIASES
        3. Check env-var AVAILABLE_MODELS (legacy)
        4. Pass through as-is if it looks like a provider-specific ID
           (contains '/' e.g. "Qwen/Qwen2.5-7B-Instruct")
        5. Fall back to default_model
        """
        from utils.model_registry import build_registry

        stripped = name.strip()
        lower = stripped.lower()

        # 1. Registry lookup (canonical + aliases)
        registry = build_registry()
        entry = registry.get(stripped)
        if entry:
            return entry.canonical

        # 2. Env-var aliases (MODEL_ALIASES)
        alias_resolved = self.model_aliases.get(lower)
        if alias_resolved:
            # Re-check against registry for the alias target
            entry = registry.get(alias_resolved)
            if entry:
                return entry.canonical
            # If alias target is in legacy available_models, accept it
            if alias_resolved in self.available_models:
                return alias_resolved

        # 3. Legacy AVAILABLE_MODELS list
        if stripped in self.available_models:
            return stripped

        # 4. Provider-specific IDs (pass-through)
        if "/" in stripped:
            return stripped

        # 5. Default
        return self.default_model

    def has_any_llm_provider(self) -> bool:
        """Return True if at least one LLM provider key is configured."""
        return any([
            self.megallm_api_key,
            self.requesty_api_key,
            self.featherless_api_key,
            self.modelslab_api_key,
            self.chutes_api_key,
            self.puter_auth_token,
        ])

    def validate(self) -> List[str]:
        """Return a list of validation errors (empty = OK)."""
        errors: List[str] = []
        if not self.discord_token:
            errors.append("DISCORD_TOKEN is required")
        if not self.has_any_llm_provider():
            errors.append(
                "At least one LLM provider API key is required. "
                "Set one or more of: REQUESTY_API_KEY, FEATHERLESS_API_KEY, "
                "MODELSLAB_API_KEY, CHUTES_API_KEY, PUTER_AUTH_TOKEN, MEGALLM_API_KEY"
            )
        # Note: AVAILABLE_MODELS is no longer required — the model registry
        # provides the full catalogue.  The env var is still respected for
        # backward compatibility but an empty list is not an error.
        if not self.database_url:
            errors.append("DATABASE_URL is required (Supabase PostgreSQL connection string)")
        return errors

    def build_provider_configs(self) -> list:
        """
        Build a list of ``ProviderConfig`` objects from environment variables.

        Only providers with a valid API key are included.  Priority is
        determined by the ``LLM_PROVIDER_PRIORITY`` env var (lower index = higher priority).
        """
        from utils.llm_client import ProviderConfig

        # Map provider name → (api_key, base_url)
        provider_creds = {
            "requesty": (self.requesty_api_key, self.requesty_base_url),
            "featherless": (self.featherless_api_key, self.featherless_base_url),
            "modelslab": (self.modelslab_api_key, self.modelslab_base_url),
            "chutes": (self.chutes_api_key, self.chutes_base_url),
            "puter": (self.puter_auth_token, self.puter_base_url),
            "megallm": (self.megallm_api_key, self.megallm_base_url),
        }

        # Default models per provider (sensible defaults)
        provider_default_models = {
            "requesty": "openai/gpt-4o-mini",
            "featherless": "Qwen/Qwen2.5-7B-Instruct",
            "modelslab": "gpt-4o",
            "chutes": "deepseek-ai/DeepSeek-V3-0324",
            "puter": "gpt-4o-mini",
            "megallm": self.default_model,
        }

        # Model name mappings: canonical name → provider-specific name.
        # Uses the consolidated map from the model registry.
        from utils.model_registry import PROVIDER_MODEL_MAP
        provider_model_maps = {
            name: PROVIDER_MODEL_MAP.get(name, {})
            for name in provider_creds
        }

        # Extra headers per provider
        provider_extra_headers = {
            "requesty": {
                "HTTP-Referer": "https://starzai.bot",
                "X-Title": "StarzAI Discord Bot",
            },
            "featherless": {
                "HTTP-Referer": "https://starzai.bot",
                "X-Title": "StarzAI Discord Bot",
            },
            "chutes": {
                "X-Title": "StarzAI Discord Bot",
            },
            "modelslab": {},
            "puter": {},
            "megallm": {},
        }

        configs: list = []
        for priority, name in enumerate(self.llm_provider_priority):
            name = name.strip().lower()
            if name not in provider_creds:
                continue
            api_key, base_url = provider_creds[name]
            if not api_key:
                continue  # Skip providers without API keys

            configs.append(
                ProviderConfig(
                    name=name,
                    base_url=base_url.rstrip("/"),
                    api_key=api_key,
                    default_model=provider_default_models.get(name, self.default_model),
                    priority=priority,
                    enabled=True,
                    model_map=provider_model_maps.get(name, {}),
                    extra_headers=provider_extra_headers.get(name, {}),
                )
            )

        return configs
