"""Internal SQL visibility composition, never HTTP or GUI acceptance."""
from contextlib import contextmanager
import importlib

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_outbound import aw
from api.tests.test_access_policy import NOW
from api.app.access.fax_resources import FaxAccessError
from api.app.access.types import StaleCredentialError

try:
    Q = importlib.import_module('api.app.access.queries')
except ModuleNotFoundError:
    Q = None


@pytest.fixture
def queries(aw):
    assert Q is not None, 'Authorized fax queries missing'
    aw.outbound.accept(aw.actor, aw.snapshot.active, aw.job)
    # A second principal's business/resource rows exist but are not visible.
    aw.outbound.accept(aw.actor, aw.snapshot.active, {**aw.job, 'id': 'second'})
    aw.update('access_resources', _resource_id(aw, 'second'), parent_id='personal-bob')
    return Q.AuthorizedFaxQueries(aw.configuration, aw.faxes, clock=aw.outbound._clock)


def _resource_id(w, job):
    table = w.tables['access_resources']
    with w.engine.connect() as c:
        return c.scalar(sa.select(table.c.id).where(table.c.fax_job_id == job))


def test_page_filters_counts_and_offsets_only_visible_rows(aw, queries):
    page = queries.page(aw.actor, limit=1, offset=0)
    assert page['total'] == 1
    assert [row['id'] for row in page['jobs']] == ['new']
    assert queries.page(aw.actor, limit=1, offset=1) == {'total': 1, 'jobs': []}
    assert queries.page(aw.actor, status='failed') == {'total': 0, 'jobs': []}
    assert queries.page(aw.actor, backend='other') == {'total': 0, 'jobs': []}
    assert queries.counts(aw.actor)['queued'] == 1
    assert 'tiff_path' not in page['jobs'][0]
    assert page['jobs'][0]['delivery_state'] == 'ready'


def test_hidden_detail_and_document_do_not_disclose_existence(aw, queries):
    for job in ('second', 'missing'):
        for operation in (queries.job, queries.document):
            with pytest.raises(FaxAccessError) as error:
                operation(aw.actor, job)
            assert error.value.code == 'not_found'


def test_document_permission_does_not_imply_metadata_permission(aw, queries):
    assignments = aw.tables['access_assignments']
    with aw.engine.begin() as c:
        c.execute(assignments.delete().where(assignments.c.principal_id == 'alice'))
    aw.role('document-only', ['fax:document'])
    aw.assignment('alice', 'document-only', _resource_id(aw, 'new'))
    assert queries.document(aw.actor, 'new') == 'new'
    assert queries.page(aw.actor) == {'total': 0, 'jobs': []}
    with pytest.raises(FaxAccessError) as error:
        queries.job(aw.actor, 'new')
    assert error.value.code == 'not_found'


def test_revocation_is_checked_before_empty_or_existing_read(aw, queries):
    aw.update('access_principals', 'alice', enabled=0, security_version=2)
    for operation in (lambda: queries.page(aw.actor), lambda: queries.counts(aw.actor),
                      lambda: queries.job(aw.actor, 'new'), lambda: queries.document(aw.actor, 'missing')):
        with pytest.raises(StaleCredentialError):
            operation()


@pytest.mark.parametrize('values', [{'limit': 0}, {'limit': 101}, {'offset': -1}, {'limit': True}])
def test_invalid_pagination_is_rejected(aw, queries, values):
    with pytest.raises(FaxAccessError) as error:
        queries.page(aw.actor, **values)
    assert error.value.code == 'invalid_input'


def test_password_reset_restriction_blocks_lists_and_counts(aw, queries):
    aw.update('access_users', 'alice', password_change_required=1)
    for operation in (queries.page, queries.counts):
        with pytest.raises(FaxAccessError) as error:
            operation(aw.actor)
        assert error.value.code == 'reset_required'


def test_metadata_only_source_cannot_download_or_refresh(aw, queries):
    assignments = aw.tables['access_assignments']
    with aw.engine.begin() as c:
        c.execute(assignments.delete().where(assignments.c.principal_id == 'alice'))
    aw.role('metadata-only', ['fax:read'])
    aw.assignment('alice', 'metadata-only', _resource_id(aw, 'new'))
    assert queries.job(aw.actor, 'new')['id'] == 'new'
    for operation in (queries.document, queries.poll_target):
        with pytest.raises(FaxAccessError) as error:
            operation(aw.actor, 'new')
        assert error.value.code == 'forbidden'


