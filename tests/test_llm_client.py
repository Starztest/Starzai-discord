"""
Tests for the LLM client.
"""

import unittest

from utils.llm_client import LLMClient, LLMClientError, LLMResponse


class TestLLMClient(unittest.TestCase):
    def test_init(self):
        client = LLMClient(
            api_key="test-key",
            base_url="https://api.test.com/v1",
            default_model="gpt-4",
        )
        self.assertEqual(client.api_key, "test-key")
        self.assertEqual(client.base_url, "https://api.test.com/v1")
        self.assertEqual(client.default_model, "gpt-4")

    def test_llm_response_dataclass(self):
        resp = LLMResponse(
            content="Hello!",
            model="gpt-4",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
        self.assertEqual(resp.content, "Hello!")
        self.assertEqual(resp.total_tokens, 15)

    def test_llm_client_error(self):
        err = LLMClientError("API down", status_code=500)
        self.assertEqual(str(err), "API down")
        self.assertEqual(err.status_code, 500)

    def test_base_url_trailing_slash_stripped(self):
        client = LLMClient(
            api_key="k",
            base_url="https://api.test.com/v1/",
        )
        self.assertEqual(client.base_url, "https://api.test.com/v1")

    def test_default_model(self):
        client = LLMClient(api_key="k", base_url="https://x.com/v1")
        self.assertEqual(client.default_model, "gpt-4")

        client2 = LLMClient(api_key="k", base_url="https://x.com/v1", default_model="claude")
        self.assertEqual(client2.default_model, "claude")


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

    async def test_session_starts_none(self):
        client = LLMClient(api_key="k", base_url="https://x.com/v1")
        self.assertIsNone(client._session)
        await client.close()  # Should not raise


if __name__ == "__main__":
    unittest.main()

