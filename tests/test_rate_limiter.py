"""
Tests for the rate limiter.
"""

import unittest
from utils.rate_limiter import RateLimiter


class TestRateLimiter(unittest.TestCase):
    def test_allows_within_limit(self):
        rl = RateLimiter(user_limit=5)
        for _ in range(5):
            result = rl.check(user_id=1)
            self.assertTrue(result.allowed)

    def test_blocks_over_limit(self):
        rl = RateLimiter(user_limit=3)
        for _ in range(3):
            rl.check(user_id=1)
        result = rl.check(user_id=1)
        self.assertFalse(result.allowed)
        self.assertGreater(result.retry_after, 0)

    def test_separate_users(self):
        rl = RateLimiter(user_limit=2)
        rl.check(user_id=1)
        rl.check(user_id=1)
        result_user1 = rl.check(user_id=1)
        result_user2 = rl.check(user_id=2)
        self.assertFalse(result_user1.allowed)
        self.assertTrue(result_user2.allowed)

    def test_expensive_limit(self):
        rl = RateLimiter(user_limit=10, expensive_limit=2)
        rl.check(user_id=1, expensive=True)
        rl.check(user_id=1, expensive=True)
        result = rl.check(user_id=1, expensive=True)
        self.assertFalse(result.allowed)
        self.assertIn("AI commands", result.reason)

    def test_token_budget(self):
        rl = RateLimiter(daily_token_limit_user=100)
        rl.record_tokens(user_id=1, tokens=90)
        result = rl.check_token_budget(user_id=1, estimated_tokens=20)
        self.assertFalse(result.allowed)

    def test_token_budget_ok(self):
        rl = RateLimiter(daily_token_limit_user=100)
        rl.record_tokens(user_id=1, tokens=50)
        result = rl.check_token_budget(user_id=1, estimated_tokens=20)
        self.assertTrue(result.allowed)

    def test_server_token_limit(self):
        rl = RateLimiter(daily_token_limit_server=200)
        rl.record_tokens(user_id=1, tokens=150, server_id=10)
        result = rl.check_token_budget(user_id=1, server_id=10, estimated_tokens=60)
        self.assertFalse(result.allowed)

    def test_get_user_usage(self):
        rl = RateLimiter(daily_token_limit_user=50000)
        rl.record_tokens(user_id=42, tokens=1234)
        usage = rl.get_user_usage(42)
        self.assertEqual(usage["tokens_today"], 1234)
        self.assertEqual(usage["token_limit"], 50000)

    def test_global_limit(self):
        rl = RateLimiter(user_limit=1000, global_limit=3)
        for i in range(3):
            rl.check(user_id=i + 100)
        result = rl.check(user_id=999)
        self.assertFalse(result.allowed)
        self.assertIn("Global", result.reason)


if __name__ == "__main__":
    unittest.main()

