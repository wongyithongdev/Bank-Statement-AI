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
    book_id: str = Form(...),
    task_name: str = Form(None),
    user: dict = Depends(get_current_user),
    token: str = Depends(get_token),
):
    # Verify book.read permission on the target book
    await check_permission(token, book_id)

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only PDF files are accepted",
        )

    task_id = str(uuid.uuid4())
    upload_dir = Path(settings.data_dir) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = str(upload_dir / f"{task_id}.pdf")

    # Save uploaded PDF
    content = await file.read()
    Path(pdf_path).write_bytes(content)

    # Persist task record
    row = await queries.insert_task(
        task_id=task_id,
        book_id=book_id,
        user_id=user["user_id"],
        pdf_path=pdf_path,
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
    book_id: str = Query(...),
    user: dict = Depends(get_current_user),
    token: str = Depends(get_token),
):
    await check_permission(token, book_id)
    rows = await queries.list_tasks(book_id)
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
