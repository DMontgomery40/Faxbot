from __future__ import annotations

"""
Identity provider (SQLAlchemy, async) — skeleton.

This shim is not wired by default (no manifest included yet). It provides a
concrete class for future manifest-first discovery and development.
"""

from typing import Any, Dict, Optional
from app.plugins.identity.base import IdentityPlugin, User, Group, Session, AuthResult  # type: ignore
from importlib import import_module


class Plugin(IdentityPlugin):
    plugin_id = "sqlalchemy"

    async def test_connection(self) -> Dict[str, Any]:
        try:
            db_async = import_module("app.db.async_db")
            engine = getattr(db_async, "engine")
            async with engine.connect() as conn:  # type: ignore
                await conn.exec_driver_sql("SELECT 1")
            return {"success": True, "message": "DB connection OK"}
        except Exception as e:
            return {"success": False, "message": f"{e}"}

    async def get_user(self, user_id: str) -> Optional[User]:  # pragma: no cover - stub
        raise NotImplementedError

    async def find_user_by_username(self, username: str) -> Optional[User]:  # pragma: no cover - stub
        raise NotImplementedError

    async def create_user(self, username: str, password: str, traits: Dict[str, Any] | None = None) -> User:  # pragma: no cover - stub
        raise NotImplementedError

    async def authenticate_password(self, username: str, password: str) -> AuthResult:  # pragma: no cover - stub
        raise NotImplementedError

    async def create_session(self, user_id: str, ttl_seconds: int = 3600) -> Session:  # pragma: no cover - stub
        raise NotImplementedError

    async def validate_session(self, token: str) -> Optional[Session]:  # pragma: no cover - stub
        raise NotImplementedError

    async def revoke_session(self, session_id: str) -> None:  # pragma: no cover - stub
        raise NotImplementedError


def get_plugin_class():  # pragma: no cover - factory optional
    return Plugin
