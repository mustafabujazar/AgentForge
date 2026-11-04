"""Password hashing and JWT helpers built on the standard library.

No passlib/PyJWT dependency: scrypt for password hashing and HMAC-SHA256 for
tokens.  This keeps the dependency surface small while remaining production-safe
as long as ``SECRET_KEY`` is strong and rotated.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import get_settings

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class TokenError(ValueError):
    """Raised when a JWT is malformed, expired or has a bad signature."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    """Hash a password with scrypt and return a self-describing string."""
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64url_encode(salt)}${_b64url_encode(digest)}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = hashed.split("$")
        if scheme != "scrypt":
            return False
        salt = _b64url_decode(salt_b64)
        expected = _b64url_decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_access_token(
    subject: str,
    *,
    role: str = "member",
    expires_minutes: int | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    expires_minutes = expires_minutes or settings.access_token_expire_minutes
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    if extra_claims:
        payload.update(extra_claims)
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join(
        [
            _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(
        settings.secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url_encode(signature)}"


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise TokenError("malformed token") from exc
    signing_input = f"{header_b64}.{payload_b64}"
    expected = hmac.new(
        settings.secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        provided = _b64url_decode(signature_b64)
    except Exception as exc:  # malformed base64
        raise TokenError("malformed signature") from exc
    if not hmac.compare_digest(expected, provided):
        raise TokenError("invalid token signature")
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:
        raise TokenError("malformed token payload") from exc
    if not isinstance(payload, dict):
        raise TokenError("malformed token payload")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or int(exp) < int(time.time()):
        raise TokenError("token expired")
    return payload


def new_id() -> str:
    """Short unique identifier used for entities and runs."""
    return secrets.token_hex(16)
