"""
Database models for canonical events with PHI-safe audit trail.

Events capture system state changes without sensitive data,
enabling diagnostics and monitoring while maintaining HIPAA compliance.
"""

from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, JSON, Index
from sqlalchemy.sql import func

# Import Base from the same module that other models use
from ..db import Base


class CanonicalEventDB(Base):  # type: ignore
    """Persistent storage for canonical events (PHI-free)."""
    __tablename__ = "canonical_events"

    # Event identification
    id = Column(String(40), primary_key=True, nullable=False)
    type = Column(String(50), nullable=False)
    occurred_at = Column(DateTime(), nullable=False, server_default=func.now())

    # Event context (no PHI)
    job_id = Column(String(40), nullable=True)
    provider_id = Column(String(50), nullable=True)
    external_id = Column(String(100), nullable=True)
    user_id = Column(String(100), nullable=True)

    # Correlation and metadata (PHI-free JSON)
    correlation_id = Column(String(40), nullable=True)
    payload_meta = Column(JSON, nullable=True)

    # Audit tracking
    created_at = Column(DateTime(), nullable=False, server_default=func.now())

    __table_args__ = (
        Index('idx_events_type', 'type'),
        Index('idx_events_provider', 'provider_id'),
        Index('idx_events_job', 'job_id'),
        Index('idx_events_occurred', 'occurred_at'),
        Index('idx_events_correlation', 'correlation_id'),
        Index('idx_events_user', 'user_id'),
    )