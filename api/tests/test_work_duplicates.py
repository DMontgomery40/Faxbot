"""A partner's notice fax and a repaired call's first pages fold into the document: no second item or email.

The direct path's notice and repair tables come with revision 0046 (``direct/notice.py``,
``direct/repair.py``). Where they are not migrated yet, these tests create them with the columns
``work/duplicates.py`` reads, exactly as 0046 defines them; where they are, the migrated ones are used.
"""
from datetime import timedelta
import json
import uuid

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database  # noqa: F401 - fixture
from api.tests.test_access_policy import NOW
from api.tests.test_work_store import WorkWorld, operator
from api.app.intake.store import IntakeStore
from api.app.work import duplicates
from api.app.work.worker import WorkWorker


PARTNER_NUMBER = '+15557770100'


def direct_path_tables(engine):
    """0046's notice, scan and repair tables (the columns read here), unless the migration made them."""
    if set(duplicates.DIRECT_TABLES) <= set(sa.inspect(engine).get_table_names()):
        return
    metadata = sa.MetaData()

    def text(name, length=40, null=False):
        return sa.Column(name, sa.String(length), nullable=null)

    def when(name, null=False):
        return sa.Column(name, sa.DateTime(), nullable=null)
    sa.Table('direct_notices', metadata, sa.Column('id', sa.String(40), primary_key=True),
             text('role', 8), text('notice_id', 20), text('message_id', 32), text('peer_id'),
             text('document_sha256', 64), text('state', 16), sa.Column('link_statement', sa.Text(), nullable=False),
             text('link_signature', 128), text('inbound_id', null=True), text('matched_by', 8, null=True),
             when('paired_at', null=True), when('created_at'), when('updated_at'))
    sa.Table('direct_notice_scans', metadata, sa.Column('id', sa.String(40), primary_key=True),
             text('inbound_id'), text('found', 20, null=True), text('method', 8, null=True),
             text('notice_row_id', null=True), when('scanned_at'))
    sa.Table('direct_call_repairs', metadata, sa.Column('id', sa.String(40), primary_key=True),
             text('role', 8), text('repair_id', 32), text('peer_id'), text('job_id', null=True),
             text('attempt_id', null=True), text('message_id', 32, null=True), text('inbound_id', null=True),
             sa.Column('total_pages', sa.Integer(), nullable=False), sa.Column('pages_held', sa.Integer(), nullable=False),
             text('state', 16), sa.Column('statement', sa.Text(), nullable=False), text('signature', 128),
             when('created_at'), when('updated_at'))
    metadata.create_all(engine)


class DuplicateWorld(WorkWorld):
    def __init__(self, engine):
        super().__init__(engine)
        direct_path_tables(engine)
        names = ('direct_notices', 'direct_notice_scans', 'direct_call_repairs', 'direct_peers', 'direct_deliveries',
                 'intake_items', 'intake_connectors')
        metadata = sa.MetaData()
        self.direct = {name: sa.Table(name, metadata, autoload_with=engine) for name in names}
        self.add('direct_peers', id='peer-1', organization='Lakeside', phone_number=PARTNER_NUMBER,
                 endpoint_url='https://lakeside.example', signing_key='a' * 64, exchange_key='b' * 64,
                 state='verified', challenge_failures=0, version=1)
        self.intake = IntakeStore(engine, secrets=None)
        self.add('intake_connectors', kind='email', name='Front desk', enabled=1, match_number=None,
                 settings=json.dumps({'host': 'smtp.clinic.example', 'port': 587, 'security': 'starttls',
                                      'from_address': 'fax@clinic.example', 'recipients': ['desk@clinic.example']}),
                 secret_envelope=None, version=1, created_at=NOW - timedelta(days=1), updated_at=NOW - timedelta(days=1))

    def add(self, table_name, **values):
        table = self.direct[table_name]
        values.setdefault('id', uuid.uuid4().hex)
        for field in ('created_at', 'updated_at'):
            if field in table.c:
                values.setdefault(field, NOW - timedelta(minutes=20))
        with self.engine.begin() as connection:
            connection.execute(table.insert().values(**values))
        return values['id']

    def arrived(self, identity, *, pages, minutes_ago, from_number=PARTNER_NUMBER):
        moment = NOW - timedelta(minutes=minutes_ago)
        self.inbound.accept(dict(id=identity, from_number=from_number, to_number='+15550100001', status='received',
                                 backend='sip', pages=pages, size_bytes=10, pdf_path='/synthetic/' + identity + '.pdf',
                                 created_at=moment, received_at=moment, updated_at=moment), now=NOW)

    def filed(self, identity, message_id, *, pages):
        """The partner's document, filed in Received as direct/filing.py files it."""
        self.arrived(identity, pages=pages, minutes_ago=2)
        self.insert_work('inbound_imports', source='local', account='direct:peer-1', operation_id=message_id,
                         revision='', state='received', attempts=1, imported_at=NOW - timedelta(minutes=2),
                         acquired_at=NOW - timedelta(minutes=2), artifact_digest='a' * 64, artifact_size=10,
                         artifact_media_type='application/pdf', inbound_fax_id=identity)

    def waiting_original(self, message_id='m' * 32):
        self.add('direct_deliveries', direction='inbound', message_id=message_id, peer_id='peer-1',
                 recipient_number='+15550100001', digest='d' * 64, size_bytes=10, manifest='{}', state='accepted',
                 accepted_at=NOW - timedelta(minutes=5))
        return self.add('direct_notices', role='receiver', notice_id='1' * 20, message_id=message_id,
                        peer_id='peer-1', document_sha256='d' * 64, state='waiting', link_statement='{}',
                        link_signature='s' * 86, created_at=NOW - timedelta(minutes=5))

    def email_of(self, inbound_id):
        table = self.direct['intake_items']
        with self.engine.connect() as connection:
            row = connection.execute(sa.select(table).where(table.c.inbound_fax_id == inbound_id)).mappings().first()
        return dict(row) if row is not None else None

    def step(self):
        WorkWorker(self.work, control=lambda: self.control, values=lambda: type('V', (), {
            'work_acknowledge_hours': 0})()).step(now=NOW)
        self.intake.feed_inbound(now=NOW)


