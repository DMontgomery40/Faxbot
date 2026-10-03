"""Exact mutation inputs and nonsecret, immutable committed-result metadata."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

from .types import AccessError, ResourceRef, ScopedPermission


@dataclass(frozen=True)
class VersionedEntity:
    id: str
    version: int


@dataclass(frozen=True)
class UserValues:
    login: str
    display_name: str
    enabled: bool


@dataclass(frozen=True)
class OwnerEnrollment:
    login: str
    display_name: str


@dataclass(frozen=True)
class IntegrationValues:
    display_name: str
    enabled: bool


@dataclass(frozen=True)
class GroupValues:
    name: str
    description: str
    enabled: bool


@dataclass(frozen=True)
class CustomRoleValues:
    name: str
    description: str
    enabled: bool
    permissions: frozenset[str]


@dataclass(frozen=True)
class PrincipalSubject:
    principal: VersionedEntity


@dataclass(frozen=True)
class GroupSubject:
    group: VersionedEntity


@dataclass(frozen=True)
class AssignmentValues:
    subject: PrincipalSubject | GroupSubject
    role: VersionedEntity
    resource: ResourceRef


@dataclass(frozen=True)
class KeyValues:
    principal: VersionedEntity
    name: str | None
    note: str | None
    expires_at: datetime | None
    ceiling: tuple[ScopedPermission, ...]


@dataclass(frozen=True)
class KeyMetadata:
    name: str | None
    note: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class MutationReceipt:
    target: VersionedEntity
    policy_version: int
    changed: bool
    related: tuple[VersionedEntity, ...] = ()


@dataclass(frozen=True, kw_only=True)
class KeyMutationReceipt(MutationReceipt):
    public_key_id: str
    principal_id: str
    ceiling: tuple[ScopedPermission, ...]


class MutationReason(str, Enum):
    FORBIDDEN = 'forbidden'
    OWNER_REQUIRED = 'owner_required'
    LAST_OWNER = 'last_owner'
    RESET_REQUIRED = 'reset_required'
    STALE_VERSION = 'stale_version'
    INVALID_TARGET = 'invalid_target'
    INVALID_INPUT = 'invalid_input'
    DUPLICATE = 'duplicate'
    CREDENTIAL_STALE = 'credential_stale'


ReceiptT = TypeVar('ReceiptT', bound=MutationReceipt)


@dataclass(frozen=True)
class MutationOutcome(Generic[ReceiptT]):
    receipt: ReceiptT | None
    reason: MutationReason | None
    policy_version: int

    def __post_init__(self):
        if ((self.receipt is None) == (self.reason is None)
                or (self.reason is not None and type(self.reason) is not MutationReason)
                or type(self.policy_version) is not int or self.policy_version < 1):
            raise ValueError('invalid mutation outcome')


class MutationDeniedError(AccessError):
    """Closed ordinary denial; the standalone transaction already committed audit."""
    def __init__(self, reason: MutationReason):
        if type(reason) is not MutationReason:
            raise ValueError('invalid mutation reason')
        self.reason = reason
        self.code = reason.value
        super().__init__()


class StaleVersionError(MutationDeniedError):
    def __init__(self):
        super().__init__(MutationReason.STALE_VERSION)
