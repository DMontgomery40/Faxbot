"""Inbound placement, visibility and documents on migrated SQLite and PostgreSQL."""
from datetime import timedelta
import json

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_policy import World, NOW
from api.app.access.fax_resources import FaxAccessError
from api.app.access.inbound import AuthorizedInboundQueries, InboundResources
from api.app.access.types import StaleCredentialError


class InboundWorld(World):
    def __init__(self, engine):
        super().__init__(engine)
        self.inbound = InboundResources(self.control)
        self.queries = AuthorizedInboundQueries(self.inbound, clock=lambda: NOW)
        self.mailbox('front', 'Front Desk', '+1 (555) 010-0001')
        self.mailbox('billing', 'Billing', '+15550100002')

    def mailbox(self, identity, label, number, enabled=1):
        self.insert('mailboxes', id=identity, label=label)
        self.resource('mailbox-' + identity, 'mailbox', 'installation', 'installation', mailbox_id=identity, enabled=enabled)
        self.insert('inbound_rules', id='rule-' + identity, to_number=number, mailbox_label=label)
        self.insert('access_mailbox_routes', id='rule-' + identity, mailbox_id=identity)

    def fax(self, identity, to_number, received=0):
        moment = NOW - timedelta(minutes=30) + timedelta(seconds=received)
        return self.inbound.accept(dict(id=identity, from_number='+15559990000', to_number=to_number,
            status='received', backend='sip', pages=1, size_bytes=10, pdf_path='/synthetic/' + identity + '.pdf',
            pdf_token='token-' + identity, pdf_token_expires_at=NOW + timedelta(minutes=5),
            created_at=moment, received_at=moment, updated_at=moment), now=NOW)

    def resource_of(self, inbound_id):
        table = self.tables['access_resources']
        with self.engine.connect() as c:
            return dict(c.execute(sa.select(table).where(table.c.inbound_fax_id == inbound_id)).mappings().one())

    def audit(self, operation):
        table = self.tables['access_audit']
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(sa.select(table).where(table.c.operation == operation)).mappings()]


@pytest.fixture
def iw(database):
    return InboundWorld(database)


def test_ingest_routes_by_number_digits_and_audits_a_system_actor(iw):
    iw.fax('routed', '+15550100001')
    iw.fax('loose', '+1 555 010 0002')
    iw.fax('unrouted', '+15550109999')
    iw.fax('blank', None)
    assert iw.resource_of('routed')['parent_id'] == 'mailbox-front'
    assert iw.resource_of('loose')['parent_id'] == 'mailbox-billing'
    assert iw.resource_of('unrouted')['parent_id'] == 'legacy'
    assert iw.resource_of('blank')['parent_kind'] == 'legacy'
    audits = iw.audit('inbound.receive')
    assert len(audits) == 4
    assert all(a['actor_principal_id'] is None and a['actor_session_id'] is None for a in audits)
    # Random audit ids are hex and can contain '555'; check the recorded fields only.
    recorded = repr([{k: v for k, v in a.items() if k != 'id'} for a in audits])
    assert '555' not in recorded and 'token-' not in recorded
    assert {json.loads(a['details'])['placement'] for a in audits} == {'mailbox', 'unassigned'}


def test_disabled_mailbox_does_not_receive_new_faxes(iw):
    iw.update('access_resources', 'mailbox-front', enabled=0)
    iw.fax('while-disabled', '+15550100001')
    assert iw.resource_of('while-disabled')['parent_id'] == 'legacy'


