from datetime import datetime
from typing import Any
from pydantic import BaseModel


class UploadResponse(BaseModel):
    task_id: str
    status: str
    task_name: str | None
    created_at: datetime


class TaskListItem(BaseModel):
    task_id: str
    task_name: str | None
    book_id: str
    user_id: str
    status: str
    created_at: datetime


class TaskDetail(BaseModel):
    task_id: str
    book_id: str
    user_id: str
    status: str
    task_name: str | None
    score: float | None
    iterations: int | None
    file_link: str | None
    error: str | None
    stream_events: list[Any]
    chat_history: list[Any] | None  # Full conversation with Claude (Generator + Evaluator)
    created_at: datetime
    updated_at: datetime
