"""Internal SQL visibility composition, never HTTP or GUI acceptance."""
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
