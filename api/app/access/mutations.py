"""Locked, candidate-before-write access mutations; never disclose credentials.

Standalone methods own one transaction and commit routine denial audit before
raising a fixed safe error. Connection-first methods require the supplied store's
existing access lock and return outcomes without ending the transaction. Storage
failures propagate to their owner, who must roll back and map them to a fixed
service failure outside the transaction. An
aggregate owner must roll back its entire transaction on denial if it has already
made business writes: committing that denial would also commit those writes.

Preparation/KDF happens before entry. Only the owning future adapter may disclose
its retained preparation after the standalone method returns a confirmed commit.
Rollback, denial and uncertain commit discard it; issuance is never retried here.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import unicodedata
import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from .catalog import KEY_SCOPES, PERMISSIONS
from .credentials import (CredentialCodec, InvalidCredentialInputError,
    PreparedTemporaryPassword, PreparedNewKey, PreparedKeyRotation, normalize_login)
from .mutation_types import (AssignmentValues, CustomRoleValues, GroupSubject,
    GroupValues, InboundRuleValues, IntegrationKeyValues, IntegrationValues, KeyMetadata, KeyMutationReceipt, KeyValues,
    MailboxValues, MutationDeniedError, MutationOutcome, MutationReason, MutationReceipt,
    OwnerEnrollment, PrincipalSubject, StaleVersionError, UserValues, VersionedEntity)
from .types import (AccessUnavailableError, AuthenticationError, InvalidScopeError,
    InvalidTransactionError, PrincipalContext, ResourceRef, ScopedPermission,
    StaleCredentialError)


class _Denied(Exception):
    def __init__(self, reason):
        self.reason = reason


def _deny(reason=MutationReason.INVALID_INPUT):
    raise _Denied(reason)


def _id(value):
    return type(value) is str and 0 < len(value) <= 40 and all(32 <= ord(c) < 127 for c in value)


def _epoch(value):
    return type(value) is int and value >= 1


def _clock(value):
    return type(value) is datetime and value.tzinfo is None


def _text(value, bound, *, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not str or len(value) > bound:
        _deny()
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        _deny()


def _name(value):
    _text(value, 100)
    if any(unicodedata.category(c).startswith('C') for c in value):
        _deny()
    display = unicodedata.normalize('NFKC', value).strip()
    normalized = display.casefold()
    if not display or len(display) > 100 or not normalized or len(normalized) > 100:
        _deny()
    return display, normalized


def _permissions(value):
    if type(value) is not frozenset or any(type(p) is not str or p not in PERMISSIONS for p in value):
        _deny()
    return value


def _login(value):
    try:
        return normalize_login(value)
    except InvalidCredentialInputError:
        _deny()


@dataclass
class _Attempt:
    connection: object
    actor: PrincipalContext
    now: datetime
    before: int
    operation: str
    kind: str
    target_id: str | None = None
    attribution: tuple = (None, None, None)


@dataclass
class _Plan:
    receipt: MutationReceipt
    writes: object


class _Graph:
    """Candidate overlays for edge enumeration and named-Owner eligibility only.

    Actor authority remains the core's locked before graph. This planner neither
    evaluates general permissions nor maps their scope kinds.
    """
    def __init__(self, connection, tables):
        for short, name in (('principals', 'access_principals'), ('users', 'access_users'),
            ('groups', 'access_groups'), ('roles', 'access_roles'),
            ('memberships', 'access_memberships'), ('assignments', 'access_assignments')):
            setattr(self, short, {r['id']: dict(r) for r in connection.execute(sa.select(tables[name])).mappings()})
        self.permissions = {}
        for role, permission in connection.execute(sa.select(tables['access_role_permissions'].c.role_id,
                tables['access_role_permissions'].c.permission_id)):
            self.permissions.setdefault(role, set()).add(permission)

    def role_edges(self, role_id):
        return [a for a in self.assignments.values() if a['role_id'] == role_id]

    def group_edges(self, group_id):
        return [a for a in self.assignments.values() if a['group_id'] == group_id]

    def owner_edge(self, assignment):
        role = self.roles.get(assignment['role_id'])
        return role is not None and role['id'] == 'role_owner' and role['kind'] == 'builtin'

    def protected_principal(self, principal_id):
        groups = {m['group_id'] for m in self.memberships.values() if m['principal_id'] == principal_id}
        return any(self.owner_edge(a) and (a['principal_id'] == principal_id or a['group_id'] in groups)
            for a in self.assignments.values())

    def owners(self, codec):
        owner = self.roles.get('role_owner')
        if owner is None or owner['kind'] != 'builtin' or owner['enabled'] != 1:
            return frozenset()
        direct = set()
        owner_groups = set()
        for a in self.assignments.values():
            if a['role_id'] != 'role_owner' or a['resource_id'] != 'installation':
                continue
            if a['principal_id'] is not None:
                direct.add(a['principal_id'])
            elif self.groups.get(a['group_id'], {}).get('enabled') == 1:
                owner_groups.add(a['group_id'])
        direct.update(m['principal_id'] for m in self.memberships.values() if m['group_id'] in owner_groups)
        eligible = set()
        for identity in direct:
            p, u = self.principals.get(identity), self.users.get(identity)
            if p is None or p['kind'] != 'user' or p['enabled'] != 1 or u is None:
                continue
            try:
                display, normalized = normalize_login(u['login'])
            except InvalidCredentialInputError:
                continue
            if (u['login'] == display and u['normalized_login'] == normalized
                    and codec.supports_hash(u['password_hash'])):
                eligible.add(identity)
        return frozenset(eligible)


class AccessMutations:
    def __init__(self, store, control, codec: CredentialCodec):
        if getattr(control, 'store', None) is not store:
            raise InvalidTransactionError()
        self.store, self.control, self.codec = store, control, codec
        self.tables = store.tables

    def _standalone(self, method, actor, *args, expected_policy_version, now):
        failed = False
        try:
            with self.store.transaction() as connection:
                outcome = method(connection, actor, *args,
                    expected_policy_version=expected_policy_version, now=now)
        except (sa.exc.SQLAlchemyError, AccessUnavailableError):
            failed = True
        # Raise outside the handler so no storage exception context survives.
        if failed:
            raise AccessUnavailableError()
        if outcome.reason == MutationReason.CREDENTIAL_STALE:
            raise StaleCredentialError()
        if outcome.reason == MutationReason.STALE_VERSION:
            raise StaleVersionError()
        if outcome.reason is not None:
            raise MutationDeniedError(outcome.reason)
        return outcome.receipt

    def _run(self, connection, actor, operation, kind, category, planner, *, expected_policy_version, now):
        before = self.store.require_lock_on(connection)
        attempt = _Attempt(connection, actor, now, before, operation, kind)
        plan, reason = None, None
        try:
            if not _clock(now):
                _deny()
            decision = self.control.authorize_on(connection, actor, category, ResourceRef('installation'), now=now)
            # These references are used only after the core verified this live source.
            attempt.attribution = (actor.principal_id, getattr(actor.credential, 'binding_id', None),
                getattr(actor.credential, 'session_id', None))
            if not decision.allowed:
                _deny(MutationReason.RESET_REQUIRED if decision.reason.value == 'reset_required' else MutationReason.FORBIDDEN)
            if not _epoch(expected_policy_version):
                _deny()
            if expected_policy_version != before:
                _deny(MutationReason.STALE_VERSION)
            plan = planner(attempt)
        except _Denied as denial:
            reason = denial.reason
        except AuthenticationError:
            reason = MutationReason.CREDENTIAL_STALE
            attempt.attribution = (None, None, None)
        after = before
        if plan is not None:
            plan.writes()
            after = plan.receipt.policy_version
            if plan.receipt.changed:
                state = self.tables['access_state']
                connection.execute(state.update().where(state.c.id == 'state').values(policy_version=after, updated_at=now))
            attempt.target_id = plan.receipt.target.id
        details = {'reason': reason.value} if reason is not None else {'changed': plan.receipt.changed}
        self._audit(attempt, after, reason, details)
        return MutationOutcome(None if plan is None else plan.receipt, reason, after)

    def _audit(self, attempt, after, reason, details):
        encoded = json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True)
        if len(encoded.encode('utf-8')) > 2048:
            raise AccessUnavailableError()
        principal, key, session = attempt.attribution
        self._insert(attempt.connection, 'access_audit', id=uuid.uuid4().hex,
            actor_principal_id=principal, actor_key_binding_id=key, actor_session_id=session,
            operation=attempt.operation, target_kind=attempt.kind,
            target_id=attempt.target_id if _id(attempt.target_id) else None,
            policy_version_before=attempt.before, policy_version_after=after,
            outcome='denied' if reason else 'allowed', details=encoded,
            created_at=attempt.now if _clock(attempt.now) else datetime.now(timezone.utc).replace(tzinfo=None))

    def _insert(self, connection, table_name, **values):
        connection.execute(self.tables[table_name].insert().values(**values))

    def _update(self, connection, table_name, identity, **values):
        table = self.tables[table_name]
        connection.execute(table.update().where(table.c.id == identity).values(**values))

    def _delete(self, connection, name, identity):
        table = self.tables[name]
        connection.execute(table.delete().where(table.c.id == identity))

    def _load(self, attempt, name, target, *, audit_target=False):
        if type(target) is not VersionedEntity or not _id(target.id) or not _epoch(target.version):
            _deny()
        table = self.tables[name]
        row = attempt.connection.execute(sa.select(table).where(table.c.id == target.id)).mappings().one_or_none()
        if row is None:
            _deny(MutationReason.INVALID_TARGET)
        if audit_target:
            attempt.target_id = row['id']
        if row['version'] != target.version:
            _deny(MutationReason.STALE_VERSION)
        return dict(row)

    def _unique(self, attempt, name, field, value, exclude=None):
        table = self.tables[name]
        query = sa.select(table.c.id).where(table.c[field] == value)
        if exclude is not None:
            query = query.where(table.c.id != exclude)
        if attempt.connection.execute(query).first() is not None:
            _deny(MutationReason.DUPLICATE)

    def _owner(self, attempt):
        if not self.control.is_complete_owner_on(attempt.connection, attempt.actor, now=attempt.now):
            _deny(MutationReason.OWNER_REQUIRED)

    def _dominance(self, attempt, principal_id, graph=None):
        graph = graph or _Graph(attempt.connection, self.tables)
        if graph.protected_principal(principal_id):
            self._owner(attempt)
        decision = self.control.dominates_principal_on(attempt.connection, attempt.actor, principal_id, now=attempt.now)
        if not decision.allowed:
            _deny(MutationReason.OWNER_REQUIRED if decision.reason.value == 'owner_required' else MutationReason.FORBIDDEN)

    def _last_owner(self, graph, before):
        if before and not graph.owners(self.codec):
            _deny(MutationReason.LAST_OWNER)

    def _edges(self, attempt, graph, edges, *, replacement=None):
        for edge in sorted(edges, key=lambda e: e['id']):
            if graph.owner_edge(edge):
                self._owner(attempt)
            old = frozenset(graph.permissions.get(edge['role_id'], ()))
            self._coverage(attempt, old, ResourceRef(edge['resource_id']))
            if replacement is not None:
                self._coverage(attempt, replacement, ResourceRef(edge['resource_id']))

    def _coverage(self, attempt, permissions, resource):
        try:
            projected = self.control.scope_projection_on(attempt.connection, permissions, resource)
        except InvalidScopeError:
            _deny(MutationReason.INVALID_INPUT)
        decision = self.control.can_grant_on(attempt.connection, attempt.actor, projected.applicable, now=attempt.now)
        if not decision.allowed:
            _deny(MutationReason.FORBIDDEN)

    def _ceiling(self, attempt, ceiling):
        if type(ceiling) is not tuple:
            _deny()
        seen = set()
        for grant in ceiling:
            if (type(grant) is not ScopedPermission or type(grant.permission) is not str
                    or grant.permission not in PERMISSIONS or type(grant.resource) is not ResourceRef
                    or not _id(grant.resource.id)):
                _deny()
            pair = (grant.permission, grant.resource.id)
            if pair in seen:
                _deny()
            seen.add(pair)
            try:
                projected = self.control.scope_projection_on(attempt.connection, frozenset({grant.permission}), grant.resource)
            except InvalidScopeError:
                _deny()
            if not projected.applicable:
                _deny()
        if not self.control.can_grant_on(attempt.connection, attempt.actor, ceiling, now=attempt.now).allowed:
            _deny(MutationReason.FORBIDDEN)
        return tuple(sorted(ceiling, key=lambda g: (g.permission, g.resource.id)))

    def _plan(self, attempt, identity, version, changed, writes, *, related=(), key=None):
        values = dict(target=VersionedEntity(identity, version), policy_version=attempt.before + int(changed),
            changed=changed, related=tuple(sorted(related, key=lambda row: row.id)))
        receipt = MutationReceipt(**values) if key is None else KeyMutationReceipt(**values, **key)
        return _Plan(receipt, writes)

    def _new_principal(self, attempt, kind, display, enabled, *, login=None, password=None, owner=False):
        identity, resource_id = uuid.uuid4().hex, uuid.uuid4().hex
        normalized = None
        if kind == 'user':
            login, normalized = _login(login)
            self._unique(attempt, 'access_users', 'normalized_login', normalized)
        if kind == 'user':
            if type(password) is not PreparedTemporaryPassword or not self.codec.supports_hash(password._password_hash_for_storage()):
                _deny()
        _text(display, 200)
        if type(enabled) is not bool:
            _deny()
        if owner:
            self._owner(attempt)
            # A valid installation ancestry is required even when all permissions
            # are implicit through verified bootstrap recovery.
            self._coverage(attempt, PERMISSIONS, ResourceRef('installation'))
        def writes():
            timestamps = dict(created_at=attempt.now, updated_at=attempt.now)
            self._insert(attempt.connection, 'access_principals', id=identity, kind=kind,
                display_name=display, enabled=int(enabled), security_version=1, version=1, **timestamps)
            if kind == 'user':
                self._insert(attempt.connection, 'access_users', id=identity, login=login,
                    normalized_login=normalized, password_hash=password._password_hash_for_storage(),
                    password_change_required=1, password_version=1, **timestamps)
            self._insert(attempt.connection, 'access_resources', id=resource_id, kind='personal',
                parent_id='installation', parent_kind='installation', principal_id=identity,
                mailbox_id=None, fax_job_id=None, inbound_fax_id=None, enabled=1, version=1, **timestamps)
            if owner:
                self._insert(attempt.connection, 'access_assignments', id=uuid.uuid4().hex,
                    principal_id=identity, group_id=None, role_id='role_owner', resource_id='installation', version=1, **timestamps)
        return self._plan(attempt, identity, 1, True, writes)

    def create_user_on(self, connection: Connection, actor: PrincipalContext, values: UserValues, password: PreparedTemporaryPassword, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            if type(values) is not UserValues: _deny()
            return self._new_principal(a, 'user', values.display_name, values.enabled, login=values.login, password=password)
        return self._run(connection, actor, 'create_user', 'principal', 'users:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def create_user(self, actor: PrincipalContext, values: UserValues, password: PreparedTemporaryPassword, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.create_user_on, actor, values, password, expected_policy_version=expected_policy_version, now=now)

    def create_integration_on(self, connection: Connection, actor: PrincipalContext, values: IntegrationValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            if type(values) is not IntegrationValues: _deny()
            return self._new_principal(a, 'integration', values.display_name, values.enabled)
        return self._run(connection, actor, 'create_integration', 'principal', 'users:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def create_integration(self, actor: PrincipalContext, values: IntegrationValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.create_integration_on, actor, values, expected_policy_version=expected_policy_version, now=now)

    def enroll_owner_on(self, connection: Connection, actor: PrincipalContext, values: OwnerEnrollment, password: PreparedTemporaryPassword, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            if type(values) is not OwnerEnrollment: _deny()
            return self._new_principal(a, 'user', values.display_name, True, login=values.login, password=password, owner=True)
        return self._run(connection, actor, 'enroll_owner', 'principal', 'owner:recover', planner, expected_policy_version=expected_policy_version, now=now)

    def enroll_owner(self, actor: PrincipalContext, values: OwnerEnrollment, password: PreparedTemporaryPassword, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.enroll_owner_on, actor, values, password, expected_policy_version=expected_policy_version, now=now)

    def _revoke_sessions(self, a, *, principal=None, binding=None):
        table = self.tables['access_sessions']
        predicate = table.c.principal_id == principal if principal is not None else table.c.source_key_id == binding
        a.connection.execute(table.update().where(predicate, table.c.revoked_at.is_(None)).values(revoked_at=a.now))

    def _principal_bindings(self, a, identity):
        bindings = self.tables['access_key_bindings']
        return tuple(a.connection.execute(sa.select(bindings.c.id).where(bindings.c.principal_id == identity)).scalars())

    def _revoke_principal_sources(self, a, identity, identities, *, reset=False):
        self._revoke_sessions(a, principal=identity)
        bindings, keys = self.tables['access_key_bindings'], self.tables['api_keys']
        query = bindings.update().where(bindings.c.principal_id == identity)
        if not reset:
            query = query.where(bindings.c.state != 'revoked')
        a.connection.execute(query.values(state='revoked', revoked_at=sa.func.coalesce(bindings.c.revoked_at, a.now),
            version=bindings.c.version + 1, security_version=bindings.c.security_version + 1, updated_at=a.now))
        if identities:
            a.connection.execute(keys.update().where(keys.c.id.in_(identities), keys.c.revoked_at.is_(None)).values(revoked_at=a.now))

    def _update_principal(self, a, target, values, kind):
        expected_type = UserValues if kind == 'user' else IntegrationValues
        if type(values) is not expected_type or type(values.enabled) is not bool: _deny()
        _text(values.display_name, 200)
        p = self._load(a, 'access_principals', target, audit_target=True)
        if p['kind'] != kind: _deny(MutationReason.INVALID_TARGET)
        graph = _Graph(a.connection, self.tables)
        self._dominance(a, p['id'], graph)
        before = graph.owners(self.codec)
        login_changed = False
        login = normalized = None
        if kind == 'user':
            login, normalized = _login(values.login)
            u = graph.users.get(p['id'])
            if u is None: _deny(MutationReason.INVALID_TARGET)
            self._unique(a, 'access_users', 'normalized_login', normalized, p['id'])
            login_changed = (u['login'], u['normalized_login']) != (login, normalized)
            graph.users[p['id']] = dict(u, login=login, normalized_login=normalized)
        enabled_changed = p['enabled'] != int(values.enabled)
        changed = login_changed or enabled_changed or p['display_name'] != values.display_name
        graph.principals[p['id']] = dict(p, enabled=int(values.enabled))
        self._last_owner(graph, before)
        bindings = self._principal_bindings(a, p['id']) if enabled_changed and not values.enabled else ()
        def writes():
            if not changed: return
            self._update(a.connection, 'access_principals', p['id'], display_name=values.display_name,
                enabled=int(values.enabled), version=p['version'] + 1,
                security_version=p['security_version'] + int(login_changed or enabled_changed), updated_at=a.now)
            if kind == 'user' and login_changed:
                self._update(a.connection, 'access_users', p['id'], login=login, normalized_login=normalized, updated_at=a.now)
            if enabled_changed and not values.enabled:
                self._revoke_principal_sources(a, p['id'], bindings)
            elif login_changed:
                self._revoke_sessions(a, principal=p['id'])
        return self._plan(a, p['id'], p['version'] + int(changed), changed, writes)

    def update_user_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, values: UserValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        return self._run(connection, actor, 'update_user', 'principal', 'users:manage', lambda a: self._update_principal(a, target, values, 'user'), expected_policy_version=expected_policy_version, now=now)

    def update_user(self, actor: PrincipalContext, target: VersionedEntity, values: UserValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.update_user_on, actor, target, values, expected_policy_version=expected_policy_version, now=now)

    def update_integration_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, values: IntegrationValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        return self._run(connection, actor, 'update_integration', 'principal', 'users:manage', lambda a: self._update_principal(a, target, values, 'integration'), expected_policy_version=expected_policy_version, now=now)

    def update_integration(self, actor: PrincipalContext, target: VersionedEntity, values: IntegrationValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.update_integration_on, actor, target, values, expected_policy_version=expected_policy_version, now=now)

    def reset_password_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, password: PreparedTemporaryPassword, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            if type(password) is not PreparedTemporaryPassword or not self.codec.supports_hash(password._password_hash_for_storage()): _deny()
            p = self._load(a, 'access_principals', target, audit_target=True)
            if p['kind'] != 'user': _deny(MutationReason.INVALID_TARGET)
            graph = _Graph(a.connection, self.tables)
            self._dominance(a, p['id'], graph)
            u = graph.users.get(p['id'])
            if u is None: _deny(MutationReason.INVALID_TARGET)
            bindings = self._principal_bindings(a, p['id'])
            def writes():
                self._update(a.connection, 'access_principals', p['id'], version=p['version'] + 1,
                    security_version=p['security_version'] + 1, updated_at=a.now)
                self._update(a.connection, 'access_users', p['id'], password_hash=password._password_hash_for_storage(),
                    password_change_required=1, password_version=u['password_version'] + 1, updated_at=a.now)
                self._revoke_principal_sources(a, p['id'], bindings, reset=True)
            return self._plan(a, p['id'], p['version'] + 1, True, writes)
        return self._run(connection, actor, 'reset_password', 'principal', 'users:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def reset_password(self, actor: PrincipalContext, target: VersionedEntity, password: PreparedTemporaryPassword, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.reset_password_on, actor, target, password, expected_policy_version=expected_policy_version, now=now)

    def _group_values(self, values):
        if type(values) is not GroupValues or type(values.enabled) is not bool: _deny()
        name, normalized = _name(values.name)
        _text(values.description, 500)
        return name, normalized

    def create_group_on(self, connection: Connection, actor: PrincipalContext, values: GroupValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            name, normalized = self._group_values(values)
            self._unique(a, 'access_groups', 'normalized_name', normalized)
            identity = uuid.uuid4().hex
            return self._plan(a, identity, 1, True, lambda: self._insert(a.connection, 'access_groups',
                id=identity, name=name, normalized_name=normalized, description=values.description,
                enabled=int(values.enabled), version=1, created_at=a.now, updated_at=a.now))
        return self._run(connection, actor, 'create_group', 'group', 'groups:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def create_group(self, actor: PrincipalContext, values: GroupValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.create_group_on, actor, values, expected_policy_version=expected_policy_version, now=now)

    def update_group_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, values: GroupValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            name, normalized = self._group_values(values)
            row = self._load(a, 'access_groups', target, audit_target=True)
            self._unique(a, 'access_groups', 'normalized_name', normalized, row['id'])
            graph = _Graph(a.connection, self.tables)
            before = graph.owners(self.codec)
            if row['enabled'] != int(values.enabled):
                self._edges(a, graph, graph.group_edges(row['id']))
            graph.groups[row['id']] = dict(row, enabled=int(values.enabled))
            self._last_owner(graph, before)
            changed = (row['name'], row['normalized_name'], row['description'], row['enabled']) != (name, normalized, values.description, int(values.enabled))
            def writes():
                if changed:
                    self._update(a.connection, 'access_groups', row['id'], name=name, normalized_name=normalized,
                        description=values.description, enabled=int(values.enabled), version=row['version'] + 1, updated_at=a.now)
            return self._plan(a, row['id'], row['version'] + int(changed), changed, writes)
        return self._run(connection, actor, 'update_group', 'group', 'groups:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def update_group(self, actor: PrincipalContext, target: VersionedEntity, values: GroupValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.update_group_on, actor, target, values, expected_policy_version=expected_policy_version, now=now)

    def _membership(self, a, group, *, principal=None, membership=None):
        g = self._load(a, 'access_groups', group)
        graph = _Graph(a.connection, self.tables)
        before = graph.owners(self.codec)
        if membership is None:
            p = self._load(a, 'access_principals', principal)
            if p['kind'] not in {'user', 'integration'}: _deny(MutationReason.INVALID_TARGET)
            identity = uuid.uuid4().hex
            if any(m['group_id'] == g['id'] and m['principal_id'] == p['id'] for m in graph.memberships.values()):
                _deny(MutationReason.DUPLICATE)
            graph.memberships[identity] = dict(id=identity, group_id=g['id'], principal_id=p['id'])
            version = 1
        else:
            m = self._load(a, 'access_memberships', membership, audit_target=True)
            if m['group_id'] != g['id']: _deny(MutationReason.INVALID_TARGET)
            identity, version = m['id'], m['version']
            del graph.memberships[identity]
        self._edges(a, graph, graph.group_edges(g['id']))
        self._last_owner(graph, before)
        def writes():
            if membership is None:
                self._insert(a.connection, 'access_memberships', id=identity, group_id=g['id'], principal_id=p['id'],
                    version=1, created_at=a.now, updated_at=a.now)
            else:
                self._delete(a.connection, 'access_memberships', identity)
            self._update(a.connection, 'access_groups', g['id'], version=g['version'] + 1, updated_at=a.now)
        return self._plan(a, identity, version, True, writes, related=(VersionedEntity(g['id'], g['version'] + 1),))

    def add_membership_on(self, connection: Connection, actor: PrincipalContext, group: VersionedEntity, principal: VersionedEntity, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        return self._run(connection, actor, 'add_membership', 'membership', 'groups:manage', lambda a: self._membership(a, group, principal=principal), expected_policy_version=expected_policy_version, now=now)

    def add_membership(self, actor: PrincipalContext, group: VersionedEntity, principal: VersionedEntity, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.add_membership_on, actor, group, principal, expected_policy_version=expected_policy_version, now=now)

    def remove_membership_on(self, connection: Connection, actor: PrincipalContext, membership: VersionedEntity, group: VersionedEntity, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        return self._run(connection, actor, 'remove_membership', 'membership', 'groups:manage', lambda a: self._membership(a, group, membership=membership), expected_policy_version=expected_policy_version, now=now)

    def remove_membership(self, actor: PrincipalContext, membership: VersionedEntity, group: VersionedEntity, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.remove_membership_on, actor, membership, group, expected_policy_version=expected_policy_version, now=now)

    def _role_values(self, values):
        if type(values) is not CustomRoleValues or type(values.enabled) is not bool: _deny()
        name, normalized = _name(values.name)
        _text(values.description, 500)
        return name, normalized, _permissions(values.permissions)

    def _role_members(self, a, identity, permissions):
        table = self.tables['access_role_permissions']
        a.connection.execute(table.delete().where(table.c.role_id == identity))
        for permission in sorted(permissions):
            self._insert(a.connection, 'access_role_permissions', id=uuid.uuid4().hex, role_id=identity, permission_id=permission)

    def create_custom_role_on(self, connection: Connection, actor: PrincipalContext, values: CustomRoleValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            name, normalized, permissions = self._role_values(values)
            self._unique(a, 'access_roles', 'normalized_name', normalized)
            identity = uuid.uuid4().hex
            def writes():
                self._insert(a.connection, 'access_roles', id=identity, name=name, normalized_name=normalized,
                    description=values.description, kind='custom', enabled=int(values.enabled), version=1, created_at=a.now, updated_at=a.now)
                self._role_members(a, identity, permissions)
            return self._plan(a, identity, 1, True, writes)
        return self._run(connection, actor, 'create_custom_role', 'role', 'roles:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def create_custom_role(self, actor: PrincipalContext, values: CustomRoleValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.create_custom_role_on, actor, values, expected_policy_version=expected_policy_version, now=now)

    def update_custom_role_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, values: CustomRoleValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            name, normalized, permissions = self._role_values(values)
            row = self._load(a, 'access_roles', target, audit_target=True)
            if row['kind'] != 'custom': _deny(MutationReason.INVALID_TARGET)
            self._unique(a, 'access_roles', 'normalized_name', normalized, row['id'])
            graph = _Graph(a.connection, self.tables)
            old = frozenset(graph.permissions.get(row['id'], ()))
            edge_changed = old != permissions or row['enabled'] != int(values.enabled)
            if edge_changed:
                self._edges(a, graph, graph.role_edges(row['id']), replacement=permissions)
            changed = edge_changed or (row['name'], row['normalized_name'], row['description']) != (name, normalized, values.description)
            def writes():
                if not changed: return
                self._update(a.connection, 'access_roles', row['id'], name=name, normalized_name=normalized,
                    description=values.description, enabled=int(values.enabled), version=row['version'] + 1, updated_at=a.now)
                if old != permissions: self._role_members(a, row['id'], permissions)
            return self._plan(a, row['id'], row['version'] + int(changed), changed, writes)
        return self._run(connection, actor, 'update_custom_role', 'role', 'roles:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def update_custom_role(self, actor: PrincipalContext, target: VersionedEntity, values: CustomRoleValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.update_custom_role_on, actor, target, values, expected_policy_version=expected_policy_version, now=now)

    def create_assignment_on(self, connection: Connection, actor: PrincipalContext, values: AssignmentValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            if type(values) is not AssignmentValues: _deny()
            if type(values.subject) is PrincipalSubject:
                subject = self._load(a, 'access_principals', values.subject.principal)
                if subject['kind'] not in {'user', 'integration'}: _deny(MutationReason.INVALID_TARGET)
                table, principal, group = 'access_principals', subject['id'], None
            elif type(values.subject) is GroupSubject:
                subject = self._load(a, 'access_groups', values.subject.group)
                table, principal, group = 'access_groups', None, subject['id']
            else: _deny()
            role = self._load(a, 'access_roles', values.role)
            graph = _Graph(a.connection, self.tables)
            self._coverage(a, frozenset(graph.permissions.get(role['id'], ())), values.resource)
            if role['id'] == 'role_owner' and role['kind'] == 'builtin': self._owner(a)
            if any(e['principal_id'] == principal and e['group_id'] == group and e['role_id'] == role['id']
                    and e['resource_id'] == values.resource.id for e in graph.assignments.values()):
                _deny(MutationReason.DUPLICATE)
            identity = uuid.uuid4().hex
            def writes():
                self._insert(a.connection, 'access_assignments', id=identity, principal_id=principal, group_id=group,
                    role_id=role['id'], resource_id=values.resource.id, version=1, created_at=a.now, updated_at=a.now)
                self._update(a.connection, table, subject['id'], version=subject['version'] + 1, updated_at=a.now)
            return self._plan(a, identity, 1, True, writes, related=(VersionedEntity(subject['id'], subject['version'] + 1),))
        return self._run(connection, actor, 'create_assignment', 'assignment', 'grants:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def create_assignment(self, actor: PrincipalContext, values: AssignmentValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.create_assignment_on, actor, values, expected_policy_version=expected_policy_version, now=now)

    def remove_assignment_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            row = self._load(a, 'access_assignments', target, audit_target=True)
            graph = _Graph(a.connection, self.tables)
            self._edges(a, graph, [row])
            before = graph.owners(self.codec)
            del graph.assignments[row['id']]
            self._last_owner(graph, before)
            table = 'access_principals' if row['principal_id'] is not None else 'access_groups'
            identity = row['principal_id'] or row['group_id']
            subject = graph.principals[identity] if row['principal_id'] is not None else graph.groups[identity]
            def writes():
                self._delete(a.connection, 'access_assignments', row['id'])
                self._update(a.connection, table, identity, version=subject['version'] + 1, updated_at=a.now)
            return self._plan(a, row['id'], row['version'], True, writes, related=(VersionedEntity(identity, subject['version'] + 1),))
        return self._run(connection, actor, 'remove_assignment', 'assignment', 'grants:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def remove_assignment(self, actor: PrincipalContext, target: VersionedEntity, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.remove_assignment_on, actor, target, expected_policy_version=expected_policy_version, now=now)

    def _key_metadata(self, values, expected_type):
        if type(values) is not expected_type: _deny()
        _text(values.name, 100, nullable=True)
        _text(values.note, 2000, nullable=True)
        if values.expires_at is not None and not _clock(values.expires_at): _deny()

    def _key_principal(self, a, target):
        p = self._load(a, 'access_principals', target)
        if p['kind'] not in {'user', 'integration'} or p['enabled'] != 1: _deny(MutationReason.INVALID_TARGET)
        self._dominance(a, p['id'])
        return p

    def _valid_key(self, a, row, key):
        if (row['state'] != 'active' or row['revoked_at'] is not None or key['revoked_at'] is not None
                or (key['expires_at'] is not None and key['expires_at'] <= a.now)
                or re.fullmatch('[0-9a-f]{12}', key['key_id']) is None
                or not self.codec.supports_hash(key['key_hash'])):
            _deny(MutationReason.INVALID_TARGET)
        p = a.connection.execute(sa.select(self.tables['access_principals']).where(
            self.tables['access_principals'].c.id == row['principal_id'])).mappings().one_or_none()
        if p is None or p['enabled'] != 1 or p['kind'] not in {'user', 'integration'}:
            _deny(MutationReason.INVALID_TARGET)

    def _load_key(self, a, target):
        row = self._load(a, 'access_key_bindings', target, audit_target=True)
        self._dominance(a, row['principal_id'])
        keys = self.tables['api_keys']
        key = a.connection.execute(sa.select(keys).where(keys.c.id == row['id'])).mappings().one_or_none()
        if key is None: _deny(MutationReason.INVALID_TARGET)
        return row, dict(key)

    def _key_receipt_fields(self, a, row, key):
        grants = self.tables['access_key_grants']
        ceiling = tuple(ScopedPermission(p, ResourceRef(r)) for p, r in a.connection.execute(
            sa.select(grants.c.permission_id, grants.c.resource_id).where(grants.c.key_binding_id == row['id'])
            .order_by(grants.c.permission_id, grants.c.resource_id)))
        return dict(public_key_id=key['key_id'], principal_id=row['principal_id'], ceiling=ceiling)

    def _write_ceiling(self, a, identity, ceiling):
        grants = self.tables['access_key_grants']
        a.connection.execute(grants.delete().where(grants.c.key_binding_id == identity))
        for grant in ceiling:
            self._insert(a.connection, 'access_key_grants', id=uuid.uuid4().hex, key_binding_id=identity,
                permission_id=grant.permission, resource_id=grant.resource.id)

    def issue_key_on(self, connection: Connection, actor: PrincipalContext, values: KeyValues, key: PreparedNewKey, *, expected_policy_version: int, now: datetime) -> MutationOutcome[KeyMutationReceipt]:
        def planner(a):
            self._key_metadata(values, KeyValues)
            if (type(key) is not PreparedNewKey or not _id(key.row_id)
                    or re.fullmatch('[0-9a-f]{12}', key.public_key_id) is None
                    or not self.codec.supports_hash(key._key_hash_for_storage())): _deny()
            p = self._key_principal(a, values.principal)
            ceiling = self._ceiling(a, values.ceiling)
            self._unique(a, 'api_keys', 'id', key.row_id)
            self._unique(a, 'api_keys', 'key_id', key.public_key_id)
            def writes():
                self._insert(a.connection, 'api_keys', id=key.row_id, key_id=key.public_key_id,
                    key_hash=key._key_hash_for_storage(), name=values.name, owner=None, scopes=None,
                    created_at=a.now, last_used_at=None, expires_at=values.expires_at, revoked_at=None, note=values.note)
                self._insert(a.connection, 'access_key_bindings', id=key.row_id, principal_id=p['id'],
                    state='active', version=1, security_version=1, revoked_at=None, created_at=a.now, updated_at=a.now)
                self._write_ceiling(a, key.row_id, ceiling)
            return self._plan(a, key.row_id, 1, True, writes,
                key=dict(public_key_id=key.public_key_id, principal_id=p['id'], ceiling=ceiling))
        return self._run(connection, actor, 'issue_key', 'binding', 'keys:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def issue_key(self, actor: PrincipalContext, values: KeyValues, key: PreparedNewKey, *, expected_policy_version: int, now: datetime) -> KeyMutationReceipt:
        return self._standalone(self.issue_key_on, actor, values, key, expected_policy_version=expected_policy_version, now=now)

    def issue_integration_key_on(self, connection: Connection, actor: PrincipalContext, values: IntegrationKeyValues, key: PreparedNewKey, *, expected_policy_version: int, now: datetime) -> MutationOutcome[KeyMutationReceipt]:
        """Enroll a new key exactly as revision0005 enrolled an existing one.

        A distinct integration principal holds an immutable per-key role at
        installation with exactly the requested ordinary scopes, and the key's
        ceiling matches it. The actor must be able to grant every scope.
        """
        def planner(a):
            self._key_metadata(values, IntegrationKeyValues)
            _text(values.display_name, 200)
            if not values.display_name.strip(): _deny()
            _text(values.owner, 100, nullable=True)
            if (type(key) is not PreparedNewKey or not _id(key.row_id)
                    or re.fullmatch('[0-9a-f]{12}', key.public_key_id) is None
                    or not self.codec.supports_hash(key._key_hash_for_storage())): _deny()
            permissions = _permissions(values.permissions)
            if not permissions <= KEY_SCOPES: _deny()
            ceiling = self._ceiling(a, tuple(ScopedPermission(p, ResourceRef('installation')) for p in sorted(permissions)))
            self._unique(a, 'api_keys', 'id', key.row_id)
            self._unique(a, 'api_keys', 'key_id', key.public_key_id)
            role_name = 'API key ' + key.public_key_id
            if permissions:
                self._unique(a, 'access_roles', 'normalized_name', role_name.lower())
            principal_id, resource_id, role_id = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
            def writes():
                timestamps = dict(created_at=a.now, updated_at=a.now)
                self._insert(a.connection, 'access_principals', id=principal_id, kind='integration',
                    display_name=values.display_name, enabled=1, security_version=1, version=1, **timestamps)
                self._insert(a.connection, 'access_resources', id=resource_id, kind='personal',
                    parent_id='installation', parent_kind='installation', principal_id=principal_id,
                    mailbox_id=None, fax_job_id=None, inbound_fax_id=None, enabled=1, version=1, **timestamps)
                if permissions:
                    self._insert(a.connection, 'access_roles', id=role_id, name=role_name,
                        normalized_name=role_name.lower(), description='Fixed permissions for one API key',
                        kind='legacy', enabled=1, version=1, **timestamps)
                    self._role_members(a, role_id, permissions)
                    self._insert(a.connection, 'access_assignments', id=uuid.uuid4().hex, principal_id=principal_id,
                        group_id=None, role_id=role_id, resource_id='installation', version=1, **timestamps)
                # Scopes mirror the ceiling for header routes that still read this column.
                self._insert(a.connection, 'api_keys', id=key.row_id, key_id=key.public_key_id,
                    key_hash=key._key_hash_for_storage(), name=values.name, owner=values.owner,
                    scopes=','.join(sorted(permissions)), created_at=a.now, last_used_at=None,
                    expires_at=values.expires_at, revoked_at=None, note=values.note)
                self._insert(a.connection, 'access_key_bindings', id=key.row_id, principal_id=principal_id,
                    state='active', version=1, security_version=1, revoked_at=None, **timestamps)
                self._write_ceiling(a, key.row_id, ceiling)
            return self._plan(a, key.row_id, 1, True, writes,
                key=dict(public_key_id=key.public_key_id, principal_id=principal_id, ceiling=ceiling))
        return self._run(connection, actor, 'issue_integration_key', 'binding', 'keys:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def issue_integration_key(self, actor: PrincipalContext, values: IntegrationKeyValues, key: PreparedNewKey, *, expected_policy_version: int, now: datetime) -> KeyMutationReceipt:
        return self._standalone(self.issue_integration_key_on, actor, values, key, expected_policy_version=expected_policy_version, now=now)

    def update_key_metadata_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, values: KeyMetadata, *, expected_policy_version: int, now: datetime) -> MutationOutcome[KeyMutationReceipt]:
        def planner(a):
            self._key_metadata(values, KeyMetadata)
            row, key = self._load_key(a, target)
            if (row['state'] != 'active' or row['revoked_at'] is not None or key['revoked_at'] is not None
                    or (key['expires_at'] is not None and key['expires_at'] <= a.now)):
                _deny(MutationReason.INVALID_TARGET)
            expiry_changed = key['expires_at'] != values.expires_at
            changed = expiry_changed or (key['name'], key['note']) != (values.name, values.note)
            fields = self._key_receipt_fields(a, row, key)
            def writes():
                if not changed: return
                self._update(a.connection, 'api_keys', row['id'], name=values.name, note=values.note, expires_at=values.expires_at)
                self._update(a.connection, 'access_key_bindings', row['id'], version=row['version'] + 1,
                    security_version=row['security_version'] + int(expiry_changed), updated_at=a.now)
                if expiry_changed: self._revoke_sessions(a, binding=row['id'])
            return self._plan(a, row['id'], row['version'] + int(changed), changed, writes, key=fields)
        return self._run(connection, actor, 'update_key_metadata', 'binding', 'keys:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def update_key_metadata(self, actor: PrincipalContext, target: VersionedEntity, values: KeyMetadata, *, expected_policy_version: int, now: datetime) -> KeyMutationReceipt:
        return self._standalone(self.update_key_metadata_on, actor, target, values, expected_policy_version=expected_policy_version, now=now)

    def rotate_key_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, key: PreparedKeyRotation, *, expected_policy_version: int, now: datetime) -> MutationOutcome[KeyMutationReceipt]:
        def planner(a):
            if type(key) is not PreparedKeyRotation or not self.codec.supports_hash(key._key_hash_for_storage()): _deny()
            row, stored = self._load_key(a, target)
            self._valid_key(a, row, stored)
            if key.public_key_id != stored['key_id']: _deny(MutationReason.INVALID_TARGET)
            fields = self._key_receipt_fields(a, row, stored)
            def writes():
                self._update(a.connection, 'api_keys', row['id'], key_hash=key._key_hash_for_storage())
                self._update(a.connection, 'access_key_bindings', row['id'], version=row['version'] + 1,
                    security_version=row['security_version'] + 1, updated_at=a.now)
                self._revoke_sessions(a, binding=row['id'])
            return self._plan(a, row['id'], row['version'] + 1, True, writes, key=fields)
        return self._run(connection, actor, 'rotate_key', 'binding', 'keys:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def rotate_key(self, actor: PrincipalContext, target: VersionedEntity, key: PreparedKeyRotation, *, expected_policy_version: int, now: datetime) -> KeyMutationReceipt:
        return self._standalone(self.rotate_key_on, actor, target, key, expected_policy_version=expected_policy_version, now=now)

    def revoke_key_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, *, expected_policy_version: int, now: datetime) -> MutationOutcome[KeyMutationReceipt]:
        def planner(a):
            row, key = self._load_key(a, target)
            changed = row['state'] != 'revoked' or row['revoked_at'] is None or key['revoked_at'] is None
            fields = self._key_receipt_fields(a, row, key)
            def writes():
                if not changed: return
                self._update(a.connection, 'api_keys', row['id'], revoked_at=key['revoked_at'] or a.now)
                self._update(a.connection, 'access_key_bindings', row['id'], state='revoked', revoked_at=row['revoked_at'] or a.now,
                    version=row['version'] + 1, security_version=row['security_version'] + 1, updated_at=a.now)
                self._revoke_sessions(a, binding=row['id'])
            return self._plan(a, row['id'], row['version'] + int(changed), changed, writes, key=fields)
        return self._run(connection, actor, 'revoke_key', 'binding', 'keys:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def revoke_key(self, actor: PrincipalContext, target: VersionedEntity, *, expected_policy_version: int, now: datetime) -> KeyMutationReceipt:
        return self._standalone(self.revoke_key_on, actor, target, expected_policy_version=expected_policy_version, now=now)

    def approve_pending_key_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, principal: VersionedEntity, ceiling: tuple[ScopedPermission, ...], *, expected_policy_version: int, now: datetime) -> MutationOutcome[KeyMutationReceipt]:
        def planner(a):
            row, key = self._load_key(a, target)
            if (row['state'] != 'pending_review' or row['revoked_at'] is not None or key['revoked_at'] is not None
                    or (key['expires_at'] is not None and key['expires_at'] <= a.now)
                    or re.fullmatch('[0-9a-f]{12}', key['key_id']) is None
                    or not self.codec.supports_hash(key['key_hash'])):
                _deny(MutationReason.INVALID_TARGET)
            p = self._key_principal(a, principal)
            proposed = self._ceiling(a, ceiling)
            sessions = self.tables['access_sessions']
            if a.connection.execute(sa.select(sessions.c.id).where(sessions.c.source_key_id == row['id'],
                    sessions.c.principal_id != p['id'])).first() is not None:
                _deny(MutationReason.INVALID_TARGET)
            def writes():
                self._revoke_sessions(a, binding=row['id'])
                self._update(a.connection, 'access_key_bindings', row['id'], principal_id=p['id'], state='active',
                    version=row['version'] + 1, security_version=row['security_version'] + 1, updated_at=a.now)
                self._write_ceiling(a, row['id'], proposed)
            return self._plan(a, row['id'], row['version'] + 1, True, writes,
                key=dict(public_key_id=key['key_id'], principal_id=p['id'], ceiling=proposed))
        return self._run(connection, actor, 'approve_pending_key', 'binding', 'keys:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def approve_pending_key(self, actor: PrincipalContext, target: VersionedEntity, principal: VersionedEntity, ceiling: tuple[ScopedPermission, ...], *, expected_policy_version: int, now: datetime) -> KeyMutationReceipt:
        return self._standalone(self.approve_pending_key_on, actor, target, principal, ceiling, expected_policy_version=expected_policy_version, now=now)

    # Mailboxes: the stable id is mailboxes.id; its enabled state and editor
    # version live on the mailbox's access_resources row, which never changes id.
    # A disabled mailbox receives no new faxes (routing falls back to the
    # unassigned container) and its faxes are hidden until it is enabled again.

    def _mailbox_values(self, a, values, exclude=None):
        if type(values) is not MailboxValues or type(values.enabled) is not bool: _deny()
        label, normalized = _name(values.label)
        mailboxes = self.tables['mailboxes']
        for identity, existing in a.connection.execute(sa.select(mailboxes.c.id, mailboxes.c.label)):
            if identity != exclude and type(existing) is str and unicodedata.normalize('NFKC', existing).strip().casefold() == normalized:
                _deny(MutationReason.DUPLICATE)
        return label

    @staticmethod
    def _optional_version(target):
        # Mailboxes without a resource row and rules without a route binding
        # (left unmatched by the access migration) report version 0.
        if (type(target) is not VersionedEntity or not _id(target.id)
                or type(target.version) is not int or target.version < 0):
            _deny()

    def create_mailbox_on(self, connection: Connection, actor: PrincipalContext, values: MailboxValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            label = self._mailbox_values(a, values)
            identity, resource_id = uuid.uuid4().hex, uuid.uuid4().hex
            def writes():
                self._insert(a.connection, 'mailboxes', id=identity, label=label, allowed_scopes=None, note=None,
                    created_at=a.now, updated_at=a.now)
                self._insert(a.connection, 'access_resources', id=resource_id, kind='mailbox', parent_id='installation',
                    parent_kind='installation', principal_id=None, mailbox_id=identity, fax_job_id=None,
                    inbound_fax_id=None, enabled=int(values.enabled), version=1, created_at=a.now, updated_at=a.now)
            return self._plan(a, identity, 1, True, writes, related=(VersionedEntity(resource_id, 1),))
        return self._run(connection, actor, 'create_mailbox', 'mailbox', 'mailboxes:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def create_mailbox(self, actor: PrincipalContext, values: MailboxValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.create_mailbox_on, actor, values, expected_policy_version=expected_policy_version, now=now)

    def update_mailbox_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, values: MailboxValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        """Rename and/or enable or disable; the mailbox id and resource id never change."""
        def planner(a):
            self._optional_version(target)
            mailboxes, resources = self.tables['mailboxes'], self.tables['access_resources']
            row = a.connection.execute(sa.select(mailboxes).where(mailboxes.c.id == target.id)).mappings().one_or_none()
            if row is None: _deny(MutationReason.INVALID_TARGET)
            a.target_id = row['id']
            resource = a.connection.execute(sa.select(resources).where(resources.c.kind == 'mailbox',
                resources.c.mailbox_id == row['id'])).mappings().one_or_none()
            version = resource['version'] if resource is not None else 0
            if version != target.version: _deny(MutationReason.STALE_VERSION)
            label = self._mailbox_values(a, values, exclude=row['id'])
            renamed = row['label'] != label
            changed = renamed or resource is None or resource['enabled'] != int(values.enabled)
            resource_id = resource['id'] if resource is not None else uuid.uuid4().hex
            def writes():
                if not changed: return
                if renamed:
                    self._update(a.connection, 'mailboxes', row['id'], label=label, updated_at=a.now)
                    rules, routes = self.tables['inbound_rules'], self.tables['access_mailbox_routes']
                    a.connection.execute(rules.update().where(rules.c.id.in_(
                        sa.select(routes.c.id).where(routes.c.mailbox_id == row['id']))).values(mailbox_label=label))
                if resource is None:
                    self._insert(a.connection, 'access_resources', id=resource_id, kind='mailbox', parent_id='installation',
                        parent_kind='installation', principal_id=None, mailbox_id=row['id'], fax_job_id=None,
                        inbound_fax_id=None, enabled=int(values.enabled), version=1, created_at=a.now, updated_at=a.now)
                else:
                    self._update(a.connection, 'access_resources', resource_id, enabled=int(values.enabled),
                        version=version + 1, updated_at=a.now)
            new_version = version + int(changed)
            return self._plan(a, row['id'], new_version, changed, writes, related=(VersionedEntity(resource_id, new_version),))
        return self._run(connection, actor, 'update_mailbox', 'mailbox', 'mailboxes:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def update_mailbox(self, actor: PrincipalContext, target: VersionedEntity, values: MailboxValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.update_mailbox_on, actor, target, values, expected_policy_version=expected_policy_version, now=now)

    def _rule_values(self, a, values, exclude=None):
        if type(values) is not InboundRuleValues or type(values.to_number) is not str or not _id(values.mailbox_id): _deny()
        number = values.to_number.strip()
        if re.fullmatch(r'\+?[0-9]{2,20}', number) is None: _deny()
        mailboxes, rules = self.tables['mailboxes'], self.tables['inbound_rules']
        mailbox = a.connection.execute(sa.select(mailboxes.c.id, mailboxes.c.label).where(
            mailboxes.c.id == values.mailbox_id)).first()
        if mailbox is None: _deny(MutationReason.INVALID_TARGET)
        query = sa.select(rules.c.id).where(rules.c.to_number == number)
        if exclude is not None:
            query = query.where(rules.c.id != exclude)
        if a.connection.execute(query).first() is not None: _deny(MutationReason.DUPLICATE)
        return number, mailbox

    def create_inbound_rule_on(self, connection: Connection, actor: PrincipalContext, values: InboundRuleValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        def planner(a):
            number, mailbox = self._rule_values(a, values)
            identity = uuid.uuid4().hex
            def writes():
                self._insert(a.connection, 'inbound_rules', id=identity, to_number=number,
                    mailbox_label=mailbox.label, created_at=a.now)
                self._insert(a.connection, 'access_mailbox_routes', id=identity, mailbox_id=mailbox.id,
                    version=1, created_at=a.now, updated_at=a.now)
            return self._plan(a, identity, 1, True, writes)
        return self._run(connection, actor, 'create_inbound_rule', 'inbound_rule', 'mailboxes:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def create_inbound_rule(self, actor: PrincipalContext, values: InboundRuleValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.create_inbound_rule_on, actor, values, expected_policy_version=expected_policy_version, now=now)

    def update_inbound_rule_on(self, connection: Connection, actor: PrincipalContext, target: VersionedEntity, values: InboundRuleValues, *, expected_policy_version: int, now: datetime) -> MutationOutcome[MutationReceipt]:
        """Change the number or bind the rule to another mailbox id; version 0 binds an unmatched rule."""
        def planner(a):
            self._optional_version(target)
            rules, routes = self.tables['inbound_rules'], self.tables['access_mailbox_routes']
            row = a.connection.execute(sa.select(rules).where(rules.c.id == target.id)).mappings().one_or_none()
            if row is None: _deny(MutationReason.INVALID_TARGET)
            a.target_id = row['id']
            route = a.connection.execute(sa.select(routes).where(routes.c.id == row['id'])).mappings().one_or_none()
            version = route['version'] if route is not None else 0
            if version != target.version: _deny(MutationReason.STALE_VERSION)
            number, mailbox = self._rule_values(a, values, exclude=row['id'])
            changed = (route is None or route['mailbox_id'] != mailbox.id or row['to_number'] != number
                       or row['mailbox_label'] != mailbox.label)
            def writes():
                if not changed: return
                self._update(a.connection, 'inbound_rules', row['id'], to_number=number, mailbox_label=mailbox.label)
                if route is None:
                    self._insert(a.connection, 'access_mailbox_routes', id=row['id'], mailbox_id=mailbox.id,
                        version=1, created_at=a.now, updated_at=a.now)
                else:
                    self._update(a.connection, 'access_mailbox_routes', row['id'], mailbox_id=mailbox.id,
                        version=version + 1, updated_at=a.now)
            return self._plan(a, row['id'], version + int(changed), changed, writes)
        return self._run(connection, actor, 'update_inbound_rule', 'inbound_rule', 'mailboxes:manage', planner, expected_policy_version=expected_policy_version, now=now)

    def update_inbound_rule(self, actor: PrincipalContext, target: VersionedEntity, values: InboundRuleValues, *, expected_policy_version: int, now: datetime) -> MutationReceipt:
        return self._standalone(self.update_inbound_rule_on, actor, target, values, expected_policy_version=expected_policy_version, now=now)
