"""
Multi-level rate limiter: per-user, per-server, global, and token-based.
Uses in-memory caches with TTL for token quotas.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from cachetools import TTLCache


@dataclass
class RateLimitResult:
    """Result of a rate-limit check."""

    allowed: bool
    retry_after: float = 0.0  # seconds until the bucket resets
    reason: str = ""


class _SlidingWindowBucket:
    """Track timestamps within a sliding window."""

    def __init__(self, max_requests: int, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window = window_seconds
        self.timestamps: list[float] = []

    def acquire(self) -> RateLimitResult:
        now = time.monotonic()
        # Purge expired timestamps
        cutoff = now - self.window
        self.timestamps = [t for t in self.timestamps if t > cutoff]

        if len(self.timestamps) >= self.max_requests:
            oldest = self.timestamps[0]
            retry_after = oldest + self.window - now
            return RateLimitResult(
                allowed=False,
                retry_after=max(retry_after, 0.1),
                reason="Too many requests",
            )

        self.timestamps.append(now)
        return RateLimitResult(allowed=True)


class RateLimiter:
    """
    Multi-level rate limiter with per-user, per-server, and global buckets.

    Parameters
    ----------
    user_limit : int
        Max requests per user per minute.
    expensive_limit : int
        Max expensive-command requests per user per minute.
    server_limit : int
        Max requests per server per minute.
    global_limit : int
        Max requests globally per minute.
    daily_token_limit_user : int
        Max tokens per user per day.
    daily_token_limit_server : int
        Max tokens per server per day.
    """

    def __init__(
        self,
        user_limit: int = 10,
        expensive_limit: int = 5,
        server_limit: int = 100,
        global_limit: int = 200,
        daily_token_limit_user: int = 50_000,
        daily_token_limit_server: int = 500_000,
    ):
        self.user_limit = user_limit
        self.expensive_limit = expensive_limit
        self.server_limit = server_limit
        self.global_limit = global_limit
        self.daily_token_limit_user = daily_token_limit_user
        self.daily_token_limit_server = daily_token_limit_server

        # In-memory caches (auto-expire after 2 minutes for buckets, 24h for tokens)
        self._user_buckets: TTLCache[int, _SlidingWindowBucket] = TTLCache(
            maxsize=10_000, ttl=120
        )
        self._expensive_buckets: TTLCache[int, _SlidingWindowBucket] = TTLCache(
            maxsize=10_000, ttl=120
        )
        self._server_buckets: TTLCache[int, _SlidingWindowBucket] = TTLCache(
            maxsize=5_000, ttl=120
        )
        self._global_bucket = _SlidingWindowBucket(global_limit, 60.0)

        # Daily token tracking (TTL = 24h)
        self._user_tokens: TTLCache[int, int] = TTLCache(maxsize=10_000, ttl=86_400)
        self._server_tokens: TTLCache[int, int] = TTLCache(maxsize=5_000, ttl=86_400)

    # ── Bucket Helpers ───────────────────────────────────────────────

    def _get_user_bucket(self, user_id: int) -> _SlidingWindowBucket:
        if user_id not in self._user_buckets:
            self._user_buckets[user_id] = _SlidingWindowBucket(self.user_limit)
        return self._user_buckets[user_id]

    def _get_expensive_bucket(self, user_id: int) -> _SlidingWindowBucket:
        if user_id not in self._expensive_buckets:
            self._expensive_buckets[user_id] = _SlidingWindowBucket(
                self.expensive_limit
            )
        return self._expensive_buckets[user_id]

    def _get_server_bucket(self, server_id: int) -> _SlidingWindowBucket:
        if server_id not in self._server_buckets:
            self._server_buckets[server_id] = _SlidingWindowBucket(self.server_limit)
        return self._server_buckets[server_id]

    # ── Public API ───────────────────────────────────────────────────

    def check(
        self,
        user_id: int,
        server_id: Optional[int] = None,
        expensive: bool = False,
    ) -> RateLimitResult:
        """
        Check all rate limit layers. Returns the first failure or success.
        """
        # 1. Global
        result = self._global_bucket.acquire()
        if not result.allowed:
            result.reason = "Global rate limit reached"
            return result

        # 2. Server
        if server_id is not None:
            result = self._get_server_bucket(server_id).acquire()
            if not result.allowed:
                result.reason = "Server rate limit reached"
                return result

        # 3. User (general)
        result = self._get_user_bucket(user_id).acquire()
        if not result.allowed:
            result.reason = "User rate limit reached"
            return result

        # 4. Expensive command check
        if expensive:
            result = self._get_expensive_bucket(user_id).acquire()
            if not result.allowed:
                result.reason = "Rate limit for AI commands reached"
                return result

        return RateLimitResult(allowed=True)

    def check_token_budget(
        self,
        user_id: int,
        server_id: Optional[int] = None,
        estimated_tokens: int = 0,
    ) -> RateLimitResult:
        """Check if user/server has remaining daily token budget."""
        estimated = max(0, int(estimated_tokens or 0))

        user_limit = int(self.daily_token_limit_user or 0)
        if user_limit > 0:
            used = self._user_tokens.get(user_id, 0)
            if used + estimated > user_limit:
                return RateLimitResult(
                    allowed=False,
                    reason=(
                        f"User daily token limit reached ({used}/{user_limit}). "
                        "Try again later."
                    ),
                )

        if server_id is not None:
            server_limit = int(self.daily_token_limit_server or 0)
            if server_limit > 0:
                used = self._server_tokens.get(server_id, 0)
                if used + estimated > server_limit:
                    return RateLimitResult(
                        allowed=False,
                        reason=(
                            f"Server daily token limit reached ({used}/{server_limit}). "
                            "Try again later."
                        ),
                    )

        return RateLimitResult(allowed=True)

    def record_tokens(
        self,
        user_id: int,
        tokens: int,
        server_id: Optional[int] = None,
    ) -> None:
        """Record token usage after a successful API call."""
        tokens = max(0, int(tokens or 0))
        self._user_tokens[user_id] = self._user_tokens.get(user_id, 0) + tokens
        if server_id is not None:
            self._server_tokens[server_id] = (
                self._server_tokens.get(server_id, 0) + tokens
            )

    def get_user_usage(self, user_id: int) -> dict:
        """Return current usage stats for a user."""
        return {
            "tokens_today": self._user_tokens.get(user_id, 0),
            "token_limit": self.daily_token_limit_user,
        }
