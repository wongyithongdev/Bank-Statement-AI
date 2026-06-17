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
    file_link: str | None  # Object Server link to download Excel
    error: str | None
    stream_events: list[Any]
    chat_history: list[Any] | None
    created_at: datetime
    updated_at: datetime
    # Note: score, iterations, token_count are internal only, not exposed to external API
