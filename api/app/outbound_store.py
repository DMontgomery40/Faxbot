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
import re
from uuid import uuid4

import sqlalchemy as sa


TERMINAL = frozenset({'success', 'failed', 'cancelled'})
OBSERVED = TERMINAL | {'in_progress'}
_PROVIDER_SID = re.compile(r'[A-Za-z0-9_-]{1,100}', re.ASCII)
_ACTOR = re.compile(r'(?:key|principal):[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', re.ASCII)
_EVENT_KINDS = frozenset({'accepted', 'legacy_migrated', 'binding_unavailable',
    'held_acceptance_restored', 'claimed', 'dispatch_paused', 'submission_authorized',
    'submission_uncertain', 'preparation_failed', 'preparation_expired',
    'provider_observation_refused', 'terminal_conflict', 'late_observation',
    'provider_observed', 'operator_identity_bound'})
_CATEGORIES = frozenset({'transport_ambiguous', 'response_unusable', 'submission_cancelled',
    'worker_lost', 'artifact_unavailable', 'provider_unavailable', 'preparation_failed',
    'profile_mismatch', 'sid_mismatch'})


def _valid_actor(actor):
    return isinstance(actor, str) and (actor in {'admin', 'development'} or _ACTOR.fullmatch(actor) is not None)


