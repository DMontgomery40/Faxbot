"""Durable worker ownership; no HTTP or user-facing acceptance substitutes."""
from datetime import datetime, timedelta
from dataclasses import replace
import itertools
import json
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_store import OutboundStore, DeliveryConflict


@pytest.fixture
def installation(database, tmp_path):
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false'})
    profile = ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-key'})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': profile})
    return configuration, OutboundStore(configuration), snapshot


_NUMBERS = itertools.count()


def another_number():
    """A different synthetic number (+1 202 555 0150 to 0199) for each call.

    Faxbot places one call at a time to a number (``capacity.py``), so a test
    that needs several faxes on the line at once sends each to its own number.
    """
    return '+1202555%04d' % (150 + next(_NUMBERS) % 50)


def accept(installation, identity=None, *, to_number='+12025550123'):
    configuration, delivery, snapshot = installation
    identity = identity or uuid4().hex
    now = datetime.utcnow()
    configuration.accept_outbound(snapshot.active, {'id': identity, 'to_number': to_number,
        'file_name': 'synthetic.txt', 'tiff_path': '', 'status': 'queued', 'pages': 3,
        'created_at': now, 'updated_at': now})
    return identity


def test_new_acceptance_atomically_has_one_delivery_and_initial_history(installation):
    configuration, delivery, snapshot = installation
    identity = accept(installation)
    record = delivery.get(identity)
    assert record['state'] == 'ready'
    assert record['dispatch_mode'] == 'normal'
    assert record['version'] == 1
    assert [event['kind'] for event in delivery.history(identity)] == ['accepted']
    assert configuration.outbound_profile(identity).id == snapshot.active.profile_id('outbound')


def test_one_claim_owns_job_and_retains_accepted_profile(installation):
    configuration, delivery, snapshot = installation
    identity = accept(installation)
    now = datetime.utcnow()
    claim = delivery.claim('worker-one', now=now, lease_seconds=30)
    assert claim.job_id == identity
    assert claim.profile_id == snapshot.active.profile_id('outbound')
    assert delivery.claim('worker-two', now=now, lease_seconds=30) is None
    assert delivery.begin_submission(claim, now=now + timedelta(seconds=1)) is True
    assert delivery.begin_submission(claim, now=now + timedelta(seconds=2)) is False


def test_expired_preparation_is_reclaimable_but_old_token_cannot_submit(installation):
    _, delivery, _ = installation
    identity = accept(installation)
    now = datetime.utcnow()
    old = delivery.claim('worker-one', now=now, lease_seconds=2)
    delivery.recover_expired(now=now + timedelta(seconds=3))
    new = delivery.claim('worker-two', now=now + timedelta(seconds=3), lease_seconds=30)
    assert new.job_id == identity and new.attempt_id != old.attempt_id
    assert delivery.begin_submission(old, now=now + timedelta(seconds=4)) is False
    assert delivery.begin_submission(new, now=now + timedelta(seconds=4)) is True


def test_expired_submission_never_requeues_and_late_receipt_can_reconcile(installation):
    _, delivery, _ = installation
    identity = accept(installation)
    now = datetime.utcnow()
    claim = delivery.claim('worker-one', now=now, lease_seconds=2)
    assert delivery.begin_submission(claim, now=now) is True
    delivery.recover_expired(now=now + timedelta(seconds=3))
    assert delivery.get(identity)['state'] == 'reconciliation_required'
    assert delivery.claim('worker-two', now=now + timedelta(seconds=4)) is None
    delivery.record_receipt(claim, provider_sid='remote-123', status='in_progress', now=now + timedelta(seconds=5))
    assert delivery.get(identity)['state'] == 'in_progress'


