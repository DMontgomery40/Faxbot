"""Read projections for the access management console, in the contract's field names.

Runtime wiring (owned by runtime.py):
    from .read import AccessReads
    reads = AccessReads(store, control, sessions)     # AccessRuntime slot 'reads'

Every method opens its own access transaction, rereads current authority and
returns plain dicts whose keys match the MANAGEMENT HTTP CONTRACT. Datetimes are
naive UTC datetime objects. Lists are {'items': [...], 'next_cursor': str|None}
with an opaque keyset cursor and limit 1..200. Category reads require their
permission at the installation resource; a principal may always read itself,
its own assignments, keys and sessions. Denials raise MutationDeniedError
(forbidden, reset_required, invalid_target, invalid_input), never HTTP errors.
"""
import base64
from datetime import datetime, timezone
import json

import sqlalchemy as sa

from .catalog import PERMISSIONS
from .mutation_types import MutationDeniedError, MutationReason
from .sessions import SessionCursor
from .types import ResourceRef, ScopedPermission

INSTALLATION = ResourceRef('installation')
INSTALLATION_NAME = 'Whole installation'
LEGACY_NAME = 'Unassigned faxes'
LEGACY_ROLE_NAME = 'Key permissions'
_GROUP_ORDER = ('fax', 'inbound', 'mailbox', 'identity', 'config', 'host', 'audit')
_KIND_RANK = {'installation': 0, 'legacy': 1, 'mailbox': 2, 'personal': 3}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _deny(reason=MutationReason.INVALID_INPUT):
    raise MutationDeniedError(reason)


def _permission_group(permission):
    prefix = permission.split(':', 1)[0]
    if prefix in {'fax', 'inbound', 'host', 'audit'}:
        return prefix
    if prefix == 'mailboxes':
        return 'mailbox'
    if prefix in {'settings', 'providers', 'diagnostics', 'logs', 'tunnels'}:
        return 'config'
    return 'identity'


def _identifier(value):
    return type(value) is str and 0 < len(value) <= 100 and all(32 <= ord(c) < 127 for c in value)


def _limit(value):
    if type(value) is not int or not 1 <= value <= 200:
        _deny()
    return value


def _encode(values):
    raw = json.dumps(values, separators=(',', ':')).encode('ascii')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _decode(cursor, kinds):
    if cursor is None:
        return None
    try:
        if type(cursor) is not str or not 0 < len(cursor) <= 1024:
            raise ValueError()
        values = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
        if type(values) is not list or len(values) != len(kinds):
            raise ValueError()
        result = []
        for kind, value in zip(kinds, values):
            if kind == 'int' and type(value) is int:
                result.append(value)
            elif kind == 'time' and type(value) is str:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is not None:
                    raise ValueError()
                result.append(parsed)
            elif kind == 'text' and type(value) is str and len(value) <= 200:
                result.append(value)
            else:
                raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError):
        _deny()


