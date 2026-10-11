"""Current-authority fax projections on one configuration/access transaction.

The visibility predicate is applied before filters, count and pagination. No
query, connection or ORM object escapes the lock; returned data is a snapshot,
never authority for a later operation. Provider lifecycle work remains separate.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import time
import uuid

import sqlalchemy as sa

from ..outbound_summary import dashboard_counts
from ..outbound_store import OutboundStore
from .fax_resources import FaxAccessError
from .types import AccessUnavailableError, InvalidTransactionError


# Faxbot's own trunk and fax engines (power.LOCAL_PROVIDERS): the call is Faxbot's own, so there is no provider
# account to check.
OWN_ROUTES = frozenset({'sip', 'freeswitch'})


# A list's access checks (``visible_jobs``) end their transaction and start another after LIST_SECONDS or
# LIST_CHUNK faxes, whichever comes first, so the write lock is never held long enough to make other access checks
# wait out their 1 s (AccessStore.LOCK_TIMEOUT_MS). A fax's check is about 3 ms of Python on a Mac and took
# 250-500 ms on a loaded installation (acceptance, 2026-10-11), so a count alone is no bound.
LIST_CHUNK = 20
LIST_SECONDS = 0.2


def delivery_fields(row):
    """The delivery fields of a sent fax, with one sentence for an uncertain result that fits the route that carried
    it: the provider account for a fax service, the recipient for Faxbot's own trunk or engines. ``route_provider``
    is the provider of the account its latest attempt used, else the account it was accepted with (``backend``)."""
    reason = None
    if row['delivery_state'] == 'reconciliation_required':
        own = (row.get('route_provider') or row.get('backend')) in OWN_ROUTES
        if row['dispatch_mode'] == 'legacy':
            reason = ('Faxbot has no delivery record for this older fax; check with the recipient before sending it '
                      'again.' if own else
                      'Faxbot has no delivery record for this older fax; check its status in your provider account.')
        else:
            reason = ('The call ended before the receiving fax machine confirmed the pages; check with the recipient '
                      'before sending this fax again.' if own else
                      'Faxbot could not confirm delivery; check your provider account before sending this fax again.')
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
        # 0020: whether the sender asked for a real call even to one of the installation's own numbers.
        columns += ('send_by_call',) if 'send_by_call' in jobs.c else ()
        # 0021: an urgent fax goes before others waiting for the same line.
        columns += ('urgent',) if 'urgent' in jobs.c else ()
        # 0040: the time the sender needs the fax sent by.
        columns += ('send_by',) if 'send_by' in jobs.c else ()
        # The provider of the account the latest attempt used (an attempt bound to another route), else the
        # account the fax was accepted with: which advice an uncertain result gets (delivery_fields). Wording only.
        # Each join is on a primary key, so a fax is still one row.
        attempt = self.configuration.delivery_tables['outbound_attempts'].alias('route_attempt')
        binding = self.configuration.job_bindings.alias('route_binding')
        profile = self.configuration.profiles.alias('route_profile')
        return sa.select(*(jobs.c[name] for name in columns),
            delivery.c.state.label('delivery_state'), delivery.c.dispatch_mode,
            delivery.c.version.label('delivery_version'), profile.c.provider_id.label('route_provider')).select_from(
                jobs.outerjoin(delivery, delivery.c.id == jobs.c.id)
                .outerjoin(attempt, attempt.c.id == delivery.c.attempt_id)
                .outerjoin(binding, binding.c.id == jobs.c.id)
                .outerjoin(profile, profile.c.id == sa.func.coalesce(attempt.c.profile_id, binding.c.profile_id)))

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

    def page(self, actor, *, status=None, backend=None, limit=50, offset=0, since_hours=None):
        if (type(limit) is not int or not 1 <= limit <= 100
                or type(offset) is not int or offset < 0
                or (since_hours is not None and (type(since_hours) is not int or not 1 <= since_hours <= 8784))
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
            if since_hours is not None:
                # The delivery changed within the hours asked: the window of the Overview's recent counts
                # (outbound_summary.dashboard_counts).
                changed = self.deliveries.c.updated_at
                query = query.where(changed >= now - timedelta(hours=since_hours), changed <= now)
            total = connection.scalar(sa.select(sa.func.count()).select_from(query.subquery()))
            rows = connection.execute(query.order_by(self.jobs.c.created_at.desc(), self.jobs.c.id.desc())
                .offset(offset).limit(limit)).mappings().all()
            return {'total': total, 'jobs': [self._record(row) for row in rows]}

    def job(self, actor, job_id):
        with self._transaction() as (connection, now):
            self.resources.require_outbound_on(connection, actor, job_id, 'fax:read', now=now)
            row = connection.execute(self._selection().where(self.jobs.c.id == job_id)).mappings().one_or_none()
            return self._record(row)

    def visible_jobs(self, actor, job_ids):
        """``{job_id: record}`` for the faxes among ``job_ids`` this actor may read, in order.

        Each id gets exactly ``job``'s check; an id it refuses is left out, never explained, and does not hide
        the others. Anything other than a refusal (a revoked credential, unavailable storage) fails the whole call,
        as it fails ``job``. Lists ask with this: a few faxes share one transaction, at most ``LIST_CHUNK`` and no
        more than fit in ``LIST_SECONDS``, so a page of 100 takes the lock a handful of times, not 100, and never
        long enough to make other access checks wait out their ``AccessStore.LOCK_TIMEOUT_MS``.
        """
        wanted = list(dict.fromkeys(job_ids))
        found, start = {}, 0
        while start < len(wanted):
            checked, visible = self._visible_jobs(actor, wanted[start:start + LIST_CHUNK])
            found.update(visible)
            start += checked
        return found

    def _visible_jobs(self, actor, wanted):
        """(how many of ``wanted`` were checked, {job_id: record} for those visible), in one transaction that
        takes no further fax once the slowest check so far would carry it past ``LIST_SECONDS``; the first fax is
        always checked, so the list always moves on."""
        with self._transaction() as (connection, now):
            began, slowest, checked, visible = time.monotonic(), 0.0, 0, []
            for job_id in wanted:
                if checked and time.monotonic() - began + slowest > LIST_SECONDS:
                    break
                checked += 1
                one = time.monotonic()
                try:
                    self.resources.require_outbound_on(connection, actor, job_id, 'fax:read', now=now)
                except FaxAccessError:
                    continue
                finally:
                    slowest = max(slowest, time.monotonic() - one)
                visible.append(job_id)
            rows = {}
            if visible:
                rows = {row['id']: row for row in connection.execute(
                    self._selection().where(self.jobs.c.id.in_(visible))).mappings()}
            return checked, {job_id: self._record(rows.get(job_id)) for job_id in visible}

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
