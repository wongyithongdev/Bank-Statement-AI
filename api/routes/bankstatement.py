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
from fastapi.responses import FileResponse, StreamingResponse

from api.config import settings
from api.dependencies import get_current_user, get_token
from api.services.auth_client import check_permission, verify_identity
from api.services.docker_runner import start_worker
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
    book_id: str = Form(None, description="Book ID (optional, defaults to first book)"),
    task_name: str = Form(None),
    user: dict = Depends(get_current_user),
    token: str = Depends(get_token),
):
    # Get user's books from AuthServer identity
    user_books = user.get("books", [])
    if not user_books:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not belong to any books",
        )

    # Determine which book to use
    if not book_id:
        book_id = user_books[0]["book_id"]
    else:
        # Verify the requested book is in user's books
        if book_id not in [b["book_id"] for b in user_books]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User does not have access to book {book_id}",
            )

    # Verify book.read permission
    await check_permission(token, book_id)

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

    # Start isolated Docker worker
    try:
        start_worker(task_id)
    except RuntimeError as exc:
        # Mark as failed immediately if Docker can't start
        await queries.update_task_status(task_id, "failed", error=str(exc))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

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
    task = await _assert_task_access(task_id, user)

    if task["status"] != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task is not completed yet (status: {task['status']})",
        )

    xlsx_path = task.get("xlsx_path")
    if not xlsx_path or not Path(xlsx_path).exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Excel output file not found",
        )

    filename = (task.get("task_name") or task_id).replace(" ", "_") + ".xlsx"
    return FileResponse(
        path=xlsx_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
