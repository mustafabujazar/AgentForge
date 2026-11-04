"""Authentication endpoints: register, login and the current-user probe.

Passwords are hashed with the stdlib scrypt helper and sessions are stateless
HS256 JWTs (see ``app.core.security``), so the API needs no external auth stack.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, user_read
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserRead

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(user: User) -> TokenResponse:
    token = create_access_token(user.id, role=user.role or "member")
    return TokenResponse(access_token=token, token_type="bearer", user=user_read(user))


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Create an account and immediately return an access token."""
    email = payload.email.strip().lower()
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email already registered",
        )
    full_name = (payload.full_name or "").strip() or email.split("@", 1)[0]
    user = User(
        email=email,
        full_name=full_name,
        hashed_password=hash_password(payload.password),
        role="member",
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:  # concurrent registration of the same email
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email already registered",
        ) from exc
    db.refresh(user)
    logger.info("user_registered", extra={"user_id": user.id})
    return _token_response(user)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Exchange email + password for a Bearer token."""
    email = payload.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(payload.password, user.hashed_password):
        # Same message for both branches: do not reveal which emails exist.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    logger.info("user_login", extra={"user_id": user.id})
    return _token_response(user)


@router.get("/me", response_model=UserRead)
def me(current_user: User = Depends(get_current_user)) -> UserRead:
    """Return the authenticated user's profile."""
    return user_read(current_user)
