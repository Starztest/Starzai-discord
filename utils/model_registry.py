"""
Model Registry — single source of truth for all available AI models.

Maps canonical model names to providers, categories, and display metadata.
The registry is populated from provider model maps at startup and provides:

- Model discovery and search (for Discord autocomplete)
- Categorized browsing (for /models UI with Discord's 25-item limits)
- Provider-aware routing (which provider can serve a given model)
- Validation (is this a valid model name?)
- Backward compatibility (legacy model names still resolve)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ── Model Categories ─────────────────────────────────────────────────


class ModelCategory(str, Enum):
    """User-facing categories for the /models UI."""

    POWERFUL = "powerful"       # Flagship models (GPT-4, Claude 3 Opus, etc.)
    FAST = "fast"              # Optimised for speed (GPT-4o-mini, Haiku, etc.)
    CODE = "code"              # Code-specialised (DeepSeek Coder, etc.)
    CREATIVE = "creative"      # Creative writing, roleplay
    OPEN_SOURCE = "open_source" # OSS models (Qwen, Llama, Mistral)
    FREE = "free"              # Free-tier models (Puter, etc.)


CATEGORY_DISPLAY = {
    ModelCategory.POWERFUL:     {"emoji": "\u26a1",  "label": "Powerful",    "description": "Flagship models — best quality"},
    ModelCategory.FAST:         {"emoji": "\U0001f3ce\ufe0f", "label": "Fast", "description": "Optimised for speed"},
    ModelCategory.CODE:         {"emoji": "\U0001f4bb",  "label": "Code",       "description": "Code generation & analysis"},
    ModelCategory.CREATIVE:     {"emoji": "\U0001f3a8",  "label": "Creative",   "description": "Creative writing & roleplay"},
    ModelCategory.OPEN_SOURCE:  {"emoji": "\U0001f310",  "label": "Open Source", "description": "Community models (Qwen, Llama, Mistral)"},
    ModelCategory.FREE:         {"emoji": "\U0001f193",  "label": "Free",       "description": "Free-tier models — no cost"},
}


# ── Data Classes ──────────────────────────────────────────────────────


@dataclass
class ModelEntry:
    """A single model known to the bot."""

    canonical: str              # e.g. "gpt-4o"
    display_name: str           # e.g. "GPT-4o"
    category: ModelCategory
    description: str = ""       # short description for UI
    # provider_name → provider-specific model ID
    providers: Dict[str, str] = field(default_factory=dict)
    # Extra tags for search (e.g. ["openai", "vision", "128k"])
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


# ── Registry ──────────────────────────────────────────────────────────


class ModelRegistry:
    """
    Central registry of all models the bot can serve.

    Populated at startup from provider model maps and the built-in
    catalogue below.
    """

    def __init__(self) -> None:
        self._models: Dict[str, ModelEntry] = {}
        # Alias map: alternative name → canonical name
        self._aliases: Dict[str, str] = {}

    # ── Registration ──────────────────────────────────────────────

    def register(self, entry: ModelEntry) -> None:
        """Register a model entry (overwrites if already present)."""
        self._models[entry.canonical] = entry

    def add_alias(self, alias: str, canonical: str) -> None:
        """Add an alternative name that resolves to *canonical*."""
        self._aliases[alias.lower()] = canonical

    def register_provider(
        self, canonical: str, provider_name: str, api_model_id: str
    ) -> None:
        """Add a provider mapping to an existing model entry."""
        if canonical in self._models:
            self._models[canonical].providers[provider_name] = api_model_id
        else:
            logger.debug(
                "register_provider: model %r not in registry, skipping", canonical
            )

    # ── Lookup ────────────────────────────────────────────────────

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

    # ── Querying ──────────────────────────────────────────────────

    def all_models(self) -> List[ModelEntry]:
        """Return all registered models."""
        return list(self._models.values())

    def by_category(self, category: ModelCategory) -> List[ModelEntry]:
        """Return models in a specific category."""
        return [m for m in self._models.values() if m.category == category]

    def for_provider(self, provider_name: str) -> List[ModelEntry]:
        """Return all models a specific provider can serve."""
        return [m for m in self._models.values() if provider_name in m.providers]

    def search(self, query: str, limit: int = 25) -> List[ModelEntry]:
        """
        Fuzzy search for models matching *query*.

        Used by Discord autocomplete — returns at most *limit* results.
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

    # ── Provider Routing ──────────────────────────────────────────

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

    # ── Internals ─────────────────────────────────────────────────

    def _match_score(self, entry: ModelEntry, query: str) -> int:
        """Simple relevance scoring for search."""
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
        # Show one model per category, prioritising Powerful and Fast
        result: List[ModelEntry] = []
        for cat in [
            ModelCategory.POWERFUL,
            ModelCategory.FAST,
            ModelCategory.CODE,
            ModelCategory.CREATIVE,
            ModelCategory.OPEN_SOURCE,
            ModelCategory.FREE,
        ]:
            models = self.by_category(cat)
            result.extend(models[:4])  # up to 4 per category

        return result[:limit]


