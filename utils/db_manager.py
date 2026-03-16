"""
Async PostgreSQL database manager for user data, conversations, and analytics.
Uses asyncpg to connect to Supabase PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse, urlunparse

import asyncpg

logger = logging.getLogger(__name__)

# All exception types that can occur during asyncpg connection attempts.
# - OSError / TimeoutError: network-level failures
# - asyncpg.PostgresError / InterfaceError: driver-level failures
# - ValueError: raised by Python's urllib.parse when the DSN contains a
#   hostname that looks like a bracketed IPv6 literal but isn't
#   (common with Supabase connection strings on some Python versions).
_CONNECT_ERRORS = (
    OSError,
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    TimeoutError,
    ValueError,
)


def _to_pooler_url(url: str) -> str:
    """Auto-convert a **direct** Supabase connection string to the
    free IPv4-compatible **Session Pooler** format.

    Direct format (IPv6 only — fails on Railway / Render / most VPS):
        postgresql://postgres:[PASS]@db.<REF>.supabase.co:5432/postgres

    Session Pooler format (IPv4 + IPv6 — works everywhere, FREE):
        postgresql://postgres.<REF>:[PASS]@aws-0-<REGION>.pooler.supabase.com:5432/postgres

    If the URL is already a pooler URL, or is not a Supabase URL at all,
    it is returned unchanged.
    """
    if not url:
        return url

    # Already using pooler → nothing to do
    if "pooler.supabase.com" in url:
        logger.info("DATABASE_URL already uses Supabase Session Pooler (IPv4 OK)")
        return url

    # Match direct Supabase connection: db.<REF>.supabase.co
    m = re.search(r"@db\.([a-z0-9]+)\.supabase\.co", url)
    if not m:
        # Not a Supabase direct URL — could be local/other PG, leave as-is
        return url

    project_ref = m.group(1)
    logger.warning(
        "Detected direct Supabase connection string (db.%s.supabase.co). "
        "Direct connections require IPv6 which is unavailable on most "
        "deployment platforms.  Auto-converting to Session Pooler…",
        project_ref,
    )

    try:
        parsed = urlparse(url)
    except Exception:
        logger.error("Could not parse DATABASE_URL — returning as-is")
        return url

    # Original user is usually 'postgres', pooler needs 'postgres.<REF>'
    orig_user = parsed.username or "postgres"
    # Strip any existing .ref suffix to avoid double-appending
    base_user = orig_user.split(".")[0]
    new_user = f"{base_user}.{project_ref}"

    password = parsed.password or ""

    # Supabase region detection: try to pull from env, otherwise
    # default to the most common region.  The user can override by
    # providing the pooler URL directly or setting SUPABASE_REGION.
    region = os.getenv("SUPABASE_REGION", "us-east-1")
    new_host = f"aws-0-{region}.pooler.supabase.com"

    # Session Pooler uses port 5432 (supports prepared statements)
    # Transaction Pooler uses port 6543 (does NOT support prepared stmts)
    new_port = 5432

    new_netloc = f"{new_user}:{password}@{new_host}:{new_port}"
    pooler_url = urlunparse((
        parsed.scheme or "postgresql",
        new_netloc,
        parsed.path or "/postgres",
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))

    logger.info(
        "Converted to Session Pooler: %s@%s:%d (region=%s)",
        new_user, new_host, new_port, region,
    )
    return pooler_url


class DatabaseManager:
    """Async PostgreSQL wrapper for all bot persistence (Supabase)."""

    def __init__(self, database_url: str):
        self.database_url = _to_pooler_url(database_url)
        self._pool: Optional[asyncpg.Pool] = None
        self._connect_task: Optional[asyncio.Task] = None

    # ── Lifecycle ────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """True when the connection pool is open and usable."""
        return self._pool is not None

    async def _try_connect(self) -> bool:
        """Attempt a single connection cycle. Returns True on success."""
        try:
            import ssl as _ssl
            _ctx = _ssl.create_default_context()
            # Supabase Session Pooler requires TLS but the cert CN
            # won't match the pooler hostname \u2014 skip verification.
            _ctx.check_hostname = False
            _ctx.verify_mode = _ssl.CERT_NONE

            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=2,
                max_size=10,
                statement_cache_size=0,  # Required for Supabase pooler compatibility
                command_timeout=30,
                ssl=_ctx,
            )
            await self._create_tables()
            await self._migrate_dodo_tasks_remind_character()
            await self._migrate_dodo_config_role_columns()
            logger.info(
                "Database initialized (PostgreSQL via asyncpg + Session Pooler)"
            )
            return True
        except _CONNECT_ERRORS as exc:
            # Clean up partial pool if it was created
            if self._pool is not None:
                try:
                    await self._pool.close()
                except Exception:
                    pass
                self._pool = None
            raise exc

    async def initialize(
        self, *, max_retries: int = 5, base_delay: float = 2.0
    ) -> bool:
        """Open the connection pool and create tables if needed.

        Retries with exponential back-off so transient network errors
        (common on containerised platforms like Railway) don't crash
        the bot on startup.  Returns True on success, False if all
        retries were exhausted (the bot can still start without DB).
        """
        for attempt in range(1, max_retries + 1):
            try:
                await self._try_connect()
                return True
            except _CONNECT_ERRORS as exc:
                delay = base_delay * (2 ** (attempt - 1))  # 2, 4, 8, 16, 32 s
                logger.warning(
                    "DB connect attempt %d/%d failed: %s — retrying in %.0fs",
                    attempt, max_retries, exc, delay,
                )
                await asyncio.sleep(delay)

        logger.error("Could not connect to database after %d attempts", max_retries)
        return False

    async def connect_forever(
        self, *, interval: float = 30.0
    ) -> None:
        """Keep trying to connect in the background until successful.

        Intended to be launched as an ``asyncio.Task`` when the initial
        ``initialize()`` fails so the bot can come online and recover
        once the DB becomes reachable.
        """
        while not self.is_ready:
            try:
                await self._try_connect()
                logger.info("Background DB reconnect succeeded")
                return
            except _CONNECT_ERRORS as exc:
                logger.warning(
                    "Background DB reconnect failed: %s — next try in %.0fs",
                    exc, interval,
                )
                await asyncio.sleep(interval)

    def start_background_connect(self) -> None:
        """Spawn a background task that keeps retrying the DB connection."""
        if self._connect_task is None or self._connect_task.done():
            self._connect_task = asyncio.create_task(
                self.connect_forever(), name="db-reconnect"
            )
            logger.info("Spawned background DB reconnect task")

    async def close(self) -> None:
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._pool:
            await self._pool.close()
            logger.info("Database connection pool closed")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._pool

    async def _migrate_dodo_tasks_remind_character(self) -> None:
        """Add remind_character column to dodo_tasks if it doesn't exist."""
        col = await self.pool.fetchval(
            """SELECT 1 FROM information_schema.columns
               WHERE table_name = 'dodo_tasks' AND column_name = 'remind_character'"""
        )
        if not col:
            tbl = await self.pool.fetchval(
                """SELECT 1 FROM information_schema.tables
                   WHERE table_name = 'dodo_tasks'"""
            )
            if tbl:
                logger.info("Migrating dodo_tasks: adding remind_character column")
                await self.pool.execute("ALTER TABLE dodo_tasks ADD COLUMN remind_character TEXT")

    async def _migrate_dodo_config_role_columns(self) -> None:
        """Add MVP role columns to dodo_config if they don't exist."""
        tbl = await self.pool.fetchval(
            """SELECT 1 FROM information_schema.tables
               WHERE table_name = 'dodo_config'"""
        )
        if not tbl:
            return
        for col_name in ("daily_mvp_role_id", "weekly_mvp_role_id"):
            exists = await self.pool.fetchval(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'dodo_config' AND column_name = $1""",
                col_name,
            )
            if not exists:
                logger.info("Migrating dodo_config: adding %s column", col_name)
                await self.pool.execute(
                    f"ALTER TABLE dodo_config ADD COLUMN {col_name} BIGINT"
                )

    # ── Schema ───────────────────────────────────────────────────────

    async def _create_tables(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id         BIGINT PRIMARY KEY,
                    preferred_model TEXT    DEFAULT NULL,
                    total_tokens    INTEGER DEFAULT 0,
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT NOT NULL,
                    guild_id    BIGINT,
                    messages    TEXT    DEFAULT '[]',
                    model_used  TEXT,
                    active      INTEGER DEFAULT 1,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    updated_at  TIMESTAMPTZ DEFAULT NOW(),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    guild_id            BIGINT PRIMARY KEY,
                    rate_limit_override INTEGER DEFAULT NULL,
                    disabled_features   TEXT    DEFAULT '[]',
                    created_at          TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_logs (
                    id              SERIAL PRIMARY KEY,
                    user_id         BIGINT NOT NULL,
                    guild_id        BIGINT,
                    command         TEXT    NOT NULL,
                    model           TEXT,
                    tokens_used     INTEGER DEFAULT 0,
                    latency_ms      REAL    DEFAULT 0,
                    success         INTEGER DEFAULT 1,
                    error_message   TEXT,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_messages (
                    id              SERIAL PRIMARY KEY,
                    user_id         TEXT    NOT NULL,
                    guild_id        TEXT    NOT NULL,
                    channel_id      TEXT    NOT NULL,
                    message_content TEXT    NOT NULL,
                    timestamp       TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_context (
                    user_id             TEXT    NOT NULL,
                    guild_id            TEXT    NOT NULL,
                    recent_messages     TEXT    DEFAULT '[]',
                    personality_summary TEXT    DEFAULT NULL,
                    interests           TEXT    DEFAULT '[]',
                    last_updated        TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (user_id, guild_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_privacy (
                    user_id         TEXT    PRIMARY KEY,
                    data_collection INTEGER DEFAULT 1,
                    opted_out_at    TIMESTAMPTZ DEFAULT NULL,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_identities (
                    user_id         TEXT    NOT NULL,
                    guild_id        TEXT    NOT NULL,
                    bot_name        TEXT    NOT NULL,
                    relationship    TEXT    DEFAULT 'assistant',
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (user_id, guild_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_analyses (
                    id              SERIAL PRIMARY KEY,
                    target_user_id  TEXT    NOT NULL,
                    guild_id        TEXT    NOT NULL,
                    analyzer_user_id TEXT   NOT NULL,
                    analysis_data   TEXT    NOT NULL,
                    message_count   INTEGER DEFAULT 0,
                    date_range      TEXT    DEFAULT NULL,
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(target_user_id, guild_id, analyzer_user_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS analysis_opt_in (
                    user_id         TEXT    NOT NULL,
                    guild_id        TEXT    NOT NULL,
                    opted_in        INTEGER DEFAULT 0,
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (user_id, guild_id)
                )
            """)
            # ── Music Premium tables ─────────────────────────────
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_favorites (
                    id          SERIAL PRIMARY KEY,
                    user_id     TEXT    NOT NULL,
                    song_data   TEXT    NOT NULL,
                    added_at    TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(user_id, song_data)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_playlists (
                    id          SERIAL PRIMARY KEY,
                    user_id     TEXT    NOT NULL,
                    name        TEXT    NOT NULL,
                    description TEXT    DEFAULT '',
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    updated_at  TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(user_id, name)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS playlist_songs (
                    id          SERIAL PRIMARY KEY,
                    playlist_id INTEGER NOT NULL REFERENCES user_playlists(id) ON DELETE CASCADE,
                    song_data   TEXT    NOT NULL,
                    position    INTEGER NOT NULL DEFAULT 0,
                    added_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS listening_history (
                    id               SERIAL PRIMARY KEY,
                    user_id          TEXT    NOT NULL,
                    guild_id         TEXT    NOT NULL,
                    song_data        TEXT    NOT NULL,
                    listened_seconds REAL    DEFAULT 0,
                    played_at        TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS music_profiles (
                    user_id                 TEXT    PRIMARY KEY,
                    total_listening_seconds REAL    DEFAULT 0,
                    total_songs_played      INTEGER DEFAULT 0,
                    created_at              TIMESTAMPTZ DEFAULT NOW(),
                    updated_at              TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS song_request_channels (
                    guild_id        TEXT    PRIMARY KEY,
                    channel_id      TEXT    NOT NULL,
                    configured_by   TEXT    NOT NULL,
                    configured_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sleep_timers (
                    guild_id        TEXT    PRIMARY KEY,
                    expires_at      TEXT    NOT NULL,
                    set_by          TEXT    NOT NULL,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # ── Guild management ─────────────────────────────────
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS allowed_guilds (
                    guild_id    BIGINT PRIMARY KEY,
                    allowed_by  TEXT    DEFAULT NULL,
                    allowed_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS news_channels (
                    guild_id            TEXT    PRIMARY KEY,
                    channel_id          TEXT    NOT NULL,
                    topic               TEXT    NOT NULL,
                    interval_minutes    INTEGER DEFAULT 30,
                    enabled             INTEGER DEFAULT 1,
                    last_sent_at        TIMESTAMPTZ DEFAULT NULL,
                    last_sent_urls      TEXT    DEFAULT '[]',
                    configured_by       TEXT    NOT NULL,
                    configured_at       TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # ── Dodo tables ──────────────────────────────────────
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dodo_tasks (
                    id              SERIAL PRIMARY KEY,
                    user_id         BIGINT NOT NULL,
                    guild_id        BIGINT NOT NULL,
                    task_text       TEXT    NOT NULL,
                    priority        TEXT    NOT NULL,
                    is_hidden       INTEGER DEFAULT 0,
                    is_completed    INTEGER DEFAULT 0,
                    is_expired      INTEGER DEFAULT 0,
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    completed_at    TIMESTAMPTZ,
                    timer_expires   TIMESTAMPTZ,
                    cook_time_mins  INTEGER,
                    remind_enabled  INTEGER DEFAULT 0,
                    remind_intervals TEXT   DEFAULT '[]',
                    next_remind_at  TIMESTAMPTZ,
                    remind_stage    INTEGER DEFAULT 0,
                    remind_character TEXT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dodo_users (
                    user_id         BIGINT NOT NULL,
                    guild_id        BIGINT NOT NULL,
                    xp              INTEGER DEFAULT 0,
                    streak          INTEGER DEFAULT 0,
                    last_active     TEXT,
                    steal_shield    INTEGER DEFAULT 0,
                    streak_mercy    INTEGER DEFAULT 0,
                    joined_at       TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (user_id, guild_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dodo_mvp (
                    id              SERIAL PRIMARY KEY,
                    guild_id        BIGINT NOT NULL,
                    user_id         BIGINT NOT NULL,
                    mvp_type        TEXT    NOT NULL,
                    awarded_at      TIMESTAMPTZ DEFAULT NOW(),
                    boost_available INTEGER DEFAULT 0,
                    steal_available INTEGER DEFAULT 0,
                    boost_used      INTEGER DEFAULT 0,
                    steal_used      INTEGER DEFAULT 0,
                    steal_target_id BIGINT,
                    expires_at      TIMESTAMPTZ
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dodo_steal_log (
                    id              SERIAL PRIMARY KEY,
                    guild_id        BIGINT NOT NULL,
                    stealer_id      BIGINT NOT NULL,
                    target_id       BIGINT NOT NULL,
                    week_start      TEXT    NOT NULL,
                    stolen_xp       INTEGER,
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(stealer_id, target_id, week_start)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dodo_strikes (
                    user_id         BIGINT NOT NULL,
                    guild_id        BIGINT NOT NULL,
                    strike_date     TEXT    NOT NULL,
                    strike_count    INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, guild_id, strike_date)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dodo_threads (
                    user_id         BIGINT NOT NULL,
                    guild_id        BIGINT NOT NULL,
                    thread_id       BIGINT NOT NULL,
                    message_id      BIGINT NOT NULL,
                    PRIMARY KEY (user_id, guild_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dodo_config (
                    guild_id                BIGINT PRIMARY KEY,
                    tasks_channel_id        BIGINT,
                    gc_channel_id           BIGINT,
                    daily_mvp_role_id       BIGINT,
                    weekly_mvp_role_id      BIGINT,
                    configured_by           TEXT,
                    configured_at           TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # ── Indexes ──────────────────────────────────────────
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, active)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_logs_user ON usage_logs(user_id, created_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_logs_guild ON usage_logs(guild_id, created_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_messages ON user_messages(user_id, guild_id, timestamp)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_context ON user_context(user_id, guild_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_favorites_user ON user_favorites(user_id, added_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_playlist_songs_playlist ON playlist_songs(playlist_id, position)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_listening_history_user ON listening_history(user_id, played_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_listening_history_guild ON listening_history(user_id, guild_id, played_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_news_channels_enabled ON news_channels(enabled, last_sent_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_dodo_tasks_user ON dodo_tasks(user_id, guild_id, is_completed)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_dodo_tasks_expiry ON dodo_tasks(timer_expires, is_completed, is_expired)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_dodo_tasks_remind ON dodo_tasks(remind_enabled, next_remind_at, is_completed)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_dodo_users_guild ON dodo_users(guild_id, xp)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_dodo_mvp_guild ON dodo_mvp(guild_id, mvp_type, awarded_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_dodo_strikes_date ON dodo_strikes(strike_date)")

    # ── Users ────────────────────────────────────────────────────────

    async def ensure_user(self, user_id: int) -> None:
        """Insert user row if it doesn't exist."""
        await self.pool.execute(
            "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
        )

    async def get_user_model(self, user_id: int) -> Optional[str]:
        row = await self.pool.fetchrow(
            "SELECT preferred_model FROM users WHERE user_id = $1", user_id
        )
        return row["preferred_model"] if row else None

    async def set_user_model(self, user_id: int, model: str) -> None:
        await self.ensure_user(user_id)
        await self.pool.execute(
            "UPDATE users SET preferred_model = $1, updated_at = NOW() WHERE user_id = $2",
            model, user_id,
        )

    async def add_user_tokens(self, user_id: int, tokens: int) -> None:
        await self.ensure_user(user_id)
        await self.pool.execute(
            "UPDATE users SET total_tokens = total_tokens + $1, updated_at = NOW() WHERE user_id = $2",
            tokens, user_id,
        )

    async def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        row = await self.pool.fetchrow(
            "SELECT * FROM users WHERE user_id = $1", user_id
        )
        if row:
            return dict(row)
        return {"user_id": user_id, "total_tokens": 0, "preferred_model": None}

    # ── Conversations ────────────────────────────────────────────────

    async def start_conversation(
        self, user_id: int, guild_id: Optional[int] = None, model: Optional[str] = None
    ) -> int:
        """Start a new conversation and return its ID."""
        await self.end_conversation(user_id, guild_id)
        await self.ensure_user(user_id)
        row = await self.pool.fetchrow(
            "INSERT INTO conversations (user_id, guild_id, model_used) VALUES ($1, $2, $3) RETURNING id",
            user_id, guild_id, model,
        )
        return row["id"]

    async def get_active_conversation(
        self, user_id: int, guild_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Get the active conversation for a user in a guild."""
        if guild_id is not None:
            row = await self.pool.fetchrow(
                "SELECT * FROM conversations WHERE user_id = $1 AND active = 1 AND guild_id = $2 ORDER BY updated_at DESC LIMIT 1",
                user_id, guild_id,
            )
        else:
            row = await self.pool.fetchrow(
                "SELECT * FROM conversations WHERE user_id = $1 AND active = 1 ORDER BY updated_at DESC LIMIT 1",
                user_id,
            )
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
        row = await self.pool.fetchrow(
            "SELECT messages FROM conversations WHERE id = $1", conversation_id
        )
        if not row:
            return
        messages: List[Dict] = json.loads(row["messages"])
        messages.append({"role": role, "content": content})
        messages = messages[-max_messages:]
        await self.pool.execute(
            "UPDATE conversations SET messages = $1, updated_at = NOW() WHERE id = $2",
            json.dumps(messages), conversation_id,
        )

    async def clear_conversation(self, conversation_id: int) -> None:
        """Clear messages in a conversation."""
        await self.pool.execute(
            "UPDATE conversations SET messages = '[]', updated_at = NOW() WHERE id = $1",
            conversation_id,
        )

    async def end_conversation(
        self, user_id: int, guild_id: Optional[int] = None
    ) -> None:
        """Deactivate all active conversations for a user in a guild."""
        if guild_id is not None:
            await self.pool.execute(
                "UPDATE conversations SET active = 0, updated_at = NOW() WHERE user_id = $1 AND active = 1 AND guild_id = $2",
                user_id, guild_id,
            )
        else:
            await self.pool.execute(
                "UPDATE conversations SET active = 0, updated_at = NOW() WHERE user_id = $1 AND active = 1",
                user_id,
            )

    async def get_conversation_export(self, conversation_id: int) -> str:
        """Export a conversation as a readable text transcript."""
        row = await self.pool.fetchrow(
            "SELECT messages, model_used, created_at FROM conversations WHERE id = $1",
            conversation_id,
        )
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
        await self.pool.execute(
            """INSERT INTO usage_logs
               (user_id, guild_id, command, model, tokens_used, latency_ms, success, error_message)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            user_id, guild_id, command, model, tokens_used, latency_ms,
            1 if success else 0, error_message,
        )

    async def get_global_stats(self) -> Dict[str, Any]:
        """Return aggregate bot statistics."""
        stats: Dict[str, Any] = {}
        row = await self.pool.fetchrow("SELECT COUNT(*) as cnt FROM users")
        stats["total_users"] = row["cnt"] if row else 0
        row = await self.pool.fetchrow(
            "SELECT COUNT(*) as cnt, SUM(tokens_used) as tokens FROM usage_logs"
        )
        stats["total_commands"] = row["cnt"] if row else 0
        stats["total_tokens"] = row["tokens"] or 0 if row else 0
        row = await self.pool.fetchrow(
            "SELECT COUNT(*) as cnt FROM conversations WHERE active = 1"
        )
        stats["active_conversations"] = row["cnt"] if row else 0
        return stats

    # ── Server Settings ──────────────────────────────────────────────

    async def ensure_server(self, guild_id: int) -> None:
        await self.pool.execute(
            "INSERT INTO servers (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
            guild_id,
        )

    # ── User Messages & Personalization ──────────────────────────────

    async def store_user_message(
        self, user_id: str, guild_id: str, channel_id: str, content: str
    ) -> None:
        """Store a user message for personalization."""
        row = await self.pool.fetchrow(
            "SELECT data_collection FROM user_privacy WHERE user_id = $1", user_id
        )
        if row and row["data_collection"] == 0:
            return
        await self.pool.execute(
            "INSERT INTO user_messages (user_id, guild_id, channel_id, message_content) VALUES ($1, $2, $3, $4)",
            user_id, guild_id, channel_id, content,
        )

    async def get_recent_messages(
        self, user_id: str, guild_id: str, limit: int = 20
    ) -> List[str]:
        """Get recent messages from a user."""
        rows = await self.pool.fetch(
            "SELECT message_content FROM user_messages WHERE user_id = $1 AND guild_id = $2 ORDER BY timestamp DESC LIMIT $3",
            user_id, guild_id, limit,
        )
        return [row["message_content"] for row in rows]

    async def update_user_context(
        self, user_id: str, guild_id: str, recent_messages: List[str]
    ) -> None:
        """Update user context with recent messages."""
        await self.pool.execute(
            """INSERT INTO user_context (user_id, guild_id, recent_messages, last_updated)
               VALUES ($1, $2, $3, NOW())
               ON CONFLICT(user_id, guild_id) DO UPDATE SET
                   recent_messages = EXCLUDED.recent_messages,
                   last_updated = NOW()""",
            user_id, guild_id, json.dumps(recent_messages),
        )

    async def get_user_context(
        self, user_id: str, guild_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get user context for personalization."""
        row = await self.pool.fetchrow(
            "SELECT recent_messages, personality_summary, interests FROM user_context WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )
        if row:
            return {
                "recent_messages": json.loads(row["recent_messages"]),
                "personality_summary": row["personality_summary"],
                "interests": json.loads(row["interests"]) if row["interests"] else [],
            }
        return None

    async def delete_user_data(self, user_id: str) -> None:
        """Delete all data for a user (for /forget-me command)."""
        await self.pool.execute("DELETE FROM user_messages WHERE user_id = $1", user_id)
        await self.pool.execute("DELETE FROM user_context WHERE user_id = $1", user_id)
        await self.pool.execute(
            """INSERT INTO user_privacy (user_id, data_collection, opted_out_at)
               VALUES ($1, 0, NOW())
               ON CONFLICT(user_id) DO UPDATE SET
                   data_collection = 0,
                   opted_out_at = NOW()""",
            user_id,
        )

    async def cleanup_old_messages(self, days: int = 30) -> int:
        """Delete messages older than specified days. Returns count of deleted messages."""
        result = await self.pool.execute(
            "DELETE FROM user_messages WHERE timestamp < NOW() - CAST($1 || ' days' AS INTERVAL)",
            str(days),
        )
        # asyncpg returns 'DELETE N'
        try:
            return int(result.split()[-1])
        except (IndexError, ValueError):
            return 0

    # ── Bot Identity & Personalization ───────────────────────────────

    async def set_bot_identity(
        self, user_id: str, guild_id: str, bot_name: str, relationship: str = "assistant"
    ) -> None:
        """Set personalized bot identity for a user."""
        await self.pool.execute(
            """INSERT INTO bot_identities (user_id, guild_id, bot_name, relationship, updated_at)
               VALUES ($1, $2, $3, $4, NOW())
               ON CONFLICT(user_id, guild_id) DO UPDATE SET
                   bot_name = EXCLUDED.bot_name,
                   relationship = EXCLUDED.relationship,
                   updated_at = NOW()""",
            user_id, guild_id, bot_name, relationship,
        )

    async def get_bot_identity(
        self, user_id: str, guild_id: str
    ) -> Optional[Dict[str, str]]:
        """Get personalized bot identity for a user."""
        row = await self.pool.fetchrow(
            "SELECT bot_name, relationship FROM bot_identities WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )
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
        """Deep search for user messages with optional time range."""
        if days_back:
            rows = await self.pool.fetch(
                """SELECT message_content, channel_id, timestamp
                   FROM user_messages
                   WHERE user_id = $1 AND guild_id = $2
                     AND timestamp >= NOW() - CAST($3 || ' days' AS INTERVAL)
                   ORDER BY timestamp DESC LIMIT $4""",
                user_id, guild_id, str(days_back), limit,
            )
        else:
            rows = await self.pool.fetch(
                """SELECT message_content, channel_id, timestamp
                   FROM user_messages
                   WHERE user_id = $1 AND guild_id = $2
                   ORDER BY timestamp DESC LIMIT $3""",
                user_id, guild_id, limit,
            )
        return [
            {
                "content": row["message_content"],
                "channel_id": row["channel_id"],
                "timestamp": str(row["timestamp"]),
            }
            for row in rows
        ]

    async def get_message_count(
        self, user_id: str, guild_id: str, days_back: Optional[int] = None
    ) -> int:
        """Get total message count for a user."""
        if days_back:
            row = await self.pool.fetchrow(
                "SELECT COUNT(*) as cnt FROM user_messages WHERE user_id = $1 AND guild_id = $2 AND timestamp >= NOW() - CAST($3 || ' days' AS INTERVAL)",
                user_id, guild_id, str(days_back),
            )
        else:
            row = await self.pool.fetchrow(
                "SELECT COUNT(*) as cnt FROM user_messages WHERE user_id = $1 AND guild_id = $2",
                user_id, guild_id,
            )
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
        await self.pool.execute(
            """INSERT INTO user_analyses
               (target_user_id, guild_id, analyzer_user_id, analysis_data, message_count, date_range)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT(target_user_id, guild_id, analyzer_user_id) DO UPDATE SET
                   analysis_data = EXCLUDED.analysis_data,
                   message_count = EXCLUDED.message_count,
                   date_range = EXCLUDED.date_range,
                   created_at = NOW()""",
            target_user_id, guild_id, analyzer_user_id,
            json.dumps(analysis_data), message_count, date_range,
        )

    async def get_user_analysis(
        self, target_user_id: str, guild_id: str, analyzer_user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve stored user analysis."""
        row = await self.pool.fetchrow(
            """SELECT analysis_data, message_count, date_range, created_at
               FROM user_analyses
               WHERE target_user_id = $1 AND guild_id = $2 AND analyzer_user_id = $3""",
            target_user_id, guild_id, analyzer_user_id,
        )
        if row:
            return {
                "analysis": json.loads(row["analysis_data"]),
                "message_count": row["message_count"],
                "date_range": row["date_range"],
                "created_at": str(row["created_at"]),
            }
        return None

    async def set_analysis_opt_in(self, user_id: str, guild_id: str, opted_in: bool) -> None:
        """Set user's analysis opt-in preference."""
        await self.pool.execute(
            """INSERT INTO analysis_opt_in (user_id, guild_id, opted_in, updated_at)
               VALUES ($1, $2, $3, NOW())
               ON CONFLICT(user_id, guild_id) DO UPDATE SET
                   opted_in = EXCLUDED.opted_in,
                   updated_at = NOW()""",
            user_id, guild_id, 1 if opted_in else 0,
        )

    async def get_analysis_opt_in(self, user_id: str, guild_id: str) -> bool:
        """Check if user has opted in to analysis features."""
        row = await self.pool.fetchrow(
            "SELECT opted_in FROM analysis_opt_in WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )
        return bool(row["opted_in"]) if row else False

    # ══════════════════════════════════════════════════════════════════
    #  Music Premium — Favorites
    # ══════════════════════════════════════════════════════════════════

    async def add_favorite(self, user_id: str, song_data: str) -> bool:
        """Add a song to user's favorites. Returns True if added, False if duplicate."""
        try:
            await self.pool.execute(
                "INSERT INTO user_favorites (user_id, song_data) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, song_data,
            )
            return True
        except Exception:
            return False

    async def remove_favorite(self, user_id: str, song_data: str) -> bool:
        """Remove a song from favorites. Returns True if removed."""
        result = await self.pool.execute(
            "DELETE FROM user_favorites WHERE user_id = $1 AND song_data = $2",
            user_id, song_data,
        )
        return int(result.split()[-1]) > 0

    async def remove_favorite_by_id(self, user_id: str, fav_id: int) -> bool:
        """Remove a favorite by its row ID."""
        result = await self.pool.execute(
            "DELETE FROM user_favorites WHERE id = $1 AND user_id = $2",
            fav_id, user_id,
        )
        return int(result.split()[-1]) > 0

    async def is_favorite(self, user_id: str, song_data: str) -> bool:
        """Check if a song is in the user's favorites."""
        row = await self.pool.fetchrow(
            "SELECT 1 FROM user_favorites WHERE user_id = $1 AND song_data = $2",
            user_id, song_data,
        )
        return row is not None

    async def get_favorites(
        self, user_id: str, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Get user's favorite songs (newest first)."""
        rows = await self.pool.fetch(
            "SELECT id, song_data, added_at FROM user_favorites WHERE user_id = $1 ORDER BY added_at DESC LIMIT $2 OFFSET $3",
            user_id, limit, offset,
        )
        results = []
        for row in rows:
            try:
                song = json.loads(row["song_data"])
                song["_fav_id"] = row["id"]
                song["_added_at"] = str(row["added_at"])
                results.append(song)
            except json.JSONDecodeError:
                continue
        return results

    async def get_favorites_count(self, user_id: str) -> int:
        """Get total number of favorites for a user."""
        row = await self.pool.fetchrow(
            "SELECT COUNT(*) as cnt FROM user_favorites WHERE user_id = $1",
            user_id,
        )
        return row["cnt"] if row else 0

    # ══════════════════════════════════════════════════════════════════
    #  Music Premium — Playlists
    # ══════════════════════════════════════════════════════════════════

    async def create_playlist(self, user_id: str, name: str, description: str = "") -> Optional[int]:
        """Create a new playlist. Returns playlist ID or None if name already exists."""
        try:
            row = await self.pool.fetchrow(
                "INSERT INTO user_playlists (user_id, name, description) VALUES ($1, $2, $3) RETURNING id",
                user_id, name, description,
            )
            return row["id"] if row else None
        except Exception:
            return None

    async def delete_playlist(self, user_id: str, playlist_id: int) -> bool:
        """Delete a playlist and all its songs."""
        result = await self.pool.execute(
            "DELETE FROM user_playlists WHERE id = $1 AND user_id = $2",
            playlist_id, user_id,
        )
        return int(result.split()[-1]) > 0

    async def rename_playlist(self, user_id: str, playlist_id: int, new_name: str) -> bool:
        """Rename a playlist."""
        try:
            result = await self.pool.execute(
                "UPDATE user_playlists SET name = $1, updated_at = NOW() WHERE id = $2 AND user_id = $3",
                new_name, playlist_id, user_id,
            )
            return int(result.split()[-1]) > 0
        except Exception:
            return False

    async def get_playlists(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all playlists for a user."""
        rows = await self.pool.fetch(
            """SELECT p.id, p.name, p.description, p.created_at, p.updated_at,
                      (SELECT COUNT(*) FROM playlist_songs WHERE playlist_id = p.id) as song_count
               FROM user_playlists p WHERE p.user_id = $1 ORDER BY p.updated_at DESC""",
            user_id,
        )
        return [dict(row) for row in rows]

    async def get_playlist(self, user_id: str, playlist_id: int) -> Optional[Dict[str, Any]]:
        """Get a single playlist with metadata."""
        row = await self.pool.fetchrow(
            """SELECT p.id, p.name, p.description, p.created_at, p.updated_at,
                      (SELECT COUNT(*) FROM playlist_songs WHERE playlist_id = p.id) as song_count
               FROM user_playlists p WHERE p.id = $1 AND p.user_id = $2""",
            playlist_id, user_id,
        )
        return dict(row) if row else None

    async def get_playlist_by_name(self, user_id: str, name: str) -> Optional[Dict[str, Any]]:
        """Get a playlist by name."""
        row = await self.pool.fetchrow(
            """SELECT p.id, p.name, p.description, p.created_at, p.updated_at,
                      (SELECT COUNT(*) FROM playlist_songs WHERE playlist_id = p.id) as song_count
               FROM user_playlists p WHERE p.user_id = $1 AND LOWER(p.name) = LOWER($2)""",
            user_id, name,
        )
        return dict(row) if row else None

    async def add_song_to_playlist(self, playlist_id: int, song_data: str) -> bool:
        """Add a song to a playlist at the end."""
        try:
            row = await self.pool.fetchrow(
                "SELECT COALESCE(MAX(position), -1) + 1 as next_pos FROM playlist_songs WHERE playlist_id = $1",
                playlist_id,
            )
            pos = row["next_pos"] if row else 0
            await self.pool.execute(
                "INSERT INTO playlist_songs (playlist_id, song_data, position) VALUES ($1, $2, $3)",
                playlist_id, song_data, pos,
            )
            await self.pool.execute(
                "UPDATE user_playlists SET updated_at = NOW() WHERE id = $1",
                playlist_id,
            )
            return True
        except Exception:
            return False

    async def remove_song_from_playlist(self, playlist_id: int, position: int) -> bool:
        """Remove a song from a playlist by position (0-based)."""
        result = await self.pool.execute(
            "DELETE FROM playlist_songs WHERE playlist_id = $1 AND position = $2",
            playlist_id, position,
        )
        return int(result.split()[-1]) > 0

    async def get_playlist_songs(self, playlist_id: int) -> List[Dict[str, Any]]:
        """Get all songs in a playlist, ordered by position."""
        rows = await self.pool.fetch(
            "SELECT id, song_data, position, added_at FROM playlist_songs WHERE playlist_id = $1 ORDER BY position",
            playlist_id,
        )
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
        result = await self.pool.execute(
            "DELETE FROM playlist_songs WHERE playlist_id = $1",
            playlist_id,
        )
        try:
            return int(result.split()[-1])
        except (IndexError, ValueError):
            return 0

    async def get_playlist_song_keys(self, playlist_id: int) -> set:
        """Return a set of song_data JSON strings already in a playlist."""
        rows = await self.pool.fetch(
            "SELECT song_data FROM playlist_songs WHERE playlist_id = $1",
            playlist_id,
        )
        return {row["song_data"] for row in rows}

    async def update_playlist_timestamp(self, playlist_id: int) -> None:
        """Touch a playlist's updated_at without changing its contents."""
        await self.pool.execute(
            "UPDATE user_playlists SET updated_at = NOW() WHERE id = $1",
            playlist_id,
        )

    # ══════════════════════════════════════════════════════════════════
    #  Music Premium — Listening Profiles & History
    # ══════════════════════════════════════════════════════════════════

    async def log_listening_session(
        self, user_id: str, guild_id: str, song_data: str, listened_seconds: float
    ) -> None:
        """Log a listening session for a user."""
        await self.pool.execute(
            "INSERT INTO listening_history (user_id, guild_id, song_data, listened_seconds) VALUES ($1, $2, $3, $4)",
            user_id, guild_id, song_data, listened_seconds,
        )
        await self.pool.execute(
            """INSERT INTO music_profiles (user_id, total_listening_seconds, total_songs_played)
               VALUES ($1, $2, 1)
               ON CONFLICT(user_id) DO UPDATE SET
                   total_listening_seconds = music_profiles.total_listening_seconds + EXCLUDED.total_listening_seconds,
                   total_songs_played = music_profiles.total_songs_played + 1,
                   updated_at = NOW()""",
            user_id, listened_seconds,
        )

    async def get_music_profile(self, user_id: str) -> Dict[str, Any]:
        """Get a user's music profile stats."""
        profile: Dict[str, Any] = {
            "total_listening_seconds": 0,
            "total_songs_played": 0,
            "top_artists": [],
            "top_songs": [],
            "recent_songs": [],
        }
        row = await self.pool.fetchrow(
            "SELECT total_listening_seconds, total_songs_played, created_at FROM music_profiles WHERE user_id = $1",
            user_id,
        )
        if row:
            profile["total_listening_seconds"] = row["total_listening_seconds"]
            profile["total_songs_played"] = row["total_songs_played"]
            profile["member_since"] = str(row["created_at"])

        rows = await self.pool.fetch(
            "SELECT song_data, listened_seconds, played_at FROM listening_history WHERE user_id = $1 ORDER BY played_at DESC LIMIT 10",
            user_id,
        )
        for row in rows:
            try:
                song = json.loads(row["song_data"])
                song["_listened"] = row["listened_seconds"]
                song["_played_at"] = str(row["played_at"])
                profile["recent_songs"].append(song)
            except json.JSONDecodeError:
                continue

        rows = await self.pool.fetch(
            """SELECT song_data, COUNT(*) as plays, SUM(listened_seconds) as total_time
               FROM listening_history WHERE user_id = $1
               GROUP BY song_data ORDER BY plays DESC LIMIT 10""",
            user_id,
        )
        for row in rows:
            try:
                song = json.loads(row["song_data"])
                song["_plays"] = row["plays"]
                song["_total_time"] = row["total_time"]
                profile["top_songs"].append(song)
            except json.JSONDecodeError:
                continue

        rows = await self.pool.fetch(
            """SELECT song_data, COUNT(*) as plays
               FROM listening_history WHERE user_id = $1
               GROUP BY song_data ORDER BY plays DESC LIMIT 50""",
            user_id,
        )
        artist_counts: Dict[str, int] = {}
        for row in rows:
            try:
                song = json.loads(row["song_data"])
                artist = song.get("artist", "Unknown")
                artist_counts[artist] = artist_counts.get(artist, 0) + row["plays"]
            except json.JSONDecodeError:
                continue
        sorted_artists = sorted(artist_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        profile["top_artists"] = [{"name": a, "plays": c} for a, c in sorted_artists]
        return profile

    # ══════════════════════════════════════════════════════════════════
    #  Music Premium — Song Request Channels
    # ══════════════════════════════════════════════════════════════════

    async def set_request_channel(self, guild_id: str, channel_id: str, configured_by: str) -> None:
        """Set the song request channel for a guild."""
        await self.pool.execute(
            """INSERT INTO song_request_channels (guild_id, channel_id, configured_by)
               VALUES ($1, $2, $3)
               ON CONFLICT(guild_id) DO UPDATE SET
                   channel_id = EXCLUDED.channel_id,
                   configured_by = EXCLUDED.configured_by,
                   configured_at = NOW()""",
            guild_id, channel_id, configured_by,
        )

    async def get_request_channel(self, guild_id: str) -> Optional[str]:
        """Get the song request channel ID for a guild."""
        row = await self.pool.fetchrow(
            "SELECT channel_id FROM song_request_channels WHERE guild_id = $1",
            guild_id,
        )
        return row["channel_id"] if row else None

    async def remove_request_channel(self, guild_id: str) -> bool:
        """Remove the song request channel for a guild."""
        result = await self.pool.execute(
            "DELETE FROM song_request_channels WHERE guild_id = $1",
            guild_id,
        )
        return int(result.split()[-1]) > 0

    # ── Allowed Guilds ────────────────────────────────────────────────

    async def get_allowed_guilds(self) -> Set[int]:
        """Load all allowed guild IDs from the database."""
        rows = await self.pool.fetch("SELECT guild_id FROM allowed_guilds")
        return {row["guild_id"] for row in rows}

    async def add_allowed_guild(self, guild_id: int, allowed_by: str = "") -> None:
        """Add a guild to the allowlist."""
        await self.pool.execute(
            "INSERT INTO allowed_guilds (guild_id, allowed_by) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            guild_id, allowed_by,
        )

    async def remove_allowed_guild(self, guild_id: int) -> bool:
        """Remove a guild from the allowlist. Returns True if removed."""
        result = await self.pool.execute(
            "DELETE FROM allowed_guilds WHERE guild_id = $1", guild_id,
        )
        return int(result.split()[-1]) > 0

    async def migrate_allowed_guilds_from_json(self, json_path: str) -> int:
        """One-time migration: import allowed_guilds.json into the DB."""
        if not os.path.exists(json_path):
            return 0
        try:
            with open(json_path) as f:
                data = json.load(f)
            if not isinstance(data, list):
                return 0
            count = 0
            for gid in data:
                await self.pool.execute(
                    "INSERT INTO allowed_guilds (guild_id, allowed_by) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    int(gid), "migrated_from_json",
                )
                count += 1
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
        await self.pool.execute(
            """INSERT INTO news_channels
                   (guild_id, channel_id, topic, interval_minutes, configured_by)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT(guild_id) DO UPDATE SET
                   channel_id       = EXCLUDED.channel_id,
                   topic            = EXCLUDED.topic,
                   interval_minutes = EXCLUDED.interval_minutes,
                   configured_by    = EXCLUDED.configured_by,
                   enabled          = 1,
                   configured_at    = NOW()
            """,
            guild_id, channel_id, topic, interval_minutes, configured_by,
        )

    async def get_news_channel(self, guild_id: str) -> Optional[Dict[str, Any]]:
        """Get the auto-news config for a guild."""
        row = await self.pool.fetchrow(
            "SELECT * FROM news_channels WHERE guild_id = $1", guild_id
        )
        return dict(row) if row else None

    async def get_all_active_news_channels(self) -> List[Dict[str, Any]]:
        """Get all enabled auto-news channel configs."""
        rows = await self.pool.fetch(
            "SELECT * FROM news_channels WHERE enabled = 1"
        )
        return [dict(r) for r in rows]

    async def remove_news_channel(self, guild_id: str) -> bool:
        """Remove the auto-news channel for a guild."""
        result = await self.pool.execute(
            "DELETE FROM news_channels WHERE guild_id = $1", guild_id,
        )
        return int(result.split()[-1]) > 0

    async def toggle_news_channel(self, guild_id: str, enabled: bool) -> bool:
        """Enable or disable auto-news for a guild."""
        result = await self.pool.execute(
            "UPDATE news_channels SET enabled = $1 WHERE guild_id = $2",
            1 if enabled else 0, guild_id,
        )
        return int(result.split()[-1]) > 0

    async def update_news_last_sent(
        self, guild_id: str, sent_urls: Optional[List[str]] = None,
    ) -> None:
        """Update last_sent_at and optionally the dedup URL list."""
        if sent_urls is not None:
            urls_json = json.dumps(sent_urls[-50:])
            await self.pool.execute(
                "UPDATE news_channels SET last_sent_at = NOW(), last_sent_urls = $1 WHERE guild_id = $2",
                urls_json, guild_id,
            )
        else:
            await self.pool.execute(
                "UPDATE news_channels SET last_sent_at = NOW() WHERE guild_id = $1",
                guild_id,
            )

    # ══════════════════════════════════════════════════════════════════
    #  Dodo — Per-Guild Channel Config
    # ══════════════════════════════════════════════════════════════════

    async def set_dodo_config(
        self,
        guild_id: int,
        tasks_channel_id: Optional[int] = None,
        gc_channel_id: Optional[int] = None,
        daily_mvp_role_id: Optional[int] = None,
        weekly_mvp_role_id: Optional[int] = None,
        configured_by: str = "",
    ) -> None:
        """Set or update the Dodo config for a guild (upsert)."""
        await self.pool.execute(
            """INSERT INTO dodo_config
                   (guild_id, tasks_channel_id, gc_channel_id,
                    daily_mvp_role_id, weekly_mvp_role_id, configured_by)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT(guild_id) DO UPDATE SET
                   tasks_channel_id   = COALESCE(EXCLUDED.tasks_channel_id,   dodo_config.tasks_channel_id),
                   gc_channel_id      = COALESCE(EXCLUDED.gc_channel_id,      dodo_config.gc_channel_id),
                   daily_mvp_role_id  = COALESCE(EXCLUDED.daily_mvp_role_id,  dodo_config.daily_mvp_role_id),
                   weekly_mvp_role_id = COALESCE(EXCLUDED.weekly_mvp_role_id, dodo_config.weekly_mvp_role_id),
                   configured_by      = EXCLUDED.configured_by,
                   configured_at      = NOW()
            """,
            guild_id, tasks_channel_id, gc_channel_id,
            daily_mvp_role_id, weekly_mvp_role_id, configured_by,
        )

    async def get_dodo_config(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Get the Dodo config for a guild."""
        row = await self.pool.fetchrow(
            """SELECT tasks_channel_id, gc_channel_id,
                      daily_mvp_role_id, weekly_mvp_role_id,
                      configured_by, configured_at
               FROM dodo_config WHERE guild_id = $1""",
            guild_id,
        )
        return dict(row) if row else None