@pytest.fixture
def dw(database):  # noqa: F811
    return DuplicateWorld(database)


def test_a_possible_notice_waits_for_the_matcher_then_folds_into_the_document(dw):
    notice_row = dw.waiting_original()
    dw.arrived('notice-fax', pages=1, minutes_ago=1)
    dw.arrived('long-fax', pages=5, minutes_ago=1)
    dw.step()
    # A one-page fax while an original waits: no item and no email until the matcher has looked at it.
    assert dw.item_for('notice-fax') is None and dw.email_of('notice-fax') is None
    assert dw.item_for('long-fax') is not None and dw.email_of('long-fax') is not None
    # The matcher reads the subaddress, pairs them and files the original.
    dw.add('direct_notice_scans', inbound_id='notice-fax', found='1' * 20, method='sub', notice_row_id=notice_row,
           scanned_at=NOW)
    with dw.engine.begin() as connection:
        notices = dw.direct['direct_notices']
        connection.execute(notices.update().values(state='paired', inbound_id='notice-fax', matched_by='sub',
                                                   paired_at=NOW))
    dw.filed('original-doc', 'm' * 32, pages=12)
    dw.step()
    notice, document = dw.item_for('notice-fax'), dw.item_for('original-doc')
    assert document['state'] == 'open'
    assert notice['state'] == 'done' and notice['done_by'] is None
    assert notice['done_note'] == ('This page is the fax notice for a document Lakeside delivered directly; see the '
                                   'whole document.')
    folded = [event for event in dw.events(notice['id']) if event['kind'] == 'done']
    assert json.loads(folded[0]['details'])['folded_into'] == document['id']
    # Its email is not sent; the document's is.
    email = dw.email_of('notice-fax')
    assert email['next_attempt_at'] is None and email['state'] == 'received'
    assert email['last_error'] == ('This is the fax notice for a document delivered directly; that document is '
                                   'emailed instead.')
    assert dw.email_of('original-doc')['next_attempt_at'] is not None
    dw.step()
    assert len([event for event in dw.events(notice['id']) if event['kind'] == 'done']) == 1


def test_a_held_fax_is_released_after_a_while_so_a_real_fax_is_never_kept_back(dw):
    dw.waiting_original()
    dw.arrived('old-page', pages=1, minutes_ago=16)
    dw.step()
    assert dw.item_for('old-page') is not None and dw.email_of('old-page') is not None


def test_nothing_is_held_while_no_original_waits(dw):
    dw.arrived('one-page', pages=1, minutes_ago=1)
    dw.step()
    assert dw.item_for('one-page') is not None and dw.email_of('one-page') is not None


def test_a_repaired_calls_first_pages_fold_into_the_whole_document_with_the_reason(dw):
    dana = operator(dw, 'dana')
    dw.arrived('first-pages', pages=6, minutes_ago=30)
    dw.arrived('other-fax', pages=3, minutes_ago=30)
    dw.step()
    first = dw.item_for('first-pages')
    dw.service.assign(operator(dw, 'admin', 'installation', 'role_administrator'), first['id'], 'dana', version=1)
    # The other fax's email already went; the first pages' email is still waiting for its retry.
    with dw.engine.begin() as connection:
        items = dw.direct['intake_items']
        connection.execute(items.update().where(items.c.inbound_fax_id == 'other-fax').values(state='delivered'))
    dw.add('direct_call_repairs', role='receiver', repair_id='r' * 32, peer_id='peer-1', message_id='w' * 32,
           inbound_id='first-pages', total_pages=10, pages_held=6, state='completed', statement='{}',
           signature='s' * 86)
    dw.add('direct_call_repairs', role='receiver', repair_id='q' * 32, peer_id='peer-1', message_id='x' * 32,
           inbound_id='other-fax', total_pages=3, pages_held=3, state='completed', statement='{}', signature='s' * 86)
    dw.filed('whole-fax', 'w' * 32, pages=10)
    dw.step()
    first, whole = dw.item_for('first-pages'), dw.item_for('whole-fax')
    assert first['state'] == 'done'
    assert first['done_note'] == 'Pages 1–6 of this fax were completed directly by Lakeside; see the whole document.'
    # The owner of the first pages owns the whole document now.
    assert whole['owner_principal_id'] == 'dana' and whole['state'] == 'open'
    assert dw.service.detail(dana, whole['id'])['owner']['name'] == 'dana'
    assert dw.email_of('first-pages')['next_attempt_at'] is None
    assert dw.email_of('first-pages')['last_error'] == (
        'Pages 1–6 of this fax were completed directly by Lakeside; the whole document is emailed instead.')
    # A partner holding every page sends nothing more, so no whole document is filed: that fax stays as it is.
    assert dw.item_for('other-fax')['state'] == 'open' and dw.email_of('other-fax')['state'] == 'delivered'


def test_without_the_direct_path_tables_nothing_is_held_or_folded(database):  # noqa: F811
    world = WorkWorld(database)
    if set(duplicates.DIRECT_TABLES) <= set(sa.inspect(database).get_table_names()):
        pytest.skip('The direct path tables are migrated here.')
    assert duplicates.fold(world.work, world.control, now=NOW) == 0
    held = duplicates.held(database, world.work.inbound, now=NOW)
    assert held is sa.false() or str(held) == 'false'
