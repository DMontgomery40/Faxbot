"""Internal fax/resource transaction contracts, never HTTP acceptance."""
import importlib

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_policy import World, NOW
from api.app.access.types import InvalidTransactionError, ResourceRef, StaleCredentialError

try:
    R = importlib.import_module('api.app.access.fax_resources')
except ModuleNotFoundError:
    R = None


@pytest.fixture
def fw(database):
    assert R is not None, 'Fax resource bridge not implemented'
    w = World(database)
    w.actor = w.user('alice')
    w.user('bob')
    w.assignment('alice', 'role_fax_operator', 'personal-alice')
    w.faxes = R.FaxResources(w.control)
    return w


def test_bridge_module_exists():
    assert R is not None, 'Fax resource bridge not implemented'


def test_new_outbound_owner_and_audit_share_the_business_transaction(fw):
    w = fw
    with w.store.transaction() as c:
        parent = w.faxes.authorize_send_on(c, w.actor, now=NOW)
        assert parent == ResourceRef('personal-alice')
        c.execute(w.tables['fax_jobs'].insert().values(id='new', to_number='+12025550123',
            file_name='private-file.pdf', tiff_path='/private/file.tiff', status='queued', backend='sip', created_at=NOW, updated_at=NOW))
        resource = w.faxes.record_outbound_on(c, w.actor, 'new', now=NOW)
        assert w.control.authorize_on(c, w.actor, 'fax:document', resource, now=NOW).allowed
        audit = c.execute(sa.select(w.tables['access_audit']).where(
            w.tables['access_audit'].c.operation == 'fax.accept')).mappings().one()
        assert audit['actor_principal_id'] == 'alice' and audit['actor_session_id'] == 'session-alice'
        assert audit['target_id'] == resource.id
        assert 'private' not in repr(dict(audit)) and '+1202' not in repr(dict(audit))
        assert audit['policy_version_before'] == audit['policy_version_after'] == 1
    with w.engine.connect() as c:
        row = c.execute(sa.select(w.tables['access_resources']).where(
            w.tables['access_resources'].c.id == resource.id)).mappings().one()
        assert row['parent_id'] == 'personal-alice' and row['fax_job_id'] == 'new'


