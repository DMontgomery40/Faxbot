"""Closed, nonsecret authentication evidence and bounded policy results."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Union


class AccessError(Exception):
    """Public failures contain a fixed code, never input or database details."""
    code = "access_unavailable"

    def __init__(self):
        super().__init__(self.code)


class AccessUnavailableError(AccessError):
    code = "access_unavailable"


class InvalidTransactionError(AccessError):
    code = "invalid_transaction"


class AuthenticationError(AccessError):
    code = "unauthenticated"


class StaleCredentialError(AuthenticationError):
    code = "credential_stale"


class DecisionReason(str, Enum):
    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"
    UNKNOWN_PERMISSION = "unknown_permission"
    INVALID_RESOURCE = "invalid_resource"
    INVALID_SCOPE = "invalid_scope"
    OWNER_REQUIRED = "owner_required"
    RESET_REQUIRED = "reset_required"


class _SafeEvidence:
    def __repr__(self):
        return type(self).__name__ + "()"


@dataclass(frozen=True, repr=False)
class KeyEvidence(_SafeEvidence):
    binding_id: str
    key_security_version: int


@dataclass(frozen=True, repr=False)
class PasswordSessionEvidence(_SafeEvidence):
    session_id: str
    password_version: int


@dataclass(frozen=True, repr=False)
class KeySessionEvidence(_SafeEvidence):
    session_id: str
    binding_id: str
    key_security_version: int


@dataclass(frozen=True, repr=False)
class BootstrapEvidence(_SafeEvidence):
    fingerprint: str = field(repr=False)
    session_id: str | None = None


CredentialEvidence = Union[KeyEvidence, PasswordSessionEvidence, KeySessionEvidence, BootstrapEvidence]


@dataclass(frozen=True, repr=False)
class ResourceRef(_SafeEvidence):
    id: str


@dataclass(frozen=True, repr=False)
class PrincipalContext(_SafeEvidence):
    principal_id: str
    principal_security_version: int
    credential: CredentialEvidence
    replay_scope: str


@dataclass(frozen=True, repr=False)
class ScopedPermission(_SafeEvidence):
    permission: str
    resource: ResourceRef


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: DecisionReason
    policy_version: int