def _uncertain(aw, queries):
    claim = queries.delivery.claim('internal-test', now=NOW)
    assert claim.job_id == 'new'
    queries.delivery.begin_submission(claim, now=NOW)
    queries.delivery.record_uncertain(claim, now=NOW)
    return queries.delivery.get('new')['version']


def test_history_and_reconcile_apply_distinct_permissions(aw, queries):
    version = _uncertain(aw, queries)
    assert queries.history(aw.actor, 'new')['can_bind_provider_identity'] is False
    with pytest.raises(FaxAccessError) as error:
        queries.reconcile(aw.actor, 'new', expected_version=version, provider_sid='remote-proof')
    assert error.value.code == 'forbidden'
    aw.role('reconciler', ['fax:reconcile'])
    aw.assignment('alice', 'reconciler', _resource_id(aw, 'new'))
    assert queries.history(aw.actor, 'new')['can_bind_provider_identity'] is True
    result = queries.reconcile(aw.actor, 'new', expected_version=version, provider_sid='remote-proof')
    assert result['attempt']['provider_sid'] == 'remote-proof'
    with aw.engine.connect() as c:
        assert c.scalar(sa.text("SELECT count(*) FROM access_audit WHERE operation='fax.reconcile'")) == 1
    profile, attempt, sid = queries.poll_target(aw.actor, 'new')
    assert profile.id == aw.snapshot.active.profile_id('outbound') and sid == 'remote-proof'


