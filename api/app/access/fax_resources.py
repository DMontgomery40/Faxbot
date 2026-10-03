"""Fax identity/resource seams on the existing locked access transaction.

This module does not own a transaction or authorize a later transaction. Callers
must apply returned visibility queries before filtering/counting/pagination and
consume them on this same connection. Fax acceptance inserts its business row,
resource and audit atomically; an error requires rollback of that whole unit.
Provider lifecycle processing is deliberately separate from human access.
"""
import json
import uuid

import sqlalchemy as sa

from .catalog import OUTBOUND_PERMISSIONS
from .types import AccessError, ResourceRef


class FaxAccessError(AccessError):
    def __init__(self, code):
        if code not in {'not_found', 'forbidden', 'reset_required', 'invalid_target', 'invalid_input'}:
            raise ValueError('invalid fax access reason')
        self.code = code
        super().__init__()


def _identity(value):
    return type(value) is str and 0 < len(value) <= 40 and all(32 <= ord(c) < 127 for c in value)


class FaxResources:
    def __init__(self, control):
        self.control, self.store, self.tables = control, control.store, control.store.tables

    def _permission(self, permission):
        if type(permission) is not str or permission not in OUTBOUND_PERMISSIONS:
            raise FaxAccessError('invalid_input')

    def authorize_send_on(self, connection, actor, *, now):
        """Resolve this authenticated creator's own personal container afresh."""
        source = self.control._current_source_on(connection, actor, now)
        if source.reset_required:
            raise FaxAccessError('reset_required')
        resources = self.tables['access_resources']
        identity = connection.execute(sa.select(resources.c.id).where(
            resources.c.kind == 'personal', resources.c.principal_id == actor.principal_id)).scalar_one_or_none()
        if identity is None:
            raise FaxAccessError('forbidden')
        resource = ResourceRef(identity)
        decision = self.control.authorize_on(connection, actor, 'fax:send', resource, now=now)
        if not decision.allowed:
            raise FaxAccessError('forbidden')
        return resource

    def record_outbound_on(self, connection, actor, job_id, *, now):
        """Attach ownership to the new business row in the acceptance transaction.

        The acceptance owner inserts the new job immediately before this call.
        This is not a historical adoption or transfer API. Existing resource links
        are immutable here, and every failure rolls back acceptance as a whole.
        """
        version = self.store.require_lock_on(connection)
        parent = self.authorize_send_on(connection, actor, now=now)
        if not _identity(job_id):
            raise FaxAccessError('invalid_input')
        resources, jobs = self.tables['access_resources'], self.tables['fax_jobs']
        if (connection.execute(sa.select(jobs.c.id).where(jobs.c.id == job_id)).first() is None
                or connection.execute(sa.select(resources.c.id).where(resources.c.fax_job_id == job_id)).first() is not None):
            raise FaxAccessError('invalid_target')
        identity = uuid.uuid4().hex
        connection.execute(resources.insert().values(id=identity, kind='outbound',
            parent_id=parent.id, parent_kind='personal', principal_id=None,
            mailbox_id=None, fax_job_id=job_id, inbound_fax_id=None, enabled=1,
            version=1, created_at=now, updated_at=now))
        evidence = actor.credential
        connection.execute(self.tables['access_audit'].insert().values(
            id=uuid.uuid4().hex, actor_principal_id=actor.principal_id,
            actor_key_binding_id=getattr(evidence, 'binding_id', None),
            actor_session_id=getattr(evidence, 'session_id', None),
            operation='fax.accept', target_kind='resource', target_id=identity,
            policy_version_before=version, policy_version_after=version, outcome='allowed',
            details=json.dumps({'source': 'outbound_acceptance'}, separators=(',', ':')),
            created_at=now))
        return ResourceRef(identity)

    def visible_outbound_ids_on(self, connection, actor, permission, *, now):
        self.store.require_lock_on(connection)
        self._permission(permission)
        visible = self.control.visible_resource_ids_on(connection, actor, permission, 'outbound', now=now)
        resources = self.tables['access_resources']
        return sa.select(resources.c.fax_job_id).where(resources.c.id.in_(visible),
            resources.c.kind == 'outbound', resources.c.fax_job_id.is_not(None))

    def require_outbound_on(self, connection, actor, job_id, permission, *, now):
        """Permit the exact action, or distinguish hidden from visible denial.

        A document-only grant can read bytes without adding metadata visibility.
        A denied action reveals existence only with independent fax:read access.
        """
        source = self.control._current_source_on(connection, actor, now)
        self._permission(permission)
        if source.reset_required:
            raise FaxAccessError('reset_required')
        if not _identity(job_id):
            raise FaxAccessError('not_found')
        resources = self.tables['access_resources']
        identity = connection.execute(sa.select(resources.c.id).where(
            resources.c.kind == 'outbound', resources.c.fax_job_id == job_id)).scalar_one_or_none()
        if identity is None:
            raise FaxAccessError('not_found')
        resource = ResourceRef(identity)
        if self.control.authorize_on(connection, actor, permission, resource, now=now).allowed:
            return resource
        visible = self.control.authorize_on(connection, actor, 'fax:read', resource, now=now).allowed
        raise FaxAccessError('forbidden' if visible else 'not_found')
