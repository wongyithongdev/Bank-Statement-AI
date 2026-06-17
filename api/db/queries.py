"""
All SQL queries for the bankstatement API.
Uses asyncpg directly — no ORM.
"""
import json
from datetime import datetime, timezone
from api.db.connection import get_pool


async def insert_task(
    task_id: str,
    book_id: str,
    user_id: str,
    pdf_path: str,
    task_name: str,
) -> dict:
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO bankstatement (task_id, book_id, user_id, status, pdf_path, task_name)
        VALUES ($1, $2, $3, 'queued', $4, $5)
        RETURNING task_id, book_id, user_id, status, task_name, created_at, updated_at
        """,
        task_id, book_id, user_id, pdf_path, task_name,
    )
    return dict(row)


async def get_task(task_id: str) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT task_id, book_id, user_id, status, pdf_path, xlsx_path,
               task_name, file_link, score, iterations, error,
               stream_events, created_at, updated_at
        FROM bankstatement
        WHERE task_id = $1
        """,
        task_id,
    )
    if row is None:
        return None
    return dict(row)


async def list_tasks(book_ids: list[str]) -> list[dict]:
    """List tasks for multiple books (summary only)."""
    pool = get_pool()
    if not book_ids:
        return []
    rows = await pool.fetch(
        """
        SELECT task_id, task_name, book_id, user_id, status, created_at
        FROM bankstatement
        WHERE book_id = ANY($1)
        ORDER BY created_at DESC
        """,
        book_ids,
    )
    return [dict(r) for r in rows]


async def update_task_status(
    task_id: str,
    status: str,
    *,
    score: float | None = None,
    iterations: int | None = None,
    xlsx_path: str | None = None,
    file_link: str | None = None,
    error: str | None = None,
) -> None:
    pool = get_pool()
    await pool.execute(
        """
        UPDATE bankstatement
        SET status      = $2,
            score       = COALESCE($3, score),
            iterations  = COALESCE($4, iterations),
            xlsx_path   = COALESCE($5, xlsx_path),
            file_link   = COALESCE($6, file_link),
            error       = COALESCE($7, error),
            updated_at  = now()
        WHERE task_id = $1
        """,
        task_id, status, score, iterations, xlsx_path, file_link, error,
    )


async def append_stream_event(task_id: str, event: dict) -> None:
    """Atomically append one event to the stream_events JSONB array."""
    pool = get_pool()
    await pool.execute(
        """
        UPDATE bankstatement
        SET stream_events = stream_events || $2::jsonb,
            updated_at    = now()
        WHERE task_id = $1
        """,
        task_id,
        json.dumps([event]),
    )


async def get_stream_events(task_id: str) -> tuple[list, str]:
    """Returns (stream_events list, current status)."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT stream_events, status FROM bankstatement WHERE task_id = $1",
        task_id,
    )
    if row is None:
        return [], "not_found"
    events = row["stream_events"] or []
    return list(events), row["status"]
