from __future__ import annotations

"""
Phase 2: Identity plugin base and types (no behavior change).

Defines the abstract IdentityPlugin interface used by the PluginManager for
type validation. Concrete providers (e.g., SQLAlchemy) will be added under
api/app/plugins/identity/providers/ as needed.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..base import FaxbotPlugin


@dataclass
class User:
    id: str
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    groups: List[str] = None
    traits: Dict[str, Any] = None


@dataclass
class Group:
    id: str
    name: str
    traits: Dict[str, Any] = None


@dataclass
class Session:
    id: str
    user_id: str
    created_at: float
    expires_at: float
    traits: Dict[str, Any] = None


@dataclass
class AuthResult:
    success: bool
    user: Optional[User] = None
    message: Optional[str] = None


class IdentityPlugin(FaxbotPlugin, ABC):
    plugin_type: str = "identity"

    @abstractmethod
    async def test_connection(self) -> Dict[str, Any]:
        ...

    # User management
    @abstractmethod
    async def get_user(self, user_id: str) -> Optional[User]:
        ...

    @abstractmethod
    async def find_user_by_username(self, username: str) -> Optional[User]:
        ...

    @abstractmethod
    async def create_user(self, username: str, password: str, traits: Dict[str, Any] | None = None) -> User:
        ...

    # Authentication
    @abstractmethod
    async def authenticate_password(self, username: str, password: str) -> AuthResult:
        ...

    # Sessions
    @abstractmethod
    async def create_session(self, user_id: str, ttl_seconds: int = 3600) -> Session:
        ...

    @abstractmethod
    async def validate_session(self, token: str) -> Optional[Session]:
        ...

    @abstractmethod
    async def revoke_session(self, session_id: str) -> None:
        ...

