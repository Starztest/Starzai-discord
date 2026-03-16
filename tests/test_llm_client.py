"""
Tests for the multi-provider LLM client.
"""

import unittest

from utils.llm_client import (
    LLMClient,
    LLMClientError,
    LLMResponse,
    ProviderConfig,
    ProviderName,
)


class TestLLMClient(unittest.TestCase):
    """Test LLMClient initialization and basic behavior."""

    def test_legacy_init(self):
        """Legacy single-provider init still works."""
        client = LLMClient(
            api_key="test-key",
            base_url="https://api.test.com/v1",
            default_model="gpt-4",
        )
        self.assertEqual(client.default_model, "gpt-4")
        self.assertEqual(len(client._providers), 1)
        self.assertEqual(client._providers[0].cfg.name, "megallm")
        self.assertEqual(client._providers[0].cfg.base_url, "https://api.test.com/v1")
        self.assertEqual(client._providers[0].cfg.api_key, "test-key")

    def test_multi_provider_init(self):
        """Multi-provider init with ProviderConfig list."""
        configs = [
            ProviderConfig(
                name="requesty", base_url="https://router.requesty.ai/v1",
                api_key="req-key", priority=0,
            ),
            ProviderConfig(
                name="chutes", base_url="https://llm.chutes.ai/v1",
                api_key="cpk_key", priority=1,
            ),
        ]
        client = LLMClient(providers=configs, default_model="gpt-4")
        self.assertEqual(len(client._providers), 2)
        self.assertEqual(client._providers[0].cfg.name, "requesty")
        self.assertEqual(client._providers[1].cfg.name, "chutes")

    def test_providers_sorted_by_priority(self):
        """Providers are sorted by priority regardless of input order."""
        configs = [
            ProviderConfig(name="b", base_url="https://b.ai/v1", api_key="kb", priority=2),
            ProviderConfig(name="a", base_url="https://a.ai/v1", api_key="ka", priority=0),
            ProviderConfig(name="c", base_url="https://c.ai/v1", api_key="kc", priority=1),
        ]
        client = LLMClient(providers=configs)
        self.assertEqual([p.cfg.name for p in client._providers], ["a", "c", "b"])

    def test_empty_api_key_skipped(self):
        """Providers with empty API keys are not added."""
        configs = [
            ProviderConfig(name="good", base_url="https://a.ai/v1", api_key="k", priority=0),
            ProviderConfig(name="empty", base_url="https://b.ai/v1", api_key="", priority=1),
        ]
        client = LLMClient(providers=configs)
        self.assertEqual(len(client._providers), 1)
        self.assertEqual(client._providers[0].cfg.name, "good")

    def test_disabled_provider_skipped(self):
        """Disabled providers are not added."""
        configs = [
            ProviderConfig(name="on", base_url="https://a.ai/v1", api_key="k", enabled=True),
            ProviderConfig(name="off", base_url="https://b.ai/v1", api_key="k", enabled=False),
        ]
        client = LLMClient(providers=configs)
        self.assertEqual(len(client._providers), 1)

    def test_no_providers_logs_error(self):
        """LLMClient with no providers still initializes (logs error)."""
        client = LLMClient()
        self.assertEqual(len(client._providers), 0)

    def test_provider_status(self):
        """provider_status() returns correct structure."""
        client = LLMClient(api_key="k", base_url="https://a.ai/v1")
        status = client.provider_status()
        self.assertEqual(len(status), 1)
        self.assertIn("name", status[0])
        self.assertIn("available", status[0])
        self.assertIn("base_url", status[0])


class TestLLMResponse(unittest.TestCase):
    def test_dataclass(self):
        resp = LLMResponse(
            content="Hello!",
            model="gpt-4",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
        self.assertEqual(resp.content, "Hello!")
        self.assertEqual(resp.total_tokens, 15)
        self.assertEqual(resp.provider, "")  # default

    def test_provider_field(self):
        resp = LLMResponse(content="Hi", model="gpt-4", provider="requesty")
        self.assertEqual(resp.provider, "requesty")


class TestLLMClientError(unittest.TestCase):
    def test_error(self):
        err = LLMClientError("API down", status_code=500)
        self.assertEqual(str(err), "API down")
        self.assertEqual(err.status_code, 500)

    def test_error_no_status(self):
        err = LLMClientError("timeout")
        self.assertIsNone(err.status_code)


class TestProviderConfig(unittest.TestCase):
    def test_resolve_model_mapped(self):
        cfg = ProviderConfig(
            name="test", base_url="https://a.ai/v1", api_key="k",
            model_map={"gpt-4": "openai/gpt-4"},
        )
        self.assertEqual(cfg.resolve_model("gpt-4"), "openai/gpt-4")

    def test_resolve_model_passthrough(self):
        cfg = ProviderConfig(
            name="test", base_url="https://a.ai/v1", api_key="k",
            model_map={"gpt-4": "openai/gpt-4"},
        )
        self.assertEqual(cfg.resolve_model("claude-3"), "claude-3")

    def test_base_url_trailing_slash(self):
        """ProviderConfig stores base_url as-is; LLMClient.build strips slashes."""
        cfg = ProviderConfig(name="test", base_url="https://a.ai/v1/", api_key="k")
        self.assertEqual(cfg.base_url, "https://a.ai/v1/")


class TestProviderName(unittest.TestCase):
    def test_enum_values(self):
        self.assertEqual(ProviderName.REQUESTY, "requesty")
        self.assertEqual(ProviderName.FEATHERLESS, "featherless")
        self.assertEqual(ProviderName.MODELSLAB, "modelslab")
        self.assertEqual(ProviderName.CHUTES, "chutes")
        self.assertEqual(ProviderName.PUTER, "puter")
        self.assertEqual(ProviderName.MEGALLM, "megallm")


class TestLLMClientAsync(unittest.IsolatedAsyncioTestCase):
    async def test_simple_prompt_builds_correct_messages(self):
        client = LLMClient(api_key="k", base_url="https://x.com/v1")
        called_with = {}

        async def mock_chat(messages, model=None, **kw):
            called_with["messages"] = messages
            return LLMResponse(content="ok", model="gpt-4")

        client.chat = mock_chat  # type: ignore
        await client.simple_prompt("Hello", system="Be nice")
        self.assertEqual(len(called_with["messages"]), 2)
        self.assertEqual(called_with["messages"][0]["role"], "system")
        self.assertEqual(called_with["messages"][0]["content"], "Be nice")
        self.assertEqual(called_with["messages"][1]["content"], "Hello")

    async def test_close_no_providers(self):
        """close() should not raise even with no providers."""
        client = LLMClient()
        await client.close()

    async def test_close_with_provider(self):
        """close() should not raise with a provider that has no open session."""
        client = LLMClient(api_key="k", base_url="https://x.com/v1")
        await client.close()


if __name__ == "__main__":
    unittest.main()
