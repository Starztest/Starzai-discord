"""
Tests for the model registry — model catalogue, search, aliases,
provider routing, and integration with Settings.resolve_model().
"""

import unittest

from utils.model_registry import (
    BUILTIN_ALIASES,
    BUILTIN_MODELS,
    CATEGORY_DISPLAY,
    ModelCategory,
    ModelEntry,
    ModelRegistry,
    PROVIDER_MODEL_MAP,
    build_registry,
)


class TestModelEntry(unittest.TestCase):
    """Test ModelEntry dataclass basics."""

    def test_provider_names(self):
        entry = ModelEntry(
            canonical="test",
            display_name="Test",
            category=ModelCategory.FAST,
            providers={"requesty": "openai/test", "chutes": "test-ai/test"},
        )
        self.assertEqual(set(entry.provider_names), {"requesty", "chutes"})

    def test_supports_provider(self):
        entry = ModelEntry(
            canonical="test",
            display_name="Test",
            category=ModelCategory.FAST,
            providers={"requesty": "openai/test"},
        )
        self.assertTrue(entry.supports_provider("requesty"))
        self.assertFalse(entry.supports_provider("puter"))

    def test_api_model_id_found(self):
        entry = ModelEntry(
            canonical="gpt-4",
            display_name="GPT-4",
            category=ModelCategory.POWERFUL,
            providers={"requesty": "openai/gpt-4"},
        )
        self.assertEqual(entry.api_model_id("requesty"), "openai/gpt-4")

    def test_api_model_id_fallback(self):
        entry = ModelEntry(
            canonical="gpt-4",
            display_name="GPT-4",
            category=ModelCategory.POWERFUL,
            providers={"requesty": "openai/gpt-4"},
        )
        # Unknown provider falls back to canonical name
        self.assertEqual(entry.api_model_id("megallm"), "gpt-4")


class TestModelRegistry(unittest.TestCase):
    """Test ModelRegistry core functionality."""

    def _make_registry(self) -> ModelRegistry:
        registry = ModelRegistry()
        registry.register(
            ModelEntry(
                canonical="gpt-4",
                display_name="GPT-4",
                category=ModelCategory.POWERFUL,
                description="OpenAI's most capable model",
                providers={"requesty": "openai/gpt-4", "puter": "gpt-4o"},
                tags=["openai"],
            )
        )
        registry.register(
            ModelEntry(
                canonical="gpt-4o-mini",
                display_name="GPT-4o Mini",
                category=ModelCategory.FAST,
                description="Fast and cheap",
                providers={"requesty": "openai/gpt-4o-mini"},
                tags=["openai", "fast"],
            )
        )
        registry.register(
            ModelEntry(
                canonical="qwen-2.5-72b",
                display_name="Qwen 2.5 72B",
                category=ModelCategory.OPEN_SOURCE,
                providers={"featherless": "Qwen/Qwen2.5-72B-Instruct"},
                tags=["qwen", "open-source"],
            )
        )
        registry.add_alias("gpt4", "gpt-4")
        registry.add_alias("qwen", "qwen-2.5-72b")
        return registry

    def test_get_by_canonical(self):
        reg = self._make_registry()
        entry = reg.get("gpt-4")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.canonical, "gpt-4")

    def test_get_by_alias(self):
        reg = self._make_registry()
        entry = reg.get("gpt4")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.canonical, "gpt-4")

    def test_get_case_insensitive(self):
        reg = self._make_registry()
        entry = reg.get("GPT-4")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.canonical, "gpt-4")

    def test_get_unknown(self):
        reg = self._make_registry()
        self.assertIsNone(reg.get("nonexistent-model"))

    def test_is_valid(self):
        reg = self._make_registry()
        self.assertTrue(reg.is_valid("gpt-4"))
        self.assertTrue(reg.is_valid("gpt4"))  # alias
        self.assertFalse(reg.is_valid("foobar"))

    def test_resolve_known(self):
        reg = self._make_registry()
        self.assertEqual(reg.resolve("gpt4", default="fallback"), "gpt-4")

    def test_resolve_unknown_returns_default(self):
        reg = self._make_registry()
        self.assertEqual(reg.resolve("foobar", default="gpt-4"), "gpt-4")

    def test_all_models(self):
        reg = self._make_registry()
        self.assertEqual(len(reg.all_models()), 3)

    def test_by_category(self):
        reg = self._make_registry()
        powerful = reg.by_category(ModelCategory.POWERFUL)
        self.assertEqual(len(powerful), 1)
        self.assertEqual(powerful[0].canonical, "gpt-4")

    def test_for_provider(self):
        reg = self._make_registry()
        requesty_models = reg.for_provider("requesty")
        self.assertEqual(len(requesty_models), 2)  # gpt-4, gpt-4o-mini

    def test_register_provider(self):
        reg = self._make_registry()
        reg.register_provider("gpt-4", "megallm", "gpt-4")
        entry = reg.get("gpt-4")
        self.assertIn("megallm", entry.providers)
        self.assertEqual(entry.api_model_id("megallm"), "gpt-4")

    def test_register_provider_unknown_model(self):
        """register_provider for unknown model should not crash."""
        reg = self._make_registry()
        reg.register_provider("unknown-model", "requesty", "openai/unknown")
        self.assertIsNone(reg.get("unknown-model"))

    def test_search_empty_query(self):
        reg = self._make_registry()
        results = reg.search("", limit=25)
        self.assertGreater(len(results), 0)

    def test_search_canonical_name(self):
        reg = self._make_registry()
        results = reg.search("gpt-4", limit=25)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].canonical, "gpt-4")

    def test_search_partial(self):
        reg = self._make_registry()
        results = reg.search("qwen", limit=25)
        self.assertGreater(len(results), 0)
        canonicals = [m.canonical for m in results]
        self.assertIn("qwen-2.5-72b", canonicals)

    def test_search_by_tag(self):
        reg = self._make_registry()
        results = reg.search("openai", limit=25)
        self.assertGreater(len(results), 0)
        for m in results:
            self.assertTrue(
                "openai" in m.tags or "openai" in m.canonical.lower() or "openai" in m.display_name.lower()
            )

    def test_search_limit(self):
        reg = self._make_registry()
        results = reg.search("", limit=2)
        self.assertLessEqual(len(results), 2)

    def test_canonical_names(self):
        reg = self._make_registry()
        names = reg.canonical_names()
        self.assertEqual(set(names), {"gpt-4", "gpt-4o-mini", "qwen-2.5-72b"})