# ── Built-in Model Catalogue ─────────────────────────────────────────
#
# This is the curated set of models the bot offers.  Provider mappings
# are added during initialization from the provider model maps in
# settings.py (build_provider_configs).
#

BUILTIN_MODELS: List[ModelEntry] = [
    # ── Powerful ──────────────────────────────────────────────────
    ModelEntry(
        canonical="gpt-4",
        display_name="GPT-4",
        category=ModelCategory.POWERFUL,
        description="OpenAI's most capable model",
        tags=["openai", "flagship"],
        context_window=8192,
    ),
    ModelEntry(
        canonical="gpt-4o",
        display_name="GPT-4o",
        category=ModelCategory.POWERFUL,
        description="OpenAI's fastest flagship model with vision",
        tags=["openai", "flagship", "vision", "multimodal"],
        context_window=128000,
    ),
    ModelEntry(
        canonical="claude-3-opus",
        display_name="Claude 3 Opus",
        category=ModelCategory.POWERFUL,
        description="Anthropic's most capable model",
        tags=["anthropic", "flagship"],
        context_window=200000,
    ),
    ModelEntry(
        canonical="claude-3-sonnet",
        display_name="Claude 3.5 Sonnet",
        category=ModelCategory.POWERFUL,
        description="Anthropic's balanced power & speed model",
        tags=["anthropic", "balanced"],
        context_window=200000,
    ),
    ModelEntry(
        canonical="deepseek-v3",
        display_name="DeepSeek V3",
        category=ModelCategory.POWERFUL,
        description="DeepSeek's latest flagship model",
        tags=["deepseek", "flagship", "reasoning"],
        context_window=128000,
    ),

    # ── Fast ──────────────────────────────────────────────────────
    ModelEntry(
        canonical="gpt-4o-mini",
        display_name="GPT-4o Mini",
        category=ModelCategory.FAST,
        description="Fast, affordable, and surprisingly capable",
        tags=["openai", "fast", "cheap"],
        context_window=128000,
    ),
    ModelEntry(
        canonical="gpt-3.5-turbo",
        display_name="GPT-3.5 Turbo",
        category=ModelCategory.FAST,
        description="Classic fast model — great for simple tasks",
        tags=["openai", "fast", "legacy"],
        context_window=16385,
    ),
    ModelEntry(
        canonical="claude-3-haiku",
        display_name="Claude 3 Haiku",
        category=ModelCategory.FAST,
        description="Anthropic's fastest model",
        tags=["anthropic", "fast"],
        context_window=200000,
    ),

    # ── Code ──────────────────────────────────────────────────────
    ModelEntry(
        canonical="deepseek-coder-v2",
        display_name="DeepSeek Coder V2",
        category=ModelCategory.CODE,
        description="Specialised code generation & analysis",
        tags=["deepseek", "code", "programming"],
        context_window=128000,
    ),
    ModelEntry(
        canonical="qwen-2.5-coder-32b",
        display_name="Qwen 2.5 Coder 32B",
        category=ModelCategory.CODE,
        description="Alibaba's code-specialised model (32B)",
        tags=["qwen", "code", "open-source"],
        context_window=32768,
    ),

    # ── Open Source ───────────────────────────────────────────────
    ModelEntry(
        canonical="qwen-2.5-72b",
        display_name="Qwen 2.5 72B",
        category=ModelCategory.OPEN_SOURCE,
        description="Alibaba's flagship open-source model",
        tags=["qwen", "open-source", "large"],
        context_window=32768,
    ),
    ModelEntry(
        canonical="qwen-2.5-7b",
        display_name="Qwen 2.5 7B",
        category=ModelCategory.OPEN_SOURCE,
        description="Fast and efficient open-source model",
        tags=["qwen", "open-source", "small", "fast"],
        context_window=32768,
    ),
    ModelEntry(
        canonical="llama-3.1-70b",
        display_name="Llama 3.1 70B",
        category=ModelCategory.OPEN_SOURCE,
        description="Meta's powerful open-source model",
        tags=["meta", "llama", "open-source"],
        context_window=128000,
    ),
    ModelEntry(
        canonical="llama-3.1-8b",
        display_name="Llama 3.1 8B",
        category=ModelCategory.OPEN_SOURCE,
        description="Meta's fast open-source model",
        tags=["meta", "llama", "open-source", "small", "fast"],
        context_window=128000,
    ),
    ModelEntry(
        canonical="mistral-large",
        display_name="Mistral Large",
        category=ModelCategory.OPEN_SOURCE,
        description="Mistral AI's flagship model",
        tags=["mistral", "open-source", "large"],
        context_window=128000,
    ),
    ModelEntry(
        canonical="mistral-nemo",
        display_name="Mistral Nemo",
        category=ModelCategory.OPEN_SOURCE,
        description="Mistral's efficient 12B model",
        tags=["mistral", "open-source", "fast"],
        context_window=128000,
    ),

    # ── Creative ──────────────────────────────────────────────────
    ModelEntry(
        canonical="mythomax-13b",
        display_name="MythoMax 13B",
        category=ModelCategory.CREATIVE,
        description="Creative writing & roleplay specialist",
        tags=["creative", "roleplay", "open-source"],
        context_window=4096,
    ),
    ModelEntry(
        canonical="nous-hermes-2",
        display_name="Nous Hermes 2 Mixtral",
        category=ModelCategory.CREATIVE,
        description="Community-tuned creative reasoning model",
        tags=["nous", "creative", "reasoning", "open-source"],
        context_window=32768,
    ),

    # ── Free ──────────────────────────────────────────────────────
    ModelEntry(
        canonical="gpt-4o-mini-free",
        display_name="GPT-4o Mini (Free)",
        category=ModelCategory.FREE,
        description="GPT-4o Mini via Puter — completely free",
        tags=["openai", "free", "puter"],
        context_window=128000,
    ),
    ModelEntry(
        canonical="claude-3-haiku-free",
        display_name="Claude 3 Haiku (Free)",
        category=ModelCategory.FREE,
        description="Claude 3 Haiku via Puter — completely free",
        tags=["anthropic", "free", "puter"],
        context_window=200000,
    ),
]


