"""Postgres connection + schema migration.

Single source for the async connection pool and the schema this agent
writes to. Two tables:

  spider_profiles   Persistent browser user-data (cookies, localStorage,
                    extensions, history) — reusable across sessions and
                    across container restarts. Stored as a JSONB
                    storage_state + a BYTEA zip of the user-data dir.

  spider_sessions   Long-lived browser session records. Linkable to a
                    profile via ``profile_name``. The actual
                    BrowserContext lives in memory in ``SessionManager``;
                    this table is for crash recovery + cross-replica
                    visibility.

Configuration
-------------

``POSTGRES_URL`` env: ``postgresql://user:pass@host:5432/spider``.
When unset, ``connect()`` returns ``None`` and the rest of the agent
treats persistence features as disabled (in-memory sessions, no
profiles). This keeps the agent operable in a "fast local dev"
deployment without postgres.

Schema migration
----------------

We don't use Alembic — the schema is small, additive-only, and shipped
inline as a single ``_SCHEMA_SQL`` blob run inside an ``IF NOT EXISTS``
guard on every connect. Operators who want richer migrations can layer
that on later.
"""
from __future__ import annotations

import os
from typing import Any

from .log import log

# PEP 695 type alias for the pool ABI we use.
type AsyncPgPool = Any  # asyncpg.Pool, but kept loose to avoid import at module load


_SCHEMA_SQL = """
-- ─── profiles ───────────────────────────────────────────────────
-- Reusable browser identities: cookies, localStorage, indexedDB,
-- extensions, user history. A profile can be attached to many sessions
-- over its lifetime.
CREATE TABLE IF NOT EXISTS spider_profiles (
    id              TEXT        PRIMARY KEY,
    name            TEXT        NOT NULL UNIQUE,
    backend         TEXT        NOT NULL DEFAULT 'cloak',
    -- storage_state is Playwright's portable session JSON
    -- (cookies + origins/localStorage). Small, easy to merge.
    storage_state   JSONB,
    -- user_data_zip is the full user-data dir compressed. Larger
    -- (~tens of MB for a logged-in profile). Optional.
    user_data_zip   BYTEA,
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_spider_profiles_last_used
    ON spider_profiles (last_used DESC);

CREATE INDEX IF NOT EXISTS idx_spider_profiles_backend
    ON spider_profiles (backend);

-- ─── sessions ───────────────────────────────────────────────────
-- Long-lived BrowserContext records, optionally linked to a profile.
-- Authoritative state for restart recovery: a process restart can
-- re-instantiate live BrowserContexts from the storage_state here.
CREATE TABLE IF NOT EXISTS spider_sessions (
    id              TEXT        PRIMARY KEY,
    name            TEXT,
    backend         TEXT        NOT NULL DEFAULT 'cloak',
    profile_name    TEXT,                      -- soft FK; ON DELETE breaks it
    storage_state   JSONB,
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    -- The pool process that owns this session in-memory; on restart
    -- we treat orphan rows as inactive. ``NULL`` = no live process.
    owner_pid       TEXT
);

CREATE INDEX IF NOT EXISTS idx_spider_sessions_last_used
    ON spider_sessions (last_used DESC);

CREATE INDEX IF NOT EXISTS idx_spider_sessions_profile_name
    ON spider_sessions (profile_name);

CREATE INDEX IF NOT EXISTS idx_spider_sessions_expires_at
    ON spider_sessions (expires_at)
    WHERE expires_at IS NOT NULL;

-- ─── trigger: keep updated_at honest on profile mutations ───────
CREATE OR REPLACE FUNCTION spider_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_spider_profiles_updated_at ON spider_profiles;
CREATE TRIGGER trg_spider_profiles_updated_at
    BEFORE UPDATE ON spider_profiles
    FOR EACH ROW
    EXECUTE FUNCTION spider_touch_updated_at();
"""


# ─── public ─────────────────────────────────────────────────────────
async def connect() -> AsyncPgPool | None:
    """Connect to postgres if ``POSTGRES_URL`` is set, else return None.

    The returned pool is an ``asyncpg.Pool`` (or None). Schema is
    migrated in the same call, so the caller always gets a usable
    pool. Errors during connect are surfaced; errors during migration
    leave the pool open so the operator can debug — they're logged at
    WARN.
    """
    url = (os.environ.get("POSTGRES_URL") or "").strip()
    if not url:
        log("storage", state="disabled", reason="POSTGRES_URL unset")
        return None

    try:
        import asyncpg
    except ImportError as e:
        log(
            "storage",
            state="disabled",
            reason="asyncpg not installed",
            msg=str(e),
        )
        return None

    pool = await asyncpg.create_pool(
        url,
        min_size=int(os.environ.get("POSTGRES_POOL_MIN", "1")),
        max_size=int(os.environ.get("POSTGRES_POOL_MAX", "5")),
        command_timeout=float(os.environ.get("POSTGRES_COMMAND_TIMEOUT_S", "30")),
    )
    log("storage", state="pool-opened", min=1, max=5)

    # Migrate inline. The schema uses IF NOT EXISTS / OR REPLACE so
    # running it twice is fine.
    try:
        async with pool.acquire() as conn:
            await conn.execute(_SCHEMA_SQL)
        log("storage", state="schema-migrated")
    except Exception as e:  # noqa: BLE001
        log(
            "storage",
            state="schema-migration-error",
            msg=f"{type(e).__name__}: {e}",
        )

    return pool