def test_ingest_is_one_transaction_with_its_resource_and_audit(iw):
    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('INSERT INTO ACCESS_AUDIT'):
            raise RuntimeError('synthetic audit refusal')
    sa.event.listen(iw.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(Exception):
            iw.fax('lost', '+15550100001')
    finally:
        sa.event.remove(iw.engine, 'before_cursor_execute', fail)
    with iw.engine.connect() as c:
        assert c.scalar(sa.text("SELECT count(*) FROM inbound_faxes WHERE id = 'lost'")) == 0
        assert c.scalar(sa.text("SELECT count(*) FROM access_resources WHERE inbound_fax_id = 'lost'")) == 0


def test_mailbox_grant_limits_lists_before_limit_and_hides_other_faxes(iw):
    iw.fax('front-old', '+15550100001', received=0)
    iw.fax('billing-new', '+15550100002', received=10)
    iw.fax('legacy-new', '+15550109999', received=20)
    alice = iw.user('alice')
    iw.assignment('alice', 'role_fax_viewer', 'mailbox-front')
    page = iw.queries.page(alice, limit=1)
    assert [row['id'] for row in page] == ['front-old']
    assert page[0]['mailbox'] == 'Front Desk' and page[0]['fr'] == '+15559990000' and page[0]['backend'] == 'sip'
    for hidden in ('billing-new', 'legacy-new', 'missing'):
        for operation in (iw.queries.item, iw.queries.document):
            with pytest.raises(FaxAccessError) as error:
                operation(alice, hidden)
            assert error.value.code == 'not_found'
    assert iw.queries.document(alice, 'front-old')['pdf_path'] == '/synthetic/front-old.pdf'
    assert iw.queries.page(alice, mailbox='Billing') == []
    assert iw.queries.page(alice, status='failed') == []


def test_metadata_grant_never_implies_document_bytes(iw):
    iw.fax('fax', '+15550109999')
    auditor = iw.user('auditor')
    iw.assignment('auditor', 'role_auditor')
    assert [row['id'] for row in iw.queries.page(auditor)] == ['fax']
    assert iw.queries.item(auditor, 'fax')['mailbox'] is None
    with pytest.raises(FaxAccessError) as error:
        iw.queries.document(auditor, 'fax')
    assert error.value.code == 'forbidden'


def test_mailbox_filter_resolves_the_current_label_to_a_stable_id(iw):
    iw.fax('fax', '+15550100001')
    admin = iw.user('admin')
    iw.assignment('admin', 'role_fax_viewer')
    iw.update('mailboxes', 'front', label='Reception')
    assert [row['mailbox'] for row in iw.queries.page(admin, mailbox='Reception')] == ['Reception']
    assert iw.queries.page(admin, mailbox='Front Desk') == []
    assert iw.queries.item(admin, 'fax')['mailbox'] == 'Reception'


def test_revoked_actor_is_rejected_before_any_inbound_read(iw):
    iw.fax('fax', '+15550100001')
    alice = iw.user('alice')
    iw.assignment('alice', 'role_fax_viewer')
    iw.update('access_principals', 'alice', enabled=0, security_version=2)
    for operation in (lambda: iw.queries.page(alice), lambda: iw.queries.item(alice, 'fax'),
                      lambda: iw.queries.document(alice, 'fax')):
        with pytest.raises(StaleCredentialError):
            operation()


def test_backfill_places_resource_less_rows_under_legacy_once(iw):
    with iw.engine.begin() as c:
        c.execute(iw.tables['inbound_faxes'].insert().values(id='orphan', status='received', backend='phaxio',
            to_number='+15550100001', created_at=NOW, received_at=NOW, updated_at=NOW))
    assert iw.inbound.backfill(now=NOW) == 1
    assert iw.resource_of('orphan')['parent_id'] == 'legacy'
    assert iw.inbound.backfill(now=NOW) == 0
    assert len(iw.audit('inbound.backfill')) == 1


def test_download_token_is_exact_and_expires(iw):
    iw.fax('fax', '+15550100001')
    assert iw.queries.shared_document('fax', 'token-fax')['pdf_path'] == '/synthetic/fax.pdf'
    for token in ('token-fa', 'token-faxx', ''):
        with pytest.raises(FaxAccessError):
            iw.queries.shared_document('fax', token)
    iw.update('inbound_faxes', 'fax', pdf_token_expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(FaxAccessError):
        iw.queries.shared_document('fax', 'token-fax')
