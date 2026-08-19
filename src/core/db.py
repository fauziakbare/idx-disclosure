"""Async SQLite database layer for disclosure storage."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from src.config.settings import settings

logger = logging.getLogger(__name__)

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

def _is_turso() -> bool:
    return bool(settings.TURSO_AUTH_TOKEN) and settings.DATABASE_URL.startswith("libsql://")

def _turso_client():
    try:
        import libsql_client
        return libsql_client.create_client(settings.DATABASE_URL, auth_token=settings.TURSO_AUTH_TOKEN)
    except ImportError:
        pass
    import libsql_experimental as lsx
    return lsx.connect(settings.DATABASE_URL, auth_token=settings.TURSO_AUTH_TOKEN)

async def init_db() -> None:
    """Create tables and indexes if not exist."""
    if _is_turso():
        _turso_client().execute(_DDL)
        logger.info("Database initialized on Turso (%s)", settings.DATABASE_URL)
        return
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
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
    path = _db_path()
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
    if _is_turso():
        _turso_client().execute(sql, list(values))
    else:
        async with aiosqlite.connect(path) as db:
            await db.execute(sql, values)
            await db.commit()
    logger.info("Saved analysis for %s (%s)", emiten_code, disclosure_id)


async def _get_column(emiten_code: str, column: str) -> Any:
    """Query single JSON column by emiten_code, return latest row."""
    path = _db_path()
    sql = f"SELECT {column} FROM disclosures WHERE emiten_code = ? AND {column} IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
    if _is_turso():
        res = _turso_client().execute(sql, [emiten_code])
        rows = res.rows
        row = rows[0] if rows else None
    else:
        async with aiosqlite.connect(path) as db:
            cursor = await db.execute(sql, (emiten_code,))
            row = await cursor.fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return row[0]


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
    path = _db_path()
    cols = "id, emiten_code, title, release_date, filter_status, urgency_score, is_material, extracted_data, financial_analysis, draft_x, draft_threads, is_sent_to_telegram, created_at, updated_at"
    sql = "SELECT " + cols + " FROM disclosures WHERE emiten_code = ? ORDER BY updated_at DESC LIMIT 1"
    if _is_turso():
        res = _turso_client().execute(sql, [emiten_code])
        rows = res.rows
        tup = rows[0] if rows else None
    else:
        async with aiosqlite.connect(path) as db:
            cursor = await db.execute(sql, (emiten_code,))
            tup = await cursor.fetchone()
    if tup is None:
        return None
    names = [c.strip() for c in cols.split(",")]
    d = dict(zip(names, tup))
    for col in ("extracted_data", "financial_analysis", "draft_x", "draft_threads"):
        if d.get(col):
            try:
                d[col] = json.loads(d[col])
            except (json.JSONDecodeError, TypeError):
                pass
    return d