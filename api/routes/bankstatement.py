"""
Bank Statement API routes.

POST   /api/v1/bankstatement/upload               — upload PDF, create task
GET    /api/v1/bankstatement/tasks                — list tasks for a book
GET    /api/v1/bankstatement/tasks/{task_id}      — task detail
GET    /api/v1/bankstatement/tasks/{task_id}/stream   — SSE progress stream
GET    /api/v1/bankstatement/tasks/{task_id}/download — download Excel result
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form, status
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse

from api.config import settings
from api.dependencies import get_current_user, get_token
from api.services.auth_client import check_permission, verify_identity, get_current_book
from api.services import queue
from api.services.object_server import upload_file as upload_to_object_server
from api.db import queries
from api.models.bankstatement import UploadResponse, TaskListItem, TaskDetail

router = APIRouter(prefix="/api/v1/bankstatement", tags=["bankstatement"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _user_book_ids(user: dict) -> set[str]:
    return {b["book_id"] for b in user.get("books", [])}


async def _assert_task_access(task_id: str, user: dict) -> dict:
    """Fetch task and verify the user belongs to its book."""
    task = await queries.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task["book_id"] not in _user_book_ids(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return task


# ── POST /upload ──────────────────────────────────────────────────────────────

@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_pdf(
    file: UploadFile = File(..., description="Bank statement PDF"),
    task_name: str = Form(None),
    user: dict = Depends(get_current_user),
    token: str = Depends(get_token),
):
    """
    Upload bank statement PDF for processing.
    Book is determined by AuthServer's current-book context — no book_id needed.
    """
    # Get the user's currently active book from AuthServer
    current_book = await get_current_book(token)
    book_id = current_book["book_id"]

    # Upload is a mutating operation — require book.write permission
    await check_permission(token, book_id, action="book.write")

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only PDF files are accepted",
        )

    task_id = str(uuid.uuid4())

    # Read PDF content
    content = await file.read()

    # Upload to Object Server
    try:
        upload_result = await upload_to_object_server(
            file_bytes=content,
            filename=file.filename,
            bookid=book_id,
        )
        pdf_link = upload_result["link"]
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to upload file to Object Server: {exc}",
        )

    # Persist task record (now with Object Server link instead of local path)
    row = await queries.insert_task(
        task_id=task_id,
        book_id=book_id,
        user_id=user["user_id"],
        pdf_path=pdf_link,  # Now this is the Object Server link
        task_name=task_name or file.filename,
    )

    # Enqueue for dispatch — worker is started by the background dispatcher
    # which enforces the MAX_RPM rate limit before spawning Docker containers
    await queue.enqueue_task(task_id)

    return UploadResponse(
        task_id=row["task_id"],
        status=row["status"],
        task_name=row["task_name"],
        created_at=row["created_at"],
    )


# ── GET /tasks ────────────────────────────────────────────────────────────────

@router.get("/tasks", response_model=list[TaskListItem])
async def list_tasks(
    user: dict = Depends(get_current_user),
):
    """
    List all bank statement tasks for the authenticated user's books.
    Books are automatically extracted from the user's AuthServer identity.
    """
    # Extract book_ids from user's books
    book_ids = [b["book_id"] for b in user.get("books", [])]

    if not book_ids:
        return []

    rows = await queries.list_tasks(book_ids)
    return [TaskListItem(**r) for r in rows]


# ── GET /tasks/{task_id} ──────────────────────────────────────────────────────

@router.get("/tasks/{task_id}", response_model=TaskDetail)
async def get_task(
    task_id: str,
    user: dict = Depends(get_current_user),
):
    task = await _assert_task_access(task_id, user)
    return TaskDetail(**task)


# ── GET /tasks/{task_id}/stream ───────────────────────────────────────────────

@router.get("/tasks/{task_id}/stream")
async def stream_task(
    task_id: str,
    token: str = Query(..., description="Bearer token (SSE cannot set headers)"),
):
    # Verify token and task access
    user = await verify_identity(token)
    task = await _assert_task_access(task_id, user)

    async def event_generator():
        last_index = 0
        heartbeat_counter = 0

        while True:
            events, current_status = await queries.get_stream_events(task_id)

            # Emit any new events since last poll
            new_events = events[last_index:]
            for ev in new_events:
                yield f"data: {json.dumps(ev)}\n\n"
                last_index += 1

            # Close stream when task is terminal
            if current_status in ("completed", "failed"):
                # If client connected after task finished with no events emitted,
                # send a synthetic terminal event so the client is not left hanging
                if last_index == 0:
                    yield f"data: {json.dumps({'event': current_status, 'synthetic': True})}\n\n"
                break

            # Heartbeat every ~15s (10 × 1.5s polls)
            heartbeat_counter += 1
            if heartbeat_counter >= 10:
                yield ": heartbeat\n\n"
                heartbeat_counter = 0

            await asyncio.sleep(1.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


# ── GET /tasks/{task_id}/download ─────────────────────────────────────────────

@router.get("/tasks/{task_id}/download")
async def download_result(
    task_id: str,
    user: dict = Depends(get_current_user),
):
    """
    Redirect to Object Server file link for download.
    File is stored in Object Server, not on this server.
    """
    task = await _assert_task_access(task_id, user)

    if task["status"] != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task is not completed yet (status: {task['status']})",
        )

    file_link = task.get("file_link")
    if not file_link:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Excel output file not found on Object Server",
        )

    # Validate that the redirect target is our Object Server (prevent open redirect)
    allowed_prefix = settings.object_server_base_url.rstrip("/")
    if not file_link.startswith(allowed_prefix):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Stored file link does not point to the configured Object Server",
        )

    return RedirectResponse(url=file_link, status_code=status.HTTP_302_FOUND)
