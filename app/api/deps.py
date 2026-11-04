"""Shared FastAPI dependencies: database sessions and bearer authentication.

The dependency surface is intentionally small so every router authenticates the
same way:

* ``get_db``       - short-lived SQLAlchemy session (re-export of ``app.db.base``);
* ``get_current_user``    - resolves ``Authorization: Bearer <JWT>`` to a ``User``;
* ``get_current_admin``   - same, but requires the ``admin`` role;
* ``resolve_current_user`` - helper for code that only has a raw token;
* ``is_admin`` / ``user_read`` - small pure helpers shared by handlers.

Missing/expired/invalid credentials always yield HTTP 401 with a
``WWW-Authenticate: Bearer`` challenge (the SSE endpoint relies on this too).
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import TokenError, decode_access_token
from app.db.base import get_db
from app.db.models import User
from app.schemas.auth import UserRead

__all__ = [
    "bearer_scheme",
    "get_current_admin",
    "get_current_user",
    "get_db",
    "is_admin",
    "resolve_current_user",
    "unauthorized",
    "user_read",
]

# ``auto_error=False`` lets us return a 401 (instead of FastAPI's default 403)
# for missing credentials, and keeps the SSE endpoint's auth behaviour explicit.
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="JWT access token returned by /api/auth/login or /api/auth/register",
)


def unauthorized(detail: str = "Not authenticated") -> HTTPException:
    """Build a consistent 401 response for the Bearer security scheme."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def resolve_current_user(token: str, db: Session) -> User:
    """Resolve a raw JWT to a ``User`` row, raising 401 on any problem."""
    token = (token or "").strip()
    if not token:
        raise unauthorized("missing bearer token")
    try:
        payload: dict[str, Any] = decode_access_token(token)
    except TokenError as exc:
        raise unauthorized("invalid or expired token") from exc
    user_id = str(payload.get("sub") or "").strip()
    if not user_id:
        raise unauthorized("token is missing a subject")
    user = db.get(User, user_id)
    if user is None:
        raise unauthorized("user no longer exists")
    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency: authenticated ``User`` or 401."""
    if credentials is None or not credentials.credentials:
        raise unauthorized("missing bearer token")
    return resolve_current_user(credentials.credentials, db)


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    """FastAPI dependency: authenticated user that must be an admin."""
    if not is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin privileges required",
        )
    return user


def is_admin(user: User | None) -> bool:
    """True when the user holds the ``admin`` role (case-insensitive)."""
    return bool(user is not None and (user.role or "").strip().lower() == "admin")


def user_read(user: User) -> UserRead:
    """Serialize a ``User`` row to the public ``UserRead`` schema."""
    return UserRead(
        id=user.id,
        email=user.email,
        full_name=user.full_name or "",
        role=user.role or "member",
        created_at=user.created_at,
    )