def _wire(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _keyset(query, keys, cursor, limit, *, descending=False):
    """Order by keys (the last one unique) and continue strictly after the cursor."""
    values = _decode(cursor, [kind for _, kind in keys])
    columns = [expression for expression, _ in keys]
    if values is not None:
        clauses, equal = [], []
        for column, value in zip(columns, values):
            step = column < value if descending else column > value
            clauses.append(sa.and_(*equal, step))
            equal.append(column == value)
        query = query.where(sa.or_(*clauses))
    query = query.add_columns(*(column.label(f'_sort{index}') for index, column in enumerate(columns)))
    order = [column.desc() if descending else column.asc() for column in columns]
    return query.order_by(*order).limit(limit + 1)


def _page(rows, keys, limit, project):
    items = [project(row) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode([_wire(last[f'_sort{index}']) for index in range(len(keys))])
    return {'items': items, 'next_cursor': next_cursor}


class AccessReads:
    def __init__(self, store, control, sessions, *, clock=None):
        self.store, self.control, self._sessions = store, control, sessions
        self.tables = store.tables
        self._clock = clock or _utcnow

    # -- authorization -------------------------------------------------

    def _source(self, connection, actor, now):
        return self.control._current_source_on(connection, actor, now)

    def _decision(self, connection, actor, permission, now):
        # Whoever may manage a category may also read it, so an edit can be reread.
        decision = self.control.authorize_on(connection, actor, permission, INSTALLATION, now=now)
        manage = permission[:-len(':read')] + ':manage' if permission.endswith(':read') else None
        if not decision.allowed and decision.reason.value == 'forbidden' and manage in PERMISSIONS:
            decision = self.control.authorize_on(connection, actor, manage, INSTALLATION, now=now)
        return decision

    def _allowed(self, connection, actor, permission, now):
        return self._decision(connection, actor, permission, now).allowed

    def _require(self, connection, actor, permission, now):
        decision = self._decision(connection, actor, permission, now)
        if not decision.allowed:
            _deny(MutationReason.RESET_REQUIRED if decision.reason.value == 'reset_required' else MutationReason.FORBIDDEN)

    def _require_or_self(self, connection, actor, permission, principal_id, now):
        self._source(connection, actor, now)
        if principal_id != actor.principal_id:
            self._require(connection, actor, permission, now)

    # -- shared projections --------------------------------------------

    def _resource_names(self, connection, resource_ids):
        """{resource_id: (kind, name)} for installation, legacy, mailbox and personal containers."""
        resources, mailboxes, principals = (self.tables[name] for name in ('access_resources', 'mailboxes', 'access_principals'))
        ids = sorted({identity for identity in resource_ids if identity})
        if not ids:
            return {}
        rows = connection.execute(sa.select(resources.c.id, resources.c.kind, mailboxes.c.label, principals.c.display_name)
            .select_from(resources.outerjoin(mailboxes, mailboxes.c.id == resources.c.mailbox_id)
                         .outerjoin(principals, principals.c.id == resources.c.principal_id))
            .where(resources.c.id.in_(ids))).all()
        return {row.id: (row.kind, self._resource_name(row.kind, row.label, row.display_name)) for row in rows}

    @staticmethod
    def _resource_name(kind, label, display_name):
        if kind == 'installation':
            return INSTALLATION_NAME
        if kind == 'legacy':
            return LEGACY_NAME
        if kind == 'mailbox':
            return label or 'Mailbox'
        if kind == 'personal':
            return display_name or 'Personal'
        return kind.capitalize() + ' fax'

    def _assignment_query(self):
        a, roles, principals, groups = (self.tables[name] for name in
            ('access_assignments', 'access_roles', 'access_principals', 'access_groups'))
        return sa.select(a.c.id, a.c.principal_id, a.c.group_id, a.c.resource_id, a.c.version, a.c.created_at,
            roles.c.id.label('role_id'), roles.c.name.label('role_name'), roles.c.kind.label('role_kind'),
            principals.c.display_name.label('principal_name'), groups.c.name.label('group_name')).select_from(
                a.join(roles, roles.c.id == a.c.role_id).outerjoin(principals, principals.c.id == a.c.principal_id)
                .outerjoin(groups, groups.c.id == a.c.group_id))

    def _assignment_items(self, connection, rows):
        names = self._resource_names(connection, [row['resource_id'] for row in rows])
        items = []
        for row in rows:
            kind, name = names.get(row['resource_id'], (None, None))
            subject = ({'kind': 'principal', 'id': row['principal_id'], 'name': row['principal_name']}
                       if row['principal_id'] is not None else
                       {'kind': 'group', 'id': row['group_id'], 'name': row['group_name']})
            items.append({'id': row['id'], 'subject': subject,
                # Per-key legacy roles carry generated identifiers in their stored names.
                'role': {'id': row['role_id'], 'name': LEGACY_ROLE_NAME if row['role_kind'] == 'legacy' else row['role_name'],
                         'builtin': row['role_kind'] == 'builtin'},
                'resource': {'id': row['resource_id'], 'kind': kind, 'name': name}, 'version': row['version']})
        return items

    def _key_query(self):
        keys, bindings, principals = self.tables['api_keys'], self.tables['access_key_bindings'], self.tables['access_principals']
        return sa.select(bindings.c.id.label('binding_id'), keys.c.key_id, keys.c.name, keys.c.note,
            keys.c.expires_at, keys.c.created_at, keys.c.last_used_at, keys.c.revoked_at,
            bindings.c.state, bindings.c.revoked_at.label('binding_revoked_at'), bindings.c.version,
            principals.c.id.label('principal_id'), principals.c.display_name, principals.c.kind.label('principal_kind')
        ).select_from(bindings.join(keys, keys.c.id == bindings.c.id)
                      .join(principals, principals.c.id == bindings.c.principal_id))

    def _key_items(self, connection, rows):
        grants = self.tables['access_key_grants']
        ceilings = {}
        ids = [row['binding_id'] for row in rows]
        if ids:
            for binding_id, permission, resource_id in connection.execute(sa.select(
                    grants.c.key_binding_id, grants.c.permission_id, grants.c.resource_id)
                    .where(grants.c.key_binding_id.in_(ids)).order_by(grants.c.permission_id, grants.c.resource_id)):
                ceilings.setdefault(binding_id, []).append({'permission': permission, 'resource_id': resource_id})
        return [{'id': row['key_id'],
                 'principal': {'id': row['principal_id'], 'display_name': row['display_name'], 'kind': row['principal_kind']},
                 'name': row['name'], 'note': row['note'], 'expires_at': row['expires_at'],
                 'created_at': row['created_at'], 'last_used_at': row['last_used_at'],
                 'revoked_at': row['revoked_at'] or row['binding_revoked_at'],
                 'pending_review': row['state'] == 'pending_review',
                 'ceiling': ceilings.get(row['binding_id'], []), 'version': row['version']} for row in rows]

    def _user_query(self):
        principals, users, sessions = self.tables['access_principals'], self.tables['access_users'], self.tables['access_sessions']
        last_login = (sa.select(sa.func.max(sessions.c.created_at)).where(sessions.c.principal_id == principals.c.id,
            sessions.c.source_kind == 'password').scalar_subquery())
        return sa.select(principals.c.id, principals.c.kind, users.c.login, principals.c.display_name,
            principals.c.enabled, users.c.password_change_required, principals.c.created_at,
            last_login.label('last_login_at'), principals.c.version).select_from(
                principals.outerjoin(users, users.c.id == principals.c.id))

    @staticmethod
    def _user_item(row):
        return {'id': row['id'], 'kind': row['kind'], 'login': row['login'] if row['kind'] == 'user' else None,
                'display_name': row['display_name'], 'enabled': row['enabled'] == 1,
                'password_change_required': (row['password_change_required'] == 1) if row['kind'] == 'user' else None,
                'created_at': row['created_at'], 'last_login_at': row['last_login_at'], 'version': row['version']}

    def _role_items(self, connection, rows):
        members = self.tables['access_role_permissions']
        permissions = {}
        ids = [row['id'] for row in rows]
        if ids:
            for role_id, permission in connection.execute(sa.select(members.c.role_id, members.c.permission_id)
                    .where(members.c.role_id.in_(ids))):
                permissions.setdefault(role_id, []).append(permission)
        return [{'id': row['id'], 'name': row['name'], 'description': row['description'],
                 'builtin': row['kind'] == 'builtin', 'enabled': row['enabled'] == 1,
                 'permissions': sorted(permissions.get(row['id'], [])), 'version': row['version']} for row in rows]

    def _effective(self, connection, principal):
        """Permissions currently granted at installation and at the principal's own container."""
        if principal['kind'] == 'bootstrap':
            return {'installation': sorted(PERMISSIONS), 'personal': []}
        users = self.tables['access_users']
        reset = connection.execute(sa.select(users.c.password_change_required).where(users.c.id == principal['id'])).scalar_one_or_none()
        if principal['enabled'] != 1 or reset == 1:
            return {'installation': [], 'personal': []}
        a, roles, members, memberships, groups, resources = (self.tables[name] for name in (
            'access_assignments', 'access_roles', 'access_role_permissions', 'access_memberships', 'access_groups', 'access_resources'))
        personal = connection.execute(sa.select(resources.c.id).where(resources.c.kind == 'personal',
            resources.c.principal_id == principal['id'])).scalar_one_or_none()
        subject = sa.or_(a.c.principal_id == principal['id'], sa.exists(sa.select(1)
            .select_from(memberships.join(groups, groups.c.id == memberships.c.group_id))
            .where(memberships.c.group_id == a.c.group_id, memberships.c.principal_id == principal['id'], groups.c.enabled == 1)))
        rows = connection.execute(sa.select(a.c.resource_id, members.c.permission_id).distinct()
            .select_from(a.join(roles, roles.c.id == a.c.role_id).join(members, members.c.role_id == roles.c.id))
            .where(roles.c.enabled == 1, subject, a.c.resource_id.in_(['installation', personal or 'installation']))).all()
        installation = sorted({p for resource, p in rows if resource == 'installation' and p in PERMISSIONS})
        own = sorted({p for resource, p in rows if personal is not None and resource == personal and p in PERMISSIONS})
        return {'installation': installation, 'personal': own}

    # -- catalogue and roles -------------------------------------------

    def permissions(self, actor):
        permissions = self.tables['access_permissions']
        with self.store.transaction() as connection:
            self._source(connection, actor, self._clock())
            rows = connection.execute(sa.select(permissions.c.id, permissions.c.description)).all()
        items = [{'permission': row.id, 'group': _permission_group(row.id), 'description': row.description}
                 for row in rows if row.id in PERMISSIONS]
        items.sort(key=lambda item: (_GROUP_ORDER.index(item['group']), item['permission']))
        return {'items': items}

    def roles(self, actor, *, cursor=None, limit=50):
        limit = _limit(limit)
        roles = self.tables['access_roles']
        keys = [(roles.c.normalized_name, 'text'), (roles.c.id, 'text')]
        with self.store.transaction() as connection:
            self._require(connection, actor, 'roles:read', self._clock())
            # Per-key legacy roles are fixed credential detail, not assignable catalogue roles.
            query = sa.select(roles).where(roles.c.kind.in_(['builtin', 'custom']))
            rows = connection.execute(_keyset(query, keys, cursor, limit)).mappings().all()
            items = self._role_items(connection, rows[:limit])
        page = _page(rows, keys, limit, lambda row: row)
        page['items'] = items
        return page

    def role(self, actor, role_id):
        roles = self.tables['access_roles']
        with self.store.transaction() as connection:
            self._require(connection, actor, 'roles:read', self._clock())
            row = connection.execute(sa.select(roles).where(roles.c.id == role_id)).mappings().one_or_none() if _identifier(role_id) else None
            if row is None:
                _deny(MutationReason.INVALID_TARGET)
            return self._role_items(connection, [row])[0]

    # -- users and integrations ----------------------------------------

    def users(self, actor, *, kind='all', q=None, cursor=None, limit=50):
        limit = _limit(limit)
        if kind not in {'user', 'integration', 'all'} or (q is not None and (type(q) is not str or len(q) > 100)):
            _deny()
        principals, users = self.tables['access_principals'], self.tables['access_users']
        keys = [(principals.c.created_at, 'time'), (principals.c.id, 'text')]
        with self.store.transaction() as connection:
            now = self._clock()
            self._source(connection, actor, now)
            query = self._user_query()
            if not self._allowed(connection, actor, 'users:read', now):
                query = query.where(principals.c.id == actor.principal_id)
            if kind != 'all':
                query = query.where(principals.c.kind == kind)
            if q:
                needle = q.strip().lower()
                query = query.where(sa.or_(sa.func.lower(principals.c.display_name).contains(needle, autoescape=True),
                                           sa.func.lower(users.c.login).contains(needle, autoescape=True)))
            rows = connection.execute(_keyset(query, keys, cursor, limit)).mappings().all()
        return _page(rows, keys, limit, self._user_item)

    def user(self, actor, principal_id):
        principals = self.tables['access_principals']
        with self.store.transaction() as connection:
            now = self._clock()
            self._require_or_self(connection, actor, 'users:read', principal_id, now)
            row = (connection.execute(self._user_query().where(principals.c.id == principal_id)).mappings().one_or_none()
                   if _identifier(principal_id) else None)
            if row is None:
                _deny(MutationReason.INVALID_TARGET)
            result = self._user_item(row)
            memberships, groups = self.tables['access_memberships'], self.tables['access_groups']
            result['memberships'] = [{'membership_id': m.id, 'group_id': m.group_id, 'group_name': m.name, 'version': m.version}
                for m in connection.execute(sa.select(memberships.c.id, memberships.c.group_id, groups.c.name, memberships.c.version)
                    .select_from(memberships.join(groups, groups.c.id == memberships.c.group_id))
                    .where(memberships.c.principal_id == principal_id).order_by(groups.c.normalized_name, memberships.c.id))]
            a = self.tables['access_assignments']
            result['assignments'] = self._assignment_items(connection, connection.execute(self._assignment_query()
                .where(a.c.principal_id == principal_id).order_by(a.c.created_at, a.c.id)).mappings().all())
            bindings = self.tables['access_key_bindings']
            if principal_id == actor.principal_id or self._allowed(connection, actor, 'keys:manage', now):
                result['keys'] = self._key_items(connection, connection.execute(self._key_query()
                    .where(bindings.c.principal_id == principal_id).order_by(bindings.c.created_at, bindings.c.id)).mappings().all())
            else:
                result['keys'] = []
            result['effective'] = self._effective(connection, {'id': row['id'], 'kind': row['kind'],
                                                               'enabled': 1 if result['enabled'] else 0})
            return result

    def me_extras(self, actor):
        """Owner status and the permissions this actor may grant at installation."""
        with self.store.transaction() as connection:
            now = self._clock()
            source = self._source(connection, actor, now)
            is_owner = (not source.bootstrap and not source.reset_required
                        and self.control.is_complete_owner_on(connection, actor, now=now))
            grantable = [] if source.reset_required else sorted(p for p in PERMISSIONS if self.control.can_grant_on(
                connection, actor, (ScopedPermission(p, INSTALLATION),), now=now).allowed)
        return {'is_owner': is_owner, 'grantable': {'installation': grantable}}

    # -- groups ----------------------------------------------------------

    def _group_query(self):
        groups, memberships = self.tables['access_groups'], self.tables['access_memberships']
        count = (sa.select(sa.func.count()).select_from(memberships)
                 .where(memberships.c.group_id == groups.c.id).scalar_subquery())
        return sa.select(groups.c.id, groups.c.name, groups.c.description, groups.c.enabled,
                         count.label('member_count'), groups.c.version)

    @staticmethod
    def _group_item(row):
        return {'id': row['id'], 'name': row['name'], 'description': row['description'],
                'enabled': row['enabled'] == 1, 'member_count': row['member_count'], 'version': row['version']}

    def groups(self, actor, *, cursor=None, limit=50):
        limit = _limit(limit)
        groups = self.tables['access_groups']
        keys = [(groups.c.normalized_name, 'text'), (groups.c.id, 'text')]
        with self.store.transaction() as connection:
            self._require(connection, actor, 'groups:read', self._clock())
            rows = connection.execute(_keyset(self._group_query(), keys, cursor, limit)).mappings().all()
        return _page(rows, keys, limit, self._group_item)

    def group(self, actor, group_id):
        groups, memberships, principals = self.tables['access_groups'], self.tables['access_memberships'], self.tables['access_principals']
        with self.store.transaction() as connection:
            self._require(connection, actor, 'groups:read', self._clock())
            row = (connection.execute(self._group_query().where(groups.c.id == group_id)).mappings().one_or_none()
                   if _identifier(group_id) else None)
            if row is None:
                _deny(MutationReason.INVALID_TARGET)
            result = self._group_item(row)
            result['members'] = [{'membership_id': m.id, 'principal_id': m.principal_id, 'display_name': m.display_name,
                                  'kind': m.kind, 'version': m.version}
                for m in connection.execute(sa.select(memberships.c.id, memberships.c.principal_id, principals.c.display_name,
                        principals.c.kind, memberships.c.version)
                    .select_from(memberships.join(principals, principals.c.id == memberships.c.principal_id))
                    .where(memberships.c.group_id == group_id).order_by(principals.c.display_name, memberships.c.id))]
            a = self.tables['access_assignments']
            result['assignments'] = self._assignment_items(connection, connection.execute(self._assignment_query()
                .where(a.c.group_id == group_id).order_by(a.c.created_at, a.c.id)).mappings().all())
            return result

    # -- resources and assignments --------------------------------------

    def resources(self, actor, *, kind=None, cursor=None, limit=50):
        limit = _limit(limit)
        if kind is not None and kind not in _KIND_RANK:
            _deny()
        resources, mailboxes, principals = (self.tables[name] for name in ('access_resources', 'mailboxes', 'access_principals'))
        rank = sa.case(*((resources.c.kind == name, value) for name, value in _KIND_RANK.items()), else_=9)
        keys = [(rank, 'int'), (resources.c.id, 'text')]
        with self.store.transaction() as connection:
            self._require(connection, actor, 'grants:read', self._clock())
            query = sa.select(resources.c.id, resources.c.kind, resources.c.parent_id, resources.c.mailbox_id,
                resources.c.principal_id, mailboxes.c.label, principals.c.display_name).select_from(
                    resources.outerjoin(mailboxes, mailboxes.c.id == resources.c.mailbox_id)
                    .outerjoin(principals, principals.c.id == resources.c.principal_id)
                ).where(resources.c.kind.in_([kind] if kind else list(_KIND_RANK)))
            rows = connection.execute(_keyset(query, keys, cursor, limit)).mappings().all()
        return _page(rows, keys, limit, lambda row: {'id': row['id'], 'kind': row['kind'],
            'name': self._resource_name(row['kind'], row['label'], row['display_name']), 'parent_id': row['parent_id'],
            'mailbox_id': row['mailbox_id'], 'principal_id': row['principal_id']})

    def assignments(self, actor, *, subject_id=None, resource_id=None, cursor=None, limit=50):
        limit = _limit(limit)
        if any(value is not None and not _identifier(value) for value in (subject_id, resource_id)):
            _deny()
        a = self.tables['access_assignments']
        keys = [(a.c.created_at, 'time'), (a.c.id, 'text')]
        with self.store.transaction() as connection:
            now = self._clock()
            self._source(connection, actor, now)
            if subject_id is None or subject_id != actor.principal_id:
                self._require(connection, actor, 'grants:read', now)
            query = self._assignment_query()
            if subject_id is not None:
                query = query.where(sa.or_(a.c.principal_id == subject_id, a.c.group_id == subject_id))
            if resource_id is not None:
                query = query.where(a.c.resource_id == resource_id)
            rows = connection.execute(_keyset(query, keys, cursor, limit)).mappings().all()
            items = self._assignment_items(connection, rows[:limit])
        page = _page(rows, keys, limit, lambda row: row)
        page['items'] = items
        return page

    # -- keys -------------------------------------------------------------

    def keys(self, actor, *, principal_id=None, cursor=None, limit=50):
        limit = _limit(limit)
        if principal_id is not None and not _identifier(principal_id):
            _deny()
        bindings = self.tables['access_key_bindings']
        keys = [(bindings.c.created_at, 'time'), (bindings.c.id, 'text')]
        with self.store.transaction() as connection:
            now = self._clock()
            self._source(connection, actor, now)
            if principal_id is None or principal_id != actor.principal_id:
                self._require(connection, actor, 'keys:manage', now)
            query = self._key_query()
            if principal_id is not None:
                query = query.where(bindings.c.principal_id == principal_id)
            rows = connection.execute(_keyset(query, keys, cursor, limit)).mappings().all()
            items = self._key_items(connection, rows[:limit])
        page = _page(rows, keys, limit, lambda row: row)
        page['items'] = items
        return page

    def _key_row(self, connection, actor, public_key_id, now):
        if type(public_key_id) is not str or not 0 < len(public_key_id) <= 32 or not _identifier(public_key_id):
            _deny(MutationReason.INVALID_TARGET)
        self._source(connection, actor, now)
        row = connection.execute(self._key_query().where(self.tables['api_keys'].c.key_id == public_key_id)).mappings().one_or_none()
        if row is None or row['principal_id'] != actor.principal_id:
            self._require(connection, actor, 'keys:manage', now)
        if row is None:
            _deny(MutationReason.INVALID_TARGET)
        return row

    def key(self, actor, public_key_id):
        with self.store.transaction() as connection:
            row = self._key_row(connection, actor, public_key_id, self._clock())
            return self._key_items(connection, [row])[0]

    def key_binding_id(self, actor, public_key_id):
        """The binding id that key mutations target; the public key id is what clients see."""
        with self.store.transaction() as connection:
            return self._key_row(connection, actor, public_key_id, self._clock())['binding_id']

    # -- sessions ---------------------------------------------------------

    def sessions(self, actor, *, principal_id=None, cursor=None, limit=50):
        limit = _limit(limit)
        if principal_id is not None and not _identifier(principal_id):
            _deny()
        decoded = _decode(cursor, ['time', 'text'])
        target = None if principal_id == actor.principal_id else principal_id
        principals = self.tables['access_principals']
        with self.store.transaction() as connection:
            now = self._clock()
            page = self._sessions_page(connection, actor, target, decoded, limit, now)
            owner = principal_id or actor.principal_id
            display = connection.execute(sa.select(principals.c.display_name).where(principals.c.id == owner)).scalar_one_or_none()
        items = [{'session_id': item.session_id, 'principal': {'id': owner, 'display_name': display},
                  'source_kind': item.source_kind, 'created_at': item.created_at, 'last_used_at': item.last_used_at,
                  'expires_at': item.expires_at, 'revoked_at': item.revoked_at, 'current': item.current}
                 for item in page.items]
        next_cursor = (_encode([page.next_cursor.created_at.isoformat(), page.next_cursor.session_id])
                       if page.next_cursor is not None else None)
        return {'items': items, 'next_cursor': next_cursor}

    def _sessions_page(self, connection, actor, principal_id, decoded, limit, now):
        cursor = SessionCursor(decoded[0], decoded[1]) if decoded is not None else None
        # The session service owns own-versus-other visibility (sessions:read) and caps a page at 100.
        return self._sessions.list_sessions_on(connection, actor, principal_id=principal_id,
            cursor=cursor, limit=min(limit, 100), now=now)

    # -- mailboxes and inbound rules --------------------------------------

    def _mailbox_query(self):
        mailboxes, resources, routes = self.tables['mailboxes'], self.tables['access_resources'], self.tables['access_mailbox_routes']
        rules = (sa.select(sa.func.count()).select_from(routes)
                 .where(routes.c.mailbox_id == mailboxes.c.id).scalar_subquery())
        return sa.select(mailboxes.c.id, mailboxes.c.label, resources.c.id.label('resource_id'),
            resources.c.enabled, resources.c.version, rules.label('rule_count')).select_from(
                mailboxes.outerjoin(resources, sa.and_(resources.c.kind == 'mailbox', resources.c.mailbox_id == mailboxes.c.id)))

    @staticmethod
    def _mailbox_item(row):
        return {'id': row['id'], 'label': row['label'], 'enabled': row['enabled'] == 1,
                'resource_id': row['resource_id'], 'rule_count': row['rule_count'],
                'version': row['version'] if row['version'] is not None else 0}

    def mailboxes(self, actor, *, cursor=None, limit=50):
        limit = _limit(limit)
        mailboxes = self.tables['mailboxes']
        keys = [(mailboxes.c.created_at, 'time'), (mailboxes.c.id, 'text')]
        with self.store.transaction() as connection:
            self._require(connection, actor, 'mailboxes:read', self._clock())
            rows = connection.execute(_keyset(self._mailbox_query(), keys, cursor, limit)).mappings().all()
        return _page(rows, keys, limit, self._mailbox_item)

    def mailbox(self, actor, mailbox_id):
        mailboxes = self.tables['mailboxes']
        with self.store.transaction() as connection:
            self._require(connection, actor, 'mailboxes:read', self._clock())
            row = (connection.execute(self._mailbox_query().where(mailboxes.c.id == mailbox_id)).mappings().one_or_none()
                   if _identifier(mailbox_id) else None)
            if row is None:
                _deny(MutationReason.INVALID_TARGET)
            return self._mailbox_item(row)

    def _rule_query(self):
        rules, routes, mailboxes = self.tables['inbound_rules'], self.tables['access_mailbox_routes'], self.tables['mailboxes']
        return sa.select(rules.c.id, rules.c.to_number, routes.c.mailbox_id, routes.c.version,
            sa.func.coalesce(mailboxes.c.label, rules.c.mailbox_label).label('mailbox_label')).select_from(
                rules.outerjoin(routes, routes.c.id == rules.c.id).outerjoin(mailboxes, mailboxes.c.id == routes.c.mailbox_id))

    @staticmethod
    def _rule_item(row):
        return {'id': row['id'], 'to_number': row['to_number'], 'mailbox_id': row['mailbox_id'],
                'mailbox_label': row['mailbox_label'], 'version': row['version'] if row['version'] is not None else 0}

    def inbound_rules(self, actor, *, cursor=None, limit=50):
        limit = _limit(limit)
        rules = self.tables['inbound_rules']
        keys = [(rules.c.created_at, 'time'), (rules.c.id, 'text')]
        with self.store.transaction() as connection:
            self._require(connection, actor, 'mailboxes:read', self._clock())
            rows = connection.execute(_keyset(self._rule_query(), keys, cursor, limit)).mappings().all()
        return _page(rows, keys, limit, self._rule_item)

    def inbound_rule(self, actor, rule_id):
        rules = self.tables['inbound_rules']
        with self.store.transaction() as connection:
            self._require(connection, actor, 'mailboxes:read', self._clock())
            row = (connection.execute(self._rule_query().where(rules.c.id == rule_id)).mappings().one_or_none()
                   if _identifier(rule_id) else None)
            if row is None:
                _deny(MutationReason.INVALID_TARGET)
            return self._rule_item(row)

    # -- audit --------------------------------------------------------------

    def _audit_targets(self, connection, rows):
        """{(kind, id): name} for the audit page's targets, resolved in one query per kind."""
        wanted = {}
        for row in rows:
            if row['target_id'] is not None:
                wanted.setdefault(row['target_kind'], set()).add(row['target_id'])
        names = {}
        lookups = {'principal': ('access_principals', 'display_name'), 'group': ('access_groups', 'name'),
                   'role': ('access_roles', 'name'), 'mailbox': ('mailboxes', 'label'),
                   'inbound_rule': ('inbound_rules', 'to_number'), 'binding': ('api_keys', 'key_id'),
                   'key_binding': ('api_keys', 'key_id')}
        for kind, ids in wanted.items():
            if kind in lookups:
                table_name, column = lookups[kind]
                table = self.tables[table_name]
                for identity, name in connection.execute(sa.select(table.c.id, table.c[column]).where(table.c.id.in_(sorted(ids)))):
                    names[(kind, identity)] = name
            elif kind in {'resource', 'installation'}:
                for identity, (_, name) in self._resource_names(connection, ids).items():
                    names[(kind, identity)] = name
        return names

    def audit(self, actor, *, cursor=None, limit=50, actor_id=None, target_id=None, operation=None):
        limit = _limit(limit)
        if (any(value is not None and not _identifier(value) for value in (actor_id, target_id))
                or (operation is not None and (type(operation) is not str or not 0 < len(operation) <= 64))):
            _deny()
        audit, principals = self.tables['access_audit'], self.tables['access_principals']
        keys = [(audit.c.created_at, 'time'), (audit.c.id, 'text')]
        with self.store.transaction() as connection:
            self._require(connection, actor, 'audit:read', self._clock())
            query = sa.select(audit, principals.c.display_name).select_from(
                audit.outerjoin(principals, principals.c.id == audit.c.actor_principal_id))
            if actor_id is not None:
                query = query.where(audit.c.actor_principal_id == actor_id)
            if target_id is not None:
                query = query.where(audit.c.target_id == target_id)
            if operation is not None:
                query = query.where(audit.c.operation == operation)
            rows = connection.execute(_keyset(query, keys, cursor, limit, descending=True)).mappings().all()
            names = self._audit_targets(connection, rows[:limit])

        def project(row):
            if row['actor_principal_id'] is None:
                credential_kind = 'system'
            elif row['actor_session_id'] is not None:
                credential_kind = 'session'
            elif row['actor_key_binding_id'] is not None:
                credential_kind = 'key'
            else:
                credential_kind = 'bootstrap'
            try:
                details = json.loads(row['details'])
            except (TypeError, ValueError):
                details = {}
            target = None
            if row['target_id'] is not None:
                target = {'kind': row['target_kind'], 'id': row['target_id'],
                          'name': names.get((row['target_kind'], row['target_id']))}
            return {'id': row['id'], 'at': row['created_at'],
                    'actor': ({'id': row['actor_principal_id'], 'display_name': row['display_name']}
                              if row['actor_principal_id'] is not None else None),
                    'credential_kind': credential_kind, 'operation': row['operation'], 'target': target,
                    # Migration rows record changes that happened; the contract has two outcomes.
                    'outcome': 'denied' if row['outcome'] == 'denied' else 'allowed',
                    'policy_version': row['policy_version_after'],
                    'details': details if type(details) is dict else {}}
        return _page(rows, keys, limit, project)
