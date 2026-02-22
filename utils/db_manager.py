"""
Async SQLite database manager for user data, conversations, and analytics.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "data/starzai.db")


class DatabaseManager:
    """Async SQLite wrapper for all bot persistence."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    # ── Lifecycle ────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Open the database and create tables if needed."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        logger.info("Database initialized at %s", self.db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            logger.info("Database connection closed")

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._db

    # ── Schema ───────────────────────────────────────────────────────

    async def _create_tables(self) -> None:
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id         INTEGER PRIMARY KEY,
                preferred_model TEXT    DEFAULT NULL,
                total_tokens    INTEGER DEFAULT 0,
                created_at      TEXT    DEFAULT (datetime('now')),
                updated_at      TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                guild_id    INTEGER,
                messages    TEXT    DEFAULT '[]',
                model_used  TEXT,
                active      INTEGER DEFAULT 1,
                created_at  TEXT    DEFAULT (datetime('now')),
                updated_at  TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS servers (
                guild_id            INTEGER PRIMARY KEY,
                rate_limit_override INTEGER DEFAULT NULL,
                disabled_features   TEXT    DEFAULT '[]',
                created_at          TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS usage_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                guild_id        INTEGER,
                command         TEXT    NOT NULL,
                model           TEXT,
                tokens_used     INTEGER DEFAULT 0,
                latency_ms      REAL    DEFAULT 0,
                success         INTEGER DEFAULT 1,
                error_message   TEXT,
                created_at      TEXT    DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_user
                ON conversations(user_id, active);
            CREATE INDEX IF NOT EXISTS idx_usage_logs_user
                ON usage_logs(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_usage_logs_guild
                ON usage_logs(guild_id, created_at);
            """
        )
        await self.db.commit()

    # ── Users ────────────────────────────────────────────────────────

    async def ensure_user(self, user_id: int) -> None:
        """Insert user row if it doesn't exist."""
        await self.db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        await self.db.commit()

    async def get_user_model(self, user_id: int) -> Optional[str]:
        async with self.db.execute(
            "SELECT preferred_model FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row["preferred_model"] if row else None

    async def set_user_model(self, user_id: int, model: str) -> None:
        await self.ensure_user(user_id)
        await self.db.execute(
            "UPDATE users SET preferred_model = ?, updated_at = datetime('now') WHERE user_id = ?",
            (model, user_id),
        )
        await self.db.commit()

    async def add_user_tokens(self, user_id: int, tokens: int) -> None:
        await self.ensure_user(user_id)
        await self.db.execute(
            "UPDATE users SET total_tokens = total_tokens + ?, updated_at = datetime('now') WHERE user_id = ?",
            (tokens, user_id),
        )
        await self.db.commit()

    async def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        async with self.db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
        return {"user_id": user_id, "total_tokens": 0, "preferred_model": None}

    # ── Conversations ────────────────────────────────────────────────

    async def start_conversation(
        self, user_id: int, guild_id: Optional[int] = None, model: Optional[str] = None
    ) -> int:
        """Start a new conversation and return its ID."""
        # End any existing active conversation first
        await self.end_conversation(user_id, guild_id)
        await self.ensure_user(user_id)
        cursor = await self.db.execute(
            "INSERT INTO conversations (user_id, guild_id, model_used) VALUES (?, ?, ?)",
            (user_id, guild_id, model),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_active_conversation(
        self, user_id: int, guild_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Get the active conversation for a user in a guild."""
        query = "SELECT * FROM conversations WHERE user_id = ? AND active = 1"
        params: list = [user_id]
        if guild_id is not None:
            query += " AND guild_id = ?"
            params.append(guild_id)
        query += " ORDER BY updated_at DESC LIMIT 1"

        async with self.db.execute(query, params) as cur:
            row = await cur.fetchone()
            if row:
                data = dict(row)
                data["messages"] = json.loads(data["messages"])
                return data
        return None

    async def append_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        max_messages: int = 10,
    ) -> None:
        """Append a message to a conversation, keeping the last `max_messages`."""
        async with self.db.execute(
            "SELECT messages FROM conversations WHERE id = ?", (conversation_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return
            messages: List[Dict] = json.loads(row["messages"])

        messages.append({"role": role, "content": content})
        # Sliding window: keep the last N messages
        messages = messages[-max_messages:]

        await self.db.execute(
            "UPDATE conversations SET messages = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(messages), conversation_id),
        )
        await self.db.commit()

    async def clear_conversation(self, conversation_id: int) -> None:
        """Clear messages in a conversation."""
        await self.db.execute(
            "UPDATE conversations SET messages = '[]', updated_at = datetime('now') WHERE id = ?",
            (conversation_id,),
        )
        await self.db.commit()

    async def end_conversation(
        self, user_id: int, guild_id: Optional[int] = None
    ) -> None:
        """Deactivate all active conversations for a user in a guild."""
        query = "UPDATE conversations SET active = 0, updated_at = datetime('now') WHERE user_id = ? AND active = 1"
        params: list = [user_id]
        if guild_id is not None:
            query += " AND guild_id = ?"
            params.append(guild_id)
        await self.db.execute(query, params)
        await self.db.commit()

    async def get_conversation_export(self, conversation_id: int) -> str:
        """Export a conversation as a readable text transcript."""
        async with self.db.execute(
            "SELECT messages, model_used, created_at FROM conversations WHERE id = ?",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return "No conversation found."

        messages = json.loads(row["messages"])
        lines = [
            f"# Starzai Conversation Export",
            f"# Model: {row['model_used'] or 'default'}",
            f"# Started: {row['created_at']}",
            "",
        ]
        for msg in messages:
            role = msg["role"].upper()
            lines.append(f"[{role}]")
            lines.append(msg["content"])
            lines.append("")
        return "\n".join(lines)

    # ── Usage Logging ────────────────────────────────────────────────

    async def log_usage(
        self,
        user_id: int,
        command: str,
        *,
        guild_id: Optional[int] = None,
        model: Optional[str] = None,
        tokens_used: int = 0,
        latency_ms: float = 0.0,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO usage_logs
               (user_id, guild_id, command, model, tokens_used, latency_ms, success, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                guild_id,
                command,
                model,
                tokens_used,
                latency_ms,
                1 if success else 0,
                error_message,
            ),
        )
        await self.db.commit()

    async def get_global_stats(self) -> Dict[str, Any]:
        """Return aggregate bot statistics."""
        stats: Dict[str, Any] = {}

        async with self.db.execute("SELECT COUNT(*) as cnt FROM users") as cur:
            row = await cur.fetchone()
            stats["total_users"] = row["cnt"] if row else 0

        async with self.db.execute(
            "SELECT COUNT(*) as cnt, SUM(tokens_used) as tokens FROM usage_logs"
        ) as cur:
            row = await cur.fetchone()
            stats["total_commands"] = row["cnt"] if row else 0
            stats["total_tokens"] = row["tokens"] or 0 if row else 0

        async with self.db.execute(
            "SELECT COUNT(*) as cnt FROM conversations WHERE active = 1"
        ) as cur:
            row = await cur.fetchone()
            stats["active_conversations"] = row["cnt"] if row else 0

        return stats

    # ── Server Settings ──────────────────────────────────────────────

    async def ensure_server(self, guild_id: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO servers (guild_id) VALUES (?)", (guild_id,)
        )
        await self.db.commit()

