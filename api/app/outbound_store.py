"""Transactional outbound ownership and monotonic delivery history.

No network or document work occurs here. Configuration and delivery transitions
share one short installation transaction, so Apply cannot race authorization of
an external submission. An expired submission is uncertainty, never permission
to transmit it again.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from uuid import uuid4

import sqlalchemy as sa


TERMINAL = frozenset({'success', 'failed', 'cancelled'})
OBSERVED = TERMINAL | {'in_progress'}


class DeliveryConflict(RuntimeError):
    """A stale or mismatched delivery operation must be reconciled."""


@dataclass(frozen=True)
class DispatchClaim:
    job_id: str
    attempt_id: str
    profile_id: str
    owner: str
    token: str
    expires_at: datetime


def _event(connection, events, job_id, kind, now, *, attempt_id=None, details=None, dedupe_key=None):
    connection.execute(events.insert().values(id=uuid4().hex, job_id=job_id,
        attempt_id=attempt_id, kind=kind, dedupe_key=dedupe_key,
        details=json.dumps(details or {}, sort_keys=True, separators=(',', ':')), created_at=now))


def record_acceptance(connection, tables, job_id, *, held, now):
    """Part of the same transaction as FaxJob and its captured profile binding."""
    mode, state = ('held', 'held') if held else ('normal', 'ready')
    connection.execute(tables['outbound_deliveries'].insert().values(id=job_id,
        dispatch_mode=mode, state=state, version=1, created_at=now, updated_at=now))
    _event(connection, tables['outbound_events'], job_id, 'accepted', now,
        details={'dispatch_mode': mode})


class OutboundStore:
    def __init__(self, configuration):
        self.configuration = configuration
        self.deliveries = configuration.delivery_tables['outbound_deliveries']
        self.attempts = configuration.delivery_tables['outbound_attempts']
        self.events = configuration.delivery_tables['outbound_events']

    def get(self, job_id):
        with self.configuration.engine.connect() as connection:
            record = connection.execute(sa.select(self.deliveries).where(self.deliveries.c.id == job_id)).mappings().one_or_none()
            if record is None:
                raise DeliveryConflict('Delivery record is unavailable.')
            return dict(record)

    def history(self, job_id):
        with self.configuration.engine.connect() as connection:
            return [dict(row) for row in connection.execute(sa.select(self.events).where(
                self.events.c.job_id == job_id).order_by(self.events.c.created_at, self.events.c.id)).mappings()]

    def _row(self, connection, job_id):
        return connection.execute(sa.select(self.deliveries).where(self.deliveries.c.id == job_id)).mappings().one_or_none()

    def _enabled(self, connection):
        configuration = self.configuration
        head = configuration._head(connection)
        if head is None:
            return False
        active = configuration._revision(connection, configuration._cipher(), head['installation_id'], head['active_revision_id'])
        return not active.values.fax_disabled

    def _update(self, connection, row, now, **changes):
        connection.execute(self.deliveries.update().where(self.deliveries.c.id == row['id']).values(
            **changes, version=row['version'] + 1, updated_at=now))

    def claim(self, owner, *, now=None, lease_seconds=30):
        now = now or datetime.utcnow()
        if not isinstance(owner, str) or not owner or len(owner) > 40 or not 1 <= lease_seconds <= 300:
            raise ValueError('Invalid delivery worker claim.')
        with self.configuration._locked() as connection:
            if not self._enabled(connection):
                return None
            row = connection.execute(sa.select(self.deliveries).where(
                self.deliveries.c.state == 'ready', self.deliveries.c.dispatch_mode == 'normal'
            ).order_by(self.deliveries.c.created_at, self.deliveries.c.id).limit(1)).mappings().one_or_none()
            if row is None:
                return None
            binding = connection.execute(sa.select(self.configuration.job_bindings).where(
                self.configuration.job_bindings.c.id == row['id'])).mappings().one_or_none()
            if binding is None:
                self._update(connection, row, now, state='reconciliation_required')
                _event(connection, self.events, row['id'], 'binding_unavailable', now)
                return None
            revision, profile = self.configuration._outbound_context(connection, row['id'])
            if revision.values.fax_disabled:
                self._update(connection, row, now, dispatch_mode='held', state='held')
                _event(connection, self.events, row['id'], 'held_acceptance_restored', now)
                return None
            sequence = connection.scalar(sa.select(sa.func.max(self.attempts.c.sequence)).where(
                self.attempts.c.job_id == row['id'])) or 0
            attempt, token = uuid4().hex, uuid4().hex
            expiry = now + timedelta(seconds=lease_seconds)
            connection.execute(self.attempts.insert().values(id=attempt, job_id=row['id'],
                sequence=sequence + 1, profile_id=profile.id, phase='preparing', created_at=now))
            self._update(connection, row, now, state='preparing', attempt_id=attempt,
                claim_owner=owner, claim_token=token, claim_expires_at=expiry)
            _event(connection, self.events, row['id'], 'claimed', now, attempt_id=attempt)
            return DispatchClaim(row['id'], attempt, profile.id, owner, token, expiry)

    @staticmethod
    def _owns(row, claim):
        return row is not None and row['attempt_id'] == claim.attempt_id and row['claim_owner'] == claim.owner and row['claim_token'] == claim.token

    def begin_submission(self, claim, *, now=None):
        now = now or datetime.utcnow()
        with self.configuration._locked() as connection:
            row = self._row(connection, claim.job_id)
            if (not self._owns(row, claim) or row['state'] != 'preparing'
                    or row['claim_expires_at'] is None or row['claim_expires_at'] <= now):
                return False
            if not self._enabled(connection):
                connection.execute(self.attempts.update().where(self.attempts.c.id == claim.attempt_id).values(
                    phase='abandoned', completed_at=now))
                self._update(connection, row, now, state='ready', claim_owner=None, claim_token=None, claim_expires_at=None)
                _event(connection, self.events, claim.job_id, 'dispatch_paused', now, attempt_id=claim.attempt_id)
                return False
            self._update(connection, row, now, state='submitting')
            connection.execute(self.attempts.update().where(self.attempts.c.id == claim.attempt_id).values(
                phase='submitting', submitted_at=now))
            _event(connection, self.events, claim.job_id, 'submission_authorized', now, attempt_id=claim.attempt_id)
            return True

    def record_uncertain(self, claim, *, category='transport_ambiguous', now=None):
        if category not in {'transport_ambiguous', 'response_unusable', 'submission_cancelled', 'worker_lost'}:
            raise ValueError('Invalid delivery uncertainty category.')
        now = now or datetime.utcnow()
        with self.configuration._locked() as connection:
            row = self._row(connection, claim.job_id)
            if not self._owns(row, claim):
                raise DeliveryConflict('Submission failure belongs to an obsolete attempt.')
            if row['state'] in TERMINAL:
                return False
            if row['state'] not in {'submitting', 'reconciliation_required'}:
                raise DeliveryConflict('No external submission was authorized for this attempt.')
            self._update(connection, row, now, state='reconciliation_required', claim_expires_at=None)
            connection.execute(self.attempts.update().where(self.attempts.c.id == claim.attempt_id).values(
                phase='uncertain', error_category=category))
            _event(connection, self.events, claim.job_id, 'submission_uncertain', now,
                attempt_id=claim.attempt_id, details={'category': category})
            return True

    def fail_preparation(self, claim, *, category, now=None):
        if category not in {'artifact_unavailable', 'provider_unavailable', 'preparation_failed'}:
            raise ValueError('Invalid delivery preparation category.')
        now = now or datetime.utcnow()
        with self.configuration._locked() as connection:
            row = self._row(connection, claim.job_id)
            if not self._owns(row, claim) or row['state'] != 'preparing':
                raise DeliveryConflict('Preparation failure belongs to an obsolete attempt.')
            self._update(connection, row, now, state='failed', claim_expires_at=None)
            connection.execute(self.attempts.update().where(self.attempts.c.id == claim.attempt_id).values(
                phase='failed', error_category=category, completed_at=now))
            connection.execute(self.configuration.jobs.update().where(self.configuration.jobs.c.id == claim.job_id).values(
                status='failed', error='Fax preparation failed before submission.', updated_at=now))
            _event(connection, self.events, claim.job_id, 'preparation_failed', now,
                attempt_id=claim.attempt_id, details={'category': category})

    def recover_expired(self, *, now=None):
        now = now or datetime.utcnow()
        with self.configuration._locked() as connection:
            rows = connection.execute(sa.select(self.deliveries).where(
                self.deliveries.c.state.in_(['preparing', 'submitting']), self.deliveries.c.claim_expires_at <= now)).mappings().all()
            for row in rows:
                preparing = row['state'] == 'preparing'
                changes = {'state': 'ready' if preparing else 'reconciliation_required', 'claim_expires_at': None}
                if preparing:
                    changes.update(claim_owner=None, claim_token=None)
                self._update(connection, row, now, **changes)
                connection.execute(self.attempts.update().where(self.attempts.c.id == row['attempt_id']).values(
                    phase='abandoned' if preparing else 'uncertain', error_category='worker_lost',
                    completed_at=now if preparing else None))
                _event(connection, self.events, row['id'], 'preparation_expired' if preparing else 'submission_uncertain', now,
                    attempt_id=row['attempt_id'])
            return len(rows)

    def _observe(self, connection, row, *, attempt_id, profile_id, provider_sid, status, now, event_key=None):
        if status not in OBSERVED:
            raise DeliveryConflict('Provider status requires reconciliation.')
        if (provider_sid is not None and (not isinstance(provider_sid, str) or not provider_sid
                or len(provider_sid) > 100 or any(ord(char) < 32 for char in provider_sid))):
            raise DeliveryConflict('Provider identity requires reconciliation.')
        attempt = connection.execute(sa.select(self.attempts).where(self.attempts.c.id == attempt_id)).mappings().one_or_none()
        if (row is None or row['attempt_id'] != attempt_id or attempt is None
                or attempt['job_id'] != row['id'] or attempt['profile_id'] != profile_id
                or attempt['submitted_at'] is None):
            raise DeliveryConflict('Provider observation does not match a submitted attempt.')
        if attempt['provider_sid'] and provider_sid and attempt['provider_sid'] != provider_sid:
            raise DeliveryConflict('Provider identity does not match the accepted attempt.')
        dedupe = hashlib.sha256(event_key.encode()).hexdigest() if event_key is not None else None
        if dedupe and connection.execute(sa.select(self.events.c.id).where(
                self.events.c.job_id == row['id'], self.events.c.dedupe_key == dedupe)).first():
            return False
        terminal = row['state'] in TERMINAL
        kind = ('terminal_conflict' if terminal and status in TERMINAL and row['state'] != status
                else 'late_observation' if terminal else 'provider_observed')
        _event(connection, self.events, row['id'], kind, now, attempt_id=attempt_id,
            details={'status': status}, dedupe_key=dedupe)
        if terminal:
            if provider_sid and not attempt['provider_sid']:
                connection.execute(self.attempts.update().where(self.attempts.c.id == attempt_id).values(provider_sid=provider_sid))
                connection.execute(self.configuration.jobs.update().where(self.configuration.jobs.c.id == row['id']).values(provider_sid=provider_sid))
            return False
        final_sid = provider_sid or attempt['provider_sid']
        connection.execute(self.attempts.update().where(self.attempts.c.id == attempt_id).values(
            phase=status, provider_sid=final_sid, error_category=None,
            completed_at=now if status in TERMINAL else None))
        self._update(connection, row, now, state=status, claim_expires_at=None)
        connection.execute(self.configuration.jobs.update().where(self.configuration.jobs.c.id == row['id']).values(
            status=status, provider_sid=final_sid, error=None, updated_at=now))
        return True

    def record_receipt(self, claim, *, provider_sid, status, now=None):
        now = now or datetime.utcnow()
        with self.configuration._locked() as connection:
            row = self._row(connection, claim.job_id)
            if not self._owns(row, claim):
                raise DeliveryConflict('Submission acknowledgement belongs to an obsolete attempt.')
            return self._observe(connection, row, attempt_id=claim.attempt_id, profile_id=claim.profile_id,
                provider_sid=provider_sid, status=status, now=now)

    def observe(self, job_id, *, attempt_id, profile_id, provider_sid, status, event_key, now=None):
        """Call only after the transport owner authenticates the bounded event."""
        if not isinstance(event_key, str) or not event_key or len(event_key) > 512:
            raise DeliveryConflict('Invalid provider event identity.')
        with self.configuration._locked() as connection:
            return self._observe(connection, self._row(connection, job_id), attempt_id=attempt_id,
                profile_id=profile_id, provider_sid=provider_sid, status=status,
                event_key=event_key, now=now or datetime.utcnow())
