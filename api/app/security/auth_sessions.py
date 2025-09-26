from __future__ import annotations

"""
In-memory session storage with peppered token hashes.
Fully gated by FAXBOT_SESSIONS_ENABLED at call sites.

This is a safe, non-persistent default for Phase 2.
"""

import time
from typing import Dict, Optional, Tuple

from .session_tokens import generate_token, verify_token, hash_token


_SESS_BY_ID: Dict[str, Dict[str, object]] = {}
_SESS_BY_HASH: Dict[str, str] = {}


def create_session(user_id: str, ttl_seconds: int = 3600) -> Tuple[str, str]:
    """Create a session and return (session_id, raw_token)."""
    sid_raw, sid_hash = generate_token()
    tok_raw, tok_hash = generate_token()

    now = int(time.time())
    exp = now + max(60, int(ttl_seconds))
    _SESS_BY_ID[sid_hash] = {
        "user_id": user_id,
        "token_hash": tok_hash,
        "created_at": now,
        "expires_at": exp,
    }
    _SESS_BY_HASH[tok_hash] = sid_hash
    return sid_raw, tok_raw


def validate_session(raw_token: str) -> Optional[Dict[str, object]]:
    tok_hash = hash_token(raw_token)
    sid_hash = _SESS_BY_HASH.get(tok_hash)
    if not sid_hash:
        return None
    rec = _SESS_BY_ID.get(sid_hash)
    if not rec:
        return None
    if int(rec.get("expires_at", 0)) < int(time.time()):
        # expire lazily
        try:
            _SESS_BY_ID.pop(sid_hash, None)
            _SESS_BY_HASH.pop(tok_hash, None)
        except Exception:
            pass
        return None
    return rec


def revoke_session(raw_token: str) -> None:
    tok_hash = hash_token(raw_token)
    sid_hash = _SESS_BY_HASH.pop(tok_hash, None)
    if sid_hash:
        _SESS_BY_ID.pop(sid_hash, None)


def rotate_session(raw_token: str, ttl_seconds: int = 3600) -> Optional[str]:
    """Rotate the session token; return new raw token if valid else None."""
    tok_hash = hash_token(raw_token)
    sid_hash = _SESS_BY_HASH.get(tok_hash)
    if not sid_hash:
        return None
    rec = _SESS_BY_ID.get(sid_hash)
    if not rec or int(rec.get("expires_at", 0)) < int(time.time()):
        return None
    # remove old mapping
    _SESS_BY_HASH.pop(tok_hash, None)
    # create new token
    new_raw, new_hash = generate_token()
    _SESS_BY_HASH[new_hash] = sid_hash
    rec["token_hash"] = new_hash
    rec["expires_at"] = int(time.time()) + max(60, int(ttl_seconds))
    return new_raw

