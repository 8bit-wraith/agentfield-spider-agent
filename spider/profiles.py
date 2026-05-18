"""Browser profile management — persistent user identities.

A *profile* is a saved browser identity: cookies, localStorage,
extensions, history, login state. Profiles persist across container
restarts (stored in postgres) and across sessions (one profile can be
attached to many sessions).

This is distinct from a *session* (see ``spider/sessions.py``):

  - **Session**: an isolated, in-memory ``BrowserContext`` on the
    live pool. Cheap, ephemeral, optionally backed by a profile for
    persistence.
  - **Profile**: a postgres-persisted blob of cookies + storage that
    a session can be initialized FROM and saved BACK TO. Survives
    restarts, replicas, schema migrations.

Storage format
--------------

Two formats stored side-by-side:

1. ``storage_state`` (JSONB) — Playwright's portable session JSON. Used
   when creating a fresh ``BrowserContext(storage_state=...)``. Small
   (KB), easy to merge across replicas.
2. ``user_data_zip`` (BYTEA) — Zipped browser user-data directory. Used
   when launching a persistent context with full extensions, history,
   etc. Larger (tens of MB) but covers more state.

Most callers want format 1; format 2 is for special cases (extensions,
indexedDB-heavy state, browser-use sessions that need history).

API
---

  ProfileStore.list(limit) -> [ProfileInfo]
  ProfileStore.get(name) -> ProfileInfo | None
  ProfileStore.fetch_zip(name) -> bytes | None
  ProfileStore.upsert(name, ...) -> ProfileInfo
  ProfileStore.delete(name) -> bool
  ProfileStore.touch(name) -> None
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile
from typing import Any

from pydantic import BaseModel, Field

from . import storage
from .log import log


class ProfileInfo(BaseModel):
    """Public profile record. The bulky ``user_data_zip`` blob is not
    included here — fetch via ``ProfileStore.fetch_zip(name)``.
    """

    id: str
    name: str
    backend: str = "cloak"
    has_storage_state: bool = False
    has_user_data_zip: bool = False
    user_data_zip_bytes: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None       # ISO-8601
    updated_at: str | None = None
    last_used: str | None = None


def _row_to_info(row: Any) -> ProfileInfo:
    """Build a ProfileInfo from an asyncpg Record."""
    return ProfileInfo(
        id=row["id"],
        name=row["name"],
        backend=row["backend"],
        has_storage_state=bool(row.get("has_storage_state", False)),
        has_user_data_zip=bool(row.get("has_user_data_zip", False)),
        user_data_zip_bytes=row.get("user_data_zip_bytes"),
        metadata=dict(row.get("metadata") or {}),
        created_at=_isofmt(row.get("created_at")),
        updated_at=_isofmt(row.get("updated_at")),
        last_used=_isofmt(row.get("last_used")),
    )


def _isofmt(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


# ─── zip helpers ────────────────────────────────────────────────────
def validate_zip(blob: bytes) -> tuple[bool, str]:
    """Smoke-check that ``blob`` is a real, non-corrupt zip.

    Returns ``(ok, error_message)``. Used by the upload endpoint to
    reject hostile / corrupt uploads before they hit the DB.
    """
    if not blob:
        return False, "empty"
    if len(blob) > 256 * 1024 * 1024:  # 256 MiB hard cap
        return False, f"too large ({len(blob)} bytes; cap 256 MiB)"
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            bad = zf.testzip()
            if bad is not None:
                return False, f"corrupt entry: {bad}"
    except zipfile.BadZipFile:
        return False, "not a valid zip"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return True, ""


# ─── ProfileStore ───────────────────────────────────────────────────
class ProfileStore:
    """Postgres-backed CRUD for browser profiles.

    Construct with a live asyncpg pool. When the pool is ``None``
    (postgres unconfigured) all methods become no-ops / return empty
    so the rest of the agent keeps working — features that need
    profiles return clean "feature disabled" responses at the routes
    layer.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: storage.AsyncPgPool | None) -> None:
        self._pool = pool

    @property
    def enabled(self) -> bool:
        return self._pool is not None

    # ─── list ────────────────────────────────────────────────────────
    async def list(self, *, limit: int = 100) -> list[ProfileInfo]:
        if self._pool is None:
            return []
        limit = max(1, min(limit, 500))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(storage.SQL_PROFILE_LIST, limit)
        return [_row_to_info(r) for r in rows]

    # ─── get / fetch_zip ─────────────────────────────────────────────
    async def get(self, name: str) -> ProfileInfo | None:
        if self._pool is None:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(storage.SQL_PROFILE_GET, name)
        if row is None:
            return None
        return _row_to_info(row)

    async def fetch_zip(self, name: str) -> bytes | None:
        """Return the profile's user_data_zip blob, or None if missing."""
        if self._pool is None:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(storage.SQL_PROFILE_GET_FULL, name)
        if row is None:
            return None
        blob: bytes | None = row["user_data_zip"]
        if blob is not None:
            # Touch last_used since this is a real access. Best-effort:
            # cap stalls don't break the export.
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(storage.SQL_PROFILE_TOUCH, name)
            except Exception:  # noqa: BLE001
                pass
        return blob

    async def fetch_storage_state(self, name: str) -> dict[str, Any] | None:
        """Return the parsed Playwright storage_state JSON, or None."""
        if self._pool is None:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(storage.SQL_PROFILE_GET_FULL, name)
        if row is None:
            return None
        ss = row["storage_state"]
        if ss is None:
            return None
        if isinstance(ss, str):
            try:
                return json.loads(ss)
            except json.JSONDecodeError:
                return None
        return dict(ss)

    # ─── upsert ──────────────────────────────────────────────────────
    async def upsert(
        self,
        *,
        name: str,
        backend: str = "cloak",
        storage_state: dict[str, Any] | None = None,
        user_data_zip: bytes | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProfileInfo:
        """Create or replace a profile by name. UPSERT semantics — calls
        ON CONFLICT update on the unique name index.

        Returns the resulting profile info.
        """
        if self._pool is None:
            raise RuntimeError("postgres unavailable; profiles disabled")
        # Validate the zip if present BEFORE touching the DB.
        if user_data_zip is not None:
            ok, err = validate_zip(user_data_zip)
            if not ok:
                raise ValueError(f"invalid user_data_zip: {err}")
        pid = uuid.uuid4().hex[:16]
        meta_json = json.dumps(metadata or {})
        ss_json = json.dumps(storage_state) if storage_state is not None else None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                storage.SQL_PROFILE_INSERT,
                pid,
                name,
                backend,
                ss_json,
                user_data_zip,
                meta_json,
            )
        log("profile", state="upserted", name=name, backend=backend,
            has_zip=bool(user_data_zip))
        return _row_to_info(row)

    # ─── delete ──────────────────────────────────────────────────────
    async def delete(self, name: str) -> bool:
        if self._pool is None:
            return False
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(storage.SQL_PROFILE_DELETE, name)
        deleted = row is not None
        if deleted:
            log("profile", state="deleted", name=name)
        return deleted

    async def touch(self, name: str) -> None:
        """Bump last_used. Used when a session attaches to a profile.

        Best-effort: errors logged, not raised.
        """
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(storage.SQL_PROFILE_TOUCH, name)
        except Exception as e:  # noqa: BLE001
            log("profile", state="touch-error", name=name,
                msg=f"{type(e).__name__}: {e}")


__all__ = ["ProfileInfo", "ProfileStore", "validate_zip"]
