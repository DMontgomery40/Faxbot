"""Current authorization; every public read uses the caller's active access lock."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
import hmac

import sqlalchemy as sa

from .catalog import INBOUND_PERMISSIONS, OUTBOUND_PERMISSIONS, PERMISSIONS
from .types import (AccessDecision, AccessUnavailableError, AuthenticationError, BootstrapEvidence,
                    DecisionReason, KeyEvidence, KeySessionEvidence, PasswordSessionEvidence,
                    PrincipalContext, ResourceRef, ScopedPermission, StaleCredentialError,
                    InvalidScopeError, ScopeProjection)


def _safe_storage(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except sa.exc.SQLAlchemyError:
            raise AccessUnavailableError() from None
    return wrapped


def _text(value, maximum=40):
    return type(value) is str and 0 < len(value) <= maximum and all(32 <= ord(c) < 127 for c in value)


def _epoch(value):
    return type(value) is int and value >= 1


@dataclass(frozen=True)
class _Source:
    predicate: object
    binding_id: str | None
    bootstrap: bool
    reset_required: bool
    # True only for another person judged by what they hold (work owner, backup, fallback). A temporary
    # password they have not replaced yet stops them acting now; it does not take away what they hold.
    grants_only: bool = False


class AccessControl:
    def __init__(self, store, *, current_bootstrap_fingerprint_on=None):
        """The optional reader uses this locked Connection and active config.

        It must acquire no configuration lock, open no connection and perform
        no filesystem/network work. Absence disables bootstrap authorization.
        Canonical config activation and its principal epoch update belong to
        the primary's configuration-to-access transaction integration.
        """
        self.store = store
        self.tables = store.tables
        self._bootstrap_fingerprint_on = current_bootstrap_fingerprint_on

    def _validate_context(self, context, now):
        if type(now) is not datetime or now.tzinfo is not None:
            raise AuthenticationError()
        if (type(context) is not PrincipalContext or not _text(context.principal_id)
                or not _epoch(context.principal_security_version) or not _text(context.replay_scope, 80)):
            raise AuthenticationError()
        evidence = context.credential
        if type(evidence) is KeyEvidence:
            valid = _text(evidence.binding_id) and _epoch(evidence.key_security_version)
        elif type(evidence) is PasswordSessionEvidence:
            valid = _text(evidence.session_id) and _epoch(evidence.password_version)
        elif type(evidence) is KeySessionEvidence:
            valid = _text(evidence.session_id) and _text(evidence.binding_id) and _epoch(evidence.key_security_version)
        elif type(evidence) is BootstrapEvidence:
            valid = _text(evidence.fingerprint, 64) and (evidence.session_id is None or _text(evidence.session_id))
        else:
            valid = False
        if not valid:
            raise AuthenticationError()

    def _current_source_on(self, connection, context, now):
        self.store.require_lock_on(connection)
        self._validate_context(context, now)
        p, u = self.tables['access_principals'], self.tables['access_users']
        row = connection.execute(sa.select(p.c.kind, u.c.password_change_required)
            .select_from(p.outerjoin(u, u.c.id == p.c.id)).where(p.c.id == context.principal_id)).first()
        if row is None:
            raise StaleCredentialError()
        evidence = context.credential
        base = [p.c.id == context.principal_id, p.c.enabled == 1,
                p.c.security_version == context.principal_security_version]
        user_valid = sa.exists(sa.select(1).where(u.c.id == p.c.id, u.c.password_version >= 1,
                                                u.c.password_change_required.in_([0, 1])))
        base.append(sa.or_(sa.and_(p.c.kind == 'user', user_valid),
                           sa.and_(p.c.kind == 'integration', p.c.id != 'bootstrap'),
                           sa.and_(p.c.kind == 'bootstrap', p.c.id == 'bootstrap')))
        binding_id = None
        bootstrap = type(evidence) is BootstrapEvidence
        session_id = getattr(evidence, 'session_id', None)
        if bootstrap:
            current = self._bootstrap_fingerprint_on(connection) if self._bootstrap_fingerprint_on is not None else None
            if (context.principal_id != 'bootstrap' or row.kind != 'bootstrap'
                    or not _text(current, 64) or not hmac.compare_digest(current, evidence.fingerprint)
                    or context.replay_scope != 'key:env'):
                raise StaleCredentialError()
            base.append(p.c.kind == 'bootstrap')
        elif type(evidence) in {KeyEvidence, KeySessionEvidence}:
            binding_id = evidence.binding_id
            b, k = self.tables['access_key_bindings'], self.tables['api_keys']
            public_id = connection.execute(sa.select(k.c.key_id).where(k.c.id == binding_id)).scalar_one_or_none()
            if (not _text(public_id, 32) or public_id == 'env' or context.replay_scope != 'key:' + public_id
                    or row.kind not in {'user', 'integration'}):
                raise StaleCredentialError()
            key_valid = [b.c.id == binding_id, b.c.principal_id == context.principal_id,
                         b.c.state == 'active', b.c.revoked_at.is_(None),
                         b.c.security_version == evidence.key_security_version,
                         k.c.key_id == public_id, k.c.revoked_at.is_(None),
                         sa.or_(k.c.expires_at.is_(None), k.c.expires_at > now)]
            base.append(sa.exists(sa.select(1).select_from(b.join(k, k.c.id == b.c.id)).where(*key_valid)))
            base.append(p.c.kind.in_(['user', 'integration']))
        else:
            if row.kind != 'user' or context.replay_scope != 'principal:' + context.principal_id:
                raise StaleCredentialError()
            base.append(p.c.kind == 'user')
            base.append(sa.exists(sa.select(1).where(u.c.id == p.c.id,
                                                   u.c.password_version == evidence.password_version)))
        if session_id is not None:
            s = self.tables['access_sessions']
            created = connection.execute(sa.select(s.c.created_at).where(s.c.id == session_id)).scalar_one_or_none()
            if type(created) is not datetime or created.tzinfo is not None:
                raise StaleCredentialError()
            session_valid = [s.c.id == session_id, s.c.principal_id == context.principal_id,
                s.c.principal_security_version == context.principal_security_version,
                s.c.revoked_at.is_(None), s.c.created_at == created, s.c.created_at <= now,
                s.c.created_at >= now - timedelta(hours=12), s.c.created_at <= s.c.last_used_at,
                s.c.last_used_at <= now, s.c.last_used_at <= s.c.expires_at,
                s.c.last_used_at > now - timedelta(minutes=30), s.c.expires_at > now,
                s.c.expires_at <= created + timedelta(hours=12)]
            if bootstrap:
                session_valid += [s.c.source_kind == 'bootstrap', s.c.bootstrap_fingerprint == evidence.fingerprint,
                                  s.c.source_key_id.is_(None), s.c.source_key_version.is_(None), s.c.password_version.is_(None)]
            elif type(evidence) is KeySessionEvidence:
                session_valid += [s.c.source_kind == 'key', s.c.source_key_id == binding_id,
                                  s.c.source_key_version == evidence.key_security_version,
                                  s.c.bootstrap_fingerprint.is_(None), s.c.password_version.is_(None)]
                k = self.tables['api_keys']
                session_valid.append(sa.exists(sa.select(1).where(k.c.id == binding_id,
                    sa.or_(k.c.expires_at.is_(None), s.c.expires_at <= k.c.expires_at))))
            else:
                session_valid += [s.c.source_kind == 'password', s.c.password_version == evidence.password_version,
                                  s.c.bootstrap_fingerprint.is_(None), s.c.source_key_id.is_(None), s.c.source_key_version.is_(None)]
            base.append(sa.exists(sa.select(1).where(*session_valid)))
        predicate = sa.exists(sa.select(1).where(*base))
        if not connection.execute(sa.select(predicate)).scalar_one():
            raise StaleCredentialError()
        return _Source(predicate, binding_id, bootstrap, row.kind == 'user' and row.password_change_required == 1)

    def _resource_query(self):
        r = self.tables['access_resources'].alias('resource')
        parent = self.tables['access_resources'].alias('parent')
        root = self.tables['access_resources'].alias('root')
        tree = r.outerjoin(parent, sa.and_(r.c.parent_id == parent.c.id, r.c.parent_kind == parent.c.kind))
        tree = tree.outerjoin(root, sa.and_(parent.c.parent_id == root.c.id, parent.c.parent_kind == root.c.kind))
        valid = sa.or_(self._root_shape(r),
            sa.and_(self._container_shape(r), self._root_shape(parent)),
            sa.and_(self._leaf_shape(r), self._container_shape(parent), self._root_shape(root),
                    sa.or_(sa.and_(r.c.kind == 'outbound', parent.c.kind.in_(['personal', 'legacy'])),
                           sa.and_(r.c.kind == 'inbound', parent.c.kind.in_(['mailbox', 'legacy'])))))
        return r, parent, tree, valid

    @staticmethod
    def _empty_links(resource, except_link=None):
        return sa.and_(*(resource.c[link].is_(None) for link in
                         ('principal_id', 'mailbox_id', 'fax_job_id', 'inbound_fax_id') if link != except_link))

    def _root_shape(self, resource):
        return sa.and_(resource.c.enabled == 1, resource.c.kind == 'installation', resource.c.id == 'installation',
                       resource.c.parent_id.is_(None), resource.c.parent_kind.is_(None), self._empty_links(resource))

    def _container_shape(self, resource):
        personal = self.tables['access_principals']
        mailbox = self.tables['mailboxes']
        # A disabled historical creator still exists. Its enabled personal
        # container and accepted fax tree remain visible to other actors.
        shape = sa.or_(
            sa.and_(resource.c.kind == 'personal', self._empty_links(resource, 'principal_id'),
                    sa.exists(sa.select(1).where(personal.c.id == resource.c.principal_id))),
            sa.and_(resource.c.kind == 'mailbox', self._empty_links(resource, 'mailbox_id'),
                    sa.exists(sa.select(1).where(mailbox.c.id == resource.c.mailbox_id))),
            sa.and_(resource.c.kind == 'legacy', resource.c.id == 'legacy', self._empty_links(resource)))
        return sa.and_(resource.c.enabled == 1, resource.c.parent_id == 'installation',
                       resource.c.parent_kind == 'installation', shape)

    def _leaf_shape(self, resource):
        outbound, inbound = self.tables['fax_jobs'], self.tables['inbound_faxes']
        return sa.and_(resource.c.enabled == 1, sa.or_(
            sa.and_(resource.c.kind == 'outbound', self._empty_links(resource, 'fax_job_id'),
                    sa.exists(sa.select(1).where(outbound.c.id == resource.c.fax_job_id))),
            sa.and_(resource.c.kind == 'inbound', self._empty_links(resource, 'inbound_fax_id'),
                    sa.exists(sa.select(1).where(inbound.c.id == resource.c.inbound_fax_id)))))

    @staticmethod
    def _scope_match(scope, resource, parent):
        return sa.or_(scope == resource.c.id, scope == resource.c.parent_id, scope == parent.c.parent_id)

    def _subject_match(self, assignment, principal_id):
        membership, group = self.tables['access_memberships'], self.tables['access_groups']
        return sa.or_(sa.and_(assignment.c.principal_id == principal_id, assignment.c.group_id.is_(None)),
            sa.and_(assignment.c.principal_id.is_(None), sa.exists(sa.select(1)
                .select_from(membership.join(group, group.c.id == membership.c.group_id))
                .where(membership.c.principal_id == principal_id, membership.c.group_id == assignment.c.group_id,
                       group.c.enabled == 1))))

    def _authority_predicate(self, context, source, permission, resource, parent):
        if source.reset_required:
            return sa.false()
        if source.bootstrap:
            return source.predicate
        p, u = self.tables['access_principals'], self.tables['access_users']
        unrestricted = sa.exists(sa.select(1).select_from(p.outerjoin(u, u.c.id == p.c.id)).where(
            p.c.id == context.principal_id,
            sa.or_(p.c.kind != 'user', sa.and_(u.c.id == p.c.id, u.c.password_change_required == 0))))
        assignment, role, member = (self.tables[name] for name in
                                    ('access_assignments', 'access_roles', 'access_role_permissions'))
        authority = sa.exists(sa.select(1).select_from(assignment.join(role, role.c.id == assignment.c.role_id)
            .join(member, member.c.role_id == role.c.id)).where(role.c.enabled == 1,
                self._subject_match(assignment, context.principal_id), member.c.permission_id == permission,
                self._scope_match(assignment.c.resource_id, resource, parent)))
        if source.binding_id is not None:
            ceiling = self.tables['access_key_grants']
            authority = sa.and_(authority, sa.exists(sa.select(1).where(
                ceiling.c.key_binding_id == source.binding_id, ceiling.c.permission_id == permission,
                self._scope_match(ceiling.c.resource_id, resource, parent))))
        if source.grants_only:
            return sa.and_(source.predicate, authority)
        return sa.and_(source.predicate, unrestricted, authority)

    @staticmethod
    def _applicability(permission, resource, context):
        if permission == 'fax:send':
            return sa.and_(resource.c.kind == 'personal', resource.c.principal_id == context.principal_id)
        if permission in OUTBOUND_PERMISSIONS:
            return resource.c.kind == 'outbound'
        if permission in INBOUND_PERMISSIONS:
            return resource.c.kind == 'inbound'
        return resource.c.kind == 'installation'

    @staticmethod
    def _valid_ref(resource):
        return type(resource) is ResourceRef and _text(resource.id)

    def _allowed_query(self, context, source, permission, *, resource_id=None, kind=None):
        resource, parent, tree, valid = self._resource_query()
        query = sa.select(resource.c.id).select_from(tree).where(valid,
            self._applicability(permission, resource, context),
            self._authority_predicate(context, source, permission, resource, parent))
        if resource_id is not None:
            query = query.where(resource.c.id == resource_id)
        if kind is not None:
            query = query.where(resource.c.kind == kind)
        return query

    def authorize(self, context, permission, resource, *, now):
        with self.store.transaction() as connection:
            return self.authorize_on(connection, context, permission, resource, now=now)

    @_safe_storage
    def authorize_on(self, connection, context, permission, resource, *, now):
        source = self._current_source_on(connection, context, now)
        version = self.store.require_lock_on(connection)
        if type(permission) is not str or permission not in PERMISSIONS:
            return AccessDecision(False, DecisionReason.UNKNOWN_PERMISSION, version)
        if source.reset_required:
            return AccessDecision(False, DecisionReason.RESET_REQUIRED, version)
        if not self._valid_ref(resource):
            return AccessDecision(False, DecisionReason.INVALID_RESOURCE, version)
        allowed = connection.execute(self._allowed_query(context, source, permission, resource_id=resource.id)).first() is not None
        if allowed:
            return AccessDecision(True, DecisionReason.ALLOWED, version)
        r, _, tree, valid = self._resource_query()
        exists = connection.execute(sa.select(r.c.id).select_from(tree).where(valid, r.c.id == resource.id)).first() is not None
        return AccessDecision(False, DecisionReason.FORBIDDEN if exists else DecisionReason.INVALID_RESOURCE, version)

    @_safe_storage
    def authorize_child_on(self, connection, context, permission, container, *, now):
        """Whether ``permission`` holds for a fax filed under ``container`` that has no access record of its own.

        A form sent to a partner as values has no sent fax, and a document a
        partner delivered may not be filed as a received fax yet. Both are
        checked as a fax in that place would be: the grants on the container (a
        personal container, a mailbox or the unassigned faxes) and on the
        installation. A grant on one particular fax never applies.
        """
        source = self._current_source_on(connection, context, now)
        self.store.require_lock_on(connection)
        if permission not in OUTBOUND_PERMISSIONS | INBOUND_PERMISSIONS or permission == 'fax:send':
            return False
        if source.reset_required or not self._valid_ref(container):
            return False
        kinds = ('personal', 'legacy') if permission in OUTBOUND_PERMISSIONS else ('mailbox', 'legacy')
        r, parent, tree, valid = self._resource_query()
        query = sa.select(r.c.id).select_from(tree).where(
            valid, self._container_shape(r), r.c.kind.in_(kinds), r.c.id == container.id,
            self._authority_predicate(context, source, permission, r, parent))
        return connection.execute(query).first() is not None

    @_safe_storage
    def effective_access_on(self, connection, context, resource, *, now):
        source = self._current_source_on(connection, context, now)
        if not self._valid_ref(resource) or source.reset_required:
            return frozenset()
        return frozenset(permission for permission in PERMISSIONS if
                         connection.execute(self._allowed_query(context, source, permission, resource_id=resource.id)).first() is not None)

    @_safe_storage
    def visible_resource_ids_on(self, connection, context, permission, kind, *, now):
        """Execute this live Select on the same locked Connection, before pagination.

        The query is request-local; neither it nor its result is authority for a
        later transaction. Credential invalidity raises before an empty query.
        """
        source = self._current_source_on(connection, context, now)
        if type(permission) is not str or permission not in PERMISSIONS or type(kind) is not str:
            return sa.select(self.tables['access_resources'].c.id).where(sa.false())
        if kind not in {'installation', 'personal', 'mailbox', 'legacy', 'outbound', 'inbound'}:
            return sa.select(self.tables['access_resources'].c.id).where(sa.false())
        return self._allowed_query(context, source, permission, kind=kind)

    @staticmethod
    def _scope_kinds(permission):
        if permission == 'fax:send':
            return ('installation', 'personal')
        if permission in OUTBOUND_PERMISSIONS:
            return ('installation', 'personal', 'legacy', 'outbound')
        if permission in INBOUND_PERMISSIONS:
            return ('installation', 'mailbox', 'legacy', 'inbound')
        return ('installation',)

    def _coverage_query(self, context, source, permission, resource_id):
        resource, parent, tree, valid = self._resource_query()
        return sa.select(resource.c.id).select_from(tree).where(valid, resource.c.id == resource_id,
            resource.c.kind.in_(self._scope_kinds(permission)),
            self._authority_predicate(context, source, permission, resource, parent))

    @_safe_storage
    def scope_projection_on(self, connection, permissions: frozenset[str], resource: ResourceRef):
        """Project known role members onto a valid grant scope, without granting authority.

        Callers must separately authorize the mutation and check can_grant_on
        for its affected before/after edges in this same locked transaction.
        """
        self.store.require_lock_on(connection)
        if (type(permissions) is not frozenset or not self._valid_ref(resource)
                or any(type(item) is not str or item not in PERMISSIONS for item in permissions)):
            raise InvalidScopeError()
        node, _, tree, valid = self._resource_query()
        kind = connection.execute(sa.select(node.c.kind).select_from(tree)
                                  .where(valid, node.c.id == resource.id)).scalar_one_or_none()
        if kind is None:
            raise InvalidScopeError()
        applicable = tuple(ScopedPermission(permission, resource) for permission in sorted(permissions)
                           if kind in self._scope_kinds(permission))
        inactive = permissions - frozenset(item.permission for item in applicable)
        return ScopeProjection(applicable, inactive)

    @_safe_storage
    def can_grant_on(self, connection, actor, requested: tuple[ScopedPermission, ...], *, now):
        """Check scope coverage; the mutation separately requires its category permission."""
        source = self._current_source_on(connection, actor, now)
        version = self.store.require_lock_on(connection)
        if source.reset_required:
            return AccessDecision(False, DecisionReason.RESET_REQUIRED, version)
        if type(requested) is not tuple:
            return AccessDecision(False, DecisionReason.INVALID_SCOPE, version)
        for grant in requested:
            if (type(grant) is not ScopedPermission or type(grant.permission) is not str
                    or grant.permission not in PERMISSIONS or not self._valid_ref(grant.resource)):
                return AccessDecision(False, DecisionReason.INVALID_SCOPE, version)
            if connection.execute(self._coverage_query(actor, source, grant.permission, grant.resource.id)).first() is None:
                return AccessDecision(False, DecisionReason.FORBIDDEN, version)
        return AccessDecision(True, DecisionReason.ALLOWED, version)

    def _owner_assignment_on(self, connection, principal_id):
        assignment, role = self.tables['access_assignments'], self.tables['access_roles']
        return connection.execute(sa.select(assignment.c.id).select_from(assignment.join(role, role.c.id == assignment.c.role_id))
            .where(role.c.id == 'role_owner', role.c.kind == 'builtin', role.c.enabled == 1,
                   assignment.c.resource_id == 'installation', self._subject_match(assignment, principal_id))).first() is not None

    def _complete_owner_on(self, connection, actor, source):
        if source.reset_required:
            return False
        if not source.bootstrap and not self._owner_assignment_on(connection, actor.principal_id):
            return False
        return all(connection.execute(self._coverage_query(actor, source, permission, 'installation')).first() is not None
                   for permission in PERMISSIONS)

    @_safe_storage
    def is_complete_owner_on(self, connection, actor, *, now):
        source = self._current_source_on(connection, actor, now)
        return self._complete_owner_on(connection, actor, source)

    @_safe_storage
    def dominates_principal_on(self, connection, actor, target_principal_id, *, now):
        """Current takeover guard, treating the target as enabled without a key ceiling.

        Disabled groups/roles retain their current state. Mutation-specific
        before/after group, role and last-Owner validation is a separate slice.
        """
        source = self._current_source_on(connection, actor, now)
        version = self.store.require_lock_on(connection)
        if source.reset_required:
            return AccessDecision(False, DecisionReason.RESET_REQUIRED, version)
        principal = self.tables['access_principals']
        if (not _text(target_principal_id) or connection.execute(sa.select(principal.c.id)
                .where(principal.c.id == target_principal_id)).first() is None):
            return AccessDecision(False, DecisionReason.INVALID_RESOURCE, version)
        if target_principal_id == 'bootstrap' and not self._complete_owner_on(connection, actor, source):
            return AccessDecision(False, DecisionReason.OWNER_REQUIRED, version)
        if self._owner_assignment_on(connection, target_principal_id) and not self._complete_owner_on(connection, actor, source):
            return AccessDecision(False, DecisionReason.OWNER_REQUIRED, version)
        assignment, role, member = (self.tables[name] for name in
                                    ('access_assignments', 'access_roles', 'access_role_permissions'))
        target_authority = sa.select(member.c.permission_id, assignment.c.resource_id).distinct()
        target_authority = target_authority.select_from(assignment.join(role, role.c.id == assignment.c.role_id)
            .join(member, member.c.role_id == role.c.id)).where(role.c.enabled == 1,
                self._subject_match(assignment, target_principal_id))
        for permission, resource_id in connection.execute(target_authority):
            if permission not in PERMISSIONS:
                return AccessDecision(False, DecisionReason.INVALID_SCOPE, version)
            resource, _, tree, valid = self._resource_query()
            current_scope = sa.select(resource.c.id).select_from(tree).where(valid, resource.c.id == resource_id,
                resource.c.kind.in_(self._scope_kinds(permission)))
            if connection.execute(current_scope).first() is None:
                continue
            if connection.execute(self._coverage_query(actor, source, permission, resource_id)).first() is None:
                return AccessDecision(False, DecisionReason.FORBIDDEN, version)
        return AccessDecision(True, DecisionReason.ALLOWED, version)
