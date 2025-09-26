"""
Webhook Dead Letter Queue (DLQ) model for failed webhook processing.

The DLQ captures webhook callbacks that fail to process after retry attempts,
storing essential metadata for debugging and reprocessing.
"""

from sqlalchemy import Column, String, Text, DateTime, func
import uuid
from datetime import datetime
from typing import Dict, Any, Optional
import json

from api.app.db import Base


class WebhookDLQ(Base):
    """
    Dead Letter Queue entry for failed webhook processing.

    This table stores webhook callbacks that could not be processed
    successfully after exhausting retry attempts. Only safe header
    metadata is persisted - no Authorization headers or sensitive data.
    """
    __tablename__ = "webhook_dlq"

    id = Column(String(40), primary_key=True, nullable=False)
    provider_id = Column(String(40), nullable=False)
    external_id = Column(String(100), nullable=True)
    received_at = Column(DateTime(), nullable=False, server_default=func.now())
    status = Column(String(20), nullable=False, default='queued')  # queued|retrying|failed
    error = Column(Text(), nullable=True)
    headers_meta = Column(Text(), nullable=True)  # JSON; ALLOWLIST ONLY

    # Retry tracking
    retry_count = Column(String(10), nullable=False, default='0')
    last_retry_at = Column(DateTime(), nullable=True)
    next_retry_at = Column(DateTime(), nullable=True)

    def __init__(self, provider_id: str, external_id: Optional[str] = None,
                 error: Optional[str] = None, headers_meta: Optional[Dict[str, Any]] = None,
                 **kwargs):
        self.id = kwargs.get('id', f'dlq_{uuid.uuid4().hex}')
        self.provider_id = provider_id
        self.external_id = external_id
        self.error = error
        self.headers_meta = json.dumps(headers_meta) if headers_meta else None
        self.status = kwargs.get('status', 'queued')
        self.retry_count = str(kwargs.get('retry_count', 0))

    @classmethod
    def create_safe_headers_meta(cls, headers: Dict[str, str]) -> Dict[str, str]:
        """
        Create safe headers metadata by allowlisting only non-sensitive headers.

        Security: Never persist Authorization, secrets, or other sensitive headers.
        """
        ALLOWED_HEADERS = {
            "user-agent", "content-type", "content-length",
            "x-request-id", "x-signature", "x-timestamp",
            "x-phaxio-signature", "x-sinch-signature"
        }

        return {
            k: v for k, v in headers.items()
            if k.lower() in ALLOWED_HEADERS
        }

    def get_headers_meta(self) -> Dict[str, Any]:
        """Get parsed headers metadata."""
        if not self.headers_meta:
            return {}
        try:
            return json.loads(self.headers_meta)
        except (json.JSONDecodeError, TypeError):
            return {}

    def increment_retry(self, next_retry_at: Optional[datetime] = None):
        """Increment retry count and update retry timestamps."""
        self.retry_count = str(int(self.retry_count) + 1)
        self.last_retry_at = datetime.utcnow()
        if next_retry_at:
            self.next_retry_at = next_retry_at
            self.status = 'retrying'

    def mark_failed(self, error: str):
        """Mark the DLQ entry as permanently failed."""
        self.status = 'failed'
        self.error = error
        self.next_retry_at = None