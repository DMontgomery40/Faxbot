import base64
import hashlib
import hmac
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

from .db import SessionLocal, APIKey


def _b64u_decode(data: str) -> bytes:
    # Add padding if missing
    pad = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def verify_secret(secret: str, key_hash: str) -> bool:
    try:
        parts = key_hash.split("$")
        algo = parts[0]
        if algo == "scrypt":
            _, salt_b64, hash_b64, n_part, r_part, p_part = parts
            n = int(n_part.split("=")[1])
            r = int(r_part.split("=")[1])
            p = int(p_part.split("=")[1])
            salt = _b64u_decode(salt_b64)
            expected = _b64u_decode(hash_b64)
            dk = hashlib.scrypt(secret.encode(), salt=salt, n=n, r=r, p=p, dklen=len(expected))
            return hmac.compare_digest(dk, expected)
        elif algo == "pbkdf2":
            _, salt_b64, hash_b64, rounds_part = parts
            rounds = int(rounds_part.split("=")[1])
            salt = _b64u_decode(salt_b64)
            expected = _b64u_decode(hash_b64)
            dk = hashlib.pbkdf2_hmac('sha256', secret.encode(), salt, rounds, dklen=len(expected))
            return hmac.compare_digest(dk, expected)
        return False
    except Exception:
        return False


def parse_header_token(x_api_key: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Extract (key_id, secret) from a token like fbk_live_<key_id>_<secret>.
    If not matching, returns (None, None).
    """
    if not x_api_key:
        return None, None
    try:
        if not x_api_key.startswith("fbk_"):
            return None, None
        parts = x_api_key.split("_")
        if len(parts) < 3:
            return None, None
        # parts: ["fbk", "live", <key_id>, <secret-with-possible-underscores>]
        key_id = parts[2]
        secret = "_".join(parts[3:]) if len(parts) > 3 else ""
        if not key_id or not secret:
            return None, None
        return key_id, secret
    except Exception:
        return None, None


def verify_db_key(x_api_key: Optional[str]) -> Optional[Dict[str, Any]]:
    """Verify a DB-backed key. Returns info dict on success or None.
    Info: { key_id, scopes: List[str], name, owner }
    """
    key_id, secret = parse_header_token(x_api_key)
    if not key_id or not secret:
        return None
    with SessionLocal() as db:
        rec = db.query(APIKey).filter(APIKey.key_id == key_id).first()  # type: ignore[attr-defined]
        if not rec:
            return None
        # Check revoked/expired
        now = datetime.utcnow()
        if rec.revoked_at is not None:
            return None
        if rec.expires_at is not None and now > rec.expires_at:
            return None
        if not verify_secret(secret, rec.key_hash):
            return None
        # Update last_used_at (best-effort; ignore errors)
        try:
            rec.last_used_at = now
            db.add(rec)
            db.commit()
        except Exception:
            db.rollback()
        scopes = [s.strip() for s in (rec.scopes or "").split(",") if s.strip()]
        return {"key_id": rec.key_id, "scopes": scopes, "name": rec.name, "owner": rec.owner}