def test_terminal_callback_before_ack_is_absorbing_and_sid_is_bound(installation):
    _, delivery, _ = installation
    identity = accept(installation)
    now = datetime.utcnow()
    claim = delivery.claim('worker-one', now=now)
    delivery.begin_submission(claim, now=now)
    assert delivery.observe(identity, attempt_id=claim.attempt_id, profile_id=claim.profile_id,
        provider_sid='remote-123', status='success', event_key='verified-event-one', now=now) is True
    delivery.record_receipt(claim, provider_sid='remote-123', status='in_progress', now=now)
    assert delivery.get(identity)['state'] == 'success'
    assert delivery.observe(identity, attempt_id=claim.attempt_id, profile_id=claim.profile_id,
        provider_sid='remote-123', status='success', event_key='verified-event-one', now=now) is False
    with pytest.raises(DeliveryConflict):
        delivery.observe(identity, attempt_id=claim.attempt_id, profile_id=claim.profile_id,
            provider_sid='different-fax', status='failed', event_key='verified-event-two', now=now)
    assert delivery.get(identity)['state'] == 'success'


def test_current_disabled_mode_fences_prepared_submission(installation):
    configuration, delivery, snapshot = installation
    identity = accept(installation)
    now = datetime.utcnow()
    claim = delivery.claim('worker-one', now=now)
    configuration.apply(snapshot, snapshot.active.values.with_patch({'fax_disabled': True}),
        actor='test', restart_required=False)
    assert delivery.begin_submission(claim, now=now) is False
    assert delivery.get(identity)['state'] == 'ready'
    assert delivery.claim('worker-two', now=now) is None


def test_acceptance_rollback_leaves_neither_job_nor_delivery(installation, monkeypatch):
    configuration, delivery, _ = installation
    original = sa.engine.Connection.execute
    def fail_delivery(connection, statement, *args, **kwargs):
        if getattr(getattr(statement, 'table', None), 'name', None) == 'outbound_deliveries' and getattr(statement, 'is_insert', False):
            raise sa.exc.OperationalError(None, None, RuntimeError('synthetic failure'))
        return original(connection, statement, *args, **kwargs)
    monkeypatch.setattr(sa.engine.Connection, 'execute', fail_delivery)
    identity = uuid4().hex
    with pytest.raises(Exception):
        accept(installation, identity)
    with configuration.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(configuration.jobs)) == 0
        assert connection.scalar(sa.select(sa.func.count()).select_from(delivery.deliveries)) == 0


def test_held_acceptance_never_becomes_ready_when_sending_is_enabled(installation):
    configuration, delivery, snapshot = installation
    held_snapshot = configuration.apply(snapshot, snapshot.active.values.with_patch({'fax_disabled': True}),
        actor='test', restart_required=False)
    identity = accept((configuration, delivery, held_snapshot))
    assert delivery.get(identity)['state'] == 'held'
    configuration.apply(held_snapshot, held_snapshot.active.values.with_patch({'fax_disabled': False}),
        actor='test', restart_required=False)
    delivery.recover_expired(now=datetime.utcnow() + timedelta(days=1))
    assert delivery.claim('worker-one') is None
    assert delivery.get(identity)['state'] == 'held'


def _process_claim(url, namespace, key_path, owner, start, results):
    from api.app.schema import create_database_engine
    options = {'options': '-csearch_path=' + namespace} if namespace else {}
    engine = create_database_engine(url, connect_args=options)
    try:
        configuration = ConfigurationStore(engine, key_path)
        delivery = OutboundStore(configuration)
        start.wait(15)
        claim = delivery.claim(owner)
        results.put(('ok', claim.job_id if claim else None))
    except Exception as error:
        results.put(('error', type(error).__name__))
    finally:
        engine.dispose()


def test_two_independent_processes_cannot_claim_one_job(installation):
    import multiprocessing
    configuration, delivery, _ = installation
    identity = accept(installation)
    with configuration.engine.connect() as connection:
        namespace = connection.scalar(sa.text('SELECT current_schema()')) if connection.dialect.name == 'postgresql' else ''
    context = multiprocessing.get_context('spawn')
    start, results = context.Event(), context.Queue()
    processes = [context.Process(target=_process_claim, args=(configuration.engine.url.render_as_string(hide_password=False),
        namespace, str(configuration.key_path), f'worker-{number}', start, results)) for number in range(2)]
    try:
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
        assert sorted(outcomes, key=lambda item: str(item)) == sorted([('ok', identity), ('ok', None)], key=lambda item: str(item))
        assert len([event for event in delivery.history(identity) if event['kind'] == 'claimed']) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(10)
        results.close()
        results.join_thread()


