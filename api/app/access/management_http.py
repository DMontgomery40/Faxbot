"""Access management HTTP routes under /access, for the admin console.

Every route authenticates with require_identity (a browser cookie with CSRF, or
an X-API-Key header). Reads serialize the AccessReads projections unchanged.
Mutations call the typed standalone access mutations with the body's
expected_policy_version and entity versions, so the services keep policy,
delegation, dominance and last-Owner rules. Errors map through
access_error_response: 400 invalid input, 401 unauthenticated, 403 forbidden,
404 invisible target, 409 stale version. A secret is prepared first, committed
by one standalone mutation and disclosed once in that response.
"""
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field

from ..config_runtime import run_lifecycle_step
from .http import (AUTH_ERROR_RESPONSES, PRIVATE_HEADERS, PrivateAuthRoute, SessionRevoke, StrictInput,
                   committed_view, private_operation, require_identity, runtime, transport, user_fallback, utcnow)
from .mutation_types import (AssignmentValues, CustomRoleValues, GroupSubject, GroupValues, InboundRuleValues,
                             IntegrationValues, KeyMetadata, KeyValues, MailboxValues, MutationDeniedError,
                             MutationReason, PrincipalSubject, UserValues, VersionedEntity)
from .route_policy import require_permission
from .types import ResourceRef, ScopedPermission


