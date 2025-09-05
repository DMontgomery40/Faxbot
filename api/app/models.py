from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class FaxRequest(BaseModel):
    to: str = Field(..., description="Destination number in E.164 or national format")


class FaxJobOut(BaseModel):
    id: str
    to: str
    status: str
    error: Optional[str] = None
    pages: Optional[int] = None
    created_at: datetime
    updated_at: datetime