def test_reconciliation_audit_failure_rolls_back_provider_identity(aw, queries):
    version = _uncertain(aw, queries)
    aw.role('reconciler', ['fax:reconcile'])
    aw.assignment('alice', 'reconciler', _resource_id(aw, 'new'))
    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('INSERT INTO ACCESS_AUDIT'):
            raise RuntimeError('synthetic audit failure')
    sa.event.listen(aw.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic audit failure'):
            queries.reconcile(aw.actor, 'new', expected_version=version, provider_sid='remote-proof')
    finally:
        sa.event.remove(aw.engine, 'before_cursor_execute', fail)
    result = queries.history(aw.actor, 'new')
    assert result['attempt']['provider_sid'] is None
    assert result['version'] == version
    assert all(event['kind'] != 'operator_identity_bound' for event in result['events'])


@pytest.mark.parametrize('mode, expected', [
    ('legacy', 'Faxbot has no delivery record for this older fax; check its status in your provider account.'),
    ('normal', 'Faxbot could not confirm delivery; check your provider account before sending this fax again.'),
])
def test_unconfirmed_delivery_reason_is_one_plain_operator_sentence(mode, expected):
    row = {'delivery_state': 'reconciliation_required', 'dispatch_mode': mode, 'delivery_version': 3}
    assert Q.delivery_fields(row)['reconciliation_reason'] == expected
    assert Q.delivery_fields(dict(row, delivery_state='delivered'))['reconciliation_reason'] is None


def _locks(queries, monkeypatch):
    """Count the configuration/access transactions ``queries`` opens."""
    opened = []
    original = queries.configuration._locked

    def counted():
        opened.append(1)
        return original()
    monkeypatch.setattr(queries.configuration, '_locked', counted)
    return opened


def _one_by_one(queries, actor, ids):
    """What asking ``job`` for each id in turn shows: the readable ids, in order, with their records."""
    shown = {}
    for job_id in ids:
        try:
            shown[job_id] = queries.job(actor, job_id)
        except FaxAccessError:
            continue
    return shown


def test_a_list_of_faxes_is_checked_in_one_transaction_with_the_same_visibility(aw, queries, monkeypatch):
    # A third fax alice may download but not read: a document-only grant adds no visibility.
    aw.outbound.accept(aw.actor, aw.snapshot.active, {**aw.job, 'id': 'document-only'})
    aw.update('access_resources', _resource_id(aw, 'document-only'), parent_id='personal-bob')
    aw.role('document-only', ['fax:document'])
    aw.assignment('alice', 'document-only', _resource_id(aw, 'document-only'))
    # A refused id first, a hidden one, a missing one, a malformed one and a repeat around the one she may read.
    ids = ['second', 'new', 'missing', 'x' * 41, 'document-only', 'new', '']
    expected = _one_by_one(queries, aw.actor, ids)
    assert list(expected) == ['new']
    monkeypatch.setattr(Q, 'LIST_SECONDS', 60.0)  # the one-transaction claim only; a slow runner must not split it
    opened = _locks(queries, monkeypatch)
    assert queries.visible_jobs(aw.actor, ids) == expected
    assert len(opened) == 1
    assert queries.visible_jobs(aw.actor, []) == {} and len(opened) == 1


def test_a_full_page_is_checked_twenty_faxes_per_transaction_with_the_same_visibility(aw, queries, monkeypatch):
    """100 ids take the lock 5 times, never once for long (Q.LIST_CHUNK); faxes she may not read sit on both sides
    of a chunk boundary and are still left out, and the order is kept."""
    assert Q.LIST_CHUNK == 20
    for index in range(30):
        aw.outbound.accept(aw.actor, aw.snapshot.active, {**aw.job, 'id': f'fax{index:02d}'})
    for hidden in ('fax19', 'fax20', 'fax00'):
        aw.update('access_resources', _resource_id(aw, hidden), parent_id='personal-bob')
    ids = [f'fax{index:02d}' for index in range(30)] + ['second', 'new'] + [f'gone{index:02d}' for index in range(68)]
    assert len(ids) == 100
    expected = _one_by_one(queries, aw.actor, ids)
    assert list(expected) == [f'fax{index:02d}' for index in range(1, 30) if index not in (19, 20)] + ['new']
    monkeypatch.setattr(Q, 'LIST_SECONDS', 60.0)  # the count bound alone here; the time bound is tested below
    opened = _locks(queries, monkeypatch)
    assert queries.visible_jobs(aw.actor, ids) == expected
    assert len(opened) == 5


def test_a_slow_check_ends_its_transaction_before_the_time_limit_with_the_same_visibility(aw, queries, monkeypatch):
    """Acceptance (2026-10-11): one fax's check took 250-500 ms on a loaded installation, so 20 held the lock 4-10 s
    and every other access check gave up after its 1 s. A transaction now takes no further fax once the slowest
    check so far would carry it past Q.LIST_SECONDS; the faxes shown are the same, in the same order."""
    import time
    for index in range(10):
        aw.outbound.accept(aw.actor, aw.snapshot.active, {**aw.job, 'id': f'fax{index:02d}'})
    aw.update('access_resources', _resource_id(aw, 'fax03'), parent_id='personal-bob')
    ids = [f'fax{index:02d}' for index in range(10)] + ['second', 'missing']
    real = queries.resources.require_outbound_on

    def slow(*args, **kwargs):
        time.sleep(0.06)
        return real(*args, **kwargs)
    monkeypatch.setattr(queries.resources, 'require_outbound_on', slow)
    expected = _one_by_one(queries, aw.actor, ids)
    assert list(expected) == [f'fax{index:02d}' for index in range(10) if index != 3]
    held, transaction = [], queries._transaction

    @contextmanager
    def timed():
        with transaction() as value:
            began = time.monotonic()
            yield value
        held.append(time.monotonic() - began)
    monkeypatch.setattr(queries, '_transaction', timed)
    assert queries.visible_jobs(aw.actor, ids) == expected
    assert len(held) >= 4 and max(held) < 0.25, held  # about LIST_SECONDS (0.2), against a 1 s lock timeout
    # A single check slower than the limit still moves on, one fax to a transaction.
    monkeypatch.setattr(Q, 'LIST_SECONDS', 0.01)
    held.clear()
    assert queries.visible_jobs(aw.actor, ids[:3]) == {key: expected[key] for key in ids[:3] if key in expected}
    assert len(held) == 3


def test_one_refused_fax_does_not_hide_the_others(aw, queries):
    aw.outbound.accept(aw.actor, aw.snapshot.active, {**aw.job, 'id': 'third'})
    found = queries.visible_jobs(aw.actor, ['second', 'third', 'missing', 'new'])
    assert list(found) == ['third', 'new']
    assert found == _one_by_one(queries, aw.actor, ['third', 'new'])


def test_a_list_fails_whole_for_a_revoked_credential_and_is_empty_while_a_password_must_change(aw, queries):
    aw.update('access_users', 'alice', password_change_required=1)
    # Each fax is refused as job refuses it, so nothing is shown and nothing is explained.
    assert queries.visible_jobs(aw.actor, ['new', 'second']) == {}
    aw.update('access_users', 'alice', password_change_required=0)
    aw.update('access_principals', 'alice', enabled=0, security_version=2)
    with pytest.raises(StaleCredentialError):
        queries.visible_jobs(aw.actor, ['second', 'new'])


OWN_CALL = ('The call ended before the receiving fax machine confirmed the pages; check with the recipient before '
            'sending this fax again.')
SERVICE = 'Faxbot could not confirm delivery; check your provider account before sending this fax again.'


@pytest.mark.parametrize('route_provider, backend, expected', [
    ('sip', 'humblefax', OWN_CALL),        # accepted for HumbleFax, sent over the trunk: no provider account
    ('freeswitch', None, OWN_CALL),
    (None, 'sip', OWN_CALL),               # no attempt route on record: the account it was accepted with
    ('humblefax', 'sip', SERVICE),         # the route that carried it decides, not the acceptance
    ('phaxio', 'phaxio', SERVICE),
])
def test_an_uncertain_result_names_the_provider_account_only_for_a_fax_service(route_provider, backend, expected):
    row = {'delivery_state': 'reconciliation_required', 'dispatch_mode': 'normal', 'delivery_version': 3,
           'route_provider': route_provider, 'backend': backend}
    assert Q.delivery_fields(row)['reconciliation_reason'] == expected
    legacy = Q.delivery_fields(dict(row, dispatch_mode='legacy'))['reconciliation_reason']
    assert ('provider account' in legacy) == (expected == SERVICE)


def test_an_uncertain_fax_reads_the_route_its_attempt_used(aw, queries):
    from api.app.config_profiles import ProviderConfiguration
    _uncertain(aw, queries)
    # Accepted for and sent through the fax service: its account is the place to check.
    assert queries.job(aw.actor, 'new')['reconciliation_reason'] == SERVICE
    assert queries.page(aw.actor, status='reconciliation_required')['jobs'][0]['reconciliation_reason'] == SERVICE
    # The same fax with its attempt bound to the trunk (assign_route): the call was Faxbot's own.
    trunk = aw.configuration.account_profile(ProviderConfiguration('sip'))
    attempts = aw.configuration.delivery_tables['outbound_attempts']
    with aw.engine.begin() as connection:
        connection.execute(attempts.update().where(attempts.c.job_id == 'new').values(profile_id=trunk))
    assert queries.job(aw.actor, 'new')['reconciliation_reason'] == OWN_CALL
    assert queries.page(aw.actor, status='reconciliation_required')['jobs'][0]['reconciliation_reason'] == OWN_CALL


def test_each_fax_in_a_page_reads_its_own_route(aw, queries):
    """Two uncertain faxes on one page, one sent through the fax service and one over the trunk: each row's sentence
    comes from that fax's own attempt and binding, never another fax's."""
    from api.app.config_profiles import ProviderConfiguration
    aw.outbound.accept(aw.actor, aw.snapshot.active, {**aw.job, 'id': 'third', 'to_number': '+12025550188'})
    uncertain = set()
    for _ in range(3):  # new and third (and second, another person's, if its number is free)
        claim = queries.delivery.claim('internal-test', now=NOW)
        if claim is None:
            break
        queries.delivery.begin_submission(claim, now=NOW)
        queries.delivery.record_uncertain(claim, now=NOW)
        uncertain.add(claim.job_id)
    assert {'new', 'third'} <= uncertain
    trunk = aw.configuration.account_profile(ProviderConfiguration('sip'))
    attempts = aw.configuration.delivery_tables['outbound_attempts']
    with aw.engine.begin() as connection:
        connection.execute(attempts.update().where(attempts.c.job_id == 'third').values(profile_id=trunk))
    page = queries.page(aw.actor, status='reconciliation_required')
    assert page['total'] == 2
    assert {row['id']: row['reconciliation_reason'] for row in page['jobs']} == {'new': SERVICE, 'third': OWN_CALL}