class ManagementRoute(PrivateAuthRoute):
    """Malformed bodies and query parameters are the contract's 400, not FastAPI's 422."""
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def validated(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse({'detail': 'Invalid access request.'}, status_code=400, headers=PRIVATE_HEADERS)
        return validated


router = APIRouter(prefix='/access', tags=['Access management'], route_class=ManagementRoute,
    responses={status: AUTH_ERROR_RESPONSES[status] for status in (400, 401, 403, 404, 409, 413, 503)})

Cursor = Annotated[str | None, Query(max_length=1024, description='Opaque cursor from the previous page.')]
Limit = Annotated[int, Query(ge=1, le=200)]
Identifier = Annotated[str, Field(min_length=1, max_length=100)]
Text = Annotated[str, Field(max_length=4000)]
PolicyVersion = Annotated[int, Field(ge=1)]
OptionalTime = Annotated[datetime | None, Field(strict=False)]


def _naive_utc(value):
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


async def _read(operation):
    return await run_lifecycle_step(private_operation(operation))


async def _mutate(mutate, view, name):
    """Commit one standalone mutation, then report the entity as it now stands."""
    receipt = await run_lifecycle_step(private_operation(mutate))
    fallback = {'id': receipt.target.id, 'version': receipt.target.version}
    entity = await run_lifecycle_step(lambda: committed_view(lambda: view(receipt), fallback))
    return {name: entity, 'policy_version': receipt.policy_version}


def _related(receipt):
    return [{'id': entity.id, 'version': entity.version} for entity in receipt.related]


def _ceiling(entries):
    return tuple(ScopedPermission(entry.permission, ResourceRef(entry.resource_id)) for entry in entries)


def _key_fallback(receipt, name, note, expires_at):
    return {'id': receipt.public_key_id, 'principal': {'id': receipt.principal_id, 'display_name': None, 'kind': None},
            'name': name, 'note': note, 'expires_at': expires_at, 'created_at': None, 'last_used_at': None,
            'revoked_at': None, 'pending_review': False,
            'ceiling': [{'permission': grant.permission, 'resource_id': grant.resource.id} for grant in receipt.ceiling],
            'version': receipt.target.version}


# -- request bodies ------------------------------------------------------------

class EntityRef(StrictInput):
    id: Identifier
    version: int


class SubjectRef(StrictInput):
    kind: Literal['principal', 'group']
    id: Identifier
    version: int


class CeilingEntry(StrictInput):
    permission: Annotated[str, Field(max_length=100)]
    resource_id: Identifier


class VersionedAction(StrictInput):
    version: int
    expected_policy_version: PolicyVersion


class RoleCreate(StrictInput):
    name: Text
    description: Text = ''
    permissions: list[Annotated[str, Field(max_length=100)]] = Field(max_length=200)
    enabled: bool = True
    expected_policy_version: PolicyVersion


class RolePatch(StrictInput):
    name: Text | None = None
    description: Text | None = None
    permissions: list[Annotated[str, Field(max_length=100)]] | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    version: int
    expected_policy_version: PolicyVersion


class UserCreate(StrictInput):
    login: Annotated[str, Field(max_length=200)]
    display_name: Text
    enabled: bool = True
    expected_policy_version: PolicyVersion


class IntegrationCreate(StrictInput):
    display_name: Text
    enabled: bool = True
    expected_policy_version: PolicyVersion


class UserPatch(StrictInput):
    display_name: Text | None = None
    enabled: bool | None = None
    login: Annotated[str, Field(max_length=200)] | None = None
    version: int
    expected_policy_version: PolicyVersion


class GroupCreate(StrictInput):
    name: Text
    description: Text = ''
    enabled: bool = True
    expected_policy_version: PolicyVersion


class GroupPatch(StrictInput):
    name: Text | None = None
    description: Text | None = None
    enabled: bool | None = None
    version: int
    expected_policy_version: PolicyVersion


class MemberAdd(StrictInput):
    principal_id: Identifier
    principal_version: int
    group_version: int
    expected_policy_version: PolicyVersion


class MemberRemove(StrictInput):
    membership_version: int
    group_version: int
    expected_policy_version: PolicyVersion


class AssignmentCreate(StrictInput):
    subject: SubjectRef
    role: EntityRef
    resource_id: Identifier
    expected_policy_version: PolicyVersion


class KeyCreate(StrictInput):
    principal: EntityRef
    name: Text | None = None
    note: Text | None = None
    expires_at: OptionalTime = None
    ceiling: list[CeilingEntry] = Field(max_length=200)
    expected_policy_version: PolicyVersion


class KeyPatch(StrictInput):
    name: Text | None = None
    note: Text | None = None
    expires_at: OptionalTime = None
    version: int
    expected_policy_version: PolicyVersion


class KeyApprove(StrictInput):
    principal: EntityRef
    ceiling: list[CeilingEntry] = Field(max_length=200)
    version: int
    expected_policy_version: PolicyVersion


class MailboxCreate(StrictInput):
    label: Text
    enabled: bool = True
    expected_policy_version: PolicyVersion


class MailboxPatch(StrictInput):
    label: Text | None = None
    enabled: bool | None = None
    version: int
    expected_policy_version: PolicyVersion


Day = Literal['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
ShortText = Annotated[str, Field(max_length=64)]


class RuleOptions(StrictInput):
    """A number rule's receiving options (provider-rules design §4.9); each one left out stays as it is."""
    position: Annotated[int, Field(ge=1, le=100000)] | None = None
    enabled: bool | None = None
    any_number: bool | None = None
    account_key: ShortText | None = None
    site_key: ShortText | None = None
    subaddress: Annotated[str, Field(max_length=40)] | None = None
    from_numbers: Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=50)] | None = None
    days: Annotated[list[Day], Field(max_length=7)] | None = None
    start_minute: Annotated[int, Field(ge=0, le=1439)] | None = None
    end_minute: Annotated[int, Field(ge=0, le=1439)] | None = None
    email_connector_id: ShortText | None = None
    email_off: bool | None = None
    urgent: bool | None = None
    keep_days: Annotated[int, Field(ge=1, le=36500)] | None = None
    # Only calls forwarded from this number (X4), and whether an unsigned or unchecked forwarding counts too.
    diverted_from: Annotated[str, Field(max_length=40)] | None = None
    diversion_unsigned: bool | None = None


class RuleCreate(RuleOptions):
    to_number: Annotated[str, Field(max_length=100)] = ''
    mailbox_id: Identifier
    expected_policy_version: PolicyVersion