def _safe_event_details(encoded):
    """Expose only known structured evidence, never arbitrary history content."""
    if not isinstance(encoded, str) or len(encoded) > 4096:
        return {}
    try:
        details = json.loads(encoded)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(details, dict):
        return {}
    result = {}
    for name, value in details.items():
        if not isinstance(value, str):
            continue
        if ((name == 'status' and value in OBSERVED)
                or (name == 'category' and value in _CATEGORIES)
                or (name == 'dispatch_mode' and value in {'normal', 'held', 'legacy'})
                or (name == 'actor' and _valid_actor(value))
                or (name == 'provider_sid' and _PROVIDER_SID.fullmatch(value) is not None)
                or (name == 'legacy_status' and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _.-]{0,127}', value, re.ASCII))):
            result[name] = value
    return result


class DeliveryConflict(RuntimeError):
    """A stale or mismatched delivery operation must be reconciled."""


def _automatic_poll_interval(configuration, default):
    """Captured native policy; an HTTP manifest defines its own status action."""
    if configuration.manifest is not None:
        return default if 'get_status' in configuration.manifest.get('actions', {}) else 0
    if configuration.provider_id == 'signalwire':
        seconds = configuration.settings.get('status_poll_seconds', 0)
        if type(seconds) is not int or seconds < 0:
            raise DeliveryConflict('Captured provider polling policy is invalid.')
        return seconds
    if configuration.provider_id in {'sip', 'freeswitch'}:
        return 0
    return default


@dataclass(frozen=True)
class DispatchClaim:
    job_id: str
    attempt_id: str
    profile_id: str
    owner: str
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class _ObservationRefusal:
    message: str


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

    def _operator_context(self, connection, row):
        from .config_profiles import ConfigurationRecordError
        from .config_secrets import ConfigurationSecretError
        from .config_store import ConfigurationStoreError
        from .config_values import ConfigurationValueError
        attempt = connection.execute(sa.select(self.attempts).where(
            self.attempts.c.id == row['attempt_id'])).mappings().one_or_none()
        if attempt is not None and attempt['job_id'] != row['id']:
            attempt = None
        try:
            revision, profile = self.configuration._outbound_context(connection, row['id'])
        except (ConfigurationStoreError, ConfigurationSecretError, ConfigurationRecordError, ConfigurationValueError):
            revision, profile = None, None
        return attempt, revision, profile

    def _bind_refusal(self, connection, row, attempt, profile):
        if row['state'] != 'reconciliation_required':
            return 'Only an unresolved submitted delivery can receive a confirmed provider identity.'
        if row['dispatch_mode'] == 'legacy':
            return 'Historical delivery requires deliberate maintenance reconciliation of its original account.'
        if row['dispatch_mode'] != 'normal':
            return 'Held or unsupported dispatch mode requires deliberate maintenance reconciliation.'
        if attempt is None or attempt['submitted_at'] is None:
            return 'No verified submitted attempt is available; deliberate maintenance reconciliation is required.'
        if attempt['phase'] not in {'uncertain', 'submitting', 'in_progress'} or attempt['completed_at'] is not None:
            return 'The attempt is not an unresolved submission; deliberate maintenance reconciliation is required.'
        if profile is None or profile.id != attempt['profile_id']:
            return 'The original provider account could not be authenticated; deliberate maintenance reconciliation is required.'
        job_sid = connection.scalar(sa.select(self.configuration.jobs.c.provider_sid).where(
            self.configuration.jobs.c.id == row['id']))
        if attempt['provider_sid'] is not None or job_sid is not None:
            return 'A provider identity is already attached; refresh the original account instead.'
        configuration = profile.configuration
        manifest = configuration.manifest
        supported = ('get_status' in manifest.get('actions', {}) if manifest is not None
                     else configuration.provider_id in {'phaxio', 'signalwire', 'sinch', 'documo'})
        if not supported:
            return 'This captured provider cannot refresh status; deliberate maintenance reconciliation is required.'
        return None

    def bind_provider_identity(self, job_id, *, expected_version, provider_sid, actor):
        with self.configuration._locked() as connection:
            return self._bind_provider_identity_on(connection, job_id, expected_version=expected_version, provider_sid=provider_sid, actor=actor)

    def _bind_provider_identity_on(self, connection, job_id, *, expected_version, provider_sid, actor):
        """Attach external evidence to one issued attempt, without resend authority."""
        self.configuration._require_lock_on(connection)
        if (type(expected_version) is not int or expected_version <= 0
                or not isinstance(provider_sid, str) or _PROVIDER_SID.fullmatch(provider_sid) is None
                or not _valid_actor(actor)):
            raise ValueError('Invalid provider identity reconciliation input.')
        row = self._row(connection, job_id)
        if row is None:
            raise DeliveryConflict('Delivery record is unavailable.')
        if row['version'] != expected_version:
            raise DeliveryConflict('Delivery changed; reload before attaching a provider identity.')
        attempt, _, profile = self._operator_context(connection, row)
        reason = self._bind_refusal(connection, row, attempt, profile)
        if reason is not None:
            raise DeliveryConflict(reason)
        documo_uuid = (profile.configuration.manifest is None
                       and profile.configuration.provider_id == 'documo')
        if documo_uuid:
            if re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', provider_sid) is None:
                raise ValueError('Invalid provider identity reconciliation input.')
            provider_sid = provider_sid.lower()
        attempt_sid = self.attempts.c.provider_sid
        if documo_uuid:
            attempt_sid = sa.func.lower(attempt_sid)
        owned_attempt = connection.execute(sa.select(self.attempts.c.id).where(
                self.attempts.c.profile_id == profile.id,
                attempt_sid == provider_sid,
                self.attempts.c.job_id != job_id).limit(1)).first()
        # Captured pre-upgrade jobs can retain a SID and verified binding
        # without a fabricated outbound attempt. That identity is owned too.
        jobs, bindings = self.configuration.jobs, self.configuration.job_bindings
        job_sid_column = sa.func.lower(jobs.c.provider_sid) if documo_uuid else jobs.c.provider_sid
        owned_job = connection.execute(sa.select(jobs.c.id).join(bindings, bindings.c.id == jobs.c.id).where(
            bindings.c.profile_id == profile.id, job_sid_column == provider_sid,
            jobs.c.id != job_id).limit(1)).first()
        if owned_attempt or owned_job:
            raise DeliveryConflict('This provider identity already belongs to another delivery from the original account.')
        now = datetime.utcnow()
        connection.execute(self.attempts.update().where(self.attempts.c.id == attempt['id']).values(
            provider_sid=provider_sid))
        connection.execute(self.configuration.jobs.update().where(
            self.configuration.jobs.c.id == job_id).values(provider_sid=provider_sid, updated_at=now))
        self._update(connection, row, now, next_poll_at=None)
        _event(connection, self.events, job_id, 'operator_identity_bound', now,
            attempt_id=attempt['id'], details={'actor': actor, 'provider_sid': provider_sid})
        version = row['version'] + 1
        return version

    def operator_view(self, job_id):
        with self.configuration._locked() as connection:
            return self._operator_view_on(connection, job_id)

    def _operator_view_on(self, connection, job_id):
        """Read a consistent bounded operator projection of the original account."""
        self.configuration._require_lock_on(connection)
        row = self._row(connection, job_id)
        if row is None:
            raise DeliveryConflict('Delivery record is unavailable.')
        attempt, revision, profile = self._operator_context(connection, row)
        reason = self._bind_refusal(connection, row, attempt, profile)
        count = connection.scalar(sa.select(sa.func.count()).select_from(self.events).where(
            self.events.c.job_id == job_id))
        events = connection.execute(sa.select(self.events).where(self.events.c.job_id == job_id)
            .order_by(self.events.c.created_at.desc(), self.events.c.id.desc()).limit(100)).mappings().all()
        phases = OBSERVED | {'preparing', 'submitting', 'uncertain', 'abandoned'}
        safe_attempt = None if attempt is None else {
            'id': attempt['id'], 'phase': attempt['phase'] if attempt['phase'] in phases else 'unknown',
            'provider_sid': attempt['provider_sid'] if isinstance(attempt['provider_sid'], str)
                and _PROVIDER_SID.fullmatch(attempt['provider_sid']) else None,
            'submitted_at': attempt['submitted_at'], 'completed_at': attempt['completed_at'],
        }
        return {'version': row['version'], 'state': row['state'], 'dispatch_mode': row['dispatch_mode'],
            'provider_id': profile.configuration.provider_id if profile is not None else None,
            'profile_id': profile.id if profile is not None else None,
            'revision_id': revision.id if revision is not None else None,
            'attempt': safe_attempt, 'can_bind_provider_identity': reason is None, 'bind_refusal_reason': reason,
            'events_truncated': count > 100,
            'events': [{'id': event['id'], 'attempt_id': event['attempt_id'],
                'kind': event['kind'] if event['kind'] in _EVENT_KINDS else 'unknown',
                'created_at': event['created_at'], 'details': _safe_event_details(event['details'])}
                for event in reversed(events)]}

    def reserve_poll(self, *, now=None, interval_seconds=30):
        """Reserve a status read, never a submission or replacement attempt."""
        if not 1 <= interval_seconds <= 3600:
            raise ValueError('Invalid delivery polling interval.')
        with self.configuration._locked() as connection:
            now = datetime.utcnow() if now is None else now
            row = connection.execute(sa.select(self.deliveries, self.attempts.c.profile_id).join(self.attempts,
                self.attempts.c.id == self.deliveries.c.attempt_id).where(
                    self.deliveries.c.state.in_(['in_progress', 'reconciliation_required']),
                    self.attempts.c.job_id == self.deliveries.c.id,
                    self.attempts.c.submitted_at.is_not(None),
                    self.attempts.c.provider_sid.is_not(None),
                    sa.or_(self.deliveries.c.next_poll_at.is_(None),
                           self.deliveries.c.next_poll_at <= now)
                ).order_by(sa.func.coalesce(self.deliveries.c.next_poll_at, self.deliveries.c.created_at),
                           self.deliveries.c.created_at,
                           self.deliveries.c.id).limit(1)).mappings().one_or_none()
            if row is None:
                return None
            _, profile = self.configuration._outbound_context(connection, row['id'])
            if profile.id != row['profile_id']:
                raise DeliveryConflict('Delivery account does not match its accepted attempt.')
            seconds = _automatic_poll_interval(profile.configuration, interval_seconds)
            # Disabled/unsupported checks get a bounded revisit time, then the
            # loop can immediately reserve the next eligible read.
            try:
                next_poll_at = now + timedelta(seconds=seconds or 3600)
            except OverflowError:
                next_poll_at = datetime.max
            connection.execute(self.deliveries.update().where(self.deliveries.c.id == row['id']).values(
                next_poll_at=next_poll_at))
            return row['id']

    def poll_target(self, job_id, *, automatic=False):
        with self.configuration._locked() as connection:
            return self._poll_target_on(connection, job_id, automatic=automatic)

    def _poll_target_on(self, connection, job_id, *, automatic=False):
        """Authenticate original account and attempt before a read-only lookup."""
        self.configuration._require_lock_on(connection)
        row = self._row(connection, job_id)
        if row is None:
            raise DeliveryConflict('Delivery record is unavailable.')
        if row['state'] in TERMINAL or row['state'] == 'held':
            return None
        attempt = connection.execute(sa.select(self.attempts).where(
            self.attempts.c.id == row['attempt_id'])).mappings().one_or_none()
        if (row['state'] not in {'in_progress', 'reconciliation_required'} or attempt is None
                or attempt['job_id'] != job_id or not attempt['provider_sid']
                or attempt['submitted_at'] is None):
            raise DeliveryConflict('No acknowledged provider identity is available for refresh.')
        _, profile = self.configuration._outbound_context(connection, job_id)
        if profile.id != attempt['profile_id']:
            raise DeliveryConflict('Delivery account does not match its accepted attempt.')
        if automatic and _automatic_poll_interval(profile.configuration, 30) == 0:
            return None
        return profile, attempt['id'], attempt['provider_sid']

    def _preparing(self, connection, claim, now):
        row = self._row(connection, claim.job_id)
        if (not self._owns(row, claim) or row['state'] != 'preparing'
                or row['claim_expires_at'] is None or row['claim_expires_at'] <= now):
            raise DeliveryConflict('Delivery preparation lease is no longer current.')
        revision, profile = self.configuration._outbound_context(connection, claim.job_id)
        if profile.id != claim.profile_id:
            raise DeliveryConflict('Delivery preparation profile does not match.')
        return revision, profile

    def load_dispatch(self, claim):
        """Read private submission inputs only while the preparation lease is held."""
        with self.configuration._locked() as connection:
            revision, profile = self._preparing(connection, claim, datetime.utcnow())
            job = connection.execute(sa.select(self.configuration.jobs).where(
                self.configuration.jobs.c.id == claim.job_id)).mappings().one()
            return revision, profile, dict(job)

    def grant_pdf(self, claim, *, url, token, expires_at):
        """Persist the captured provider's media capability before submission."""
        if (not isinstance(url, str) or not url or len(url) > 512
                or not isinstance(token, str) or not token or len(token) > 128
                or not isinstance(expires_at, datetime)):
            raise ValueError('Invalid delivery media grant.')
        with self.configuration._locked() as connection:
            now = datetime.utcnow()
            if expires_at <= now:
                raise ValueError('Invalid delivery media grant.')
            self._preparing(connection, claim, now)
            connection.execute(self.configuration.jobs.update().where(
                self.configuration.jobs.c.id == claim.job_id).values(
                    pdf_url=url, pdf_token=token, pdf_token_expires_at=expires_at))

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
        if not isinstance(owner, str) or not owner or len(owner) > 40 or not 1 <= lease_seconds <= 300:
            raise ValueError('Invalid delivery worker claim.')
        with self.configuration._locked() as connection:
            now = datetime.utcnow() if now is None else now
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
        with self.configuration._locked() as connection:
            now = datetime.utcnow() if now is None else now
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
        with self.configuration._locked() as connection:
            now = datetime.utcnow() if now is None else now
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

    def _refuse_observation(self, connection, row, *, attempt_id, category, message, now, event_key):
        # Accepted event digests are lowercase hexadecimal. This separate prefix
        # prevents an accepted identity from suppressing its refusal evidence.
        dedupe = ('r' + hashlib.sha256((category + '\0' + event_key).encode()).hexdigest()[:63]
                  if event_key is not None else None)
        if not dedupe or not connection.execute(sa.select(self.events.c.id).where(
                self.events.c.job_id == row['id'], self.events.c.dedupe_key == dedupe)).first():
            _event(connection, self.events, row['id'], 'provider_observation_refused', now,
                attempt_id=attempt_id, details={'category': category}, dedupe_key=dedupe)
        return _ObservationRefusal(message)

    def _observe(self, connection, row, *, attempt_id, profile_id, provider_sid, status, now, event_key=None):
        if status not in OBSERVED:
            raise DeliveryConflict('Provider status requires reconciliation.')
        if (provider_sid is not None and (not isinstance(provider_sid, str) or not provider_sid
                or len(provider_sid) > 100 or any(ord(char) < 32 for char in provider_sid))):
            raise DeliveryConflict('Provider identity requires reconciliation.')
        attempt = connection.execute(sa.select(self.attempts).where(self.attempts.c.id == attempt_id)).mappings().one_or_none()
        if (row is None or row['attempt_id'] != attempt_id or attempt is None
                or attempt['job_id'] != row['id']
                or attempt['submitted_at'] is None):
            raise DeliveryConflict('Provider observation does not match a submitted attempt.')
        if attempt['profile_id'] != profile_id:
            return self._refuse_observation(connection, row, attempt_id=attempt_id,
                category='profile_mismatch', message='Provider observation does not match a submitted attempt.',
                now=now, event_key=event_key)
        if attempt['provider_sid'] and provider_sid and attempt['provider_sid'] != provider_sid:
            return self._refuse_observation(connection, row, attempt_id=attempt_id,
                category='sid_mismatch', message='Provider identity does not match the accepted attempt.',
                now=now, event_key=event_key)
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
            result = self._observe(connection, row, attempt_id=claim.attempt_id, profile_id=claim.profile_id,
                provider_sid=provider_sid, status=status, now=now)
        if isinstance(result, _ObservationRefusal):
            raise DeliveryConflict(result.message)
        return result

    def observe(self, job_id, *, attempt_id, profile_id, provider_sid, status, event_key, now=None):
        """Call only after the transport owner authenticates the bounded event."""
        if not isinstance(event_key, str) or not event_key or len(event_key) > 512:
            raise DeliveryConflict('Invalid provider event identity.')
        with self.configuration._locked() as connection:
            result = self._observe(connection, self._row(connection, job_id), attempt_id=attempt_id,
                profile_id=profile_id, provider_sid=provider_sid, status=status,
                event_key=event_key, now=now or datetime.utcnow())
        if isinstance(result, _ObservationRefusal):
            raise DeliveryConflict(result.message)
        return result
