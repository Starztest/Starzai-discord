"""
Async SQLite database manager for user data, conversations, and analytics.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Set

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

            -- ── Music Premium: Favorites ────────────────────────────
            CREATE TABLE IF NOT EXISTS user_favorites (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT    NOT NULL,
                song_data   TEXT    NOT NULL,
                added_at    TEXT    DEFAULT (datetime('now')),
                UNIQUE(user_id, song_data)
            );

            -- ── Music Premium: Playlists ────────────────────────────
            CREATE TABLE IF NOT EXISTS user_playlists (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                description TEXT    DEFAULT '',
                created_at  TEXT    DEFAULT (datetime('now')),
                updated_at  TEXT    DEFAULT (datetime('now')),
                UNIQUE(user_id, name)
            );

            CREATE TABLE IF NOT EXISTS playlist_songs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                song_data   TEXT    NOT NULL,
                position    INTEGER NOT NULL DEFAULT 0,
                added_at    TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (playlist_id) REFERENCES user_playlists(id) ON DELETE CASCADE
            );

            -- ── Music Premium: Listening Profiles ───────────────────
            CREATE TABLE IF NOT EXISTS music_profiles (
                user_id                 TEXT    PRIMARY KEY,
                total_listening_seconds REAL    DEFAULT 0,
                total_songs_played      INTEGER DEFAULT 0,
                created_at              TEXT    DEFAULT (datetime('now')),
                updated_at              TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS listening_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         TEXT    NOT NULL,
                guild_id        TEXT    NOT NULL,
                song_data       TEXT    NOT NULL,
                listened_seconds REAL   DEFAULT 0,
                played_at       TEXT    DEFAULT (datetime('now'))
            );

            -- ── Music Premium: Song Request Channels ────────────────
            CREATE TABLE IF NOT EXISTS song_request_channels (
                guild_id        TEXT    PRIMARY KEY,
                channel_id      TEXT    NOT NULL,
                configured_by   TEXT    NOT NULL,
                configured_at   TEXT    DEFAULT (datetime('now'))
            );

            -- ── Music Premium: Sleep Timers (persisted per-guild) ───
            CREATE TABLE IF NOT EXISTS sleep_timers (
                guild_id        TEXT    PRIMARY KEY,
                expires_at      TEXT    NOT NULL,
                set_by          TEXT    NOT NULL,
                created_at      TEXT    DEFAULT (datetime('now'))
            );

            -- ── Allowed Guilds (persistent across deploys) ────────
            CREATE TABLE IF NOT EXISTS allowed_guilds (
                guild_id    INTEGER PRIMARY KEY,
                allowed_by  TEXT    DEFAULT NULL,
                allowed_at  TEXT    DEFAULT (datetime('now'))
            );

            -- ── Auto-News Channels ──────────────────────────────────
            CREATE TABLE IF NOT EXISTS news_channels (
                guild_id            TEXT    PRIMARY KEY,
                channel_id          TEXT    NOT NULL,
                topic               TEXT    NOT NULL,
                interval_minutes    INTEGER DEFAULT 30,
                enabled             INTEGER DEFAULT 1,
                last_sent_at        TEXT    DEFAULT NULL,
                last_sent_urls      TEXT    DEFAULT '[]',
                configured_by       TEXT    NOT NULL,
                configured_at       TEXT    DEFAULT (datetime('now'))
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
            CREATE INDEX IF NOT EXISTS idx_user_favorites_user
                ON user_favorites(user_id, added_at);
            CREATE INDEX IF NOT EXISTS idx_playlist_songs_playlist
                ON playlist_songs(playlist_id, position);
            CREATE INDEX IF NOT EXISTS idx_listening_history_user
                ON listening_history(user_id, played_at);
            CREATE INDEX IF NOT EXISTS idx_listening_history_guild
                ON listening_history(user_id, guild_id, played_at);
            CREATE INDEX IF NOT EXISTS idx_news_channels_enabled
                ON news_channels(enabled, last_sent_at);
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

    # ══════════════════════════════════════════════════════════════════
    #  Music Premium — Favorites
    # ══════════════════════════════════════════════════════════════════

    async def add_favorite(self, user_id: str, song_data: str) -> bool:
        """Add a song to user's favorites. Returns True if added, False if duplicate."""
        try:
            await self.db.execute(
                "INSERT OR IGNORE INTO user_favorites (user_id, song_data) VALUES (?, ?)",
                (user_id, song_data),
            )
            await self.db.commit()
            return True
        except Exception:
            return False

    async def remove_favorite(self, user_id: str, song_data: str) -> bool:
        """Remove a song from favorites. Returns True if removed."""
        cur = await self.db.execute(
            "DELETE FROM user_favorites WHERE user_id = ? AND song_data = ?",
            (user_id, song_data),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def remove_favorite_by_id(self, user_id: str, fav_id: int) -> bool:
        """Remove a favorite by its row ID."""
        cur = await self.db.execute(
            "DELETE FROM user_favorites WHERE id = ? AND user_id = ?",
            (fav_id, user_id),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def is_favorite(self, user_id: str, song_data: str) -> bool:
        """Check if a song is in the user's favorites."""
        async with self.db.execute(
            "SELECT 1 FROM user_favorites WHERE user_id = ? AND song_data = ?",
            (user_id, song_data),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_favorites(
        self, user_id: str, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Get user's favorite songs (newest first)."""
        async with self.db.execute(
            "SELECT id, song_data, added_at FROM user_favorites WHERE user_id = ? ORDER BY added_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ) as cur:
            rows = await cur.fetchall()
            results = []
            for row in rows:
                try:
                    song = json.loads(row["song_data"])
                    song["_fav_id"] = row["id"]
                    song["_added_at"] = row["added_at"]
                    results.append(song)
                except json.JSONDecodeError:
                    continue
            return results

    async def get_favorites_count(self, user_id: str) -> int:
        """Get total number of favorites for a user."""
        async with self.db.execute(
            "SELECT COUNT(*) as cnt FROM user_favorites WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row["cnt"] if row else 0

    # ══════════════════════════════════════════════════════════════════
    #  Music Premium — Playlists
    # ══════════════════════════════════════════════════════════════════

    async def create_playlist(self, user_id: str, name: str, description: str = "") -> Optional[int]:
        """Create a new playlist. Returns playlist ID or None if name already exists."""
        try:
            cur = await self.db.execute(
                "INSERT INTO user_playlists (user_id, name, description) VALUES (?, ?, ?)",
                (user_id, name, description),
            )
            await self.db.commit()
            return cur.lastrowid
        except Exception:
            return None

    async def delete_playlist(self, user_id: str, playlist_id: int) -> bool:
        """Delete a playlist and all its songs."""
        cur = await self.db.execute(
            "DELETE FROM user_playlists WHERE id = ? AND user_id = ?",
            (playlist_id, user_id),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def rename_playlist(self, user_id: str, playlist_id: int, new_name: str) -> bool:
        """Rename a playlist."""
        try:
            cur = await self.db.execute(
                "UPDATE user_playlists SET name = ?, updated_at = datetime('now') WHERE id = ? AND user_id = ?",
                (new_name, playlist_id, user_id),
            )
            await self.db.commit()
            return (cur.rowcount or 0) > 0
        except Exception:
            return False

    async def get_playlists(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all playlists for a user."""
        async with self.db.execute(
            """SELECT p.id, p.name, p.description, p.created_at, p.updated_at,
                      (SELECT COUNT(*) FROM playlist_songs WHERE playlist_id = p.id) as song_count
               FROM user_playlists p WHERE p.user_id = ? ORDER BY p.updated_at DESC""",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def get_playlist(self, user_id: str, playlist_id: int) -> Optional[Dict[str, Any]]:
        """Get a single playlist with metadata."""
        async with self.db.execute(
            """SELECT p.id, p.name, p.description, p.created_at, p.updated_at,
                      (SELECT COUNT(*) FROM playlist_songs WHERE playlist_id = p.id) as song_count
               FROM user_playlists p WHERE p.id = ? AND p.user_id = ?""",
            (playlist_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_playlist_by_name(self, user_id: str, name: str) -> Optional[Dict[str, Any]]:
        """Get a playlist by name."""
        async with self.db.execute(
            """SELECT p.id, p.name, p.description, p.created_at, p.updated_at,
                      (SELECT COUNT(*) FROM playlist_songs WHERE playlist_id = p.id) as song_count
               FROM user_playlists p WHERE p.user_id = ? AND LOWER(p.name) = LOWER(?)""",
            (user_id, name),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def add_song_to_playlist(self, playlist_id: int, song_data: str) -> bool:
        """Add a song to a playlist at the end."""
        try:
            # Get next position
            async with self.db.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 as next_pos FROM playlist_songs WHERE playlist_id = ?",
                (playlist_id,),
            ) as cur:
                row = await cur.fetchone()
                pos = row["next_pos"] if row else 0

            await self.db.execute(
                "INSERT INTO playlist_songs (playlist_id, song_data, position) VALUES (?, ?, ?)",
                (playlist_id, song_data, pos),
            )
            await self.db.execute(
                "UPDATE user_playlists SET updated_at = datetime('now') WHERE id = ?",
                (playlist_id,),
            )
            await self.db.commit()
            return True
        except Exception:
            return False

    async def remove_song_from_playlist(self, playlist_id: int, position: int) -> bool:
        """Remove a song from a playlist by position (0-based)."""
        cur = await self.db.execute(
            "DELETE FROM playlist_songs WHERE playlist_id = ? AND position = ?",
            (playlist_id, position),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def get_playlist_songs(self, playlist_id: int) -> List[Dict[str, Any]]:
        """Get all songs in a playlist, ordered by position."""
        async with self.db.execute(
            "SELECT id, song_data, position, added_at FROM playlist_songs WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        ) as cur:
            rows = await cur.fetchall()
            results = []
            for row in rows:
                try:
                    song = json.loads(row["song_data"])
                    song["_position"] = row["position"]
                    results.append(song)
                except json.JSONDecodeError:
                    continue
            return results

    async def clear_playlist(self, playlist_id: int) -> int:
        """Remove all songs from a playlist. Returns count removed."""
        cur = await self.db.execute(
            "DELETE FROM playlist_songs WHERE playlist_id = ?",
            (playlist_id,),
        )
        await self.db.commit()
        return cur.rowcount or 0

    async def get_playlist_song_keys(self, playlist_id: int) -> set:
        """Return a set of song_data JSON strings already in a playlist."""
        async with self.db.execute(
            "SELECT song_data FROM playlist_songs WHERE playlist_id = ?",
            (playlist_id,),
        ) as cur:
            rows = await cur.fetchall()
            return {row["song_data"] for row in rows}

    async def update_playlist_timestamp(self, playlist_id: int) -> None:
        """Touch a playlist's updated_at without changing its contents."""
        await self.db.execute(
            "UPDATE user_playlists SET updated_at = datetime('now') WHERE id = ?",
            (playlist_id,),
        )
        await self.db.commit()

    # ══════════════════════════════════════════════════════════════════
    #  Music Premium — Listening Profiles & History
    # ══════════════════════════════════════════════════════════════════

    async def log_listening_session(
        self, user_id: str, guild_id: str, song_data: str, listened_seconds: float
    ) -> None:
        """Log a listening session for a user."""
        await self.db.execute(
            "INSERT INTO listening_history (user_id, guild_id, song_data, listened_seconds) VALUES (?, ?, ?, ?)",
            (user_id, guild_id, song_data, listened_seconds),
        )
        # Upsert the profile aggregate
        await self.db.execute(
            """INSERT INTO music_profiles (user_id, total_listening_seconds, total_songs_played)
               VALUES (?, ?, 1)
               ON CONFLICT(user_id) DO UPDATE SET
                   total_listening_seconds = total_listening_seconds + excluded.total_listening_seconds,
                   total_songs_played = total_songs_played + 1,
                   updated_at = datetime('now')""",
            (user_id, listened_seconds),
        )
        await self.db.commit()

    async def get_music_profile(self, user_id: str) -> Dict[str, Any]:
        """Get a user's music profile stats."""
        profile: Dict[str, Any] = {
            "total_listening_seconds": 0,
            "total_songs_played": 0,
            "top_artists": [],
            "top_songs": [],
            "recent_songs": [],
        }

        # Basic stats
        async with self.db.execute(
            "SELECT total_listening_seconds, total_songs_played, created_at FROM music_profiles WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                profile["total_listening_seconds"] = row["total_listening_seconds"]
                profile["total_songs_played"] = row["total_songs_played"]
                profile["member_since"] = row["created_at"]

        # Recent songs (last 10)
        async with self.db.execute(
            "SELECT song_data, listened_seconds, played_at FROM listening_history WHERE user_id = ? ORDER BY played_at DESC LIMIT 10",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            for row in rows:
                try:
                    song = json.loads(row["song_data"])
                    song["_listened"] = row["listened_seconds"]
                    song["_played_at"] = row["played_at"]
                    profile["recent_songs"].append(song)
                except json.JSONDecodeError:
                    continue

        # Top songs (by play count)
        async with self.db.execute(
            """SELECT song_data, COUNT(*) as plays, SUM(listened_seconds) as total_time
               FROM listening_history WHERE user_id = ?
               GROUP BY song_data ORDER BY plays DESC LIMIT 10""",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            for row in rows:
                try:
                    song = json.loads(row["song_data"])
                    song["_plays"] = row["plays"]
                    song["_total_time"] = row["total_time"]
                    profile["top_songs"].append(song)
                except json.JSONDecodeError:
                    continue

        # Top artists (by play count)
        async with self.db.execute(
            """SELECT song_data, COUNT(*) as plays
               FROM listening_history WHERE user_id = ?
               GROUP BY song_data ORDER BY plays DESC LIMIT 50""",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            artist_counts: Dict[str, int] = {}
            for row in rows:
                try:
                    song = json.loads(row["song_data"])
                    artist = song.get("artist", "Unknown")
                    artist_counts[artist] = artist_counts.get(artist, 0) + row["plays"]
                except json.JSONDecodeError:
                    continue
            # Sort by count, take top 5
            sorted_artists = sorted(artist_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            profile["top_artists"] = [{"name": a, "plays": c} for a, c in sorted_artists]

        return profile

    # ══════════════════════════════════════════════════════════════════
    #  Music Premium — Song Request Channels
    # ══════════════════════════════════════════════════════════════════

    async def set_request_channel(self, guild_id: str, channel_id: str, configured_by: str) -> None:
        """Set the song request channel for a guild."""
        await self.db.execute(
            """INSERT INTO song_request_channels (guild_id, channel_id, configured_by)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   channel_id = excluded.channel_id,
                   configured_by = excluded.configured_by,
                   configured_at = datetime('now')""",
            (guild_id, channel_id, configured_by),
        )
        await self.db.commit()

    async def get_request_channel(self, guild_id: str) -> Optional[str]:
        """Get the song request channel ID for a guild."""
        async with self.db.execute(
            "SELECT channel_id FROM song_request_channels WHERE guild_id = ?",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
            return row["channel_id"] if row else None

    async def remove_request_channel(self, guild_id: str) -> bool:
        """Remove the song request channel for a guild."""
        cur = await self.db.execute(
            "DELETE FROM song_request_channels WHERE guild_id = ?",
            (guild_id,),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    # ── Allowed Guilds ────────────────────────────────────────────────

    async def get_allowed_guilds(self) -> Set[int]:
        """Load all allowed guild IDs from the database."""
        async with self.db.execute("SELECT guild_id FROM allowed_guilds") as cur:
            rows = await cur.fetchall()
            return {row["guild_id"] for row in rows}

    async def add_allowed_guild(self, guild_id: int, allowed_by: str = "") -> None:
        """Add a guild to the allowlist."""
        await self.db.execute(
            """INSERT OR IGNORE INTO allowed_guilds (guild_id, allowed_by)
               VALUES (?, ?)""",
            (guild_id, allowed_by),
        )
        await self.db.commit()

    async def remove_allowed_guild(self, guild_id: int) -> bool:
        """Remove a guild from the allowlist. Returns True if removed."""
        cur = await self.db.execute(
            "DELETE FROM allowed_guilds WHERE guild_id = ?", (guild_id,),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def migrate_allowed_guilds_from_json(self, json_path: str) -> int:
        """One-time migration: import allowed_guilds.json into the DB.

        Returns the number of guilds imported.
        """
        import os
        if not os.path.exists(json_path):
            return 0
        try:
            with open(json_path) as f:
                data = json.load(f)
            if not isinstance(data, list):
                return 0
            count = 0
            for gid in data:
                await self.db.execute(
                    "INSERT OR IGNORE INTO allowed_guilds (guild_id, allowed_by) VALUES (?, ?)",
                    (int(gid), "migrated_from_json"),
                )
                count += 1
            await self.db.commit()
            # Rename old file so migration only runs once
            os.rename(json_path, json_path + ".migrated")
            return count
        except Exception as exc:
            logger.warning("Failed to migrate allowed_guilds.json: %s", exc)
            return 0

    # ── Auto-News Channels ────────────────────────────────────────────

    async def set_news_channel(
        self,
        guild_id: str,
        channel_id: str,
        topic: str,
        interval_minutes: int,
        configured_by: str,
    ) -> None:
        """Set or update the auto-news channel for a guild."""
        await self.db.execute(
            """INSERT INTO news_channels
                   (guild_id, channel_id, topic, interval_minutes, configured_by)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   channel_id       = excluded.channel_id,
                   topic            = excluded.topic,
                   interval_minutes = excluded.interval_minutes,
                   configured_by    = excluded.configured_by,
                   enabled          = 1,
                   configured_at    = datetime('now')
            """,
            (guild_id, channel_id, topic, interval_minutes, configured_by),
        )
        await self.db.commit()

    async def get_news_channel(self, guild_id: str) -> Optional[Dict[str, Any]]:
        """Get the auto-news config for a guild."""
        async with self.db.execute(
            "SELECT * FROM news_channels WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_all_active_news_channels(self) -> List[Dict[str, Any]]:
        """Get all enabled auto-news channel configs."""
        async with self.db.execute(
            "SELECT * FROM news_channels WHERE enabled = 1"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def remove_news_channel(self, guild_id: str) -> bool:
        """Remove the auto-news channel for a guild."""
        cur = await self.db.execute(
            "DELETE FROM news_channels WHERE guild_id = ?", (guild_id,),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def toggle_news_channel(self, guild_id: str, enabled: bool) -> bool:
        """Enable or disable auto-news for a guild."""
        cur = await self.db.execute(
            "UPDATE news_channels SET enabled = ? WHERE guild_id = ?",
            (1 if enabled else 0, guild_id),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def update_news_last_sent(
        self, guild_id: str, sent_urls: Optional[List[str]] = None,
    ) -> None:
        """Update last_sent_at and optionally the dedup URL list."""
        import json
        if sent_urls is not None:
            # Keep only the last 50 URLs for dedup
            urls_json = json.dumps(sent_urls[-50:])
            await self.db.execute(
                """UPDATE news_channels
                   SET last_sent_at = datetime('now'), last_sent_urls = ?
                   WHERE guild_id = ?""",
                (urls_json, guild_id),
            )
        else:
            await self.db.execute(
                "UPDATE news_channels SET last_sent_at = datetime('now') WHERE guild_id = ?",
                (guild_id,),
            )
        await self.db.commit()
