"""Async SQLite/Turso database layer for disclosure storage.

Provides unified async facade over two backends:
- Local SQLite via ``aiosqlite`` (default, offline MVP)
- Turso Cloud via ``libsql_client``/``libsql_experimental`` (Phase 2)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from src.config.settings import settings
from src.core.logger import logger

_DDL = """
CREATE TABLE IF NOT EXISTS disclosures (
    id TEXT PRIMARY KEY,
    emiten_code TEXT NOT NULL,
    title TEXT NOT NULL,
    release_date DATETIME,
    filter_status TEXT NOT NULL DEFAULT 'PENDING_FILTER',
    urgency_score INTEGER,
    is_material BOOLEAN DEFAULT 0,
    extracted_data TEXT,
    financial_analysis TEXT,
    draft_x TEXT,
    draft_threads TEXT,
    is_sent_to_telegram BOOLEAN DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_filter_status ON disclosures(filter_status);
CREATE INDEX IF NOT EXISTS idx_release_date ON disclosures(release_date);
CREATE INDEX IF NOT EXISTS idx_emiten ON disclosures(emiten_code);
"""


def _db_path() -> str:
    """Extract file path from DATABASE_URL setting."""
    url = settings.DATABASE_URL
    # sqlite+aiosqlite:///./idx_watcher.db -> ./idx_watcher.db
    if ":///" in url:
        return url.split(":///", 1)[1]
    return url


def is_turso() -> bool:
    """True when DATABASE_URL points to libsql:// and token set."""
    return bool(settings.TURSO_AUTH_TOKEN) and settings.DATABASE_URL.startswith("libsql://")


class TursoClient:
    """Async wrapper over Turso/libsql (libsql_client async Client).

    Mirrors the subset of aiosqlite API used by this project, so call sites
    do not branch on backend. Retries transient failures 3x w/ backoff.
    """

    _MAX_RETRIES = 3
    _BASE_BACKOFF_S = 1.0

    def __init__(self) -> None:
        self._client: Any = None

    async def _lazy_connect(self) -> Any:
        """Connect on first use; return raw async libsql client."""
        if self._client is not None:
            return self._client
        from libsql_client.http import HttpClient

        http_url = settings.DATABASE_URL.replace("libsql://", "https://", 1)
        self._client = HttpClient(
            http_url,
            auth_token=settings.TURSO_AUTH_TOKEN,
        )
        logger.info("Turso backend: libsql_client HttpClient (https)")
        return self._client

    async def execute(self, sql: str, parameters: list[Any] | None = None) -> Any:
        """Execute SQL with retry. Returns raw result rows/list."""
        client = await self._lazy_connect()
        last_exc: Exception | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                result = await client.execute(sql, parameters or [])
                return result
            except Exception as e:  # transient network / busy
                last_exc = e
                if attempt < self._MAX_RETRIES:
                    delay = self._BASE_BACKOFF_S * (2 ** (attempt - 1))
                    logger.warning(
                        "Turso execute attempt %d/%d failed: %s — retry in %.1fs",
                        attempt,
                        self._MAX_RETRIES,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    async def exec_ddl(self, ddl: str) -> None:
        """Run multi-statement DDL. libsql_client has no executescript;
        split on ';' and run sequentially."""
        client = await self._lazy_connect()
        for stmt in ddl.split(";"):
            s = stmt.strip()
            if not s:
                continue
            try:
                await client.execute(s, [])
            except Exception as e:
                # ignore "no such table/duplicate index" on re-init for idempotency
                msg = str(e).lower()
                if "no such" in msg or "already exists" in msg:
                    logger.debug("Turso DDL idempotent skip: %s", e)
                    continue
                raise
        logger.info("Turso DDL executed")

    @staticmethod
    def rows_of(result: Any) -> list[tuple[Any, ...]]:
        """Normalize result into list of tuples (libsql_client returns Row objects)."""
        rows = getattr(result, "rows", None)
        if rows is None:
            return []
        return [tuple(r) for r in rows]

    async def close(self) -> None:
        """Close underlying client session (async-safe, idempotent)."""
        if self._client is None:
            return
        client = self._client
        self._client = None
        await client.close()


# Module-level lazy singleton
_turso: TursoClient | None = None


def _get_turso() -> TursoClient:
    global _turso
    if _turso is None:
        _turso = TursoClient()
    return _turso


async def close_db() -> None:
    """Close Turso client session if open. No-op for local sqlite."""
    global _turso
    if _turso is not None:
        await _turso.close()
        _turso = None
        logger.info("Turso client session closed")


async def init_db() -> None:
    """Create tables and indexes if not exist."""
    if is_turso():
        t = _get_turso()
        await t.exec_ddl(_DDL)
        logger.info("Database initialized on Turso (%s)", settings.DATABASE_URL)
        return
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    import aiosqlite

    async with aiosqlite.connect(path) as db:
        await db.executescript(_DDL)
        await db.commit()
    logger.info("Database initialized at %s", path)


async def save_analysis(
    disclosure_id: str,
    emiten_code: str,
    title: str,
    *,
    draft_x: dict[str, Any] | list[Any] | None = None,
    draft_threads: dict[str, Any] | list[Any] | None = None,
    financial_analysis: dict[str, Any] | None = None,
    extracted_data: dict[str, Any] | None = None,
    urgency_score: int | None = None,
    is_material: bool | None = None,
    filter_status: str = "ANALYZED",
    release_date: str | None = None,
) -> None:
    """Upsert disclosure analysis result into database."""
    sql = """
        INSERT INTO disclosures (
            id, emiten_code, title, release_date, filter_status,
            urgency_score, is_material, extracted_data,
            financial_analysis, draft_x, draft_threads, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            emiten_code = excluded.emiten_code,
            title = excluded.title,
            release_date = COALESCE(excluded.release_date, disclosures.release_date),
            filter_status = excluded.filter_status,
            urgency_score = COALESCE(excluded.urgency_score, disclosures.urgency_score),
            is_material = COALESCE(excluded.is_material, disclosures.is_material),
            extracted_data = COALESCE(excluded.extracted_data, disclosures.extracted_data),
            financial_analysis = COALESCE(excluded.financial_analysis, disclosures.financial_analysis),
            draft_x = COALESCE(excluded.draft_x, disclosures.draft_x),
            draft_threads = COALESCE(excluded.draft_threads, disclosures.draft_threads),
            updated_at = datetime('now')
    """
    values = (
        disclosure_id,
        emiten_code,
        title,
        release_date,
        filter_status,
        urgency_score,
        is_material,
        json.dumps(extracted_data) if extracted_data else None,
        json.dumps(financial_analysis) if financial_analysis else None,
        json.dumps(draft_x) if draft_x else None,
        json.dumps(draft_threads) if draft_threads else None,
    )
    t0 = time.perf_counter()
    if is_turso():
        await _get_turso().execute(sql, list(values))
    else:
        import aiosqlite

        path = _db_path()
        async with aiosqlite.connect(path) as db:
            await db.execute(sql, values)
            await db.commit()
    logger.info(
        "Saved analysis for %s (%s)",
        emiten_code,
        disclosure_id,
        extra={"filter_status": filter_status},
    )
    logger.debug(
        "save_analysis duration_ms=%.1f emiten=%s",
        (time.perf_counter() - t0) * 1000,
        emiten_code,
    )


async def _fetch_rows(sql: str, sql_params: tuple[Any, ...] | list[Any]) -> list[tuple[Any, ...]]:
    """Run SELECT and return list of row tuples (backend-agnostic)."""
    t0 = time.perf_counter()
    if is_turso():
        res = await _get_turso().execute(sql, list(sql_params))
        rows = _get_turso().rows_of(res)
    else:
        import aiosqlite

        path = _db_path()
        async with aiosqlite.connect(path) as db:
            cursor = await db.execute(sql, sql_params)
            rows = await cursor.fetchall()
    logger.debug(
        "query duration_ms=%.1f sql=%s",
        (time.perf_counter() - t0) * 1000,
        sql.split()[0],
    )
    return rows


async def _get_column(emiten_code: str, column: str) -> Any:
    """Query single JSON column by emiten_code, return latest row."""
    sql = (
        f"SELECT {column} FROM disclosures WHERE emiten_code = ? "
        f"AND {column} IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
    )
    rows = await _fetch_rows(sql, (emiten_code,))
    if not rows or rows[0][0] is None:
        return None
    try:
        return json.loads(rows[0][0])
    except (json.JSONDecodeError, TypeError):
        return rows[0][0]


async def get_draft_x(emiten_code: str) -> Any:
    """Get draft_x JSON for emiten."""
    return await _get_column(emiten_code, "draft_x")


async def get_draft_threads(emiten_code: str) -> Any:
    """Get draft_threads JSON for emiten."""
    return await _get_column(emiten_code, "draft_threads")


async def get_financial_analysis(emiten_code: str) -> Any:
    """Get financial_analysis JSON for emiten."""
    return await _get_column(emiten_code, "financial_analysis")


async def get_disclosure_detail(emiten_code: str) -> dict[str, Any] | None:
    """Get full disclosure row as dict."""
    cols = "id, emiten_code, title, release_date, filter_status, urgency_score, is_material, extracted_data, financial_analysis, draft_x, draft_threads, is_sent_to_telegram, created_at, updated_at"
    sql = (
        "SELECT " + cols + " FROM disclosures "
        "WHERE emiten_code = ? ORDER BY updated_at DESC LIMIT 1"
    )
    rows = await _fetch_rows(sql, (emiten_code,))
    if not rows:
        return None
    names = [c.strip() for c in cols.split(",")]
    d = dict(zip(names, rows[0]))
    for col in ("extracted_data", "financial_analysis", "draft_x", "draft_threads"):
        if d.get(col):
            try:
                d[col] = json.loads(d[col])
            except (json.JSONDecodeError, TypeError):
                pass
    return d