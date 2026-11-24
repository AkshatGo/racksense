"""Password hashing and session tokens.

Passwords: PBKDF2-HMAC-SHA256 with a per-user salt.
Sessions: opaque 256-bit tokens stored server-side, so logout truly revokes.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

ITERATIONS = 180_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
        return hmac.compare_digest(calc.hex(), digest)
    except Exception:
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
