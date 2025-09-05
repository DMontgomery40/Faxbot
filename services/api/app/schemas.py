from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional, Any


class FaxSubmitResponse(BaseModel):
    job_id: str


class FaxStatusResponse(BaseModel):
    id: str
    to_number: str
    status: str
    attempts: int
    max_attempts: int
    error: Optional[str] = None
    caller_id: Optional[str] = None
    header: Optional[str] = None
    notify_url: Optional[str] = None
    fax_meta: Optional[dict[str, Any]] = Field(default=None)
