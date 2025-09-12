import base64
import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List

from .db import SessionLocal, APIKey
from .audit import audit_event


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64u_decode(data: str) -> bytes:
    # Add padding if missing
    pad = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def generate_token() -> Tuple[str, str, str]:
    """Return (token, key_id, secret). Token is shown once to the user.
    Format: fbk_live_<key_id>_<secret>
    """
    key_id = secrets.token_hex(6)  # 12 hex chars
    secret = secrets.token_urlsafe(32)
    token = f"fbk_live_{key_id}_{secret}"
    return token, key_id, secret


def hash_secret(secret: str, *, n: int = 16384, r: int = 8, p: int = 1) -> str:
    salt = secrets.token_bytes(16)
    try:
        dk = hashlib.scrypt(secret.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
        return f"scrypt${_b64u(salt)}${_b64u(dk)}$n={n}$r={r}$p={p}"
    except AttributeError:
        # Fallback for environments without scrypt compiled in OpenSSL/Python: use PBKDF2-HMAC-SHA256
        rounds = 200_000
        dk = hashlib.pbkdf2_hmac('sha256', secret.encode(), salt, rounds, dklen=32)
        return f"pbkdf2${_b64u(salt)}${_b64u(dk)}$rounds={rounds}"


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


def create_api_key(*, name: Optional[str], owner: Optional[str], scopes: Optional[List[str]],
                   expires_at: Optional[datetime], note: Optional[str]) -> Dict[str, Any]:
    token, key_id, secret = generate_token()
    key_hash = hash_secret(secret)
    with SessionLocal() as db:
        rec = APIKey(
            id=secrets.token_hex(16),
            key_id=key_id,
            key_hash=key_hash,
            name=name,
            owner=owner,
            scopes=",".join(scopes or []),
            created_at=datetime.utcnow(),
            expires_at=expires_at,
            note=note,
        )
        db.add(rec)
        db.commit()
    audit_event("api_key_created", key_id=key_id, owner=owner, scopes=scopes or [])
    return {"token": token, "key_id": key_id, "name": name, "owner": owner, "scopes": scopes or [], "expires_at": expires_at}


def list_api_keys() -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.query(APIKey).all()  # type: ignore[attr-defined]
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "key_id": r.key_id,
                "name": r.name,
                "owner": r.owner,
                "scopes": [s.strip() for s in (r.scopes or "").split(",") if s.strip()],
                "created_at": r.created_at,
                "last_used_at": r.last_used_at,
                "expires_at": r.expires_at,
                "revoked_at": r.revoked_at,
                "note": r.note,
            })
        return out


def revoke_api_key(key_id: str) -> bool:
    with SessionLocal() as db:
        rec = db.query(APIKey).filter(APIKey.key_id == key_id).first()  # type: ignore[attr-defined]
        if not rec:
            return False
        if rec.revoked_at is None:
            rec.revoked_at = datetime.utcnow()
            db.add(rec)
            db.commit()
        audit_event("api_key_revoked", key_id=key_id)
        return True


def rotate_api_key(key_id: str) -> Optional[Dict[str, Any]]:
    token, new_key_id, secret = generate_token()
    # Keep the same key_id for external references; only change the secret/hash.
    key_hash = hash_secret(secret)
    with SessionLocal() as db:
        rec = db.query(APIKey).filter(APIKey.key_id == key_id).first()  # type: ignore[attr-defined]
        if not rec:
            return None
        rec.key_hash = key_hash  # type: ignore[assignment]
        rec.last_used_at = None  # type: ignore[assignment]
        db.add(rec)
        db.commit()
    audit_event("api_key_rotated", key_id=key_id)
    return {"token": f"fbk_live_{key_id}_{secret}", "key_id": key_id}
