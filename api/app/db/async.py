from __future__ import annotations

"""
Phase 2: Async SQLAlchemy helpers (non-invasive)

Provides an async engine and AsyncSessionLocal for new identity/session code.
Existing synchronous DB usage remains unchanged.

Env:
  DATABASE_URL: e.g., postgresql+asyncpg://user:pass@host/db
                or sqlite+aiosqlite:///./faxbot.db (dev)
"""

import os
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


def _coerce_async_url(url: str) -> str:
    """Ensure the URL uses an async driver dialect when possible.

    - Upgrade postgresql:// → postgresql+asyncpg://
    - Upgrade sqlite:/// → sqlite+aiosqlite:///
    If already async, return unchanged.
    """
    if "+" in url:
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return url


_DEFAULT_URL = "sqlite+aiosqlite:///./faxbot_phase2.db"
_ASYNC_DATABASE_URL = _coerce_async_url(os.getenv("DATABASE_URL", _DEFAULT_URL))

# Create the async engine
engine: AsyncEngine = create_async_engine(
    _ASYNC_DATABASE_URL,
    echo=os.getenv("SQLALCHEMY_ECHO", "false").lower() in {"1", "true", "yes"},
    future=True,
)

# Async session factory
AsyncSessionLocal: sessionmaker[AsyncSession] = sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def create_tables_dev_only(Base) -> None:  # pragma: no cover - dev-only helper
    """Dev-only: create tables via metadata using the async engine.

    Prefer Alembic migrations in real deployments.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

