"""
Object Server client — handles file uploads and retrieval.
Files uploaded here are stored persistently and can be accessed via permanent links.
"""
import httpx
from pathlib import Path
from api.config import settings


async def upload_file(file_bytes: bytes, filename: str, bookid: str) -> dict:
    """
    Upload a file to Object Server.
    Returns: {"code": "...", "link": "https://files.my365biz.com/files/...", ...}
    """
    base_url = "https://files.my365biz.com"

    async with httpx.AsyncClient(timeout=60.0) as client:
        files = {
            "file": (filename, file_bytes),
            "bookid": (None, bookid),
        }
        resp = await client.post(
            f"{base_url}/upload",
            files=files,
            headers={"X-API-Key": settings.object_server_api_key},
        )

    if resp.status_code != 200:
        raise RuntimeError(
            f"Object Server upload failed: {resp.status_code} {resp.text}"
        )

    data = resp.json()
    return {
        "code": data["code"],
        "link": data["link"],
        "image_url": data.get("imageUrl"),
    }


async def rename_file(code: str, new_name: str) -> None:
    """Rename a file in Object Server (updates metadata only)."""
    base_url = "https://files.my365biz.com"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/rename",
            json={"code": code, "newName": new_name},
            headers={"X-API-Key": settings.object_server_api_key},
        )

    if resp.status_code != 200:
        raise RuntimeError(
            f"Object Server rename failed: {resp.status_code} {resp.text}"
        )
