"""DDL схемы libsql (контракт из README, раздел «Структура БД»)."""
from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    api_key_hash TEXT NOT NULL UNIQUE,
    quota_day INTEGER NOT NULL DEFAULT 1000,
    quota_month INTEGER NOT NULL DEFAULT 30000,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS models (
    provider_id TEXT NOT NULL,
    name TEXT NOT NULL,
    price_in REAL NOT NULL DEFAULT 0,
    price_out REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (provider_id, name)
);
CREATE TABLE IF NOT EXISTS cache_entries (
    prompt_hash CHAR(64) NOT NULL,
    pool TEXT NOT NULL,
    response TEXT NOT NULL,
    embedding BLOB NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (prompt_hash, pool)
);
CREATE TABLE IF NOT EXISTS requests_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    pool TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    pool TEXT NOT NULL DEFAULT '',
    history TEXT NOT NULL DEFAULT '[]',
    tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
-- Персист конфига релея (переживает рестарт). Сырые ключи НЕ хранятся:
-- в provider_keys лежит только masked, сами ключи читаются из env.
CREATE TABLE IF NOT EXISTS provider_keys (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    masked TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_check_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS provider_models (
    provider_id TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (provider_id, name)
);
CREATE TABLE IF NOT EXISTS pools (
    id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL DEFAULT 'cascade',
    members_json TEXT NOT NULL DEFAULT '[]',
    unlock_on TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS quotas (
    key TEXT PRIMARY KEY,
    limit_day INTEGER NOT NULL DEFAULT 0,
    limit_month INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS model_verdicts (
    model_key TEXT PRIMARY KEY,
    verdict TEXT NOT NULL DEFAULT '',
    served_id TEXT NOT NULL DEFAULT '',
    ceiling INTEGER NOT NULL DEFAULT 0,
    error_class TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    checked_at INTEGER NOT NULL DEFAULT 0
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);
CREATE INDEX IF NOT EXISTS idx_log_ts ON requests_log(ts);
CREATE INDEX IF NOT EXISTS idx_log_user_ts ON requests_log(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);
"""
