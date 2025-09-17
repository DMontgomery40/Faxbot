"""Plugin type definitions - HIPAA-compliant (no PHI).

These types are scaffolding for the v3 plugin system and are not used by
the runtime unless FEATURE_V3_PLUGINS is enabled.
"""
from dataclasses import dataclass
from typing import Dict, Any, Optional, List
from datetime import datetime


@dataclass
class SendResult:
    """Result from sending a fax - NO PHI"""
    job_id: str
    backend: str
    provider_sid: Optional[str] = None
    accepted: bool = True
    queued_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'job_id': self.job_id,
            'backend': self.backend,
            'provider_sid': self.provider_sid,
            'accepted': self.accepted,
            'queued_at': self.queued_at.isoformat() if self.queued_at else None,
        }


@dataclass
class StatusResult:
    """Fax transmission status - NO PHI"""
    job_id: str
    status: str  # queued|in_progress|SUCCESS|FAILED
    pages: Optional[int] = None
    error: Optional[str] = None  # Keep generic, no PHI
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'job_id': self.job_id,
            'status': self.status,
            'pages': self.pages,
            'error': self.error,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class PluginManifest:
    """Plugin metadata - public info only"""
    id: str
    name: str
    version: str
    categories: List[str]  # e.g., ['outbound']
    capabilities: List[str]  # e.g., ['send', 'get_status']
    description: Optional[str] = None
    author: Optional[str] = None
    license: Optional[str] = None
    config_schema: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'version': self.version,
            'categories': self.categories,
            'capabilities': self.capabilities,
            'description': self.description,
            'author': self.author,
            'license': self.license,
            'config_schema': self.config_schema,
        }

