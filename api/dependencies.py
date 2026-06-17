"""
FastAPI dependency: extract and verify Bearer token.
Returns the verified user object from AuthServer.
"""
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.services.auth_client import verify_identity

_bearer = HTTPBearer(auto_error=True)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> dict:
    """
    Verify the Bearer token with AuthServer and return the user dict.
    Raises 401 if missing or invalid.
    """
    return await verify_identity(credentials.credentials)


def get_token(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> str:
    """Return the raw token string (used when we need to forward it downstream)."""
    return credentials.credentials
