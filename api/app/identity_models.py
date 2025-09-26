from __future__ import annotations

from datetime import datetime
from sqlalchemy import Column, String, DateTime, Integer, UniqueConstraint, Text
from .db import Base  # reuse the existing metadata base


class DBUser(Base):  # type: ignore
    __tablename__ = "id_users"
    id = Column(String(40), primary_key=True, index=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    display_name = Column(String(200), nullable=True)
    email = Column(String(200), nullable=True)
    pwd_hash = Column(String(256), nullable=False)
    pwd_salt = Column(String(64), nullable=False)
    pwd_algo = Column(String(64), nullable=False, default="pbkdf2_sha256:200000")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    traits_json = Column(Text, nullable=True)


class DBUserSession(Base):  # type: ignore
    __tablename__ = "id_sessions"
    id = Column(String(64), primary_key=True, index=True)
    user_id = Column(String(40), index=True, nullable=False)
    token_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint('token_hash', name='uix_id_sessions_token_hash'),
    )