@pytest.mark.parametrize('committed', [False, True])
def test_submission_marker_lost_ack_never_authorizes_a_blind_send(installation, monkeypatch, committed):
    from api.app.config_store import ConfigurationCommitUncertain
    _, delivery, _ = installation
    identity = accept(installation)
    now = datetime.utcnow()
    claim = delivery.claim('worker-one', now=now, lease_seconds=2)
    original_commit = sa.engine.Connection.commit
    def lose_ack(connection):
        if committed:
            original_commit(connection)
        raise sa.exc.OperationalError(None, None, RuntimeError('synthetic failure'))
    monkeypatch.setattr(sa.engine.Connection, 'commit', lose_ack)
    with pytest.raises(ConfigurationCommitUncertain):
        delivery.begin_submission(claim, now=now)
    monkeypatch.setattr(sa.engine.Connection, 'commit', original_commit)
    assert delivery.get(identity)['state'] == ('submitting' if committed else 'preparing')
    delivery.recover_expired(now=now + timedelta(seconds=3))
    assert delivery.get(identity)['state'] == ('reconciliation_required' if committed else 'ready')
    if committed:
        assert delivery.claim('worker-two', now=now + timedelta(seconds=4)) is None


def test_transport_uncertainty_is_not_a_terminal_failure_or_retriable_job(installation):
    _, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker-one')
    delivery.begin_submission(claim)
    assert delivery.record_uncertain(claim, category='transport_ambiguous') is True
    assert delivery.get(identity)['state'] == 'reconciliation_required'
    delivery.recover_expired(now=datetime.utcnow() + timedelta(days=1))
    assert delivery.claim('worker-two') is None
    delivery.observe(identity, attempt_id=claim.attempt_id, profile_id=claim.profile_id,
        provider_sid='remote-123', status='success', event_key='verified-result')
    assert delivery.record_uncertain(claim, category='submission_cancelled') is False
    assert delivery.get(identity)['state'] == 'success'


def test_only_pre_submission_failure_can_be_classified_as_definite_preparation_failure(installation):
    _, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker-one')
    delivery.begin_submission(claim)
    with pytest.raises(DeliveryConflict):
        delivery.fail_preparation(claim, category='artifact_unavailable')
    assert delivery.get(identity)['state'] == 'submitting'