class RulePatch(RuleOptions):
    to_number: Annotated[str, Field(max_length=100)] | None = None
    mailbox_id: Identifier | None = None
    version: int
    expected_policy_version: PolicyVersion


class ReceivedExplain(StrictInput):
    to_number: Annotated[str, Field(max_length=100)]
    from_number: Annotated[str, Field(max_length=100)] | None = None
    account_key: ShortText | None = None
    subaddress: Annotated[str, Field(max_length=40)] | None = None
    # A call forwarded from this number, and how far the forwarding checked (signed, unchecked, failed, stated).
    diverted_from: Annotated[str, Field(max_length=40)] | None = None
    diversion: Literal['signed', 'unchecked', 'failed', 'stated'] | None = None
    # A local time at this installation ("2026-10-07T18:30"), or now.
    at: Annotated[str, Field(max_length=32)] | None = None


# -- catalogue and roles -----------------------------------------------------------

@router.get('/permissions', summary='Permission catalogue')
async def list_permissions(request: Request, identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.permissions(identity.actor))


@router.get('/roles', summary='Roles')
async def list_roles(request: Request, cursor: Cursor = None, limit: Limit = 50, identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.roles(identity.actor, cursor=cursor, limit=limit))


@router.post('/roles', summary='Create a custom role')
async def create_role(body: RoleCreate, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    values = CustomRoleValues(body.name, body.description, body.enabled, frozenset(body.permissions))
    return await _mutate(lambda: service.mutations.create_custom_role(actor, values,
        expected_policy_version=body.expected_policy_version, now=utcnow()),
        lambda receipt: service.reads.role(actor, receipt.target.id), 'role')


@router.patch('/roles/{role_id}', summary='Change a custom role')
async def update_role(role_id: str, body: RolePatch, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    def mutate():
        # The mutation's version check also guards this merge base.
        current = service.reads.role(actor, role_id)
        values = CustomRoleValues(
            body.name if body.name is not None else current['name'],
            body.description if body.description is not None else current['description'] or '',
            body.enabled if body.enabled is not None else current['enabled'],
            frozenset(body.permissions if body.permissions is not None else current['permissions']))
        return service.mutations.update_custom_role(actor, VersionedEntity(role_id, body.version), values,
            expected_policy_version=body.expected_policy_version, now=utcnow())
    return await _mutate(mutate, lambda receipt: service.reads.role(actor, receipt.target.id), 'role')


# -- users and integrations ---------------------------------------------------------

@router.get('/users', summary='Users and integrations')
async def list_users(request: Request, kind: str = 'all', q: Annotated[str | None, Query(max_length=100)] = None,
        cursor: Cursor = None, limit: Limit = 50, identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.users(identity.actor, kind=kind, q=q or None, cursor=cursor, limit=limit))


@router.get('/users/{principal_id}', summary='One user or integration')
async def get_user(principal_id: str, request: Request, identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.user(identity.actor, principal_id))


@router.post('/users', summary='Create a user with a temporary password')
async def create_user(body: UserCreate, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    @private_operation
    def create():
        prepared = service.credential_codec.prepare_temporary_password()
        receipt = service.mutations.create_user(actor, UserValues(body.login, body.display_name, body.enabled), prepared,
            expected_policy_version=body.expected_policy_version, now=utcnow())
        return receipt, prepared
    receipt, prepared = await service.work.run(create)
    user = await run_lifecycle_step(lambda: committed_view(lambda: service.reads.user(actor, receipt.target.id),
        user_fallback(receipt, body.login, body.display_name)))
    return {'user': user, 'temporary_password': prepared._temporary_secret_for_committed_adapter(),
            'policy_version': receipt.policy_version}


@router.post('/integrations', summary='Create an integration identity')
async def create_integration(body: IntegrationCreate, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    return await _mutate(lambda: service.mutations.create_integration(actor,
        IntegrationValues(body.display_name, body.enabled),
        expected_policy_version=body.expected_policy_version, now=utcnow()),
        lambda receipt: service.reads.user(actor, receipt.target.id), 'integration')


@router.patch('/users/{principal_id}', summary='Change a user or integration')
async def update_user(principal_id: str, body: UserPatch, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    def mutate():
        current = service.reads.user(actor, principal_id)
        target = VersionedEntity(principal_id, body.version)
        display_name = body.display_name if body.display_name is not None else current['display_name']
        enabled = body.enabled if body.enabled is not None else current['enabled']
        policy = dict(expected_policy_version=body.expected_policy_version, now=utcnow())
        if current['kind'] == 'user':
            login = body.login if body.login is not None else current['login']
            return service.mutations.update_user(actor, target, UserValues(login, display_name, enabled), **policy)
        if body.login is not None:
            raise MutationDeniedError(MutationReason.INVALID_INPUT)
        # Integrations, and the bootstrap principal (refused by the service as not editable).
        return service.mutations.update_integration(actor, target, IntegrationValues(display_name, enabled), **policy)
    return await _mutate(mutate, lambda receipt: service.reads.user(actor, receipt.target.id), 'user')


@router.post('/users/{principal_id}/reset-password', summary='Reset a password to a temporary one')
async def reset_password(principal_id: str, body: VersionedAction, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    @private_operation
    def reset():
        prepared = service.credential_codec.prepare_temporary_password()
        receipt = service.mutations.reset_password(actor, VersionedEntity(principal_id, body.version), prepared,
            expected_policy_version=body.expected_policy_version, now=utcnow())
        return receipt, prepared
    receipt, prepared = await service.work.run(reset)
    user = await run_lifecycle_step(lambda: committed_view(lambda: service.reads.user(actor, receipt.target.id),
        {'id': receipt.target.id, 'version': receipt.target.version}))
    return {'temporary_password': prepared._temporary_secret_for_committed_adapter(), 'user': user,
            'policy_version': receipt.policy_version}


# -- groups and memberships ------------------------------------------------------------

@router.get('/groups', summary='Groups')
async def list_groups(request: Request, cursor: Cursor = None, limit: Limit = 50, identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.groups(identity.actor, cursor=cursor, limit=limit))


@router.get('/groups/{group_id}', summary='One group with members and assignments')
async def get_group(group_id: str, request: Request, identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.group(identity.actor, group_id))


@router.post('/groups', summary='Create a group')
async def create_group(body: GroupCreate, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    return await _mutate(lambda: service.mutations.create_group(actor, GroupValues(body.name, body.description, body.enabled),
        expected_policy_version=body.expected_policy_version, now=utcnow()),
        lambda receipt: service.reads.group(actor, receipt.target.id), 'group')


@router.patch('/groups/{group_id}', summary='Change a group')
async def update_group(group_id: str, body: GroupPatch, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    def mutate():
        current = service.reads.group(actor, group_id)
        values = GroupValues(body.name if body.name is not None else current['name'],
            body.description if body.description is not None else current['description'] or '',
            body.enabled if body.enabled is not None else current['enabled'])
        return service.mutations.update_group(actor, VersionedEntity(group_id, body.version), values,
            expected_policy_version=body.expected_policy_version, now=utcnow())
    return await _mutate(mutate, lambda receipt: service.reads.group(actor, receipt.target.id), 'group')


@router.post('/groups/{group_id}/members', summary='Add a member')
async def add_member(group_id: str, body: MemberAdd, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    receipt = await run_lifecycle_step(private_operation(lambda: service.mutations.add_membership(actor,
        VersionedEntity(group_id, body.group_version), VersionedEntity(body.principal_id, body.principal_version),
        expected_policy_version=body.expected_policy_version, now=utcnow())))
    group = await run_lifecycle_step(lambda: committed_view(lambda: service.reads.group(actor, group_id),
        {'id': group_id, 'version': receipt.related[0].version}))
    return {'membership_id': receipt.target.id, 'membership_version': receipt.target.version, 'group': group,
            'policy_version': receipt.policy_version}


@router.post('/groups/{group_id}/members/{membership_id}/remove', summary='Remove a member')
async def remove_member(group_id: str, membership_id: str, body: MemberRemove, request: Request,
        identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    receipt = await run_lifecycle_step(private_operation(lambda: service.mutations.remove_membership(actor,
        VersionedEntity(membership_id, body.membership_version), VersionedEntity(group_id, body.group_version),
        expected_policy_version=body.expected_policy_version, now=utcnow())))
    group = await run_lifecycle_step(lambda: committed_view(lambda: service.reads.group(actor, group_id),
        {'id': group_id, 'version': receipt.related[0].version}))
    return {'membership_id': receipt.target.id, 'group': group, 'policy_version': receipt.policy_version}


# -- resources and assignments -------------------------------------------------------------

@router.get('/resources', summary='Resources that can receive assignments')
async def list_resources(request: Request, kind: str | None = None, cursor: Cursor = None, limit: Limit = 50,
        identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.resources(identity.actor, kind=kind, cursor=cursor, limit=limit))


@router.get('/assignments', summary='Role assignments')
async def list_assignments(request: Request, subject_id: str | None = None, resource_id: str | None = None,
        cursor: Cursor = None, limit: Limit = 50, identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.assignments(identity.actor, subject_id=subject_id,
        resource_id=resource_id, cursor=cursor, limit=limit))


@router.post('/assignments', summary='Assign a role')
async def create_assignment(body: AssignmentCreate, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    subject_ref = VersionedEntity(body.subject.id, body.subject.version)
    subject = PrincipalSubject(subject_ref) if body.subject.kind == 'principal' else GroupSubject(subject_ref)
    values = AssignmentValues(subject, VersionedEntity(body.role.id, body.role.version), ResourceRef(body.resource_id))
    receipt = await run_lifecycle_step(private_operation(lambda: service.mutations.create_assignment(actor, values,
        expected_policy_version=body.expected_policy_version, now=utcnow())))

    def view():
        page = service.reads.assignments(actor, subject_id=body.subject.id, resource_id=body.resource_id, limit=200)
        return next(item for item in page['items'] if item['id'] == receipt.target.id)
    assignment = await run_lifecycle_step(lambda: committed_view(view,
        {'id': receipt.target.id, 'version': receipt.target.version}))
    return {'assignment': assignment, 'subject': {'kind': body.subject.kind, **_related(receipt)[0]},
            'policy_version': receipt.policy_version}


@router.post('/assignments/{assignment_id}/remove', summary='Remove a role assignment')
async def remove_assignment(assignment_id: str, body: VersionedAction, request: Request,
        identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    receipt = await run_lifecycle_step(private_operation(lambda: service.mutations.remove_assignment(actor,
        VersionedEntity(assignment_id, body.version), expected_policy_version=body.expected_policy_version, now=utcnow())))
    return {'assignment_id': receipt.target.id, 'subject': _related(receipt)[0], 'policy_version': receipt.policy_version}


# -- keys ------------------------------------------------------------------------------------

@router.get('/keys', summary='API keys')
async def list_keys(request: Request, principal_id: str | None = None, cursor: Cursor = None, limit: Limit = 50,
        identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.keys(identity.actor, principal_id=principal_id, cursor=cursor, limit=limit))


@router.post('/keys', summary='Issue an API key (the token is shown once)')
async def create_key(body: KeyCreate, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    expires_at = _naive_utc(body.expires_at)
    values = KeyValues(VersionedEntity(body.principal.id, body.principal.version), body.name, body.note,
                       expires_at, _ceiling(body.ceiling))
    @private_operation
    def issue():
        prepared = service.credential_codec.prepare_new_key()
        receipt = service.mutations.issue_key(actor, values, prepared,
            expected_policy_version=body.expected_policy_version, now=utcnow())
        return receipt, prepared
    receipt, prepared = await service.work.run(issue)
    key = await run_lifecycle_step(lambda: committed_view(lambda: service.reads.key(actor, receipt.public_key_id),
        _key_fallback(receipt, body.name, body.note, expires_at)))
    return {'key': key, 'token': prepared._token_for_committed_adapter(), 'policy_version': receipt.policy_version}


@router.patch('/keys/{key_id}', summary='Change a key name, note or expiry')
async def update_key(key_id: str, body: KeyPatch, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    supplied = body.model_fields_set
    def mutate():
        current = service.reads.key(actor, key_id)
        binding = service.reads.key_binding_id(actor, key_id)
        # An explicit null clears a field; an absent field keeps it.
        values = KeyMetadata(body.name if 'name' in supplied else current['name'],
            body.note if 'note' in supplied else current['note'],
            _naive_utc(body.expires_at) if 'expires_at' in supplied else current['expires_at'])
        return service.mutations.update_key_metadata(actor, VersionedEntity(binding, body.version), values,
            expected_policy_version=body.expected_policy_version, now=utcnow())
    return await _mutate(mutate, lambda receipt: service.reads.key(actor, receipt.public_key_id), 'key')


@router.post('/keys/{key_id}/rotate', summary='Replace a key secret (the token is shown once)')
async def rotate_key(key_id: str, body: VersionedAction, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    @private_operation
    def rotate():
        binding = service.reads.key_binding_id(actor, key_id)
        prepared = service.credential_codec.prepare_key_rotation(key_id)
        receipt = service.mutations.rotate_key(actor, VersionedEntity(binding, body.version), prepared,
            expected_policy_version=body.expected_policy_version, now=utcnow())
        return receipt, prepared
    receipt, prepared = await service.work.run(rotate)
    key = await run_lifecycle_step(lambda: committed_view(lambda: service.reads.key(actor, receipt.public_key_id),
        _key_fallback(receipt, None, None, None)))
    return {'key': key, 'token': prepared._token_for_committed_adapter(), 'policy_version': receipt.policy_version}


@router.post('/keys/{key_id}/revoke', summary='Revoke a key')
async def revoke_key(key_id: str, body: VersionedAction, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    def mutate():
        binding = service.reads.key_binding_id(actor, key_id)
        return service.mutations.revoke_key(actor, VersionedEntity(binding, body.version),
            expected_policy_version=body.expected_policy_version, now=utcnow())
    return await _mutate(mutate, lambda receipt: service.reads.key(actor, receipt.public_key_id), 'key')


@router.post('/keys/{key_id}/approve', summary='Approve a migrated key that is waiting for review')
async def approve_key(key_id: str, body: KeyApprove, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    def mutate():
        binding = service.reads.key_binding_id(actor, key_id)
        return service.mutations.approve_pending_key(actor, VersionedEntity(binding, body.version),
            VersionedEntity(body.principal.id, body.principal.version), _ceiling(body.ceiling),
            expected_policy_version=body.expected_policy_version, now=utcnow())
    return await _mutate(mutate, lambda receipt: service.reads.key(actor, receipt.public_key_id), 'key')


# -- sessions ----------------------------------------------------------------------------------

@router.get('/sessions', summary='Sessions (your own, or another principal with sessions:read)')
async def list_sessions(request: Request, principal_id: str | None = None, cursor: Cursor = None, limit: Limit = 50,
        identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.sessions(identity.actor, principal_id=principal_id, cursor=cursor, limit=limit))


@router.post('/sessions/{session_id}/revoke', summary='Revoke a session')
async def revoke_session(session_id: str, body: SessionRevoke, request: Request, response: Response,
        identity=Depends(require_identity)):
    service = runtime(request)
    @private_operation
    def revoke():
        return service.authentication._complete(lambda connection, actor, now:
            service.sessions.revoke_session_on(connection, actor, session_id,
                expected_policy_version=body.expected_policy_version, now=now), identity.actor)
    result = await run_lifecycle_step(revoke)
    if identity.source == 'session' and getattr(identity.actor.credential, 'session_id', None) == session_id:
        selected = transport(request)
        response.delete_cookie(selected.cookie_name, path='/', secure=selected.secure, httponly=True, samesite='strict')
    return {'session_id': result.session_id, 'changed': result.changed, 'policy_version': result.policy_version}


# -- mailboxes and inbound rules -------------------------------------------------------------------

@router.get('/mailboxes', summary='Mailboxes')
async def list_mailboxes(request: Request, cursor: Cursor = None, limit: Limit = 50, identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.mailboxes(identity.actor, cursor=cursor, limit=limit))


@router.post('/mailboxes', summary='Create a mailbox')
async def create_mailbox(body: MailboxCreate, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    return await _mutate(lambda: service.mutations.create_mailbox(actor, MailboxValues(body.label, body.enabled),
        expected_policy_version=body.expected_policy_version, now=utcnow()),
        lambda receipt: service.reads.mailbox(actor, receipt.target.id), 'mailbox')


@router.patch('/mailboxes/{mailbox_id}', summary='Rename, enable or disable a mailbox')
async def update_mailbox(mailbox_id: str, body: MailboxPatch, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    def mutate():
        current = service.reads.mailbox(actor, mailbox_id)
        values = MailboxValues(body.label if body.label is not None else current['label'],
                               body.enabled if body.enabled is not None else current['enabled'])
        return service.mutations.update_mailbox(actor, VersionedEntity(mailbox_id, body.version), values,
            expected_policy_version=body.expected_policy_version, now=utcnow())
    return await _mutate(mutate, lambda receipt: service.reads.mailbox(actor, receipt.target.id), 'mailbox')


def _receiving(service):
    from .receiving_rules import tables
    return tables(service.store.engine)


def _with_options(service, items):
    """Each rule with its receiving options and its place among number rules (1 first)."""
    from .receiving_rules import DEFAULT_OPTIONS, options_on, rule_order
    receiving = _receiving(service)
    if receiving is None:
        return items
    with service.store.engine.connect() as connection:
        order = rule_order(connection, service.store.tables, receiving)
        stored = options_on(connection, receiving, [item['id'] for item in items])
    place = {identity: number + 1 for number, identity in enumerate(order)}
    for item in items:
        options = (stored.get(item['id']) or (None,))[0] or dict(DEFAULT_OPTIONS)
        item.update(options)
        item['position'] = place.get(item['id'])
    return items


def _rule_view(service, actor, rule_id):
    return _with_options(service, [service.reads.inbound_rule(actor, rule_id)])[0]


def _current_options(service, rule_id):
    """(the receiving-rule tables, a rule's stored options or None)."""
    from .receiving_rules import options_on
    receiving = _receiving(service)
    if receiving is None or rule_id is None:
        return receiving, None
    with service.store.engine.connect() as connection:
        return receiving, (options_on(connection, receiving, [rule_id]).get(rule_id) or (None,))[0]


async def _rule_mutation(mutate, view, number):
    """A number-rule change; a second rule for a number that already has a rule without conditions is refused
    with a sentence about the number (reached only after the permission check, so it reveals no mailbox)."""
    try:
        return await _mutate(mutate, view, 'rule')
    except MutationDeniedError as denied:
        if denied.code != 'duplicate':
            raise
    raise HTTPException(400, detail=f'A rule already takes every fax to {number or "this number"}. Give this rule a '
                                    'condition, such as a subaddress or a sender, or change that rule.')


async def _clean_rule_options(request, service, body, rule_id=None):
    """(complete cleaned options or None when none were given, position). 400 with a sentence when refused."""
    from .receiving_rules import ReceivingRuleError, clean_options
    given = body.model_dump(exclude_unset=True, include=set(RuleOptions.model_fields) - {'position'})
    position = body.position
    if not given:
        return None, position
    receiving, current = await _read(lambda: _current_options(service, rule_id))
    if receiving is None:
        raise HTTPException(400, detail='Upgrade the database before giving a number rule options.')
    country = request.scope['faxbot.configuration'].active.values.fax_default_country
    try:
        return clean_options(given, current, country=country,
                             subaddress_column='subaddress' in receiving['options'].c), position
    except ReceivingRuleError as error:
        raise HTTPException(400, detail=str(error)) from None


@router.get('/inbound-rules', summary='Inbound routing rules')
async def list_inbound_rules(request: Request, cursor: Cursor = None, limit: Limit = 50,
        identity=Depends(require_identity)):
    service = runtime(request)
    page = await _read(lambda: service.reads.inbound_rules(identity.actor, cursor=cursor, limit=limit))
    page['items'] = await _read(lambda: _with_options(service, page['items']))
    return page


def _fax_number(value, request):
    """A rule's number in E.164; national input is read for the installation country."""
    from ..routing.numbers import InvalidNumber, normalize_number
    country = request.scope['faxbot.configuration'].active.values.fax_default_country
    try:
        return normalize_number(value, country=country)
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


@router.post('/inbound-rules', summary='Route a fax number to a mailbox')
async def create_inbound_rule(body: RuleCreate, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    options, position = await _clean_rule_options(request, service, body)
    # An "any number" rule matches every receiving number and stores none.
    number = '' if options and options['any_number'] else _fax_number(body.to_number, request)
    return await _rule_mutation(lambda: service.mutations.create_inbound_rule(actor,
        InboundRuleValues(number, body.mailbox_id),
        expected_policy_version=body.expected_policy_version, now=utcnow(), options=options, position=position),
        lambda receipt: _rule_view(service, actor, receipt.target.id), number)


@router.patch('/inbound-rules/{rule_id}', summary='Change an inbound routing rule')
async def update_inbound_rule(rule_id: str, body: RulePatch, request: Request, identity=Depends(require_identity)):
    service, actor = runtime(request), identity.actor
    number = None if not body.to_number else _fax_number(body.to_number, request)
    options, position = await _clean_rule_options(request, service, body, rule_id)
    def mutate():
        current = service.reads.inbound_rule(actor, rule_id)
        values = InboundRuleValues(number if number is not None else current['to_number'],
                                   body.mailbox_id if body.mailbox_id is not None else current['mailbox_id'])
        return service.mutations.update_inbound_rule(actor, VersionedEntity(rule_id, body.version), values,
            expected_policy_version=body.expected_policy_version, now=utcnow(), options=options, position=position)
    return await _rule_mutation(mutate, lambda receipt: _rule_view(service, actor, receipt.target.id), number)


@router.post('/inbound-rules/explain', summary='Which mailbox and email a received fax would get, and why',
             dependencies=[Depends(require_permission('mailboxes:read'))])
async def explain_received(body: ReceivedExplain, request: Request):
    """The receiving rules' answer for a fax that has not arrived. Nothing is received or saved."""
    from .receiving_rules import ReceivingRuleError, _local_moment, explain
    values = request.scope['faxbot.configuration'].active.values
    try:
        moment = _local_moment(body.at, values.time_zone)
    except ReceivingRuleError as error:
        raise HTTPException(400, detail=str(error)) from None
    try:
        from ..intake.http import _store as intake_store
        intake = intake_store(request)
    except HTTPException:
        intake = None
    service = runtime(request)
    return await _read(lambda: explain(service.store, intake, values, to_number=body.to_number,
                                       from_number=body.from_number, account_key=body.account_key,
                                       subaddress=body.subaddress, at=moment, diverted_from=body.diverted_from,
                                       diversion=body.diversion))


# -- audit --------------------------------------------------------------------------------------------

@router.get('/audit', summary='Security audit log, newest first')
async def list_audit(request: Request, cursor: Cursor = None, limit: Limit = 50, actor_id: str | None = None,
        target_id: str | None = None, operation: str | None = None, identity=Depends(require_identity)):
    service = runtime(request)
    return await _read(lambda: service.reads.audit(identity.actor, cursor=cursor, limit=limit, actor_id=actor_id,
        target_id=target_id, operation=operation))
