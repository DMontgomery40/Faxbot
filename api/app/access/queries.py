"""Current-authority fax projections on one configuration/access transaction.

The visibility predicate is applied before filters, count and pagination. No
query, connection or ORM object escapes the lock; returned data is a snapshot,
never authority for a later operation. Provider lifecycle work remains separate.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import uuid

import sqlalchemy as sa

from ..outbound_summary import dashboard_counts
from ..outbound_store import OutboundStore
from .fax_resources import FaxAccessError
from .types import AccessUnavailableError, InvalidTransactionError


def delivery_fields(row):
    reason = None
    if row['delivery_state'] == 'reconciliation_required':
        reason = ('Historical delivery has no verified transmission record.'
            if row['dispatch_mode'] == 'legacy' else
            'Transmission outcome is uncertain. Check the original provider before taking action.')
    return {'delivery_state': row['delivery_state'], 'dispatch_mode': row['dispatch_mode'],
            'delivery_version': row['delivery_version'], 'reconciliation_reason': reason}


class AuthorizedFaxQueries:
    def __init__(self, configuration, resources, *, clock=None):
        if resources.store is not configuration.access_store:
            raise InvalidTransactionError()
        self.configuration, self.resources = configuration, resources
        self.store = resources.store
        self.jobs = configuration.jobs
        self.deliveries = configuration.delivery_tables['outbound_deliveries']
        self.delivery = OutboundStore(configuration)
        self._clock = clock or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    @contextmanager
    def _transaction(self):
        # Delivery updates share the configuration lock, including count/page.
        with self.configuration._locked() as connection:
            self.store.lock_on(connection)
            yield connection, self._clock()

    def _selection(self):
        jobs, delivery = self.jobs, self.deliveries
        columns = ('id', 'to_number', 'file_name', 'status', 'backend', 'pages',
                   'error', 'provider_sid', 'created_at', 'updated_at')
        return sa.select(*(jobs.c[name] for name in columns),
            delivery.c.state.label('delivery_state'), delivery.c.dispatch_mode,
            delivery.c.version.label('delivery_version')).select_from(
                jobs.outerjoin(delivery, delivery.c.id == jobs.c.id))

    def _visible(self, connection, actor, now):
        source = self.resources.control._current_source_on(connection, actor, now)
        if source.reset_required:
            raise FaxAccessError('reset_required')
        return self.resources.visible_outbound_ids_on(connection, actor, 'fax:read', now=now)

    @staticmethod
    def _record(row):
        if row is None or row['delivery_state'] is None:
            raise AccessUnavailableError()
        return {**dict(row), **delivery_fields(row)}

    def page(self, actor, *, status=None, backend=None, limit=50, offset=0):
        if (type(limit) is not int or not 1 <= limit <= 100
                or type(offset) is not int or offset < 0
                or any(value is not None and (type(value) is not str or len(value) > 100)
                       for value in (status, backend))):
            raise FaxAccessError('invalid_input')
        with self._transaction() as (connection, now):
            visible = self._visible(connection, actor, now)
            query = self._selection().where(self.jobs.c.id.in_(visible))
            if status:
                query = query.where(self.deliveries.c.state == status)
            if backend:
                query = query.where(self.jobs.c.backend == backend)
            total = connection.scalar(sa.select(sa.func.count()).select_from(query.subquery()))
            rows = connection.execute(query.order_by(self.jobs.c.created_at.desc(), self.jobs.c.id.desc())
                .offset(offset).limit(limit)).mappings().all()
            return {'total': total, 'jobs': [self._record(row) for row in rows]}

    def job(self, actor, job_id):
        with self._transaction() as (connection, now):
            self.resources.require_outbound_on(connection, actor, job_id, 'fax:read', now=now)
            row = connection.execute(self._selection().where(self.jobs.c.id == job_id)).mappings().one_or_none()
            return self._record(row)

    def document(self, actor, job_id):
        with self._transaction() as (connection, now):
            self.resources.require_outbound_on(connection, actor, job_id, 'fax:document', now=now)
            return job_id

    def counts(self, actor):
        with self._transaction() as (connection, now):
            visible = self._visible(connection, actor, now)
            return dashboard_counts(connection, self.deliveries, now=now, job_ids=visible)

    def _history_on(self, connection, actor, job_id, now):
        resource = self.resources.require_outbound_on(connection, actor, job_id, 'fax:read', now=now)
        result = self.delivery._operator_view_on(connection, job_id)
        can_reconcile = self.resources.control.authorize_on(connection, actor,
            'fax:reconcile', resource, now=now).allowed
        if not can_reconcile:
            result['can_bind_provider_identity'] = False
            result['bind_refusal_reason'] = 'You do not have permission to reconcile this fax.'
        return result

    def history(self, actor, job_id):
        with self._transaction() as (connection, now):
            return self._history_on(connection, actor, job_id, now)

    def reconcile(self, actor, job_id, *, expected_version, provider_sid):
        with self._transaction() as (connection, now):
            self.resources.require_outbound_on(connection, actor, job_id, 'fax:read', now=now)
            resource = self.resources.require_outbound_on(connection, actor, job_id, 'fax:reconcile', now=now)
            self.delivery._bind_provider_identity_on(connection, job_id,
                expected_version=expected_version, provider_sid=provider_sid, actor=actor.replay_scope)
            version = self.store.require_lock_on(connection)
            connection.execute(self.store.tables['access_audit'].insert().values(
                id=uuid.uuid4().hex, actor_principal_id=actor.principal_id,
                actor_key_binding_id=getattr(actor.credential, 'binding_id', None),
                actor_session_id=getattr(actor.credential, 'session_id', None),
                operation='fax.reconcile', target_kind='resource', target_id=resource.id,
                policy_version_before=version, policy_version_after=version, outcome='allowed',
                details=json.dumps({'source':'operator_confirmation'}, separators=(',', ':')), created_at=now))
            return self._history_on(connection, actor, job_id, now)

    def poll_target(self, actor, job_id):
        """Capture one authorized lookup; never hold database locks over network IO."""
        with self._transaction() as (connection, now):
            self.resources.require_outbound_on(connection, actor, job_id, 'fax:read', now=now)
            self.resources.require_outbound_on(connection, actor, job_id, 'fax:refresh', now=now)
            return self.delivery._poll_target_on(connection, job_id)