class TestBestProvider(unittest.TestCase):
    """Test model-aware provider routing."""

    def _make_registry(self) -> ModelRegistry:
        registry = ModelRegistry()
        registry.register(
            ModelEntry(
                canonical="claude-3-opus",
                display_name="Claude 3 Opus",
                category=ModelCategory.POWERFUL,
                providers={
                    "requesty": "anthropic/claude-3-opus-20240229",
                    "puter": "claude-3-opus-20240229",
                },
            )
        )
        return registry

    def test_best_provider_by_priority(self):
        reg = self._make_registry()
        result = reg.best_provider(
            "claude-3-opus",
            available_providers=["requesty", "puter"],
        )
        self.assertIsNotNone(result)
        provider_name, api_id = result
        self.assertEqual(provider_name, "requesty")
        self.assertEqual(api_id, "anthropic/claude-3-opus-20240229")

    def test_best_provider_preferred(self):
        reg = self._make_registry()
        result = reg.best_provider(
            "claude-3-opus",
            available_providers=["requesty", "puter"],
            preferred="puter",
        )
        self.assertIsNotNone(result)
        provider_name, api_id = result
        self.assertEqual(provider_name, "puter")

    def test_best_provider_unavailable(self):
        reg = self._make_registry()
        result = reg.best_provider(
            "claude-3-opus",
            available_providers=["featherless", "chutes"],
        )
        self.assertIsNone(result)

    def test_best_provider_unknown_model(self):
        reg = self._make_registry()
        result = reg.best_provider(
            "nonexistent",
            available_providers=["requesty"],
        )
        self.assertIsNone(result)


