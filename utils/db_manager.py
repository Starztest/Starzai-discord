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
        await self._migrate_user_context_table()
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

    async def _migrate_user_context_table(self) -> None:
        """Ensure user_context uses (user_id, guild_id) as the primary key."""
        async with self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_context'"
        ) as cur:
            exists = await cur.fetchone()
        if not exists:
            return

        async with self.db.execute("PRAGMA table_info(user_context)") as cur:
            cols = await cur.fetchall()

        pk_cols = [col["name"] for col in cols if col["pk"]]
        if pk_cols != ["user_id"]:
            return

        logger.info(
            "Migrating user_context table to composite primary key (user_id, guild_id)"
        )
        await self.db.executescript(
            """
            ALTER TABLE user_context RENAME TO user_context_old;

            CREATE TABLE user_context (
                user_id             TEXT    NOT NULL,
                guild_id            TEXT    NOT NULL,
                recent_messages     TEXT    DEFAULT '[]',
                personality_summary TEXT    DEFAULT NULL,
                interests           TEXT    DEFAULT '[]',
                last_updated        TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, guild_id)
            );

            INSERT INTO user_context
                (user_id, guild_id, recent_messages, personality_summary, interests, last_updated)
            SELECT
                user_id,
                COALESCE(guild_id, '0') AS guild_id,
                recent_messages,
                personality_summary,
                interests,
                last_updated
            FROM user_context_old;

            DROP TABLE user_context_old;

            CREATE INDEX IF NOT EXISTS idx_user_context
                ON user_context(user_id, guild_id);
            """
        )
        await self.db.commit()

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

            CREATE TABLE IF NOT EXISTS user_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         TEXT    NOT NULL,
                guild_id        TEXT    NOT NULL,
                channel_id      TEXT    NOT NULL,
                message_content TEXT    NOT NULL,
                timestamp       TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_context (
                user_id             TEXT    NOT NULL,
                guild_id            TEXT    NOT NULL,
                recent_messages     TEXT    DEFAULT '[]',
                personality_summary TEXT    DEFAULT NULL,
                interests           TEXT    DEFAULT '[]',
                last_updated        TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, guild_id)
            );

            CREATE TABLE IF NOT EXISTS user_privacy (
                user_id         TEXT    PRIMARY KEY,
                data_collection INTEGER DEFAULT 1,
                opted_out_at    TEXT    DEFAULT NULL,
                created_at      TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS bot_identities (
                user_id         TEXT    NOT NULL,
                guild_id        TEXT    NOT NULL,
                bot_name        TEXT    NOT NULL,
                relationship    TEXT    DEFAULT 'assistant',
                created_at      TEXT    DEFAULT (datetime('now')),
                updated_at      TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, guild_id)
            );

            CREATE TABLE IF NOT EXISTS user_analyses (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                target_user_id  TEXT    NOT NULL,
                guild_id        TEXT    NOT NULL,
                analyzer_user_id TEXT   NOT NULL,
                analysis_data   TEXT    NOT NULL,
                message_count   INTEGER DEFAULT 0,
                date_range      TEXT    DEFAULT NULL,
                created_at      TEXT    DEFAULT (datetime('now')),
                UNIQUE(target_user_id, guild_id, analyzer_user_id)
            );


            CREATE TABLE IF NOT EXISTS analysis_opt_in (
                user_id         TEXT    NOT NULL,
                guild_id        TEXT    NOT NULL,
                opted_in        INTEGER DEFAULT 0,
                created_at      TEXT    DEFAULT (datetime('now')),
                updated_at      TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, guild_id)
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_user
                ON conversations(user_id, active);
            CREATE INDEX IF NOT EXISTS idx_usage_logs_user
                ON usage_logs(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_usage_logs_guild
                ON usage_logs(guild_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_user_messages
                ON user_messages(user_id, guild_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_user_context
                ON user_context(user_id, guild_id);
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

    # ── User Messages & Personalization ──────────────────────────────

    async def store_user_message(
        self, user_id: str, guild_id: str, channel_id: str, content: str
    ) -> None:
        """Store a user message for personalization."""
        # Check if user has opted out
        async with self.db.execute(
            "SELECT data_collection FROM user_privacy WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if row and row["data_collection"] == 0:
                return  # User has opted out

        await self.db.execute(
            "INSERT INTO user_messages (user_id, guild_id, channel_id, message_content) VALUES (?, ?, ?, ?)",
            (user_id, guild_id, channel_id, content),
        )
        await self.db.commit()

    async def get_recent_messages(
        self, user_id: str, guild_id: str, limit: int = 20
    ) -> List[str]:
        """Get recent messages from a user."""
        async with self.db.execute(
            "SELECT message_content FROM user_messages WHERE user_id = ? AND guild_id = ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, guild_id, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [row["message_content"] for row in rows]

    async def update_user_context(
        self, user_id: str, guild_id: str, recent_messages: List[str]
    ) -> None:
        """Update user context with recent messages."""
        await self.db.execute(
            """INSERT INTO user_context (user_id, guild_id, recent_messages, last_updated)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(user_id, guild_id) DO UPDATE SET
                   recent_messages = excluded.recent_messages,
                   last_updated = excluded.last_updated""",
            (user_id, guild_id, json.dumps(recent_messages)),
        )
        await self.db.commit()

    async def get_user_context(
        self, user_id: str, guild_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get user context for personalization."""
        async with self.db.execute(
            "SELECT recent_messages, personality_summary, interests FROM user_context WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {
                    "recent_messages": json.loads(row["recent_messages"]),
                    "personality_summary": row["personality_summary"],
                    "interests": json.loads(row["interests"]) if row["interests"] else [],
                }
        return None

    async def delete_user_data(self, user_id: str) -> None:
        """Delete all data for a user (for /forget-me command)."""
        await self.db.execute("DELETE FROM user_messages WHERE user_id = ?", (user_id,))
        await self.db.execute("DELETE FROM user_context WHERE user_id = ?", (user_id,))
        await self.db.execute(
            "INSERT OR REPLACE INTO user_privacy (user_id, data_collection, opted_out_at) VALUES (?, 0, datetime('now'))",
            (user_id,),
        )
        await self.db.commit()

    async def cleanup_old_messages(self, days: int = 30) -> int:
        """Delete messages older than specified days. Returns count of deleted messages."""
        async with self.db.execute(
            "DELETE FROM user_messages WHERE timestamp < datetime('now', ? || ' days')",
            (f"-{days}",),
        ) as cur:
            await self.db.commit()
            return cur.rowcount if cur.rowcount else 0

    # ── Bot Identity & Personalization ───────────────────────────────

    async def set_bot_identity(
        self, user_id: str, guild_id: str, bot_name: str, relationship: str = "assistant"
    ) -> None:
        """Set personalized bot identity for a user."""
        await self.db.execute(
            """INSERT INTO bot_identities (user_id, guild_id, bot_name, relationship, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(user_id, guild_id) DO UPDATE SET
                   bot_name = excluded.bot_name,
                   relationship = excluded.relationship,
                   updated_at = excluded.updated_at""",
            (user_id, guild_id, bot_name, relationship),
        )
        await self.db.commit()

    async def get_bot_identity(
        self, user_id: str, guild_id: str
    ) -> Optional[Dict[str, str]]:
        """Get personalized bot identity for a user."""
        async with self.db.execute(
            "SELECT bot_name, relationship FROM bot_identities WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {"bot_name": row["bot_name"], "relationship": row["relationship"]}
        return None

    # ── Deep Message Search ──────────────────────────────────────────

    async def search_user_messages(
        self,
        user_id: str,
        guild_id: str,
        limit: int = 100,
        days_back: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Deep search for user messages with optional time range.
        Returns messages with timestamps for analysis.
        """
        query = """
            SELECT message_content, channel_id, timestamp
            FROM user_messages
            WHERE user_id = ? AND guild_id = ?
        """
        params: list = [user_id, guild_id]

        if days_back:
            query += " AND timestamp >= datetime('now', ? || ' days')"
            params.append(f"-{days_back}")

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with self.db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "content": row["message_content"],
                    "channel_id": row["channel_id"],
                    "timestamp": row["timestamp"],
                }
                for row in rows
            ]

    async def get_message_count(
        self, user_id: str, guild_id: str, days_back: Optional[int] = None
    ) -> int:
        """Get total message count for a user."""
        query = "SELECT COUNT(*) as cnt FROM user_messages WHERE user_id = ? AND guild_id = ?"
        params: list = [user_id, guild_id]

        if days_back:
            query += " AND timestamp >= datetime('now', ? || ' days')"
            params.append(f"-{days_back}")

        async with self.db.execute(query, params) as cur:
            row = await cur.fetchone()
            return row["cnt"] if row else 0

    # ── User Analysis Storage ────────────────────────────────────────

    async def store_user_analysis(
        self,
        target_user_id: str,
        guild_id: str,
        analyzer_user_id: str,
        analysis_data: Dict[str, Any],
        message_count: int,
        date_range: str,
    ) -> None:
        """Store comprehensive user analysis."""
        await self.db.execute(
            """INSERT INTO user_analyses 
               (target_user_id, guild_id, analyzer_user_id, analysis_data, message_count, date_range)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(target_user_id, guild_id, analyzer_user_id) DO UPDATE SET
                   analysis_data = excluded.analysis_data,
                   message_count = excluded.message_count,
                   date_range = excluded.date_range,
                   created_at = datetime('now')""",
            (
                target_user_id,
                guild_id,
                analyzer_user_id,
                json.dumps(analysis_data),
                message_count,
                date_range,
            ),
        )
        await self.db.commit()

    async def get_user_analysis(
        self, target_user_id: str, guild_id: str, analyzer_user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve stored user analysis."""
        async with self.db.execute(
            """SELECT analysis_data, message_count, date_range, created_at
               FROM user_analyses
               WHERE target_user_id = ? AND guild_id = ? AND analyzer_user_id = ?""",
            (target_user_id, guild_id, analyzer_user_id),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {
                    "analysis": json.loads(row["analysis_data"]),
                    "message_count": row["message_count"],
                    "date_range": row["date_range"],
                    "created_at": row["created_at"],
                }
        return None

    async def set_analysis_opt_in(self, user_id: str, guild_id: str, opted_in: bool) -> None:
        """Set user's analysis opt-in preference."""
        await self.db.execute(
            """
            INSERT INTO analysis_opt_in (user_id, guild_id, opted_in, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, guild_id) DO UPDATE SET
                opted_in = excluded.opted_in,
                updated_at = datetime('now')
            """,
            (user_id, guild_id, 1 if opted_in else 0),
        )
        await self.db.commit()

    async def get_analysis_opt_in(self, user_id: str, guild_id: str) -> bool:
        """Check if user has opted in to analysis features."""
        async with self.db.execute(
            "SELECT opted_in FROM analysis_opt_in WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row["opted_in"]) if row else False
