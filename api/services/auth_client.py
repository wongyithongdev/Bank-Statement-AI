"""
HTTP client for AuthServer identity and permission checks.
  GET  /api/v1/users/me              → user identity + book bindings
  POST /api/v1/permissions/check     → allow/deny for a book action
"""
import httpx
from fastapi import HTTPException, status
from api.config import settings


async def verify_identity(token: str) -> dict:
    """
    Call AuthServer GET /api/v1/users/me.
    Returns the full user object including books[].
    Raises 401 if token is invalid, 502 if AuthServer is unreachable.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.auth_server_url}/api/v1/users/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AuthServer unreachable: {exc}",
        )

    if resp.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AuthServer returned {resp.status_code}",
        )
    return resp.json()


async def check_permission(token: str, book_id: str, action: str = "book.read") -> None:
    """
    Call AuthServer POST /api/v1/permissions/check.
    Raises 403 if denied, 502 if AuthServer is unreachable.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.auth_server_url}/api/v1/permissions/check",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"book_id": book_id, "action": action},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AuthServer unreachable: {exc}",
        )

    if resp.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AuthServer returned {resp.status_code}",
        )

    data = resp.json()
    if not data.get("allowed"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {action} on {book_id}. "
                   f"Reason: {data.get('reason', 'insufficient role')}",
        )
