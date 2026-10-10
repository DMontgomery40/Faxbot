"""Transactional outbound ownership and monotonic delivery history.

No network or document work occurs here. Configuration and delivery transitions
share one short installation transaction, so Apply cannot race authorization of
an external submission. An expired submission is uncertainty, never permission
to transmit it again.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib
import json
import logging
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
    'provider_observed', 'operator_identity_bound', 'route_assigned', 'route_fallback',
    'sent_together', 'batch_split', 'capacity_wait', 'route_held', 'route_released', 'route_refused',
    'repair_started', 'repair_completed', 'repair_failed', 'plan_allocation', 'route_measured'})
_CATEGORIES = frozenset({'transport_ambiguous', 'response_unusable', 'submission_cancelled',
    'worker_lost', 'artifact_unavailable', 'provider_unavailable', 'preparation_failed',
    'profile_mismatch', 'sid_mismatch', 'provider_failed', 'partner_not_received',
    'partly_sent', 'pages_unconfirmed', 'local_not_delivered', 'notice_missing', 'person_answered',
    'wrong_station'})
# A fax whose pages were only partly confirmed, or whose pages may have arrived without confirmation:
# failed or waiting for a person, never resent automatically (no other route takes it). ``notice_missing``: a
# Direct message the recipient's HISP never confirmed within the wait (digital/direct_message.py).
# ``person_answered``: a person or a voice line answered (sip_calls.PERSON_ANSWERED): calling again on another
# route would ring that person again, so the fax fails and a person checks the number (research N9).
# ``wrong_station``: the number answered as a fax machine Faxbot did not expect there and the station check refused
# it before any page (routing/stations.py): another route would reach the same machine.
NO_FALLBACK_CATEGORIES = frozenset({'partly_sent', 'pages_unconfirmed', 'notice_missing', 'person_answered',
                                    'wrong_station'})
_ROUTE = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}', re.ASCII)


def _valid_actor(actor):
    return isinstance(actor, str) and (actor in {'admin', 'development'} or _ACTOR.fullmatch(actor) is not None)


def _failure_sentences():
    """Provider adapters' own failure sentences, the only reasons history shows."""
    from .documo_service import FAILURE_SENTENCES as DOCUMO_SENTENCES
    from .humblefax_service import FAILURE_SENTENCES
    from .sinch_service import FAILURE_SENTENCES as SINCH_SENTENCES
    return FAILURE_SENTENCES | SINCH_SENTENCES | DOCUMO_SENTENCES


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
                or (name == 'route' and _ROUTE.fullmatch(value) is not None)
                or (name == 'reason' and value in _failure_sentences())
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
    # Faxes sharing this claim's one call, in call order with this claim first;
    # empty for a fax sent on its own. Each keeps its own attempt and lease.
    members: tuple = ()

    @property
    def everyone(self):
        return self.members or (self,)


@dataclass(frozen=True)
class _ObservationRefusal:
    message: str


def _event(connection, events, job_id, kind, now, *, attempt_id=None, details=None, dedupe_key=None):
    connection.execute(events.insert().values(id=uuid4().hex, job_id=job_id,
        attempt_id=attempt_id, kind=kind, dedupe_key=dedupe_key,
        details=json.dumps(details or {}, sort_keys=True, separators=(',', ':')), created_at=now))


def _routes_reach(values):
    """callable(number) -> whether any route of this revision may call ``number`` (``routing.dialing``)."""
    if values is None:
        return None
    from .routing.dialing import reaches
    routes = [values.effective_outbound, *values.outbound_route_providers]
    return lambda number: any(reaches(route, number, values) for route in routes if route)


def accepted_dial(connection, to_number, values):
    """``(alternate or None, approval id or None)`` kept with a fax at acceptance (``routing.alternates``).

    Reads the recipient's approval through the acceptance transaction; any
    failure leaves the fax calling the number the sender entered.
    """
    from .routing import alternates
    try:
        approval = alternates.current(to_number, connection=connection)
        number, approval_id = alternates.dialed_number_for(
            {'to_number': to_number}, facts=alternates.DialFacts(approval, _routes_reach(values)))
    except Exception:
        return None, None
    return (number, approval_id) if number != to_number else (None, None)


def record_acceptance(connection, tables, job_id, *, held, now, to_number=None, values=None):
    """Part of the same transaction as FaxJob and its captured profile binding.

    With ``to_number``, the number the fax dials is decided here, once: an
    approved alternate in force now stays with this fax (migration 0027).
    """
    mode, state = ('held', 'held') if held else ('normal', 'ready')
    deliveries = tables['outbound_deliveries']
    dial = {}
    if to_number and 'alternate_number' in deliveries.c:
        alternate, approval_id = accepted_dial(connection, to_number, values)
        if alternate is not None:
            dial = {'alternate_number': alternate, 'alternate_approval': approval_id}
    connection.execute(deliveries.insert().values(id=job_id,
        dispatch_mode=mode, state=state, version=1, created_at=now, updated_at=now, **dial))
    _event(connection, tables['outbound_events'], job_id, 'accepted', now,
        details={'dispatch_mode': mode})
    if state == 'ready':
        # The idle worker starts at once instead of after its back-off (the worker
        # re-reads after the commit; a check before it simply finds nothing yet).
        from .outbound_wake import wake
        wake.notify()


FALLBACK_LIMIT = 2


