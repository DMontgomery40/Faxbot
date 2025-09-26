"""
Database models for canonical event system.

Provides PHI-free event persistence for diagnostics, monitoring, and SSE streaming.
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, JSON
from sqlalchemy.sql import func

from ..db import Base


class CanonicalEventDB(Base):  # type: ignore
    """
    Canonical event storage for system diagnostics and monitoring.

    Stores events without PHI for safe streaming via SSE and audit trails.
    """
    __tablename__ = "canonical_events"

    id = Column(String(40), primary_key=True, default=lambda: str(uuid.uuid4()))
    type = Column(String(50), nullable=False, index=True)  # EventType enum value
    occurred_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)

    # Reference fields (no PHI)
    job_id = Column(String(40), nullable=True, index=True)
    provider_id = Column(String(50), nullable=True, index=True)
    external_id = Column(String(100), nullable=True)
    user_id = Column(String(100), nullable=True)
    correlation_id = Column(String(100), nullable=True)

    # Metadata (no PHI - only status codes, counts, etc)
    payload_meta = Column(JSON, nullable=True)

    # Indices for efficient querying
    __table_args__ = (
        {'sqlite_autoincrement': True},
    )