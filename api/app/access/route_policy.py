"""Permission dependencies for HTTP routes outside the access services.

A route declares ``Depends(require_permission(...))``. The dependency carries
its ``RoutePermission`` so route coverage can be read from ``app.routes``.
Each check runs in its own access transaction and is not authority for later
work; a route that executes something privileged calls ``authorize`` again
immediately before execution.
"""
from dataclasses import dataclass
import json
import uuid

from fastapi import Depends, Request

from ..config_runtime import run_lifecycle_step
from .catalog import GLOBAL_PERMISSIONS
from .fax_resources import FaxAccessError
from .http import RequestIdentity, private_operation, require_identity, runtime, utcnow
from .mutation_types import MutationDeniedError, MutationReason
from .types import AuthenticationError, DecisionReason, ResourceRef, StaleCredentialError


@dataclass(frozen=True)
class RoutePermission:
    permission: str
    resource: str
    audit: bool
    complete_owner: bool


def _denial_on(service, connection, actor, permission, complete_owner, now):
    """Return the denial for this actor, or None when the operation is allowed."""
    if permission == 'fax:send':
        try:
            service.fax_resources.authorize_send_on(connection, actor, now=now)
        except FaxAccessError as error:
            return MutationDeniedError(MutationReason.RESET_REQUIRED if error.code == 'reset_required'
                                       else MutationReason.FORBIDDEN)
        return None
    decision = service.control.authorize_on(connection, actor, permission, ResourceRef('installation'), now=now)
    if not decision.allowed:
        return MutationDeniedError(MutationReason.RESET_REQUIRED if decision.reason == DecisionReason.RESET_REQUIRED
                                   else MutationReason.FORBIDDEN)
    if complete_owner and not service.control.is_complete_owner_on(connection, actor, now=now):
        return MutationDeniedError(MutationReason.OWNER_REQUIRED)
    return None


@private_operation
def authorize(service, actor, permission, *, complete_owner=False, audit=None):
    """Raise a typed denial unless the actor may perform this operation now.

    ``audit`` is a small dict describing the request. When given, the outcome is
    recorded in the same transaction; a denial row commits before the error.
    """
    with service.store.transaction() as connection:
        now = utcnow()
        version = service.store.require_lock_on(connection)
        try:
            error = _denial_on(service, connection, actor, permission, complete_owner, now)
            attribution = (actor.principal_id, getattr(actor.credential, 'binding_id', None),
                           getattr(actor.credential, 'session_id', None))
        except AuthenticationError:
            error, attribution = StaleCredentialError(), (None, None, None)
        if audit is not None:
            details = dict(audit)
            if error is not None:
                details['reason'] = error.code
            principal, binding, session = attribution
            connection.execute(service.store.tables['access_audit'].insert().values(
                id=uuid.uuid4().hex, actor_principal_id=principal, actor_key_binding_id=binding,
                actor_session_id=session, operation=permission.replace(':', '.'),
                target_kind='installation', target_id='installation',
                policy_version_before=version, policy_version_after=version,
                outcome='denied' if error is not None else 'allowed',
                details=json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True),
                created_at=now))
    if error is not None:
        raise error


def request_audit(request, **details):
    """Audit details for one request; callers add only bounded, validated values."""
    return {'request': f'{request.method} {request.url.path[:200]}', **details}


def require_permission(permission, *, resource='installation', audit=False, complete_owner=False):
    """Dependency factory: authenticate, then require ``permission`` at its resource.

    ``fax:send`` is checked at the caller's personal container and every
    installation-wide permission at the installation. Fax and inbound record
    permissions belong to their resource services. Returns the ``RequestIdentity``.
    """
    if not ((resource == 'installation' and permission in GLOBAL_PERMISSIONS)
            or (resource == 'personal' and permission == 'fax:send' and not audit and not complete_owner)):
        raise ValueError(f'Unsupported route permission: {permission} at {resource}')

    async def permission_dependency(request: Request,
                                    identity: RequestIdentity = Depends(require_identity)) -> RequestIdentity:
        service = runtime(request)
        details = request_audit(request) if audit else None
        await run_lifecycle_step(lambda: authorize(service, identity.actor, permission,
                                                   complete_owner=complete_owner, audit=details))
        return identity

    permission_dependency.route_permission = RoutePermission(permission, resource, audit, complete_owner)
    return permission_dependency
