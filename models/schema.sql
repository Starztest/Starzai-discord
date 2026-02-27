-- Starzai Discord Bot Database Schema
-- This file is for reference only; tables are created by db_manager.py

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
    messages    TEXT    DEFAULT '[]',   -- JSON array of {role, content}
    model_used  TEXT,
    active      INTEGER DEFAULT 1,
    created_at  TEXT    DEFAULT (datetime('now')),
    updated_at  TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS servers (
    guild_id            INTEGER PRIMARY KEY,
    rate_limit_override INTEGER DEFAULT NULL,
    disabled_features   TEXT    DEFAULT '[]',   -- JSON array of feature names
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

-- User message tracking for personalization
CREATE TABLE IF NOT EXISTS user_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL,
    guild_id        TEXT    NOT NULL,
    channel_id      TEXT    NOT NULL,
    message_content TEXT    NOT NULL,
    timestamp       TEXT    DEFAULT (datetime('now'))
);

-- User context and personality summary
CREATE TABLE IF NOT EXISTS user_context (
    user_id             TEXT    NOT NULL,
    guild_id            TEXT    NOT NULL,
    recent_messages     TEXT    DEFAULT '[]',   -- JSON array of last 20 messages
    personality_summary TEXT    DEFAULT NULL,   -- AI-generated personality summary
    interests           TEXT    DEFAULT '[]',   -- JSON array of detected interests
    last_updated        TEXT    DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, guild_id)
);

-- Privacy consent tracking
CREATE TABLE IF NOT EXISTS user_privacy (
    user_id         TEXT    PRIMARY KEY,
    data_collection INTEGER DEFAULT 1,      -- 1 = enabled, 0 = disabled
    opted_out_at    TEXT    DEFAULT NULL,
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, active);
CREATE INDEX IF NOT EXISTS idx_usage_logs_user    ON usage_logs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_logs_guild   ON usage_logs(guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_user_messages      ON user_messages(user_id, guild_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_user_context       ON user_context(user_id, guild_id);
