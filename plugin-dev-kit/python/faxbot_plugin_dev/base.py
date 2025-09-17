from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import logging


class FaxStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "cancelled"


@dataclass
class SendResult:
    job_id: str
    backend: str
    provider_sid: Optional[str] = None
    estimated_cost: Optional[float] = None
    estimated_duration: Optional[int] = None
    metadata: Dict[str, Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "backend": self.backend,
            "provider_sid": self.provider_sid,
            "estimated_cost": self.estimated_cost,
            "estimated_duration": self.estimated_duration,
            "metadata": self.metadata or {},
        }


@dataclass
class StatusResult:
    job_id: str
    status: FaxStatus
    pages: Optional[int] = None
    duration: Optional[int] = None
    cost: Optional[float] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    completed_at: Optional[datetime] = None
    raw_response: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status.value if isinstance(self.status, Enum) else self.status,
            "pages": self.pages,
            "duration": self.duration,
            "cost": self.cost,
            "error": self.error,
            "error_code": self.error_code,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "raw_response": self.raw_response,
        }


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str
    description: str
    author: str
    categories: List[str]
    capabilities: List[str]
    homepage: Optional[str] = None
    license: Optional[str] = None
    icon: Optional[str] = None
    hipaa_compliant: bool = False
    requires_baa: bool = False
    config_schema: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "categories": self.categories,
            "capabilities": self.capabilities,
            "homepage": self.homepage,
            "license": self.license,
            "icon": self.icon,
            "hipaa_compliant": self.hipaa_compliant,
            "requires_baa": self.requires_baa,
            "config_schema": self.config_schema,
        }


@dataclass
class PluginDeps:
    logger: logging.Logger
    storage: Any
    db: Any
    events: Any
    http: Any
    audit: Any
    config_dir: str
    cache: Any
    metrics: Any
    extras: Dict[str, Any] = None


class PluginBase(ABC):
    def __init__(self, deps: Optional[PluginDeps] = None):
        self.deps = deps
        self.config = {}
        self._initialized = False

    @abstractmethod
    def manifest(self) -> PluginManifest:
        raise NotImplementedError

    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def initialize(self, config: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def shutdown(self) -> None:
        raise NotImplementedError

    async def health_check(self) -> Dict[str, Any]:
        return {
            "status": "healthy" if self._initialized else "not_initialized",
            "plugin": self.manifest().id,
            "version": self.manifest().version,
        }

    async def handle_webhook(self, headers: Dict, body: bytes) -> Dict:
        return {"status": "not_implemented"}


class FaxPlugin(PluginBase):
    @abstractmethod
    async def send_fax(self, to_number: str, file_path: str, options: Optional[Dict[str, Any]] = None) -> SendResult:
        raise NotImplementedError

    @abstractmethod
    async def get_status(self, job_id: str) -> StatusResult:
        raise NotImplementedError

    async def cancel_fax(self, job_id: str) -> bool:
        return False

    async def get_fax_details(self, job_id: str) -> Dict[str, Any]:
        status = await self.get_status(job_id)
        return status.to_dict()


class StoragePlugin(PluginBase):
    @abstractmethod
    async def put(self, path: str, data: bytes, metadata: Optional[Dict[str, Any]] = None) -> str:
        raise NotImplementedError

    @abstractmethod
    async def get(self, path: str) -> bytes:
        raise NotImplementedError

    @abstractmethod
    async def delete(self, path: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def exists(self, path: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def list(self, prefix: str = "") -> List[str]:
        raise NotImplementedError

    async def get_signed_url(self, path: str, expires_in: int = 3600) -> str:
        raise NotImplementedError("Provider does not support signed URLs")


class AuthPlugin(PluginBase):
    @abstractmethod
    async def authenticate(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def validate_token(self, token: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        raise NotImplementedError

    async def get_user_info(self, token: str) -> Dict[str, Any]:
        return {}


class MessagingPlugin(PluginBase):
    @abstractmethod
    async def send_message(self, to: str, content: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def get_message_status(self, message_id: str) -> Dict[str, Any]:
        raise NotImplementedError