def test_rotation_does_not_change_claimed_profile_or_allow_new_account_callback(installation):
    configuration, delivery, snapshot = installation
    identity = accept(installation)
    rotated = configuration.apply(snapshot, snapshot.active.values.with_patch({'phaxio_api_key': 'synthetic-new'}),
        actor='test', restart_required=False, providers={'outbound': ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-new'})})
    claim = delivery.claim('worker-one')
    assert claim.profile_id == snapshot.active.profile_id('outbound')
    assert claim.profile_id != rotated.active.profile_id('outbound')
    accepted_revision, accepted_profile = configuration.outbound_context(identity)
    assert accepted_revision.id == snapshot.active.id
    assert accepted_profile.configuration.credentials['api_key'] == 'synthetic-key'
    delivery.begin_submission(claim)
    with pytest.raises(DeliveryConflict):
        delivery.observe(identity, attempt_id=claim.attempt_id, profile_id=rotated.active.profile_id('outbound'),
            provider_sid='other-account', status='success', event_key='wrong-account')
    assert delivery.get(identity)['state'] == 'submitting'


def test_late_receipt_can_fill_identity_without_regressing_early_terminal_result(installation):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker-one')
    delivery.begin_submission(claim)
    delivery.observe(identity, attempt_id=claim.attempt_id, profile_id=claim.profile_id,
        provider_sid=None, status='success', event_key='verified-internal-result')
    delivery.record_receipt(claim, provider_sid='remote-123', status='in_progress')
    assert delivery.get(identity)['state'] == 'success'
    with configuration.engine.connect() as connection:
        job = connection.execute(sa.select(configuration.jobs).where(configuration.jobs.c.id == identity)).mappings().one()
        assert job['status'] == 'success' and job['provider_sid'] == 'remote-123'
    assert not any(event['kind'] == 'terminal_conflict' for event in delivery.history(identity))


@pytest.mark.parametrize('operation', ['observe', 'record_receipt'])
@pytest.mark.parametrize('mismatch', ['profile', 'sid'])
def test_authenticated_identity_refusal_is_durably_audited_without_changing_delivery(installation, operation, mismatch):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker-one')
    assert delivery.begin_submission(claim)
    delivery.record_receipt(claim, provider_sid='synthetic-original-sid', status='in_progress')
    before = delivery.get(identity)
    history = delivery.history(identity)
    with configuration.engine.connect() as connection:
        original_job = dict(connection.execute(sa.select(configuration.jobs).where(configuration.jobs.c.id == identity)).mappings().one())
        original_attempt = dict(connection.execute(sa.select(delivery.attempts).where(delivery.attempts.c.id == claim.attempt_id)).mappings().one())
    profile = 'synthetic-wrong-profile' if mismatch == 'profile' else claim.profile_id
    sid = 'synthetic-wrong-sid' if mismatch == 'sid' else 'synthetic-original-sid'

    def refuse():
        if operation == 'observe':
            delivery.observe(identity, attempt_id=claim.attempt_id, profile_id=profile,
                provider_sid=sid, status='success', event_key='verified-rejected-event')
        else:
            delivery.record_receipt(replace(claim, profile_id=profile), provider_sid=sid, status='success')

    with pytest.raises(DeliveryConflict):
        refuse()
    assert delivery.get(identity) == before
    audit = delivery.history(identity)
    assert len(audit) == len(history) + 1
    refused = next(event for event in audit if event['kind'] == 'provider_observation_refused')
    assert refused['attempt_id'] == claim.attempt_id
    assert json.loads(refused['details']) == {'category': mismatch + '_mismatch'}
    assert len(refused['details']) < 100
    assert profile not in refused['details'] and sid not in refused['details'] and claim.token not in refused['details']
    with configuration.engine.connect() as connection:
        assert dict(connection.execute(sa.select(configuration.jobs).where(configuration.jobs.c.id == identity)).mappings().one()) == original_job
        assert dict(connection.execute(sa.select(delivery.attempts).where(delivery.attempts.c.id == claim.attempt_id)).mappings().one()) == original_attempt
    if operation == 'observe':
        with pytest.raises(DeliveryConflict):
            refuse()
        assert delivery.history(identity) == audit


def test_refused_event_has_separate_dedupe_identity_from_accepted_event(installation):
    _, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker-one')
    assert delivery.begin_submission(claim)
    delivery.observe(identity, attempt_id=claim.attempt_id, profile_id=claim.profile_id,
        provider_sid='synthetic-original-sid', status='in_progress', event_key='same-provider-event')
    with pytest.raises(DeliveryConflict):
        delivery.observe(identity, attempt_id=claim.attempt_id, profile_id=claim.profile_id,
            provider_sid='synthetic-wrong-sid', status='success', event_key='same-provider-event')
    events = [event for event in delivery.history(identity) if event['kind'] in {'provider_observed', 'provider_observation_refused'}]
    assert len(events) == 2
    assert len({event['dedupe_key'] for event in events}) == 2
    assert delivery.get(identity)['state'] == 'in_progress'


@pytest.mark.parametrize('missing', ['job', 'attempt'])
def test_unknown_observation_target_does_not_create_misleading_audit(installation, missing):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker-one')
    assert delivery.begin_submission(claim)
    with configuration.engine.connect() as connection:
        before = connection.scalar(sa.select(sa.func.count()).select_from(delivery.events))
    with pytest.raises(DeliveryConflict):
        delivery.observe('nonexistent-job' if missing == 'job' else identity,
            attempt_id='nonexistent-attempt' if missing == 'attempt' else claim.attempt_id,
            profile_id='synthetic-wrong-profile', provider_sid='synthetic-wrong-sid',
            status='success', event_key='unknown-target-event')
    with configuration.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(delivery.events)) == before
    assert delivery.get(identity)['state'] == 'submitting'