def test_audit_failure_rolls_back_fax_and_resource(fw):
    w = fw
    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('INSERT INTO ACCESS_AUDIT'):
            raise RuntimeError('synthetic audit refusal')
    sa.event.listen(w.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic audit refusal'):
            with w.store.transaction() as c:
                c.execute(w.tables['fax_jobs'].insert().values(id='rollback', to_number='+12025550123',
                    file_name='x.pdf', tiff_path='x.tiff', status='queued', backend='sip', created_at=NOW, updated_at=NOW))
                w.faxes.record_outbound_on(c, w.actor, 'rollback', now=NOW)
    finally:
        sa.event.remove(w.engine, 'before_cursor_execute', fail)
    with w.engine.connect() as c:
        assert c.execute(sa.select(w.tables['fax_jobs'].c.id).where(w.tables['fax_jobs'].c.id == 'rollback')).first() is None
        assert c.execute(sa.select(w.tables['access_resources'].c.id).where(w.tables['access_resources'].c.fax_job_id == 'rollback')).first() is None


def test_visibility_is_applied_before_count_and_pagination(fw):
    w = fw
    w.outbound('mine', 'personal-alice', 'personal')
    w.outbound('theirs', 'personal-bob', 'personal')
    w.outbound('history')
    with w.store.transaction() as c:
        ids = w.faxes.visible_outbound_ids_on(c, w.actor, 'fax:read', now=NOW)
        jobs = w.tables['fax_jobs']
        query = sa.select(jobs.c.id).where(jobs.c.id.in_(ids))
        assert c.execute(query).scalars().all() == ['mine']
        assert c.scalar(sa.select(sa.func.count()).select_from(query.subquery())) == 1
        assert c.execute(query.limit(1).offset(1)).first() is None


def test_document_and_metadata_do_not_imply_each_other(fw):
    w = fw
    fax = w.outbound('shared', 'personal-bob', 'personal')
    w.outbound('hidden', 'personal-bob', 'personal')
    w.role('metadata', ['fax:read']); w.assignment('alice', 'metadata', fax.id)
    with w.store.transaction() as c:
        assert w.faxes.require_outbound_on(c, w.actor, 'shared', 'fax:read', now=NOW) == fax
        with pytest.raises(R.FaxAccessError) as denied:
            w.faxes.require_outbound_on(c, w.actor, 'shared', 'fax:document', now=NOW)
        assert denied.value.code == 'forbidden'
        for identity in ['unknown', 'hidden']:
            with pytest.raises(R.FaxAccessError) as hidden:
                w.faxes.require_outbound_on(c, w.actor, identity, 'fax:document', now=NOW)
            assert hidden.value.code == 'not_found'
    w.role('document', ['fax:document']); w.assignment('alice', 'document', fax.id)
    with w.store.transaction() as c:
        assert w.faxes.require_outbound_on(c, w.actor, 'shared', 'fax:document', now=NOW) == fax


def test_document_only_grant_allows_direct_bytes_but_not_metadata_list(fw):
    w = fw
    fax = w.outbound('document-only', 'personal-bob', 'personal')
    w.role('bytes', ['fax:document']); w.assignment('alice', 'bytes', fax.id)
    with w.store.transaction() as c:
        assert w.faxes.require_outbound_on(c, w.actor, 'document-only', 'fax:document', now=NOW) == fax
        assert c.execute(w.faxes.visible_outbound_ids_on(c, w.actor, 'fax:read', now=NOW)).all() == []


def test_revocation_between_preparation_and_acceptance_denies(fw):
    w = fw
    with w.store.transaction() as c:
        w.faxes.authorize_send_on(c, w.actor, now=NOW)
    w.update('access_principals', 'alice', security_version=2)
    with w.store.transaction() as c:
        with pytest.raises(StaleCredentialError):
            w.faxes.authorize_send_on(c, w.actor, now=NOW)


def test_missing_personal_or_reset_gate_denies_send(fw):
    w = fw
    w.update('access_users', 'alice', password_change_required=1)
    with w.store.transaction() as c:
        with pytest.raises(R.FaxAccessError) as denied:
            w.faxes.authorize_send_on(c, w.actor, now=NOW)
        assert denied.value.code == 'reset_required'
    w.update('access_users', 'alice', password_change_required=0)
    w.update('access_resources', 'personal-alice', enabled=0)
    with w.store.transaction() as c:
        with pytest.raises(R.FaxAccessError):
            w.faxes.authorize_send_on(c, w.actor, now=NOW)


def test_no_nested_or_unlocked_resource_reads(fw):
    w = fw
    with w.engine.begin() as c:
        with pytest.raises(InvalidTransactionError):
            w.faxes.visible_outbound_ids_on(c, w.actor, 'fax:read', now=NOW)
    with w.store.transaction() as c:
        with c.begin_nested():
            with pytest.raises(InvalidTransactionError):
                w.faxes.authorize_send_on(c, w.actor, now=NOW)


def test_does_not_reassign_existing_fax(fw):
    w = fw
    w.outbound('existing')
    with w.store.transaction() as c:
        with pytest.raises(R.FaxAccessError) as denied:
            w.faxes.record_outbound_on(c, w.actor, 'existing', now=NOW)
        assert denied.value.code == 'invalid_target'


@pytest.mark.parametrize('permission', ['fax:send', 'settings:read', 'unknown', True])
def test_closed_outbound_operations(fw, permission):
    with fw.store.transaction() as c:
        with pytest.raises(R.FaxAccessError) as denied:
            fw.faxes.visible_outbound_ids_on(c, fw.actor, permission, now=NOW)
        assert denied.value.code == 'invalid_input'