class OutboundStore:
    # Optional callable(job_id, attempt_id) -> bool, consulted inside the
    # observing transaction when a submitted attempt definitely fails. True
    # returns the fax to the queue for its next route instead of failing it.
    fallback_policy = None

    def __init__(self, configuration):
        self.configuration = configuration
        self.deliveries = configuration.delivery_tables['outbound_deliveries']
        self.attempts = configuration.delivery_tables['outbound_attempts']
        self.events = configuration.delivery_tables['outbound_events']
        self._batch_tables = None

    def _batching(self, connection):
        """Reflected sending-together tables (``batching.store``), read through the open transaction on first use."""
        if self._batch_tables is None:
            from .batching.store import tables
            self._batch_tables = tables(self.configuration.engine, connection)
        return self._batch_tables

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
            revision, profile = self._attempt_context(connection, row['id'], attempt)
        except (ConfigurationStoreError, ConfigurationSecretError, ConfigurationRecordError, ConfigurationValueError,
                DeliveryConflict):
            revision, profile = None, None
        return attempt, revision, profile

    def _attempt_context(self, connection, job_id, attempt=None):
        """The accepted revision and the provider account this attempt actually uses.

        An attempt uses the fax's accepted provider unless ``assign_route`` bound
        it, before submission, to another route listed in that same revision.
        """
        revision, bound = self.configuration._outbound_context(connection, job_id)
        if attempt is None or attempt['profile_id'] is None or attempt['profile_id'] == bound.id:
            return revision, bound
        head = self.configuration._head(connection)
        if head is None:
            raise DeliveryConflict('Delivery account is unavailable.')
        profile = self.configuration._profile(connection, self.configuration._cipher(), head['installation_id'],
                                              attempt['profile_id'])
        if (profile.configuration.provider_id not in revision.values.outbound_route_providers
                and not self._chosen_account(connection, revision, attempt, profile)):
            raise DeliveryConflict('Delivery route is not permitted by the accepted configuration.')
        return revision, profile

    @staticmethod
    def _chosen_account(connection, revision, attempt, profile):
        """Whether the sending rules gave this attempt an account of the profile's provider in its revision.

        ``assign_route`` records the account (``delivery_rule_choices``) in the same transaction that binds the
        attempt, so results for a rule-chosen account (one not in ``FAX_OUTBOUND_ROUTES``) authenticate.
        """
        from .routing import envelope as envelopes
        if attempt is None or not attempt.get('id'):
            return False
        choice = envelopes.choice_on(connection, attempt['id'])
        if choice is None:
            return False
        from .accounts import account_named
        account = account_named(revision.values, choice['account_key'])
        return (account is not None and account.sends and account.provider == profile.configuration.provider_id)

    def attempt_context(self, job_id, attempt_id):
        """Authenticate the account that issued one attempt; for result authentication."""
        with self.configuration.engine.connect() as connection:
            attempt = connection.execute(sa.select(self.attempts).where(
                self.attempts.c.id == attempt_id)).mappings().one_or_none()
            if attempt is not None and attempt['job_id'] != job_id:
                raise DeliveryConflict('Attempt does not belong to this delivery.')
            return self._attempt_context(connection, job_id, attempt)

    def _bind_refusal(self, connection, row, attempt, profile):
        if row['state'] != 'reconciliation_required':
            return 'This fax is not waiting for confirmation, so it does not need a provider fax ID.'
        if row['dispatch_mode'] == 'legacy':
            return 'This fax was sent by an older Faxbot version; check its status in your provider account.'
        if row['dispatch_mode'] != 'normal':
            return 'This fax was not sent through a provider, so there is no fax ID to attach.'
        if attempt is None or attempt['submitted_at'] is None:
            return 'Faxbot has no record of sending this fax, so there is no fax ID to attach.'
        if attempt['phase'] not in {'uncertain', 'submitting', 'in_progress'} or attempt['completed_at'] is not None:
            return 'This fax already has a final result, so there is no fax ID to attach.'
        if profile is None or profile.id != attempt['profile_id']:
            return 'The provider account that sent this fax is no longer set up; check the fax in that account.'
        job_sid = connection.scalar(sa.select(self.configuration.jobs.c.provider_sid).where(
            self.configuration.jobs.c.id == row['id']))
        if attempt['provider_sid'] is not None or job_sid is not None:
            return 'This fax already has a provider fax ID; use Refresh to update its status.'
        configuration = profile.configuration
        manifest = configuration.manifest
        supported = ('get_status' in manifest.get('actions', {}) if manifest is not None
                     else configuration.provider_id in {'phaxio', 'signalwire', 'sinch', 'documo', 'humblefax', 'efax'})
        if not supported:
            return 'This provider cannot look up fax status; check the fax in your provider account.'
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
                       and profile.configuration.provider_id in {'documo', 'efax'})
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
        # Idle: no status read is due, so no lock. A poll is no lease, so a read that falls due while this
        # looks is simply taken in the next round; which read is due is decided with the time after the lock.
        due = datetime.utcnow() if now is None else now
        if not self._any(sa.select(self.deliveries.c.id).where(
                self.deliveries.c.state.in_(['in_progress', 'reconciliation_required']),
                sa.or_(self.deliveries.c.next_poll_at.is_(None), self.deliveries.c.next_poll_at <= due))):
            return None
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
            _, profile = self._attempt_context(connection, row['id'], {'profile_id': row['profile_id']})
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
        _, profile = self._attempt_context(connection, job_id, attempt)
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
        attempt = connection.execute(sa.select(self.attempts).where(
            self.attempts.c.id == claim.attempt_id)).mappings().one_or_none()
        revision, profile = self._attempt_context(connection, claim.job_id, attempt)
        if attempt is None or profile.id != attempt['profile_id'] or profile.id != claim.profile_id:
            raise DeliveryConflict('Delivery preparation profile does not match.')
        return revision, profile

    def load_dispatch(self, claim):
        """Read private submission inputs only while the preparation lease is held.

        The job also carries the number choice kept at acceptance (``dial``) and
        the number this attempt already recorded (``dialed_number``), if any.
        """
        with self.configuration._locked() as connection:
            revision, profile = self._preparing(connection, claim, datetime.utcnow())
            job = connection.execute(sa.select(self.configuration.jobs).where(
                self.configuration.jobs.c.id == claim.job_id)).mappings().one()
            job = dict(job)
            if 'dialed_number' in self.attempts.c:
                job['dial'] = self._dial_state_on(connection, claim.job_id)
                job['dialed_number'] = connection.scalar(sa.select(self.attempts.c.dialed_number).where(
                    self.attempts.c.id == claim.attempt_id))
            return revision, profile, job

    def _dial_state_on(self, connection, job_id):
        """The alternate kept at acceptance, and whether an earlier attempt to it definitely failed."""
        row = connection.execute(sa.select(self.deliveries.c.alternate_number, self.deliveries.c.alternate_approval)
                                 .where(self.deliveries.c.id == job_id)).first()
        alternate, approval = (row.alternate_number, row.alternate_approval) if row is not None else (None, None)
        refused = False
        if alternate:
            # A definite failure only: the provider said the call failed. Uncertain attempts are never sent again,
            # and pages that may have arrived (partly sent, unconfirmed) carry a category and never count.
            refused = connection.execute(sa.select(self.attempts.c.id).where(
                self.attempts.c.job_id == job_id, self.attempts.c.dialed_number == alternate,
                self.attempts.c.submitted_at.is_not(None), self.attempts.c.phase == 'failed',
                self.attempts.c.error_category.is_(None)).limit(1)).first() is not None
        return {'alternate': alternate, 'approval': approval, 'refused': refused}

    def dial_state(self, job_id):
        """``{'alternate', 'approval', 'refused'}`` for a fax; ``alternate`` is None when it calls its own number."""
        if 'dialed_number' not in self.attempts.c:
            return {'alternate': None, 'approval': None, 'refused': False}
        with self.configuration.engine.connect() as connection:
            return self._dial_state_on(connection, job_id)

    def record_dialed(self, claim, number, approval_id=None, *, now=None):
        """Record the number this attempt dials (every fax in a shared call), before its durable submission marker.

        Allowed only while this worker holds the preparation lease; once the
        attempt is submitted its number can never change. Returns False when
        there is nothing to record against (an older database).
        """
        if 'dialed_number' not in self.attempts.c:
            return False
        if not isinstance(number, str) or not number or len(number) > 32:
            raise ValueError('Invalid dialed number.')
        with self.configuration._locked() as connection:
            now = now or datetime.utcnow()
            for member in claim.everyone:
                row = self._row(connection, member.job_id)
                if (not self._owns(row, member) or row['state'] != 'preparing'
                        or row['claim_expires_at'] is None or row['claim_expires_at'] <= now):
                    raise DeliveryConflict('Delivery preparation lease is no longer current.')
                # ``approval_id`` may map each fax of a shared call to the approval it kept at acceptance.
                approval = approval_id.get(member.job_id) if isinstance(approval_id, dict) else approval_id
                updated = connection.execute(self.attempts.update().where(
                    self.attempts.c.id == member.attempt_id, self.attempts.c.job_id == member.job_id,
                    self.attempts.c.phase == 'preparing', self.attempts.c.submitted_at.is_(None)).values(
                        dialed_number=number, dialed_approval=approval))
                if updated.rowcount != 1:
                    raise DeliveryConflict('Delivery preparation lease is no longer current.')
            return True

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

    def _active_values(self, connection):
        """The active configuration's values, or None before one exists.

        A revision never changes, so its values (key file and decryption) are read once per revision.
        """
        configuration = self.configuration
        head = configuration._head(connection)
        if head is None:
            return None
        revision_id = head['active_revision_id']
        cached = getattr(self, '_values_for', None)
        if cached is not None and cached[0] == revision_id:
            return cached[1]
        values = configuration._revision(connection, configuration._cipher(), head['installation_id'],
                                         revision_id).values
        self._values_for = (revision_id, values)
        return values

    def _enabled(self, connection):
        values = self._active_values(connection)
        return values is not None and not values.fax_disabled

    def capacity(self, connection=None):
        """Room for calls (``capacity.py``), or None before the tables it reads exist.

        Inside the claim's locked transaction pass its ``connection`` so the first
        reflection reads through it and opens no second connection.
        """
        if getattr(self, '_capacity', None) is None:
            try:
                from .capacity import for_engine
                self._capacity = for_engine(self.configuration.engine, connection)
            except Exception:
                return None
        return self._capacity

    def _any(self, query):
        """True when ``query`` finds a row; a plain read, never the write lock an idle check would queue others behind."""
        try:
            with self.configuration.engine.connect() as connection:
                return connection.execute(query.limit(1)).first() is not None
        except sa.exc.SQLAlchemyError:
            from .config_store import ConfigurationStoreError
            raise ConfigurationStoreError('Configuration transaction could not complete.') from None

    def _update(self, connection, row, now, **changes):
        connection.execute(self.deliveries.update().where(self.deliveries.c.id == row['id']).values(
            **changes, version=row['version'] + 1, updated_at=now))
        if changes.get('state') in ('ready', 'in_progress', 'reconciliation_required'):
            # New work for the worker (a fax to send again) or the poller (a status to read).
            from .outbound_wake import wake
            wake.notify()

    def claim(self, owner, *, now=None, lease_seconds=30, exclude=()):
        """Claim the next fax that may start a call now; ``exclude`` names faxes the worker is pausing.

        Only a fax whose number, and trunk, have room is claimed (``capacity.py``);
        the others wait in ``ready`` and never fail for it.
        """
        if not isinstance(owner, str) or not owner or len(owner) > 40 or not 1 <= lease_seconds <= 300:
            raise ValueError('Invalid delivery worker claim.')
        # Idle: no fax is ready (alone or waiting for others), so no lock and no configuration read. A fax that
        # sending rules hold is not ready to go, so an installation with only held faxes stays idle too.
        ready = sa.select(self.deliveries.c.id).where(self.deliveries.c.state == 'ready')
        gate = self._hold_gate(datetime.utcnow() if now is None else now)
        if gate is not None:
            ready = ready.where(self.deliveries.c.id.not_in(gate))
        if not self._any(ready):
            return None
        costs = self._call_costs()
        with self.configuration._locked() as connection:
            now = datetime.utcnow() if now is None else now
            values = self._active_values(connection)
            if values is None or values.fax_disabled:
                return None
            capacity = self.capacity(connection)
            together = self._claim_together_on(connection, owner, now, lease_seconds, capacity=capacity,
                                               values=values, costs=costs)
            if together is not None:
                return together
            from .batching.store import waiting_ids
            # A fax waiting to go with others is claimed only with its group; a fax its sending rules hold
            # (approval, a time window not yet open, or no allowed route) is not claimed at all.
            waiting = waiting_ids(self._batching(connection))
            gate = self._hold_gate(now, connection)
            if gate is not None:
                waiting = sa.union_all(waiting, gate)
            if capacity is not None:
                row = capacity.next_ready(connection, values, now, waiting=waiting, exclude=exclude)
            else:
                query = sa.select(self.deliveries).where(
                    self.deliveries.c.state == 'ready', self.deliveries.c.dispatch_mode == 'normal',
                    self.deliveries.c.id.not_in(waiting))
                if exclude:
                    query = query.where(self.deliveries.c.id.not_in(list(exclude)))
                row = connection.execute(query.order_by(self.deliveries.c.created_at, self.deliveries.c.id)
                                         .limit(1)).mappings().one_or_none()
            if row is None:
                return None
            return self._claim_row_on(connection, row, owner, now, lease_seconds)

    def _hold_gate(self, now, connection=None):
        """Faxes held by sending rules (``routing.holds.blocking``), or None before the holds table exists."""
        tables = getattr(self, '_rule_tables', None)
        if tables is None:
            from .routing import envelope as envelopes
            try:
                if connection is not None:
                    tables = envelopes.tables(connection)
                else:
                    with self.configuration.engine.connect() as own:
                        tables = envelopes.tables(own)
            except sa.exc.SQLAlchemyError:
                tables = None
            if tables is None:
                return None
            self._rule_tables = tables
        from .routing.holds import blocking
        return blocking(tables, now)

    def _claim_row_on(self, connection, row, owner, now, lease_seconds):
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

    def _call_costs(self):
        """{number: cost(pages)} for faxes waiting to go together (``batching.store.call_costs``), read before the
        claim's write lock with plain reads: the predictor reads through connections of its own, which must never
        happen while the claim holds the lock. Empty when nothing waits to go together."""
        from .batching import store as batching
        try:
            with self.configuration.engine.connect() as connection:
                numbers = batching.waiting_numbers(connection, self._batching(connection))
                values = self._active_values(connection) if numbers else None
        except sa.exc.SQLAlchemyError:
            from .config_store import ConfigurationStoreError
            raise ConfigurationStoreError('Configuration transaction could not complete.') from None
        if not numbers or values is None or values.fax_disabled:
            return {}
        return batching.call_costs(self.configuration.engine, values, numbers)

    def _claim_together_on(self, connection, owner, now, lease_seconds, *, capacity=None, values=None, costs=None):
        """Claim the first due group of waiting faxes as one call; a group of one goes on its own."""
        from .batching import store as batching
        t = self._batching(connection)
        # A fax its sending rules hold never waits in a group: acceptance can decide a hold after the preview
        # put it there (a rule published in between), and a group claim must not send it before its release.
        gate = self._hold_gate(now, connection)
        if gate is not None:
            members = t['outbound_batch_members']
            for job_id in connection.execute(sa.select(members.c.id).where(
                    members.c.state == 'waiting', members.c.id.in_(gate))).scalars().all():
                batching.separate_on(connection, t, job_id, now)
        group = batching.due_group_on(connection, t, now, values=values, costs=costs)
        if group is None:
            return None
        if capacity is not None and values is not None and not capacity.group_may_start(connection, values, group, now):
            return None  # the group waits, still together, until its number (and the trunk) have room
        claims, joined = [], []
        for member in group:
            claim = self._claim_row_on(connection, self._row(connection, member['id']), owner, now, lease_seconds)
            if claim is None:
                batching.separate_on(connection, t, member['id'], now)
                continue
            claims.append(claim)
            joined.append(member)
        if len(claims) <= 1:
            for claim in claims:
                batching.separate_on(connection, t, claim.job_id, now)
            return claims[0] if claims else None
        batching.join_on(connection, t, claims, joined, now)
        for claim in claims:
            _event(connection, self.events, claim.job_id, 'sent_together', now, attempt_id=claim.attempt_id)
        return replace(claims[0], members=tuple(claims))

    def defer(self, claim, *, now=None):
        """Give a claimed fax back to the queue before anything was sent: there is no room for its call yet.

        Nothing fails and nothing is sent; a shared call's faxes wait again, still together.
        """
        from .batching.store import return_to_waiting_on
        now = now or datetime.utcnow()
        with self.configuration._locked() as connection:
            for member in claim.everyone:
                row = self._row(connection, member.job_id)
                if not self._owns(row, member) or row['state'] != 'preparing':
                    continue
                connection.execute(self.attempts.update().where(self.attempts.c.id == member.attempt_id).values(
                    phase='abandoned', completed_at=now))
                self._update(connection, row, now, state='ready', claim_owner=None, claim_token=None,
                             claim_expires_at=None)
                _event(connection, self.events, member.job_id, 'capacity_wait', now, attempt_id=member.attempt_id)
                if claim.members:
                    return_to_waiting_on(connection, self._batching(connection), member.job_id, member.attempt_id, now)

    def split_batch(self, claim, *, separate=None, now=None):
        """Undo a shared call before anything was sent; no fax is failed here.

        ``separate`` names the faxes that go on their own from now on (None:
        every fax); the others wait again and form a new call at once.
        """
        from .batching import store as batching
        now = now or datetime.utcnow()
        with self.configuration._locked() as connection:
            t = self._batching(connection)
            for member in claim.everyone:
                row = self._row(connection, member.job_id)
                if not self._owns(row, member) or row['state'] != 'preparing':
                    continue
                connection.execute(self.attempts.update().where(self.attempts.c.id == member.attempt_id).values(
                    phase='abandoned', completed_at=now))
                self._update(connection, row, now, state='ready', claim_owner=None, claim_token=None,
                             claim_expires_at=None)
                _event(connection, self.events, member.job_id, 'batch_split', now, attempt_id=member.attempt_id)
                if separate is None or member.job_id in separate:
                    batching.separate_on(connection, t, member.job_id, now)
                else:
                    batching.return_to_waiting_on(connection, t, member.job_id, member.attempt_id, now)

    def urge(self, job_id, *, now=None):
        """Mark a waiting fax "Send now": it and the faxes waiting with it go at the next claim."""
        from .batching.store import urge_on
        with self.configuration._locked() as connection:
            return urge_on(connection, self._batching(connection), job_id, now or datetime.utcnow())

    @staticmethod
    def _owns(row, claim):
        return row is not None and row['attempt_id'] == claim.attempt_id and row['claim_owner'] == claim.owner and row['claim_token'] == claim.token

    def begin_submission(self, claim, *, now=None):
        """The durable marker; for a shared call, every fax in it at once or none."""
        with self.configuration._locked() as connection:
            now = datetime.utcnow() if now is None else now
            everyone = claim.everyone
            rows = [self._row(connection, member.job_id) for member in everyone]
            if any(not self._owns(row, member) or row['state'] != 'preparing'
                   or row['claim_expires_at'] is None or row['claim_expires_at'] <= now
                   for row, member in zip(rows, everyone)):
                return False
            if not self._enabled(connection):
                from .batching.store import return_to_waiting_on
                for row, member in zip(rows, everyone):
                    connection.execute(self.attempts.update().where(self.attempts.c.id == member.attempt_id).values(
                        phase='abandoned', completed_at=now))
                    self._update(connection, row, now, state='ready', claim_owner=None, claim_token=None,
                                 claim_expires_at=None)
                    _event(connection, self.events, member.job_id, 'dispatch_paused', now, attempt_id=member.attempt_id)
                    if claim.members:
                        return_to_waiting_on(connection, self._batching(connection), member.job_id, member.attempt_id, now)
                return False
            refused = next((reason for reason in (self._route_permitted_on(connection, member) for member in everyone)
                            if reason is not None), None)
            if refused is not None:
                self._refuse_submission_on(connection, claim, rows, refused, now)
                return False
            for row, member in zip(rows, everyone):
                self._update(connection, row, now, state='submitting')
                connection.execute(self.attempts.update().where(self.attempts.c.id == member.attempt_id).values(
                    phase='submitting', submitted_at=now))
                _event(connection, self.events, member.job_id, 'submission_authorized', now,
                       attempt_id=member.attempt_id)
            return True

    def _refuse_submission_on(self, connection, claim, rows, reason, now):
        """Give back a claim the last check refused: every fax in it waits in Sent with ``reason``; nothing is sent."""
        from .routing import envelope as envelopes, holds
        from .batching.store import separate_on
        t = envelopes.tables(connection)
        for row, member in zip(rows, claim.everyone):
            connection.execute(self.attempts.update().where(self.attempts.c.id == member.attempt_id).values(
                phase='abandoned', completed_at=now))
            self._update(connection, row, now, state='ready', claim_owner=None, claim_token=None,
                         claim_expires_at=None)
            if t is not None and not holds.open_on(connection, t, member.job_id):
                holds.create_on(connection, t, job_id=member.job_id, kind='no_route', decision_id=None, now=now,
                                reason=reason)
            _event(connection, self.events, member.job_id, 'route_held', now, attempt_id=member.attempt_id)
            separate_on(connection, self._batching(connection), member.job_id, now)

    def record_uncertain(self, claim, *, category='transport_ambiguous', now=None):
        if category not in {'transport_ambiguous', 'response_unusable', 'submission_cancelled', 'worker_lost'}:
            raise ValueError('Invalid delivery uncertainty category.')
        now = now or datetime.utcnow()
        shared = bool(claim.members)
        with self.configuration._locked() as connection:
            changed = False
            for member in claim.everyone:
                row = self._row(connection, member.job_id)
                if not self._owns(row, member):
                    if shared:
                        continue
                    raise DeliveryConflict('Submission failure belongs to an obsolete attempt.')
                if row['state'] in TERMINAL:
                    continue
                if row['state'] not in {'submitting', 'reconciliation_required'}:
                    if shared:
                        continue
                    raise DeliveryConflict('No external submission was authorized for this attempt.')
                self._update(connection, row, now, state='reconciliation_required', claim_expires_at=None)
                connection.execute(self.attempts.update().where(self.attempts.c.id == member.attempt_id).values(
                    phase='uncertain', error_category=category))
                _event(connection, self.events, member.job_id, 'submission_uncertain', now,
                    attempt_id=member.attempt_id, details={'category': category})
                changed = True
            return changed

    def fail_preparation(self, claim, *, category, now=None):
        if category not in {'artifact_unavailable', 'provider_unavailable', 'preparation_failed'}:
            raise ValueError('Invalid delivery preparation category.')
        now = now or datetime.utcnow()
        if claim.members:
            # Nothing was sent: every fax in the shared call goes on its own instead.
            return self.split_batch(claim, now=now)
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

    def renew_lease(self, claim, *, lease_seconds=30, now=None):
        """Extend a preparing claim's lease while its worker is still preparing it (the worker's heartbeat).

        Only the worker that holds the claim may extend it (``_owns``), only while every fax in it is still
        ``preparing`` and its lease has not run out yet: a lease that already ran out stays lost, so a fax another
        worker recovered is never taken back. True when every fax's lease was extended."""
        if not isinstance(lease_seconds, (int, float)) or not 1 <= lease_seconds <= 300:
            raise ValueError('Invalid lease length.')
        with self.configuration._locked() as connection:
            now = datetime.utcnow() if now is None else now
            rows = [(member, self._row(connection, member.job_id)) for member in claim.everyone]
            if any(not self._owns(row, member) or row['state'] != 'preparing' or row['claim_expires_at'] is None
                   or row['claim_expires_at'] <= now for member, row in rows):
                return False
            expiry = now + timedelta(seconds=lease_seconds)
            for member, row in rows:
                connection.execute(self.deliveries.update().where(
                    self.deliveries.c.id == member.job_id, self.deliveries.c.claim_token == member.token).values(
                        claim_expires_at=max(expiry, row['claim_expires_at'])))
            return True

    def recover_expired(self, *, now=None):
        # Idle: no fax holds a lease, so no lock. Whether a lease has run out is decided only with the time read
        # after the lock, so a lease that ends while this waits for the lock is recovered in this round.
        if not self._any(sa.select(self.deliveries.c.id).where(
                self.deliveries.c.state.in_(['preparing', 'submitting']))):
            return 0
        with self.configuration._locked() as connection:
            now = datetime.utcnow() if now is None else now
            rows = connection.execute(sa.select(self.deliveries).where(
                self.deliveries.c.state.in_(['preparing', 'submitting']), self.deliveries.c.claim_expires_at <= now)).mappings().all()
            for row in rows:
                preparing = row['state'] == 'preparing'
                if preparing:
                    # Said once in the log: preparation outlived its lease, so this attempt is given up (nothing was
                    # sent) and the fax is claimed again.
                    started = connection.scalar(sa.select(self.attempts.c.created_at).where(
                        self.attempts.c.id == row['attempt_id']))
                    spent = int((now - started).total_seconds()) if started is not None else None
                    logging.getLogger(__name__).warning(
                        'Fax %s: attempt %s was given up because its preparation outlived its lease (%s seconds); '
                        'nothing was sent, and the fax is claimed again.', row['id'], row['attempt_id'],
                        spent if spent is not None else 'unknown')
                changes = {'state': 'ready' if preparing else 'reconciliation_required', 'claim_expires_at': None}
                if preparing:
                    changes.update(claim_owner=None, claim_token=None)
                self._update(connection, row, now, **changes)
                connection.execute(self.attempts.update().where(self.attempts.c.id == row['attempt_id']).values(
                    phase='abandoned' if preparing else 'uncertain', error_category='worker_lost',
                    completed_at=now if preparing else None))
                _event(connection, self.events, row['id'], 'preparation_expired' if preparing else 'submission_uncertain', now,
                    attempt_id=row['attempt_id'])
                if preparing:
                    # A shared call that was never placed: its faxes wait again and form a new call.
                    from .batching.store import return_to_waiting_on
                    return_to_waiting_on(connection, self._batching(connection), row['id'], row['attempt_id'], now)
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

    def _observe(self, connection, row, *, attempt_id, profile_id, provider_sid, status, now, event_key=None,
                 error=None, error_category=None, before_data=None):
        if status not in OBSERVED:
            raise DeliveryConflict('Provider status requires reconciliation.')
        if error_category is not None and (error_category not in NO_FALLBACK_CATEGORIES or status != 'failed'):
            raise DeliveryConflict('Provider failure category requires reconciliation.')
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
        values = dict(phase=status, provider_sid=final_sid, error_category=error_category,
                      completed_at=now if status in TERMINAL else None)
        if status == 'failed' and 'ended_before_data' in self.attempts.c:
            # What the provider or engine said about the call: it ended before any fax data (1), after (0), or it
            # did not say (NULL). A fax whose route a rule chose falls back only after 1 (owner's answer Q3).
            values['ended_before_data'] = None if before_data is None else int(bool(before_data))
        connection.execute(self.attempts.update().where(self.attempts.c.id == attempt_id).values(**values))
        # A categorized failure (part of the fax may have arrived) waits for a person, never another route.
        if status == 'failed' and error_category is None and self._fallback_due(
                connection, row, attempt_id, before_data=before_data):
            # The next route takes over in this same transaction, so the fax
            # never reads as failed while another route remains.
            self._update(connection, row, now, state='ready', attempt_id=None, claim_owner=None,
                         claim_token=None, claim_expires_at=None, next_poll_at=None)
            connection.execute(self.configuration.jobs.update().where(self.configuration.jobs.c.id == row['id']).values(
                status='queued', provider_sid=final_sid, error=None, updated_at=now))
            # The adapter's plain sentence for why this route failed, kept with the move to the next route.
            _event(connection, self.events, row['id'], 'route_fallback', now, attempt_id=attempt_id,
                   details={'category': 'provider_failed', **({'reason': error} if isinstance(error, str) else {})})
            return True
        if status == 'failed' and error_category is None and before_data is True and self._hold_after_predata(
                connection, row, attempt_id, now, error=error):
            # No allowed try is left (the fallback limit, or no other account), and nothing was sent: the fax
            # waits in Sent with its sentence instead of failing (owner's answer Q1).
            connection.execute(self.configuration.jobs.update().where(self.configuration.jobs.c.id == row['id']).values(
                status='queued', provider_sid=final_sid, error=None, updated_at=now))
            return True
        self._update(connection, row, now, state=status, claim_expires_at=None)
        connection.execute(self.configuration.jobs.update().where(self.configuration.jobs.c.id == row['id']).values(
            status=status, provider_sid=final_sid, error=error if status == 'failed' else None, updated_at=now))
        return True

    def record_receipt(self, claim, *, provider_sid, status, now=None, error=None):
        """``error``: the adapter's plain sentence when the provider refused the fax at once.

        A fax refused when it was handed over never reached a fax machine: it ended before any fax data.
        """
        now = now or datetime.utcnow()
        with self.configuration._locked() as connection:
            results = []
            for member in claim.everyone:
                row = self._row(connection, member.job_id)
                if not self._owns(row, member):
                    raise DeliveryConflict('Submission acknowledgement belongs to an obsolete attempt.')
                profile_id = member.profile_id
                attempt_profile = connection.scalar(sa.select(self.attempts.c.profile_id).where(
                    self.attempts.c.id == member.attempt_id))
                if attempt_profile != profile_id:
                    # Only assign_route changes an attempt away from the fax's accepted
                    # account. The worker's original claim then reports for that route.
                    _, accepted = self.configuration._outbound_context(connection, member.job_id)
                    if profile_id == accepted.id:
                        profile_id = attempt_profile
                # A shared SIP call identifies each fax by its own job, as a single SIP fax does.
                results.append(self._observe(connection, row, attempt_id=member.attempt_id, profile_id=profile_id,
                    provider_sid=member.job_id if claim.members else provider_sid, status=status, now=now,
                    error=error if status == 'failed' else None,
                    before_data=True if status == 'failed' and not claim.members else None))
        for result in results:
            if isinstance(result, _ObservationRefusal):
                raise DeliveryConflict(result.message)
        return results[0]

    def observe(self, job_id, *, attempt_id, profile_id, provider_sid, status, event_key, now=None, error=None,
                error_category=None, before_data=None):
        """Call only after the transport owner authenticates the bounded event.

        ``error`` is one plain sentence shown with a final failure, never provider text.
        ``error_category`` (``partly_sent``) marks a failure that waits for a person: no route fallback.
        ``before_data``: the provider's or engine's own classification of a failure, True when the call ended
        before any fax data (busy, no answer, no fax machine), False when data was exchanged, None when it did
        not say. Only True lets a fax whose route a rule chose go to its next account.
        """
        if not isinstance(event_key, str) or not event_key or len(event_key) > 512:
            raise DeliveryConflict('Invalid provider event identity.')
        with self.configuration._locked() as connection:
            result = self._observe(connection, self._row(connection, job_id), attempt_id=attempt_id,
                profile_id=profile_id, provider_sid=provider_sid, status=status,
                event_key=event_key, now=now or datetime.utcnow(), error=error, error_category=error_category,
                before_data=before_data)
        if isinstance(result, _ObservationRefusal):
            raise DeliveryConflict(result.message)
        return result

    def record_unconfirmed(self, job_id, *, attempt_id, profile_id, event_key, category='pages_unconfirmed',
                           now=None):
        """A shared call ended without a confirmed page count, or a Direct message's notice never came
        (``notice_missing``): this fax may have arrived.

        The fax waits for a person, exactly like an unacknowledged submission;
        nothing is sent again. A repeated report has no further effect.
        """
        if category not in ('pages_unconfirmed', 'notice_missing') or not isinstance(event_key, str) or not event_key \
                or len(event_key) > 512:
            raise DeliveryConflict('Invalid provider event identity.')
        now = now or datetime.utcnow()
        with self.configuration._locked() as connection:
            row = self._row(connection, job_id)
            attempt = connection.execute(sa.select(self.attempts).where(
                self.attempts.c.id == attempt_id)).mappings().one_or_none()
            if (row is None or row['attempt_id'] != attempt_id or attempt is None or attempt['job_id'] != job_id
                    or attempt['submitted_at'] is None or attempt['profile_id'] != profile_id):
                raise DeliveryConflict('Provider observation does not match a submitted attempt.')
            dedupe = hashlib.sha256(event_key.encode()).hexdigest()
            if connection.execute(sa.select(self.events.c.id).where(
                    self.events.c.job_id == job_id, self.events.c.dedupe_key == dedupe)).first():
                return False
            if row['state'] in TERMINAL:
                _event(connection, self.events, job_id, 'late_observation', now, attempt_id=attempt_id,
                       details={'category': category}, dedupe_key=dedupe)
                return False
            self._update(connection, row, now, state='reconciliation_required', claim_expires_at=None)
            connection.execute(self.attempts.update().where(self.attempts.c.id == attempt_id).values(
                phase='uncertain', error_category=category))
            _event(connection, self.events, job_id, 'submission_uncertain', now, attempt_id=attempt_id,
                   details={'category': category}, dedupe_key=dedupe)
            return True

    def _strict_blocks(self, connection, job_id, before_data):
        """Whether the fax's rules chose its route and the failure is not known to have ended before any fax data.

        Today's fallback for faxes no rule decides is unchanged (owner's answer Q3); an unreadable decision
        allows no fallback.
        """
        from .routing import envelope as envelopes
        try:
            pinned = envelopes.load_on(connection, job_id)
        except envelopes.UnreadableDecision:
            return True
        return pinned is not None and pinned.strict and before_data is not True

    def _hold_after_predata(self, connection, row, attempt_id, now, *, error=None):
        """Hold, instead of failing, a fax whose rules chose its route after a call that ended before any fax data
        when no allowed try is left; False for any other fax. Detaches the attempt, as a fallback does, so a late
        result for it cannot move the fax."""
        from .routing import envelope as envelopes, holds
        if row['dispatch_mode'] != 'normal':
            return False
        try:
            pinned = envelopes.load_on(connection, row['id'])
        except envelopes.UnreadableDecision:
            return False
        t = envelopes.tables(connection) if pinned is not None and pinned.strict else None
        if t is None:
            return False
        from .accounts import sending_accounts
        revision, _ = self.configuration._outbound_context(connection, row['id'])
        self._update(connection, row, now, state='ready', attempt_id=None, claim_owner=None, claim_token=None,
                     claim_expires_at=None, next_poll_at=None)
        holds.hold_after_predata_on(connection, t, job_id=row['id'], pinned=pinned,
                                    accounts=sending_accounts(revision.values), now=now)
        from .batching.store import separate_on
        separate_on(connection, self._batching(connection), row['id'], now)  # a held fax never waits in a group
        _event(connection, self.events, row['id'], 'route_held', now, attempt_id=attempt_id,
               details={'category': 'provider_failed', **({'reason': error} if isinstance(error, str) else {})})
        return True

    def _fallback_due(self, connection, row, attempt_id, *, before_data=None):
        """Ask the installed route policy, within the fallback limit, whether another route remains."""
        policy = type(self).fallback_policy
        if policy is None or row['dispatch_mode'] != 'normal':
            return False
        if self._strict_blocks(connection, row['id'], before_data):
            return False
        used = connection.scalar(sa.select(sa.func.count()).select_from(self.events).where(
            self.events.c.job_id == row['id'], self.events.c.kind == 'route_fallback'))
        if used >= FALLBACK_LIMIT or not self._enabled(connection):
            return False
        try:
            return bool(policy(row['id'], attempt_id))
        except Exception:
            return False  # Without a usable answer the failure stands.

    def _route_profile_on(self, connection, configuration):
        """Reuse an identical stored provider account, or store this one."""
        from .config_profiles import ConfigurationRecordError
        from .config_secrets import ConfigurationSecretError
        store = self.configuration
        installation = store._head(connection)['installation_id']
        cipher = store._cipher()
        candidates = connection.execute(sa.select(store.profiles.c.id).where(
            store.profiles.c.provider_id == configuration.provider_id).order_by(
            store.profiles.c.created_at.desc()).limit(50)).scalars().all()
        for identity in candidates:
            try:
                if store._profile(connection, cipher, installation, identity).configuration == configuration:
                    return identity
            except (ConfigurationSecretError, ConfigurationRecordError):
                continue
        return store._select_profiles(connection, cipher, installation, {'outbound': configuration}, ())[0][1]

    def assign_route(self, claim, configuration, *, account_key=None, choice=None, now=None):
        """Bind a preparing attempt to one account its accepted revision, and its sending rules, permit.

        Allowed only while this worker holds the preparation lease, before the
        durable submission marker. Results for the attempt are then accepted
        only from that account. ``account_key`` names the account (the provider
        id for the first account of a provider). A fax with a routing decision
        may use only an account its envelope allows (``accounts.account_permitted``)
        and that is on now; ``choice`` (``{'place', 'skipped', 'unreliable'}``)
        is recorded with the account in the same transaction. Returns the claim
        to prepare with.
        """
        from .config_profiles import ProviderConfiguration
        from .routing import envelope as envelopes
        if not isinstance(configuration, ProviderConfiguration):
            raise ValueError('Invalid delivery route.')
        key = account_key or configuration.provider_id
        with self.configuration._locked() as connection:
            now = now or datetime.utcnow()
            row = self._row(connection, claim.job_id)
            if (not self._owns(row, claim) or row['state'] != 'preparing'
                    or row['claim_expires_at'] is None or row['claim_expires_at'] <= now):
                raise DeliveryConflict('Delivery preparation lease is no longer current.')
            revision, bound = self.configuration._outbound_context(connection, claim.job_id)
            try:
                pinned = envelopes.load_on(connection, claim.job_id)
            except envelopes.UnreadableDecision:
                raise DeliveryConflict('Delivery route is not permitted by the accepted configuration.') from None
            if pinned is not None:
                from .accounts import account_enabled, account_named, account_permitted
                account = account_named(revision.values, key)
                current = self._active_values(connection)
                if (account is None or account.provider != configuration.provider_id
                        or not account_permitted(revision, key, pinned.envelope)
                        or (current is not None and not account_enabled(current, key))):
                    raise DeliveryConflict("Delivery route is not permitted by the fax's sending rules.")
                profile_id = bound.id if configuration == bound.configuration else \
                    self._route_profile_on(connection, configuration)
            elif configuration == bound.configuration:
                profile_id = bound.id
            elif configuration.provider_id in revision.values.outbound_route_providers:
                profile_id = self._route_profile_on(connection, configuration)
            else:
                raise DeliveryConflict('Delivery route is not permitted by the accepted configuration.')
            updated = connection.execute(self.attempts.update().where(
                self.attempts.c.id == claim.attempt_id, self.attempts.c.job_id == claim.job_id,
                self.attempts.c.phase == 'preparing', self.attempts.c.submitted_at.is_(None)).values(
                    profile_id=profile_id))
            if updated.rowcount != 1:
                raise DeliveryConflict('Delivery preparation lease is no longer current.')
            if pinned is not None:
                choice = choice or {}
                envelopes.record_choice_on(connection, attempt_id=claim.attempt_id, job_id=claim.job_id,
                                           pinned=pinned, account_key=key, place=choice.get('place', 0),
                                           skipped=choice.get('skipped', ()), unreliable=choice.get('unreliable', ()),
                                           now=now)
            _event(connection, self.events, claim.job_id, 'route_assigned', now, attempt_id=claim.attempt_id,
                   details={'route': key if _ROUTE.fullmatch(key) else configuration.provider_id})
            return replace(claim, profile_id=profile_id)

    def record_route_choice(self, claim, *, account_key, place=0, skipped=(), unreliable=(), now=None):
        """Record the route an attempt was given (its account, ``local``, ``direct`` or a partner relay), before its
        durable submission marker. Only for a fax with a routing decision; a second record keeps the first."""
        from .routing import envelope as envelopes
        with self.configuration._locked() as connection:
            now = now or datetime.utcnow()
            recorded = False
            for member in claim.everyone:
                row = self._row(connection, member.job_id)
                if not self._owns(row, member) or row['state'] != 'preparing':
                    raise DeliveryConflict('Delivery preparation lease is no longer current.')
                pinned = envelopes.load_on(connection, member.job_id)
                if pinned is None:
                    continue
                if not pinned.allows(account_key):
                    raise DeliveryConflict("Delivery route is not permitted by the fax's sending rules.")
                recorded |= envelopes.record_choice_on(connection, attempt_id=member.attempt_id, job_id=member.job_id,
                                                       pinned=pinned, account_key=account_key, place=place,
                                                       skipped=skipped, unreliable=unreliable, now=now)
            return recorded

    def hold_no_route(self, claim, *, reason, skipped=(), now=None):
        """Nothing the fax's rules allow can take it now (owner's answer Q1): give the claim back, keep the fax
        ``ready`` and hold it in Sent with ``reason``. Nothing was sent, nothing fails, and the claim never offers
        it until someone checks again, sends it anyway, or refuses it."""
        from .routing import envelope as envelopes, holds
        from .batching.store import separate_on
        with self.configuration._locked() as connection:
            now = now or datetime.utcnow()
            t = envelopes.tables(connection)
            held = False
            for member in claim.everyone:
                row = self._row(connection, member.job_id)
                if not self._owns(row, member) or row['state'] != 'preparing':
                    continue
                connection.execute(self.attempts.update().where(self.attempts.c.id == member.attempt_id).values(
                    phase='abandoned', completed_at=now))
                self._update(connection, row, now, state='ready', claim_owner=None, claim_token=None,
                             claim_expires_at=None)
                if t is not None:
                    try:
                        pinned = envelopes.load_on(connection, member.job_id)
                    except envelopes.UnreadableDecision:
                        pinned = None
                    if skipped and pinned is not None:
                        # What was skipped, for "send anyway": recorded against the abandoned attempt.
                        envelopes.record_choice_on(connection, attempt_id=member.attempt_id, job_id=member.job_id,
                                                   pinned=pinned, account_key='none', place=0, skipped=skipped,
                                                   now=now)
                    if not holds.open_on(connection, t, member.job_id):
                        holds.create_on(connection, t, job_id=member.job_id, kind='no_route',
                                        decision_id=pinned.decision_id if pinned else None, now=now, reason=reason)
                _event(connection, self.events, member.job_id, 'route_held', now, attempt_id=member.attempt_id)
                # Nothing waits to go together with a fax that is held.
                separate_on(connection, self._batching(connection), member.job_id, now)
                held = True
            return held

    def _route_permitted_on(self, connection, member):
        """The last check before anything leaves Faxbot: a fax whose rules exclude its own (bound) account may go
        only by a route recorded for this attempt that its rules allow. None when it may go, else the reason."""
        from .routing import envelope as envelopes
        try:
            pinned = envelopes.load_on(connection, member.job_id)
        except envelopes.UnreadableDecision:
            return 'Faxbot could not read the routing decision for this fax, so nothing was sent. It waits for you in Sent.'
        if pinned is None:
            return None
        from .accounts import default_sending_key
        revision, bound = self.configuration._outbound_context(connection, member.job_id)
        if pinned.allows(default_sending_key(revision.values)):
            return None
        choice = envelopes.choice_on(connection, member.attempt_id)
        profile_id = connection.scalar(sa.select(self.attempts.c.profile_id).where(
            self.attempts.c.id == member.attempt_id))
        no_call = choice is not None and (choice['account_key'] in ('local', 'direct')
                                          or envelopes.is_relay(choice['account_key'])
                                          or envelopes.is_digital(choice['account_key']))
        if choice is None or not pinned.allows(choice['account_key']) or (not no_call and profile_id == bound.id):
            return ('Faxbot could not confirm that this fax was going by a route your rules allow, so nothing was '
                    'sent. It waits for you in Sent.')
        return None

    # A call that broke part way, completed through an enrolled partner (direct/repair.py) -----------------------------

    def _repair_attempt_on(self, connection, job_id, *, broken_attempt_id, attempt_id, sent, now):
        """The repair's own attempt, written once beside the broken one (which is never changed); its row."""
        attempt = connection.execute(sa.select(self.attempts).where(
            self.attempts.c.id == attempt_id)).mappings().one_or_none()
        if attempt is not None:
            if attempt['job_id'] != job_id:
                raise DeliveryConflict('This repair belongs to another fax.')
            return dict(attempt)
        row = self._row(connection, job_id)
        broken = connection.execute(sa.select(self.attempts).where(
            self.attempts.c.id == broken_attempt_id)).mappings().one_or_none()
        if (row is None or row['state'] != 'failed' or row['attempt_id'] != broken_attempt_id or broken is None
                or broken['job_id'] != job_id or broken['error_category'] != 'partly_sent'):
            raise DeliveryConflict('Only a fax whose call broke part way can be completed through a partner.')
        sequence = connection.scalar(sa.select(sa.func.max(self.attempts.c.sequence)).where(
            self.attempts.c.job_id == job_id)) or 0
        # Pages that go directly are submitted now; a partner that already holds every page needs nothing sent,
        # so that attempt is never priced as a call.
        values = dict(id=attempt_id, job_id=job_id, sequence=sequence + 1, profile_id=broken['profile_id'],
                      phase='in_progress', created_at=now, submitted_at=now if sent else None)
        connection.execute(self.attempts.insert().values(**values))
        if sent:
            _event(connection, self.events, job_id, 'repair_started', now, attempt_id=attempt_id)
        return values

    def begin_repair(self, job_id, *, broken_attempt_id, attempt_id, now=None):
        """Write the attempt that sends only the pages a broken call left out, directly to the enrolled partner.

        Only for a fax that failed part way through its call (``partly_sent``) and is still on that attempt;
        raises DeliveryConflict otherwise. The broken attempt is kept as it was, and the fax stays failed until
        the partner accepts the pages (``complete_repair``) or says it did not (``fail_repair``).
        """
        with self.configuration._locked() as connection:
            now = now or datetime.utcnow()
            self._repair_attempt_on(connection, job_id, broken_attempt_id=broken_attempt_id, attempt_id=attempt_id,
                                    sent=True, now=now)

    def complete_repair(self, job_id, *, broken_attempt_id, attempt_id, sent=True, now=None):
        """The partner holds the whole fax: the repair's attempt succeeded and the fax is delivered.

        ``sent`` False: the partner already held every page, so nothing went again. Idempotent; False when the
        fax was already completed, or has moved on (someone sent it again), which it then leaves alone.
        """
        with self.configuration._locked() as connection:
            now = now or datetime.utcnow()
            attempt = self._repair_attempt_on(connection, job_id, broken_attempt_id=broken_attempt_id,
                                              attempt_id=attempt_id, sent=sent, now=now)
            if attempt['phase'] not in TERMINAL:
                connection.execute(self.attempts.update().where(self.attempts.c.id == attempt_id).values(
                    phase='success', completed_at=now))
            row = self._row(connection, job_id)
            if row is None or row['state'] != 'failed' or row['attempt_id'] != broken_attempt_id:
                return False
            # A late result for the broken attempt can no longer move the fax: it is no longer the current one.
            self._update(connection, row, now, state='success', attempt_id=attempt_id, claim_owner=None,
                         claim_token=None, claim_expires_at=None, next_poll_at=None)
            connection.execute(self.configuration.jobs.update().where(self.configuration.jobs.c.id == job_id).values(
                status='success', error=None, updated_at=now))
            _event(connection, self.events, job_id, 'repair_completed', now, attempt_id=attempt_id)
            return True

    def fail_repair(self, job_id, *, attempt_id, now=None):
        """The partner signed that the missing pages did not arrive: the repair's attempt failed, and the fax still
        waits for a person, as a broken call does. False when there is no such unfinished attempt."""
        with self.configuration._locked() as connection:
            now = now or datetime.utcnow()
            changed = connection.execute(self.attempts.update().where(
                self.attempts.c.id == attempt_id, self.attempts.c.job_id == job_id,
                self.attempts.c.phase.not_in(tuple(TERMINAL))).values(
                phase='failed', error_category='partner_not_received', completed_at=now)).rowcount
            if changed:
                _event(connection, self.events, job_id, 'repair_failed', now, attempt_id=attempt_id)
            return bool(changed)

    def fallback_count(self, job_id):
        with self.configuration.engine.connect() as connection:
            return connection.scalar(sa.select(sa.func.count()).select_from(self.events).where(
                self.events.c.job_id == job_id, self.events.c.kind == 'route_fallback'))

    def requeue_after_failure(self, job_id, *, attempt_id, category, max_fallbacks=FALLBACK_LIMIT, now=None):
        """Return a definitely failed delivery to the queue for its next route.

        ``provider_failed``: the attempt's provider reported a final failure.
        ``partner_not_received``: a direct partner signed that it never received
        the document. ``local_not_delivered``: a fax to one of the installation's
        own numbers has no received-fax record, so nothing was delivered inside
        Faxbot. Never used for an ambiguous outcome. The next claim creates a new
        attempt; at most ``max_fallbacks`` per fax.
        """
        if (category not in {'provider_failed', 'partner_not_received', 'local_not_delivered'}
                or type(max_fallbacks) is not int):
            raise ValueError('Invalid delivery fallback.')
        with self.configuration._locked() as connection:
            now = now or datetime.utcnow()
            row = self._row(connection, job_id)
            if row is None or row['attempt_id'] != attempt_id or row['dispatch_mode'] != 'normal':
                return False
            attempt = connection.execute(sa.select(self.attempts).where(
                self.attempts.c.id == attempt_id)).mappings().one_or_none()
            if attempt is None or attempt['job_id'] != job_id or attempt['submitted_at'] is None:
                return False
            if category == 'provider_failed':
                definite = (row['state'] == 'failed' and attempt['phase'] == 'failed'
                            and attempt['error_category'] is None and attempt['completed_at'] is not None)
                ended = attempt.get('ended_before_data')
                if definite and self._strict_blocks(connection, job_id, None if ended is None else bool(ended)):
                    return False
            else:
                definite = row['state'] == 'reconciliation_required' and attempt['phase'] == 'uncertain'
            used = connection.scalar(sa.select(sa.func.count()).select_from(self.events).where(
                self.events.c.job_id == job_id, self.events.c.kind == 'route_fallback'))
            if not definite or used >= max_fallbacks or not self._enabled(connection):
                return False
            if category != 'provider_failed':
                connection.execute(self.attempts.update().where(self.attempts.c.id == attempt_id).values(
                    phase='failed', error_category=category, completed_at=now))
            # Detach the finished attempt so a late result for it cannot move the requeued fax.
            self._update(connection, row, now, state='ready', attempt_id=None, claim_owner=None,
                         claim_token=None, claim_expires_at=None, next_poll_at=None)
            connection.execute(self.configuration.jobs.update().where(self.configuration.jobs.c.id == job_id).values(
                status='queued', error=None, updated_at=now))
            _event(connection, self.events, job_id, 'route_fallback', now, attempt_id=attempt_id,
                   details={'category': category})
            return True
