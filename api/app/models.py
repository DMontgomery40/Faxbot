from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class FaxJobOut(BaseModel):
    id: str
    to: str
    status: str
    error: Optional[str] = None
    pages: Optional[int] = None
    backend: str = "sip"  # "sip" or "phaxio"
    provider_sid: Optional[str] = None  # Phaxio fax ID or other cloud provider ID
    delivery_state: Optional[str] = None
    dispatch_mode: Optional[str] = None
    delivery_version: Optional[int] = None
    reconciliation_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
