from __future__ import annotations

"""
Session token generation and verification using a peppered hash.

No persistence here — the storage layer lives in auth_sessions.
"""

import os
import hmac
import hashlib
import secrets
from typing import Tuple


def _pepper() -> str:
    p = os.getenv("FAXBOT_SESSION_PEPPER", "")
    if not p:
        raise RuntimeError("FAXBOT_SESSION_PEPPER missing (required when sessions are enabled)")
    return p


def generate_token() -> Tuple[str, str]:
    """Return (raw_token, hashed). Raw must be sent to client; only hash is stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw_token: str) -> str:
    pep = _pepper().encode("utf-8")
    # SHA-256 HMAC with pepper; store hex
    return hmac.new(pep, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_token(hashed: str, raw_attempt: str) -> bool:
    calc = hash_token(raw_attempt)
    return hmac.compare_digest(calc, hashed)

