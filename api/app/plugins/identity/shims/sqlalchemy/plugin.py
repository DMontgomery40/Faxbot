from __future__ import annotations

"""
Identity provider (SQLAlchemy, async) — skeleton.

This shim is not wired by default (no manifest included yet). It provides a
concrete class for future manifest-first discovery and development.
"""

from typing import Any, Dict, Optional
from app.plugins.identity.base import IdentityPlugin, User, Group, Session, AuthResult  # type: ignore
from importlib import import_module
import os
import secrets
import hashlib
from typing import Any, Dict, Optional
from sqlalchemy import select


class Plugin(IdentityPlugin):
    plugin_id = "sqlalchemy"

    async def test_connection(self) -> Dict[str, Any]:
        try:
            db_async = import_module("app.database.async_db")
            engine = getattr(db_async, "engine")
            async with engine.connect() as conn:  # type: ignore
                await conn.exec_driver_sql("SELECT 1")
            return {"success": True, "message": "DB connection OK"}
        except Exception as e:
            return {"success": False, "message": f"{e}"}

    async def initialize(self, config: Dict[str, Any]) -> None:
        # Optional: dev-only auto DDL
        if os.getenv("FAXBOT_DEV_IDENTITY_INIT", "false").lower() in {"1","true","yes"}:
            try:
                # ensure models are imported and create tables
                import app.identity_models as _idm  # noqa: F401
                db_async = import_module("app.database.async_db")
                from app.db import Base
                await getattr(db_async, "create_tables_dev_only")(Base)  # type: ignore
            except Exception:
                pass

    # --- password hashing helpers ---
    def _pepper(self) -> str:
        p = os.getenv("FAXBOT_SESSION_PEPPER", "")
        if not p:
            raise RuntimeError("FAXBOT_SESSION_PEPPER required for identity auth")
        return p

    def _hash_password(self, password: str, salt: str, rounds: int = 200_000) -> str:
        data = (self._pepper() + password).encode("utf-8")
        dk = hashlib.pbkdf2_hmac("sha256", data, salt.encode("utf-8"), rounds)
        return dk.hex()

    def _verify_password(self, password: str, salt: str, algo: str, expected_hex: str) -> bool:
        try:
            name, rounds = (algo or "pbkdf2_sha256:200000").split(":", 1)
            rounds_i = int(rounds)
        except Exception:
            name, rounds_i = "pbkdf2_sha256", 200_000
        calc = self._hash_password(password, salt, rounds_i)
        return secrets.compare_digest(calc, expected_hex)

    async def get_user(self, user_id: str) -> Optional[User]:
        from app.identity_models import DBUser
        db_async = import_module("app.database.async_db")
        AsyncSessionLocal = getattr(db_async, "AsyncSessionLocal")
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(DBUser).where(DBUser.id == user_id))
            row = res.scalar_one_or_none()
            if not row:
                return None
            return User(id=row.id, username=row.username, display_name=row.display_name, email=row.email, traits={})

    async def find_user_by_username(self, username: str) -> Optional[User]:
        from app.identity_models import DBUser
        db_async = import_module("app.database.async_db")
        AsyncSessionLocal = getattr(db_async, "AsyncSessionLocal")
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(DBUser).where(DBUser.username == username))
            row = res.scalar_one_or_none()
            if not row:
                return None
            return User(id=row.id, username=row.username, display_name=row.display_name, email=row.email, traits={})

    async def create_user(self, username: str, password: str, traits: Dict[str, Any] | None = None) -> User:
        from app.identity_models import DBUser
        from app.db import Base
        db_async = import_module("app.database.async_db")
        AsyncSessionLocal = getattr(db_async, "AsyncSessionLocal")
        # ensure tables exist in dev mode if requested
        if os.getenv("FAXBOT_DEV_IDENTITY_INIT", "false").lower() in {"1","true","yes"}:
            try:
                await getattr(db_async, "create_tables_dev_only")(Base)  # type: ignore
            except Exception:
                pass
        salt = secrets.token_hex(16)
        pwd_hex = self._hash_password(password, salt)
        user = DBUser(
            id=secrets.token_hex(16),
            username=username,
            display_name=None,
            email=None,
            pwd_hash=pwd_hex,
            pwd_salt=salt,
            pwd_algo="pbkdf2_sha256:200000",
        )
        async with AsyncSessionLocal() as db:
            db.add(user)
            await db.commit()
        return User(id=user.id, username=user.username, display_name=None, email=None, traits=traits or {})

    async def authenticate_password(self, username: str, password: str) -> AuthResult:
        from app.identity_models import DBUser
        db_async = import_module("app.database.async_db")
        AsyncSessionLocal = getattr(db_async, "AsyncSessionLocal")
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(DBUser).where(DBUser.username == username))
            row = res.scalar_one_or_none()
            if not row:
                return AuthResult(success=False, message="user not found")
            ok = self._verify_password(password, row.pwd_salt, row.pwd_algo, row.pwd_hash)
            if not ok:
                return AuthResult(success=False, message="invalid password")
            return AuthResult(success=True, user=User(id=row.id, username=row.username))

    async def create_session(self, user_id: str, ttl_seconds: int = 3600) -> Session:
        # Delegate to in-memory sessions for now
        from app.security.auth_sessions import create_session as _cs
        sid, token = _cs(user_id=user_id, ttl_seconds=ttl_seconds)
        # We return a placeholder; session token is returned via /auth/login cookie
        return Session(id=sid, user_id=user_id, created_at=0.0, expires_at=0.0, traits={})

    async def validate_session(self, token: str) -> Optional[Session]:
        from app.security.auth_sessions import validate_session as _vs
        rec = _vs(token)
        if not rec:
            return None
        return Session(id="n/a", user_id=str(rec.get("user_id")), created_at=float(rec.get("created_at", 0)), expires_at=float(rec.get("expires_at", 0)), traits={})

    async def revoke_session(self, session_id: str) -> None:
        # Not tracked by session_id in the in-memory store; no-op
        return None


def get_plugin_class():  # pragma: no cover - factory optional
    return Plugin