async def close(pool: AsyncPgPool | None) -> None:
    """Close the pool. Idempotent. None-safe."""
    if pool is None:
        return
    try:
        await pool.close()
        log("storage", state="pool-closed")
    except Exception as e:  # noqa: BLE001 - cleanup is best-effort
        log("storage", state="pool-close-error", msg=f"{type(e).__name__}: {e}")


# ─── query helpers — kept here so SQL is centralized ────────────────
# Profiles:
SQL_PROFILE_INSERT = """
INSERT INTO spider_profiles (id, name, backend, storage_state,
                             user_data_zip, metadata)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (name) DO UPDATE
SET backend = EXCLUDED.backend,
    storage_state = EXCLUDED.storage_state,
    user_data_zip = EXCLUDED.user_data_zip,
    metadata = EXCLUDED.metadata,
    updated_at = NOW()
RETURNING id, name, backend, metadata, created_at, updated_at, last_used,
          (storage_state IS NOT NULL) AS has_storage_state,
          (user_data_zip IS NOT NULL) AS has_user_data_zip,
          octet_length(user_data_zip) AS user_data_zip_bytes
"""

SQL_PROFILE_LIST = """
SELECT id, name, backend, metadata, created_at, updated_at, last_used,
       (storage_state IS NOT NULL) AS has_storage_state,
       (user_data_zip IS NOT NULL) AS has_user_data_zip,
       octet_length(user_data_zip) AS user_data_zip_bytes
FROM spider_profiles
ORDER BY last_used DESC
LIMIT $1
"""

SQL_PROFILE_GET = """
SELECT id, name, backend, metadata, created_at, updated_at, last_used,
       (storage_state IS NOT NULL) AS has_storage_state,
       (user_data_zip IS NOT NULL) AS has_user_data_zip,
       octet_length(user_data_zip) AS user_data_zip_bytes
FROM spider_profiles
WHERE name = $1
"""

SQL_PROFILE_GET_FULL = """
SELECT id, name, backend, storage_state, user_data_zip, metadata,
       created_at, updated_at, last_used
FROM spider_profiles
WHERE name = $1
"""

SQL_PROFILE_DELETE = """
DELETE FROM spider_profiles WHERE name = $1 RETURNING id
"""

SQL_PROFILE_TOUCH = """
UPDATE spider_profiles SET last_used = NOW() WHERE name = $1
"""

# Sessions (used by SessionManager's postgres-persistent mode):
SQL_SESSION_UPSERT = """
INSERT INTO spider_sessions (id, name, backend, profile_name,
                             storage_state, metadata, expires_at, owner_pid)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (id) DO UPDATE
SET name = EXCLUDED.name,
    backend = EXCLUDED.backend,
    profile_name = EXCLUDED.profile_name,
    storage_state = EXCLUDED.storage_state,
    metadata = EXCLUDED.metadata,
    expires_at = EXCLUDED.expires_at,
    owner_pid = EXCLUDED.owner_pid,
    last_used = NOW()
"""

SQL_SESSION_GET = """
SELECT id, name, backend, profile_name, storage_state, metadata,
       created_at, last_used, expires_at, owner_pid
FROM spider_sessions
WHERE id = $1
"""

SQL_SESSION_LIST = """
SELECT id, name, backend, profile_name, metadata, created_at,
       last_used, expires_at
FROM spider_sessions
WHERE expires_at IS NULL OR expires_at > NOW()
ORDER BY last_used DESC
LIMIT $1
"""

SQL_SESSION_DELETE = """
DELETE FROM spider_sessions WHERE id = $1 RETURNING id
"""


__all__ = [
    "AsyncPgPool",
    "SQL_PROFILE_DELETE",
    "SQL_PROFILE_GET",
    "SQL_PROFILE_GET_FULL",
    "SQL_PROFILE_INSERT",
    "SQL_PROFILE_LIST",
    "SQL_PROFILE_TOUCH",
    "SQL_SESSION_DELETE",
    "SQL_SESSION_GET",
    "SQL_SESSION_LIST",
    "SQL_SESSION_UPSERT",
    "close",
    "connect",
]