class TestBuildRegistry(unittest.TestCase):
    """Test the build_registry() factory function."""

    def test_all_builtin_models_registered(self):
        registry = build_registry()
        for model in BUILTIN_MODELS:
            self.assertTrue(
                registry.is_valid(model.canonical),
                f"Built-in model {model.canonical!r} should be in registry",
            )

    def test_all_builtin_aliases_registered(self):
        registry = build_registry()
        for alias, canonical in BUILTIN_ALIASES.items():
            entry = registry.get(alias)
            self.assertIsNotNone(
                entry,
                f"Alias {alias!r} -> {canonical!r} should resolve",
            )
            self.assertEqual(entry.canonical, canonical)

    def test_provider_maps_attached(self):
        registry = build_registry()
        # GPT-4 should have requesty provider from PROVIDER_MODEL_MAP
        entry = registry.get("gpt-4")
        self.assertIsNotNone(entry)
        self.assertIn("requesty", entry.providers)
        self.assertEqual(entry.providers["requesty"], "openai/gpt-4")

    def test_provider_maps_multiple_providers(self):
        registry = build_registry()
        # DeepSeek V3 should be on both requesty and chutes
        entry = registry.get("deepseek-v3")
        self.assertIsNotNone(entry)
        self.assertIn("requesty", entry.providers)
        self.assertIn("chutes", entry.providers)

    def test_active_providers_filter(self):
        registry = build_registry(active_providers=["requesty"])
        # Should still register all models, but logging differs
        self.assertGreater(len(registry.all_models()), 0)

    def test_categories_have_models(self):
        registry = build_registry()
        for cat in ModelCategory:
            models = registry.by_category(cat)
            self.assertGreater(
                len(models), 0,
                f"Category {cat.value} should have at least one model",
            )


class TestModelCategory(unittest.TestCase):
    """Test ModelCategory enum and display metadata."""

    def test_all_categories_have_display(self):
        for cat in ModelCategory:
            self.assertIn(cat, CATEGORY_DISPLAY)
            meta = CATEGORY_DISPLAY[cat]
            self.assertIn("emoji", meta)
            self.assertIn("label", meta)
            self.assertIn("description", meta)

    def test_category_values(self):
        expected = {"powerful", "fast", "code", "creative", "open_source", "free"}
        actual = {cat.value for cat in ModelCategory}
        self.assertEqual(actual, expected)


class TestProviderModelMap(unittest.TestCase):
    """Test the PROVIDER_MODEL_MAP data is consistent."""

    def test_all_providers_present(self):
        expected = {"requesty", "featherless", "chutes", "modelslab", "puter", "megallm"}
        self.assertEqual(set(PROVIDER_MODEL_MAP.keys()), expected)

    def test_gpt4_has_multiple_providers(self):
        providers_with_gpt4 = [
            name for name, maps in PROVIDER_MODEL_MAP.items() if "gpt-4" in maps
        ]
        self.assertGreater(len(providers_with_gpt4), 2)

    def test_no_empty_api_ids(self):
        for provider, maps in PROVIDER_MODEL_MAP.items():
            for canonical, api_id in maps.items():
                self.assertTrue(
                    api_id.strip(),
                    f"Provider {provider} has empty API ID for {canonical}",
                )


class TestSettingsResolveModel(unittest.TestCase):
    """Test Settings.resolve_model() with the new registry integration."""

    def _make_settings(self, **overrides):
        """Create a minimal Settings for testing."""
        import os
        # Temporarily set env vars for testing
        old_env = {}
        defaults = {
            "DISCORD_TOKEN": "test",
            "DATABASE_URL": "postgresql://test",
            "REQUESTY_API_KEY": "test-key",
        }
        defaults.update(overrides)
        for k, v in defaults.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v

        from config.settings import Settings
        settings = Settings()

        # Restore env
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

        return settings

    def test_canonical_model_resolves(self):
        settings = self._make_settings()
        # "gpt-4" is in the registry
        self.assertEqual(settings.resolve_model("gpt-4"), "gpt-4")

    def test_alias_resolves(self):
        settings = self._make_settings()
        # "gpt4" is a built-in alias for "gpt-4"
        self.assertEqual(settings.resolve_model("gpt4"), "gpt-4")

    def test_registry_alias_resolves(self):
        settings = self._make_settings()
        # "deepseek" is a built-in alias for "deepseek-v3"
        self.assertEqual(settings.resolve_model("deepseek"), "deepseek-v3")

    def test_provider_specific_passthrough(self):
        settings = self._make_settings()
        # Provider-specific IDs with "/" should pass through
        self.assertEqual(
            settings.resolve_model("Qwen/Qwen2.5-7B-Instruct"),
            "Qwen/Qwen2.5-7B-Instruct",
        )

    def test_unknown_model_returns_default(self):
        settings = self._make_settings()
        result = settings.resolve_model("totally-fake-model")
        self.assertEqual(result, settings.default_model)


if __name__ == "__main__":
    unittest.main()
