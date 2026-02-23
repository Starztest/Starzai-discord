"""
Tests for the database manager.
"""

import asyncio
import os
import tempfile
import unittest

from utils.db_manager import DatabaseManager


class TestDatabaseManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        self.db = DatabaseManager(self.db_path)
        await self.db.initialize()

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    async def test_ensure_user(self):
        await self.db.ensure_user(123)
        stats = await self.db.get_user_stats(123)
        self.assertEqual(stats["user_id"], 123)
        self.assertEqual(stats["total_tokens"], 0)

    async def test_set_get_user_model(self):
        await self.db.set_user_model(456, "gpt-4")
        model = await self.db.get_user_model(456)
        self.assertEqual(model, "gpt-4")

    async def test_conversation_lifecycle(self):
        conv_id = await self.db.start_conversation(1, guild_id=10, model="gpt-4")
        self.assertIsNotNone(conv_id)

        conv = await self.db.get_active_conversation(1, 10)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["model_used"], "gpt-4")

        await self.db.append_message(conv_id, "user", "Hello")
        await self.db.append_message(conv_id, "assistant", "Hi there!")

        conv = await self.db.get_active_conversation(1, 10)
        self.assertEqual(len(conv["messages"]), 2)

        await self.db.end_conversation(1, 10)
        conv = await self.db.get_active_conversation(1, 10)
        self.assertIsNone(conv)

    async def test_conversation_clear(self):
        conv_id = await self.db.start_conversation(2, guild_id=20)
        await self.db.append_message(conv_id, "user", "Test")
        await self.db.clear_conversation(conv_id)

        conv = await self.db.get_active_conversation(2, 20)
        self.assertEqual(conv["messages"], [])

    async def test_conversation_sliding_window(self):
        conv_id = await self.db.start_conversation(3, guild_id=30)
        for i in range(15):
            await self.db.append_message(conv_id, "user", f"Message {i}", max_messages=10)

        conv = await self.db.get_active_conversation(3, 30)
        self.assertEqual(len(conv["messages"]), 10)
        self.assertEqual(conv["messages"][0]["content"], "Message 5")

    async def test_log_usage(self):
        await self.db.log_usage(
            user_id=1, command="chat", guild_id=10,
            model="gpt-4", tokens_used=100, latency_ms=200.0,
        )
        stats = await self.db.get_global_stats()
        self.assertEqual(stats["total_commands"], 1)
        self.assertEqual(stats["total_tokens"], 100)

    async def test_add_user_tokens(self):
        await self.db.add_user_tokens(99, 500)
        await self.db.add_user_tokens(99, 300)
        stats = await self.db.get_user_stats(99)
        self.assertEqual(stats["total_tokens"], 800)

    async def test_conversation_export(self):
        conv_id = await self.db.start_conversation(5, guild_id=50, model="claude")
        await self.db.append_message(conv_id, "user", "Hey!")
        await self.db.append_message(conv_id, "assistant", "Hello!")

        export = await self.db.get_conversation_export(conv_id)
        self.assertIn("[USER]", export)
        self.assertIn("[ASSISTANT]", export)
        self.assertIn("Hey!", export)


if __name__ == "__main__":
    unittest.main()

