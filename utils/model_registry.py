"""
Model Registry — single source of truth for all available AI models.

Models are organized into three tiers (Free, Premium, Ultra) and are
configured via environment variables.  The owner adds model names to the
tier env vars; the bot handles provider routing internally.

**No provider information is ever shown to end users.**
Provider details are used only for internal API routing and logged at
startup for debugging.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ── Model Tiers ──────────────────────────────────────────────────────


class ModelTier(str, Enum):
    """User-facing tier for the /models UI."""

    FREE = "free"
    PREMIUM = "premium"
    ULTRA = "ultra"


TIER_DISPLAY = {
    ModelTier.FREE:    {"emoji": "🆓", "label": "Free",    "description": "Free models — no cost"},
    ModelTier.PREMIUM: {"emoji": "⭐", "label": "Premium", "description": "High-quality models"},
    ModelTier.ULTRA:   {"emoji": "👑", "label": "Ultra",   "description": "Top-tier flagship models"},
}


# ── Data Classes ─────────────────────────────────────────────────────


@dataclass
class ModelEntry:
    """A single model known to the bot."""

    canonical: str              # e.g. "gpt-4o"
    display_name: str           # e.g. "GPT-4o"
    tier: ModelTier
    description: str = ""       # short description for UI
    # provider_name → provider-specific model ID  (INTERNAL ONLY — never shown to users)
    providers: Dict[str, str] = field(default_factory=dict)
    # Extra tags for search (e.g. ["vision", "128k"])
    tags: List[str] = field(default_factory=list)
    # Context window size (0 = unknown)
    context_window: int = 0

    @property
    def provider_names(self) -> List[str]:
        return list(self.providers.keys())

    def supports_provider(self, provider: str) -> bool:
        return provider in self.providers

    def api_model_id(self, provider: str) -> str:
        """Return the API model ID for a specific provider, or canonical as fallback."""
        return self.providers.get(provider, self.canonical)


# ── Display Names & Descriptions ─────────────────────────────────────
# Cosmetic metadata for known models.  Unknown models get auto-formatted
# names.  This is NOT a model list — it's just presentation data.

KNOWN_MODEL_META: Dict[str, Dict[str, str]] = {
    # GPT family
    "gpt-4":            {"display": "GPT-4",             "desc": "OpenAI's most capable model"},
    "gpt-4o":           {"display": "GPT-4o",            "desc": "OpenAI's fastest flagship with vision"},
    "gpt-4o-mini":      {"display": "GPT-4o Mini",       "desc": "Fast, affordable, and surprisingly capable"},
    "gpt-4.1":          {"display": "GPT-4.1",           "desc": "OpenAI's latest flagship model"},
    "gpt-4.1-mini":     {"display": "GPT-4.1 Mini",      "desc": "Fast and efficient GPT-4.1"},
    "gpt-4.1-nano":     {"display": "GPT-4.1 Nano",      "desc": "Ultra-fast lightweight model"},
    "gpt-3.5-turbo":    {"display": "GPT-3.5 Turbo",     "desc": "Classic fast model — great for simple tasks"},
    "o3":               {"display": "O3",                 "desc": "OpenAI's reasoning model"},
    "o3-mini":          {"display": "O3 Mini",            "desc": "Efficient reasoning model"},
    "o4-mini":          {"display": "O4 Mini",            "desc": "Next-gen reasoning model"},
    # Claude family
    "claude-3-opus":    {"display": "Claude 3 Opus",      "desc": "Anthropic's most capable model"},
    "claude-3-sonnet":  {"display": "Claude 3.5 Sonnet",  "desc": "Anthropic's balanced power & speed"},
    "claude-3-haiku":   {"display": "Claude 3 Haiku",     "desc": "Anthropic's fastest model"},
    "claude-sonnet-4":  {"display": "Claude Sonnet 4",    "desc": "Anthropic's latest model"},
    # Gemini
    "gemini-2.0-flash": {"display": "Gemini 2.0 Flash",   "desc": "Google's fast multimodal model"},
    "gemini-2.5-pro":   {"display": "Gemini 2.5 Pro",     "desc": "Google's premium model"},
    "gemini-2.5-flash": {"display": "Gemini 2.5 Flash",   "desc": "Google's latest fast model"},
    # DeepSeek
    "deepseek-v3":      {"display": "DeepSeek V3",        "desc": "DeepSeek's latest flagship model"},
    "deepseek-r1":      {"display": "DeepSeek R1",        "desc": "DeepSeek's reasoning model"},
    "deepseek-coder-v2":{"display": "DeepSeek Coder V2",  "desc": "Code generation & analysis"},
    # Qwen
    "qwen-2.5-72b":    {"display": "Qwen 2.5 72B",       "desc": "Alibaba's flagship open-source model"},
    "qwen-2.5-7b":     {"display": "Qwen 2.5 7B",        "desc": "Fast and efficient open-source model"},
    "qwen-2.5-coder-32b": {"display": "Qwen 2.5 Coder 32B", "desc": "Code-specialised model"},
    # Llama
    "llama-3.1-70b":   {"display": "Llama 3.1 70B",      "desc": "Meta's powerful open-source model"},
    "llama-3.1-8b":    {"display": "Llama 3.1 8B",       "desc": "Meta's fast open-source model"},
    "llama-3.3-70b":   {"display": "Llama 3.3 70B",      "desc": "Meta's latest open-source model"},
    # Mistral
    "mistral-large":   {"display": "Mistral Large",       "desc": "Mistral AI's flagship model"},
    "mistral-nemo":    {"display": "Mistral Nemo",        "desc": "Mistral's efficient 12B model"},
    "mistral-small":   {"display": "Mistral Small",       "desc": "Mistral's compact fast model"},
    # Creative / Other
    "mythomax-13b":    {"display": "MythoMax 13B",        "desc": "Creative writing & roleplay specialist"},
    "nous-hermes-2":   {"display": "Nous Hermes 2 Mixtral","desc": "Community-tuned creative reasoning"},
}


def _auto_display_name(canonical: str) -> str:
    """Generate a display name from a canonical model ID."""
    # "gpt-4o-mini" → "Gpt 4o Mini", "deepseek-v3" → "Deepseek V3"
    parts = canonical.replace("-", " ").replace("_", " ").split()
    return " ".join(p.capitalize() if not any(c.isdigit() for c in p) else p.upper()
                    for p in parts)


def _get_model_meta(canonical: str) -> Tuple[str, str]:
    """Return (display_name, description) for a model, auto-generating if unknown."""
    meta = KNOWN_MODEL_META.get(canonical, {})
    display = meta.get("display", _auto_display_name(canonical))
    desc = meta.get("desc", "")
    return display, desc


# ── Registry ─────────────────────────────────────────────────────────


class ModelRegistry:
    """
    Central registry of all models the bot can serve.

    Populated at startup from tier env vars.  Provider routing info
    is attached internally but never exposed to users.
    """

    def __init__(self) -> None:
        self._models: Dict[str, ModelEntry] = {}
        # Alias map: alternative name → canonical name
        self._aliases: Dict[str, str] = {}

    # ── Registration ─────────────────────────────────────────────

    def register(self, entry: ModelEntry) -> None:
        """Register a model entry (overwrites if already present)."""
        self._models[entry.canonical] = entry

    def add_alias(self, alias: str, canonical: str) -> None:
        """Add an alternative name that resolves to *canonical*."""
        self._aliases[alias.lower()] = canonical

    def register_provider(
        self, canonical: str, provider_name: str, api_model_id: str
    ) -> None:
        """Add a provider mapping to an existing model entry (internal)."""
        if canonical in self._models:
            self._models[canonical].providers[provider_name] = api_model_id
        else:
            logger.debug(
                "register_provider: model %r not in registry, skipping", canonical
            )

    # ── Lookup ───────────────────────────────────────────────────

    def get(self, name: str) -> Optional[ModelEntry]:
        """Look up a model by canonical name or alias."""
        key = name.strip().lower()
        # Direct lookup
        if key in self._models:
            return self._models[key]
        # Try aliases
        canonical = self._aliases.get(key)
        if canonical and canonical in self._models:
            return self._models[canonical]
        # Case-insensitive canonical search
        for k, entry in self._models.items():
            if k.lower() == key:
                return entry
        return None

    def is_valid(self, name: str) -> bool:
        """Return True if *name* resolves to a known model."""
        return self.get(name) is not None

    def resolve(self, name: str, default: str = "") -> str:
        """Resolve a name/alias to its canonical form.  Returns *default* if unknown."""
        entry = self.get(name)
        if entry:
            return entry.canonical
        return default

    # ── Querying ─────────────────────────────────────────────────

    def all_models(self) -> List[ModelEntry]:
        """Return all registered models."""
        return list(self._models.values())

    def by_tier(self, tier: ModelTier) -> List[ModelEntry]:
        """Return models in a specific tier."""
        return [m for m in self._models.values() if m.tier == tier]

    def for_provider(self, provider_name: str) -> List[ModelEntry]:
        """Return all models a specific provider can serve (internal use)."""
        return [m for m in self._models.values() if provider_name in m.providers]

    def search(self, query: str, limit: int = 25) -> List[ModelEntry]:
        """
        Fuzzy search for models matching *query*.

        Used by Discord autocomplete — returns at most *limit* results.
        No provider info is included in search results.
        """
        q = query.strip().lower()
        if not q:
            # Return a curated "best of" selection when query is empty
            return self._default_selection(limit)

        scored: List[Tuple[int, ModelEntry]] = []
        for entry in self._models.values():
            score = self._match_score(entry, q)
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda t: -t[0])
        return [entry for _, entry in scored[:limit]]

    def canonical_names(self) -> List[str]:
        """Return all canonical model names."""
        return list(self._models.keys())

    # ── Provider Routing (internal) ──────────────────────────────

    def best_provider(
        self,
        canonical: str,
        available_providers: Sequence[str],
        preferred: Optional[str] = None,
    ) -> Optional[Tuple[str, str]]:
        """
        Pick the best provider for *canonical*, returning ``(provider_name, api_model_id)``
        or ``None`` if no available provider can serve it.

        *available_providers* should be ordered by priority (first = highest).
        This is an internal method — provider info is never shown to users.
        """
        entry = self.get(canonical)
        if not entry:
            return None

        # Preferred provider first
        if preferred and preferred in entry.providers and preferred in available_providers:
            return (preferred, entry.providers[preferred])

        # Otherwise, follow the priority order from available_providers
        for prov in available_providers:
            if prov in entry.providers:
                return (prov, entry.providers[prov])

        return None

    # ── Internals ────────────────────────────────────────────────

    def _match_score(self, entry: ModelEntry, query: str) -> int:
        """Simple relevance scoring for search (no provider info used)."""
        score = 0
        canonical_lower = entry.canonical.lower()
        # Exact canonical match
        if query == canonical_lower:
            return 1000
        # Exact display name match
        if query == entry.display_name.lower():
            return 900
        # Canonical starts with query (higher bonus for shorter canonical = closer match)
        if canonical_lower.startswith(query):
            score += 80 - len(canonical_lower)  # Prefer shorter/closer matches
        # Substring in canonical
        elif query in canonical_lower:
            score += 30
        # Substring in display name
        if query in entry.display_name.lower():
            score += 25
        # Substring in description
        if query in entry.description.lower():
            score += 10
        # Tags match
        for tag in entry.tags:
            if query in tag.lower():
                score += 15
        return score

    def _default_selection(self, limit: int) -> List[ModelEntry]:
        """Return a curated default selection when no search query is provided."""
        # Show models from each tier, prioritising Ultra and Premium
        result: List[ModelEntry] = []
        for tier in [ModelTier.ULTRA, ModelTier.PREMIUM, ModelTier.FREE]:
            models = self.by_tier(tier)
            result.extend(models[:8])  # up to 8 per tier

        return result[:limit]


# ── Provider → Model Mappings (INTERNAL ONLY) ───────────────────────
#
# Maps canonical model names to provider-specific API IDs.
# This is used for internal routing and is NEVER shown to users.
#

PROVIDER_MODEL_MAP: Dict[str, Dict[str, str]] = {
    "requesty": {
        "gpt-4": "openai/gpt-4",
        "gpt-4o": "openai/gpt-4o",
        "gpt-4o-mini": "openai/gpt-4o-mini",
        "gpt-4.1": "openai/gpt-4.1",
        "gpt-4.1-mini": "openai/gpt-4.1-mini",
        "gpt-4.1-nano": "openai/gpt-4.1-nano",
        "gpt-3.5-turbo": "openai/gpt-3.5-turbo",
        "o3": "openai/o3",
        "o3-mini": "openai/o3-mini",
        "o4-mini": "openai/o4-mini",
        "claude-3-opus": "anthropic/claude-3-opus-20240229",
        "claude-3-sonnet": "anthropic/claude-3-5-sonnet-20241022",
        "claude-3-haiku": "anthropic/claude-3-haiku-20240307",
        "claude-sonnet-4": "anthropic/claude-sonnet-4-20250514",
        "gemini-2.0-flash": "google/gemini-2.0-flash",
        "gemini-2.5-pro": "google/gemini-2.5-pro-preview",
        "gemini-2.5-flash": "google/gemini-2.5-flash-preview",
        "deepseek-v3": "deepseek/deepseek-chat",
        "deepseek-r1": "deepseek/deepseek-reasoner",
        "deepseek-coder-v2": "deepseek/deepseek-coder",
        "llama-3.1-70b": "meta-llama/llama-3.1-70b-instruct",
        "llama-3.1-8b": "meta-llama/llama-3.1-8b-instruct",
        "llama-3.3-70b": "meta-llama/llama-3.3-70b-instruct",
        "mistral-large": "mistralai/mistral-large-latest",
        "mistral-nemo": "mistralai/mistral-nemo",
        "mistral-small": "mistralai/mistral-small-latest",
        "qwen-2.5-72b": "qwen/qwen-2.5-72b-instruct",
        "qwen-2.5-7b": "qwen/qwen-2.5-7b-instruct",
        "qwen-2.5-coder-32b": "qwen/qwen-2.5-coder-32b-instruct",
        "nous-hermes-2": "nousresearch/nous-hermes-2-mixtral-8x7b-dpo",
    },
    "featherless": {
        "qwen-2.5-72b": "Qwen/Qwen2.5-72B-Instruct",
        "qwen-2.5-7b": "Qwen/Qwen2.5-7B-Instruct",
        "qwen-2.5-coder-32b": "Qwen/Qwen2.5-Coder-32B-Instruct",
        "llama-3.1-70b": "meta-llama/Meta-Llama-3.1-70B-Instruct",
        "llama-3.1-8b": "meta-llama/Meta-Llama-3.1-8B-Instruct",
        "llama-3.3-70b": "meta-llama/Llama-3.3-70B-Instruct",
        "mistral-nemo": "mistralai/Mistral-Nemo-Instruct-2407",
        "nous-hermes-2": "NousResearch/Nous-Hermes-2-Mixtral-8x7B-DPO",
        "mythomax-13b": "Gryphe/MythoMax-L2-13b",
        # Fallback mappings for closed-source names
        "gpt-4": "Qwen/Qwen2.5-72B-Instruct",
        "gpt-4o": "Qwen/Qwen2.5-72B-Instruct",
        "gpt-4o-mini": "Qwen/Qwen2.5-7B-Instruct",
        "gpt-3.5-turbo": "Qwen/Qwen2.5-7B-Instruct",
    },
    "chutes": {
        "deepseek-v3": "deepseek-ai/DeepSeek-V3-0324",
        "deepseek-r1": "deepseek-ai/DeepSeek-R1",
        "deepseek-coder-v2": "deepseek-ai/DeepSeek-Coder-V2-Instruct",
        "qwen-2.5-72b": "Qwen/Qwen2.5-72B-Instruct",
        "qwen-2.5-7b": "Qwen/Qwen2.5-7B-Instruct",
        "llama-3.1-70b": "meta-llama/Llama-3.1-70B-Instruct",
        "llama-3.1-8b": "meta-llama/Llama-3.1-8B-Instruct",
        "llama-3.3-70b": "meta-llama/Llama-3.3-70B-Instruct",
        # Fallback mappings for closed-source names
        "gpt-4": "deepseek-ai/DeepSeek-V3-0324",
        "gpt-4o": "deepseek-ai/DeepSeek-V3-0324",
        "gpt-3.5-turbo": "deepseek-ai/DeepSeek-V3-0324",
    },
    "modelslab": {
        "gpt-4": "gpt-4",
        "gpt-4o": "gpt-4o",
        "gpt-4o-mini": "gpt-4o-mini",
        "gpt-3.5-turbo": "gpt-3.5-turbo",
    },
    "puter": {
        "gpt-4": "gpt-4o",
        "gpt-4o": "gpt-4o",
        "gpt-4o-mini": "gpt-4o-mini",
        "gpt-3.5-turbo": "gpt-4o-mini",
        "claude-3-haiku": "claude-3-haiku-20240307",
    },
    "megallm": {
        # MegaLLM passes model names through
        "gpt-4": "gpt-4",
        "gpt-4o": "gpt-4o",
        "gpt-4o-mini": "gpt-4o-mini",
        "gpt-3.5-turbo": "gpt-3.5-turbo",
    },
}


# ── Aliases ──────────────────────────────────────────────────────────
#
# Common shorthand → canonical name.  Users can type these in
# /set-model or /ask model= and they resolve correctly.
#

BUILTIN_ALIASES: Dict[str, str] = {
    # GPT family
    "gpt4": "gpt-4",
    "gpt4o": "gpt-4o",
    "gpt4-mini": "gpt-4o-mini",
    "gpt4o-mini": "gpt-4o-mini",
    "gpt-4-mini": "gpt-4o-mini",
    "gpt35": "gpt-3.5-turbo",
    "gpt-35": "gpt-3.5-turbo",
    "gpt3": "gpt-3.5-turbo",
    "turbo": "gpt-3.5-turbo",
    # Claude family
    "claude": "claude-3-sonnet",
    "opus": "claude-3-opus",
    "sonnet": "claude-3-sonnet",
    "haiku": "claude-3-haiku",
    "claude-opus": "claude-3-opus",
    "claude-sonnet": "claude-3-sonnet",
    "claude-haiku": "claude-3-haiku",
    # DeepSeek
    "deepseek": "deepseek-v3",
    "deepseek-coder": "deepseek-coder-v2",
    # Gemini
    "gemini": "gemini-2.5-flash",
    "gemini-flash": "gemini-2.0-flash",
    "gemini-pro": "gemini-2.5-pro",
    # Qwen
    "qwen": "qwen-2.5-72b",
    "qwen-72b": "qwen-2.5-72b",
    "qwen-7b": "qwen-2.5-7b",
    "qwen-coder": "qwen-2.5-coder-32b",
    # Llama
    "llama": "llama-3.3-70b",
    "llama-70b": "llama-3.1-70b",
    "llama-8b": "llama-3.1-8b",
    # Mistral
    "mistral": "mistral-large",
    "nemo": "mistral-nemo",
}


# ── Default Tier Assignments ─────────────────────────────────────────
# Used when the owner hasn't set tier env vars.  These are sensible
# defaults that can be fully overridden via FREE_MODELS, PREMIUM_MODELS,
# ULTRA_MODELS environment variables.

DEFAULT_FREE_MODELS = [
    "gpt-4o-mini",
    "gpt-3.5-turbo",
    "gemini-2.0-flash",
    "llama-3.1-8b",
    "qwen-2.5-7b",
    "mistral-nemo",
    "mistral-small",
    "deepseek-v3",
    "mythomax-13b",
    "nous-hermes-2",
]

DEFAULT_PREMIUM_MODELS = [
    "gpt-4",
    "gpt-4o",
    "gpt-4.1-mini",
    "claude-3-sonnet",
    "claude-3-haiku",
    "gemini-2.5-flash",
    "deepseek-r1",
    "deepseek-coder-v2",
    "llama-3.1-70b",
    "llama-3.3-70b",
    "qwen-2.5-72b",
    "qwen-2.5-coder-32b",
    "mistral-large",
]

DEFAULT_ULTRA_MODELS = [
    "gpt-4.1",
    "o3",
    "o3-mini",
    "o4-mini",
    "claude-3-opus",
    "claude-sonnet-4",
    "gemini-2.5-pro",
]


# ── Initialization ──────────────────────────────────────────────────


def _parse_model_list(raw: str) -> List[str]:
    """Parse a comma-separated model list, stripping whitespace."""
    return [m.strip() for m in raw.split(",") if m.strip()]


def build_registry(
    active_providers: Optional[List[str]] = None,
    free_models: Optional[List[str]] = None,
    premium_models: Optional[List[str]] = None,
    ultra_models: Optional[List[str]] = None,
) -> ModelRegistry:
    """
    Build and return a fully-populated ModelRegistry.

    Model lists can be passed directly or are read from environment
    variables: FREE_MODELS, PREMIUM_MODELS, ULTRA_MODELS.

    If none are set, sensible defaults are used.
    """
    registry = ModelRegistry()

    # 1. Determine model lists per tier
    #    Priority: explicit args > env vars > defaults
    env_free = os.getenv("FREE_MODELS", "")
    env_premium = os.getenv("PREMIUM_MODELS", "")
    env_ultra = os.getenv("ULTRA_MODELS", "")

    if free_models is not None:
        tier_free = free_models
    elif env_free:
        tier_free = _parse_model_list(env_free)
    else:
        tier_free = DEFAULT_FREE_MODELS

    if premium_models is not None:
        tier_premium = premium_models
    elif env_premium:
        tier_premium = _parse_model_list(env_premium)
    else:
        tier_premium = DEFAULT_PREMIUM_MODELS

    if ultra_models is not None:
        tier_ultra = ultra_models
    elif env_ultra:
        tier_ultra = _parse_model_list(env_ultra)
    else:
        tier_ultra = DEFAULT_ULTRA_MODELS

    # 2. Register models per tier
    tier_map = {
        ModelTier.FREE: tier_free,
        ModelTier.PREMIUM: tier_premium,
        ModelTier.ULTRA: tier_ultra,
    }

    for tier, model_names in tier_map.items():
        for name in model_names:
            display_name, description = _get_model_meta(name)
            entry = ModelEntry(
                canonical=name,
                display_name=display_name,
                tier=tier,
                description=description,
            )
            registry.register(entry)

    # 3. Add provider mappings from the consolidated map (INTERNAL)
    for provider_name, model_map in PROVIDER_MODEL_MAP.items():
        for canonical, api_id in model_map.items():
            registry.register_provider(canonical, provider_name, api_id)

    # 4. Register aliases
    for alias, canonical in BUILTIN_ALIASES.items():
        registry.add_alias(alias, canonical)

    # 5. Log summary (provider info only appears in logs, never to users)
    total = len(registry.all_models())
    tier_counts = {t: len(registry.by_tier(t)) for t in ModelTier}
    if active_providers:
        available = sum(
            1 for m in registry.all_models()
            if any(p in m.providers for p in active_providers)
        )
        logger.info(
            "Model registry: %d models (%d free, %d premium, %d ultra), "
            "%d available with current providers (%s)",
            total, tier_counts[ModelTier.FREE], tier_counts[ModelTier.PREMIUM],
            tier_counts[ModelTier.ULTRA], available,
            ", ".join(active_providers),
        )
    else:
        logger.info(
            "Model registry: %d models (%d free, %d premium, %d ultra)",
            total, tier_counts[ModelTier.FREE], tier_counts[ModelTier.PREMIUM],
            tier_counts[ModelTier.ULTRA],
        )

    return registry