# ── Provider → Model Mappings ────────────────────────────────────────
#
# This maps canonical model names to provider-specific API IDs.
# It extends the per-provider model_map in settings.py.
#

PROVIDER_MODEL_MAP: Dict[str, Dict[str, str]] = {
    "requesty": {
        "gpt-4": "openai/gpt-4",
        "gpt-4o": "openai/gpt-4o",
        "gpt-4o-mini": "openai/gpt-4o-mini",
        "gpt-3.5-turbo": "openai/gpt-3.5-turbo",
        "claude-3-opus": "anthropic/claude-3-opus-20240229",
        "claude-3-sonnet": "anthropic/claude-3-5-sonnet-20241022",
        "claude-3-haiku": "anthropic/claude-3-haiku-20240307",
        "deepseek-v3": "deepseek/deepseek-chat",
        "deepseek-coder-v2": "deepseek/deepseek-coder",
        "llama-3.1-70b": "meta-llama/llama-3.1-70b-instruct",
        "llama-3.1-8b": "meta-llama/llama-3.1-8b-instruct",
        "mistral-large": "mistralai/mistral-large-latest",
        "mistral-nemo": "mistralai/mistral-nemo",
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
        "mistral-nemo": "mistralai/Mistral-Nemo-Instruct-2407",
        "nous-hermes-2": "NousResearch/Nous-Hermes-2-Mixtral-8x7B-DPO",
        "mythomax-13b": "Gryphe/MythoMax-L2-13b",
        # Map canonical closed-source names to closest open-source equivalents
        "gpt-4": "Qwen/Qwen2.5-72B-Instruct",
        "gpt-4o": "Qwen/Qwen2.5-72B-Instruct",
        "gpt-4o-mini": "Qwen/Qwen2.5-7B-Instruct",
        "gpt-3.5-turbo": "Qwen/Qwen2.5-7B-Instruct",
    },
    "chutes": {
        "deepseek-v3": "deepseek-ai/DeepSeek-V3-0324",
        "deepseek-coder-v2": "deepseek-ai/DeepSeek-Coder-V2-Instruct",
        "qwen-2.5-72b": "Qwen/Qwen2.5-72B-Instruct",
        "qwen-2.5-7b": "Qwen/Qwen2.5-7B-Instruct",
        "llama-3.1-70b": "meta-llama/Llama-3.1-70B-Instruct",
        "llama-3.1-8b": "meta-llama/Llama-3.1-8B-Instruct",
        # Map closed-source names to DeepSeek equivalent
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
        "gpt-4o-mini-free": "gpt-4o-mini",
        "claude-3-haiku-free": "claude-3-haiku-20240307",
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
    # Qwen
    "qwen": "qwen-2.5-72b",
    "qwen-72b": "qwen-2.5-72b",
    "qwen-7b": "qwen-2.5-7b",
    "qwen-coder": "qwen-2.5-coder-32b",
    # Llama
    "llama": "llama-3.1-70b",
    "llama-70b": "llama-3.1-70b",
    "llama-8b": "llama-3.1-8b",
    # Mistral
    "mistral": "mistral-large",
    "nemo": "mistral-nemo",
    # Free
    "free": "gpt-4o-mini-free",
    "free-gpt": "gpt-4o-mini-free",
    "free-claude": "claude-3-haiku-free",
}


# ── Initialization ───────────────────────────────────────────────────


def build_registry(active_providers: Optional[List[str]] = None) -> ModelRegistry:
    """
    Build and return a fully-populated ModelRegistry.

    *active_providers* is an optional list of provider names that have
    valid API keys.  If given, models that have NO active provider will
    still be registered but can be filtered in the UI.
    """
    registry = ModelRegistry()

    # 1. Register all built-in models
    for entry in BUILTIN_MODELS:
        registry.register(entry)

    # 2. Add provider mappings from the consolidated map
    for provider_name, model_map in PROVIDER_MODEL_MAP.items():
        for canonical, api_id in model_map.items():
            registry.register_provider(canonical, provider_name, api_id)

    # 3. Register aliases
    for alias, canonical in BUILTIN_ALIASES.items():
        registry.add_alias(alias, canonical)

    # 4. Log summary
    total = len(registry.all_models())
    if active_providers:
        available = sum(
            1 for m in registry.all_models()
            if any(p in m.providers for p in active_providers)
        )
        logger.info(
            "Model registry: %d models registered, %d available with current providers (%s)",
            total, available, ", ".join(active_providers),
        )
    else:
        logger.info("Model registry: %d models registered", total)

    return registry
