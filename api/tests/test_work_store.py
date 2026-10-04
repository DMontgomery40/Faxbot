"""Work items over migrated SQLite and PostgreSQL: feeding, ownership, deadlines and visibility."""
from datetime import timedelta
import itertools
import json
import threading
import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_policy import NOW
from api.tests.test_inbound_access import InboundWorld
from api.app.work.service import WorkConflict, WorkForbidden, WorkInputError, WorkNotFound, WorkService
from api.app.work.store import WorkStore
from api.app.work.worker import WorkWorker


class WorkWorld(InboundWorld):
    def __init__(self, engine):
        super().__init__(engine)
        self.hours = 0
        self.work = WorkStore(engine)
        self.service = self.service_at(NOW)

    def service_at(self, moment):
        ticks = itertools.count()  # each operation a moment later, so history has an order
        return WorkService(self.work, SimpleNamespace(store=self.store, control=self.control),
                           values=lambda: SimpleNamespace(work_acknowledge_hours=self.hours),
                           clock=lambda: moment + timedelta(milliseconds=next(ticks)))

    def at(self, moment):
        """A service hours later, with every synthetic session still current then."""
        sessions = self.tables['access_sessions']
        with self.engine.begin() as connection:
            connection.execute(sessions.update().values(last_used_at=moment - timedelta(minutes=1),
                                                        expires_at=moment + timedelta(hours=1)))
        return self.service_at(moment)

    def insert_work(self, name, **values):
        table = getattr(self.work, {'work_mailbox_settings': 'settings', 'inbound_imports': 'imports'}[name])
        values.setdefault('id', uuid.uuid4().hex)
        for field in ('created_at', 'updated_at'):
            values.setdefault(field, NOW - timedelta(minutes=5))
        if 'version' in table.c:
            values.setdefault('version', 1)
        with self.engine.begin() as connection:
            connection.execute(table.insert().values(**values))

    def received(self, identity, to_number, *, digest=None, status='received', minutes_ago=30, pdf=True):
        moment = NOW - timedelta(minutes=minutes_ago)
        self.inbound.accept(dict(id=identity, from_number='+15559990000', to_number=to_number, status=status,
            backend='sip', pages=2, size_bytes=10, sha256=digest, pdf_path=('/synthetic/' + identity + '.pdf') if pdf else None,
            created_at=moment, received_at=moment, updated_at=moment), now=NOW)

    def feed(self, now=NOW):
        return self.work.feed(installation_hours=self.hours, now=now)

    def item_for(self, inbound_id):
        with self.engine.connect() as connection:
            row = connection.execute(sa.select(self.work.items).where(
                self.work.items.c.inbound_fax_id == inbound_id)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def events(self, item_id):
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(sa.select(self.work.events).where(
                self.work.events.c.work_item_id == item_id).order_by(self.work.events.c.occurred_at,
                                                                    self.work.events.c.created_at)).mappings()]

    def setting(self, mailbox_id, **values):
        self.insert_work('work_mailbox_settings', mailbox_id=mailbox_id, **values)


@pytest.fixture
def ww(database):
    return WorkWorld(database)


def operator(ww, name, mailbox='mailbox-front', role='role_fax_operator'):
    actor = ww.user(name)
    ww.assignment(name, role, mailbox)
    return actor


def test_received_document_becomes_one_owned_item_through_its_lifecycle(ww):
    ww.hours = 24
    admin = operator(ww, 'admin', 'installation', 'role_administrator')
    dana = operator(ww, 'dana')
    ww.received('fax-1', '+15550100001', digest='d' * 64)
    assert ww.feed() == 1 and ww.feed() == 0
    item = ww.item_for('fax-1')
    assert item['mailbox_id'] == 'front' and item['state'] == 'open' and item['content_digest'] == 'd' * 64
    assert item['available_at'] == NOW - timedelta(minutes=30)
    assert item['due_at'] == item['available_at'] + timedelta(hours=24)
    assert (item['due_hours'], item['due_source']) == (24, 'installation')
    view = ww.service.detail(admin, item['id'])
    assert view['state_text'] == 'Waiting for an owner.' and view['mailbox'] == 'Front Desk'
    assert view['due_text'] == 'Acknowledge within 24 hours of the document arriving (installation setting)'
    assert view['actions'] == ['assign', 'done', 'export', 'document'] and view['is_test'] is False
    assert [person['name'] for person in ww.service.assignees(admin, item['id'])] == ['admin', 'dana']

    assigned = ww.service.assign(admin, item['id'], 'dana', version=1)
    assert assigned['state_text'] == 'Assigned to dana; acknowledge by 4 Oct 11:30 UTC.'
    assert assigned['owner'] == {'id': 'dana', 'name': 'dana'} and assigned['version'] == 2
    with pytest.raises(WorkForbidden, match='Only the owner'):
        ww.service.acknowledge(admin, item['id'], version=2)
    acknowledged = ww.service.acknowledge(dana, item['id'], version=2)
    assert acknowledged['state_text'] == 'Acknowledged by dana.' and acknowledged['state'] == 'acknowledged'
    with pytest.raises(WorkInputError, match='short note'):
        ww.service.done(dana, item['id'], note=' ', version=3)
    done = ww.service.done(dana, item['id'], note='filed in the case system', version=3)
    assert done['state_text'] == 'Done: filed in the case system.' and done['actions'] == ['reopen', 'document']
    with pytest.raises(WorkConflict, match='This item changed; reload and try again.'):
        ww.service.reopen(admin, item['id'], version=3)
    reopened = ww.service.reopen(admin, item['id'], version=4)
    assert reopened['state'] == 'open' and reopened['owner']['id'] == 'dana'
    history = ww.service.history(admin, item['id'])
    assert [event['kind'] for event in history] == ['received', 'assigned', 'acknowledged', 'done', 'reopened']
    assert [event['text'] for event in history] == [
        'The document arrived in Front Desk.', 'admin assigned it to dana.', 'dana acknowledged it.',
        'dana marked it done: filed in the case system.', 'admin reopened it.']


def test_mailbox_target_overrides_the_installation_and_zero_means_none(ww):
    ww.hours = 24
    ww.setting('front', acknowledge_hours=4)
    ww.setting('billing', acknowledge_hours=0)
    ww.received('front-fax', '+15550100001')
    ww.received('billing-fax', '+15550100002')
    ww.received('loose-fax', '+15550109999')
    ww.feed()
    front, billing, loose = (ww.item_for(name) for name in ('front-fax', 'billing-fax', 'loose-fax'))
    assert (front['due_hours'], front['due_source']) == (4, 'mailbox')
    assert billing['due_at'] is None and billing['due_source'] is None
    assert loose['mailbox_id'] is None and (loose['due_hours'], loose['due_source']) == (24, 'installation')
    admin = operator(ww, 'admin', 'installation', 'role_administrator')
    assert ww.service.detail(admin, front['id'])['due_text'] == (
        'Acknowledge within 4 hours of the document arriving (mailbox setting)')
    assert ww.service.detail(admin, billing['id'])['due_text'] == 'No acknowledgement target set'


def test_equal_bytes_from_distinct_events_stay_two_items_marked_as_the_same_document(ww):
    admin = operator(ww, 'admin', 'installation', 'role_administrator')
    ww.received('first', '+15550100001', digest='e' * 64, minutes_ago=40)
    ww.received('second', '+15550100001', digest='e' * 64, minutes_ago=10)
    ww.received('other', '+15550100001', digest='f' * 64)
    assert ww.feed() == 3
    views = {view['inbound_fax_id']: view for view in ww.service.list(admin)}
    assert views['first']['duplicate_of']['id'] == views['second']['id']
    assert views['second']['duplicate_of'] == {'id': views['first']['id'], 'available_at': NOW - timedelta(minutes=40)}
    assert views['other']['duplicate_of'] is None


def test_waiting_or_failed_documents_have_no_item_until_received(ww):
    ww.received('waiting', '+15550100001', status='waiting', pdf=False)
    ww.received('failed', '+15550100001', status='failed', pdf=False)
    ww.received('legacy', '+15550100001', status='SUCCESS')  # an older status with a stored document
    ww.received('placeholder', '+15550100001', status='SUCCESS', pdf=False)
    assert ww.feed() == 1
    assert ww.item_for('waiting') is None and ww.item_for('failed') is None and ww.item_for('placeholder') is None
    assert ww.item_for('legacy') is not None
    ww.update('inbound_faxes', 'waiting', status='received', pdf_path='/synthetic/waiting.pdf')
    assert ww.feed() == 1 and ww.item_for('waiting')['state'] == 'open'


def test_clock_starts_when_the_document_was_acquired(ww):
    ww.hours = 2
    ww.received('late', '+15550100001', minutes_ago=300)
    ww.insert_work('inbound_imports', source='import', account='import:admin', operation_id='op-1', revision='',
              state='received', attempts=1, imported_at=NOW - timedelta(minutes=300),
              acquired_at=NOW - timedelta(minutes=5), artifact_digest='a' * 64, artifact_size=10,
              artifact_media_type='application/pdf', inbound_fax_id='late')
    ww.feed()
    item = ww.item_for('late')
    assert item['available_at'] == NOW - timedelta(minutes=5)
    assert item['due_at'] == NOW - timedelta(minutes=5) + timedelta(hours=2)


def test_missed_target_escalates_once_to_a_backup_who_can_see_it_and_survives_restart(ww):
    ww.hours = 1
    dana, sam = operator(ww, 'dana'), operator(ww, 'sam')
    admin = operator(ww, 'admin', 'installation', 'role_administrator')
    ww.setting('front', backup_principal_id='sam')
    ww.received('fax', '+15550100001')
    ww.feed()
    item = ww.item_for('fax')
    ww.service.assign(admin, item['id'], 'dana', version=1)
    later = NOW + timedelta(hours=2)
    assert ww.work.escalate(ww.control, now=NOW) == 0  # not yet due
    assert ww.work.escalate(ww.control, now=later) == 1
    assert ww.work.escalate(ww.control, now=later + timedelta(minutes=5)) == 0
    escalated = ww.item_for('fax')
    assert escalated['owner_principal_id'] == 'sam' and escalated['due_at'] == item['due_at']
    kinds = [event['kind'] for event in ww.events(item['id'])]
    assert kinds.count('escalated') == 1
    event = next(event for event in ww.events(item['id']) if event['kind'] == 'escalated')
    assert json.loads(event['details'])['from_name'] == 'dana' and event['dedupe_key'].startswith('escalated:')
    later_service = ww.at(later)
    view = later_service.detail(admin, item['id'])
    assert view['state_text'] == 'Overdue; escalated to sam.'
    assert later_service.history(admin, item['id'])[-1]['text'] == 'Not acknowledged in time; escalated to sam.'
    # A restarted worker and a replayed notification change nothing already decided.
    restarted = WorkStore(ww.engine)
    worker = WorkWorker(restarted, control=lambda: ww.control, values=lambda: SimpleNamespace(work_acknowledge_hours=9))
    worker.step(now=later + timedelta(hours=1))
    assert ww.item_for('fax')['due_at'] == item['due_at']
    assert [event['kind'] for event in ww.events(item['id'])].count('escalated') == 1
    assert later_service.list(sam, view='mine')[0]['id'] == item['id']


def test_escalation_without_a_usable_backup_keeps_the_owner_and_says_why(ww):
    ww.hours = 1
    ww.user('outsider')  # no access to the mailbox
    ww.setting('front', backup_principal_id='outsider')
    ww.received('fax', '+15550100001')
    ww.received('billing', '+15550100002')
    ww.feed()
    later = NOW + timedelta(hours=2)
    assert ww.work.escalate(ww.control, now=later) == 2
    front, billing = ww.item_for('fax'), ww.item_for('billing')
    assert front['owner_principal_id'] is None and front['escalated_at'] == later
    admin = operator(ww, 'admin', 'installation', 'role_administrator')
    later_service = ww.at(later)
    texts = [event['text'] for event in later_service.history(admin, front['id'])]
    assert texts[-1] == ('Not acknowledged in time; outsider is the backup but cannot see this document, '
                         'so the owner did not change.')
    texts = [event['text'] for event in later_service.history(admin, billing['id'])]
    assert texts[-1] == 'Not acknowledged in time; no backup person is set, so the owner did not change.'
    assert later_service.detail(admin, front['id'])['state_text'] == 'Overdue; waiting for an owner.'


def test_escalation_that_loses_a_race_to_a_person_writes_nothing_and_retries(ww, monkeypatch):
    ww.hours = 1
    admin = operator(ww, 'admin', 'installation', 'role_administrator')
    operator(ww, 'dana')
    ww.received('fax', '+15550100001')
    ww.feed()
    item = ww.item_for('fax')
    stale = dict(item)
    original = ww.work.item_on
    calls = []

    def racing(connection, identity):
        calls.append(identity)
        return stale if len(calls) == 1 else original(connection, identity)
    monkeypatch.setattr(ww.work, 'item_on', racing)
    ww.service.assign(admin, item['id'], 'dana', version=1)  # the person's change commits first
    later = NOW + timedelta(hours=2)
    assert ww.work.escalate(ww.control, now=later) == 0
    assert [event['kind'] for event in ww.events(item['id'])] == ['received', 'assigned']
    assert ww.work.escalate(ww.control, now=later) == 1
    assert [event['kind'] for event in ww.events(item['id'])] == ['received', 'assigned', 'escalated']


def test_two_assignments_from_the_same_version_have_one_winner(ww):
    alice, bob = operator(ww, 'alice'), operator(ww, 'bob')
    operator(ww, 'carol'), operator(ww, 'dave')
    ww.received('fax', '+15550100001')
    ww.feed()
    item = ww.item_for('fax')
    barrier, results = threading.Barrier(2), {}

    def assign(actor, target):
        barrier.wait()
        try:
            results[target] = ww.service.assign(actor, item['id'], target, version=1)['owner']['id']
        except WorkConflict as conflict:
            results[target] = conflict.message
    threads = [threading.Thread(target=assign, args=(alice, 'carol')), threading.Thread(target=assign, args=(bob, 'dave'))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [owner for owner in results.values() if owner in {'carol', 'dave'}]
    assert len(winners) == 1 and list(results.values()).count('This item changed; reload and try again.') == 1
    final = ww.item_for('fax')
    assert final['owner_principal_id'] == winners[0] and final['version'] == 2
    assert [event['kind'] for event in ww.events(item['id'])] == ['received', 'assigned']


def test_mailbox_operator_sees_only_their_mailbox_and_cannot_assign_outsiders(ww):
    ww.hours = 1
    front = operator(ww, 'front-operator')
    operator(ww, 'billing-operator', 'mailbox-billing')
    ww.user('nobody')
    ww.received('front-fax', '+15550100001')
    ww.received('billing-fax', '+15550100002')
    ww.received('loose-fax', '+15550109999')
    ww.feed()
    viewer = operator(ww, 'viewer', role='role_fax_viewer')
    strangers = ww.user('stranger'), ww.user('stranger2')
    later = ww.at(NOW + timedelta(hours=2))
    assert [view['inbound_fax_id'] for view in later.list(front)] == ['front-fax']
    assert later.counts(front) == {'open': 1, 'acknowledged': 0, 'done': 0, 'unassigned': 1, 'mine': 0, 'overdue': 1}
    billing = ww.item_for('billing-fax')
    for call in (lambda: later.detail(front, billing['id']), lambda: later.history(front, billing['id']),
                 lambda: later.assignees(front, billing['id']),
                 lambda: later.assign(front, billing['id'], 'front-operator', version=1),
                 lambda: later.detail(front, 'missing')):
        with pytest.raises(WorkNotFound, match='This work item was not found.'):
            call()
    own = ww.item_for('front-fax')
    with pytest.raises(WorkInputError, match='nobody cannot see this document, so it cannot be assigned to them.'):
        later.assign(front, own['id'], 'nobody', version=1)
    with pytest.raises(WorkInputError, match='billing-operator cannot see this document'):
        later.assign(front, own['id'], 'billing-operator', version=1)
    assert [view['actions'] for view in later.list(viewer)] == [['document']]
    with pytest.raises(WorkForbidden):
        later.assign(viewer, own['id'], 'front-operator', version=1)
    assert later.list(strangers[0]) == []
    assert later.counts(strangers[1])['open'] == 0


def test_owner_who_loses_access_is_shown_for_reassignment(ww):
    admin = operator(ww, 'admin', 'installation', 'role_administrator')
    operator(ww, 'dana')
    ww.received('fax', '+15550100001')
    ww.feed()
    item = ww.item_for('fax')
    ww.service.assign(admin, item['id'], 'dana', version=1)
    assert ww.service.detail(admin, item['id'])['owner_can_see'] is True
    ww.update('access_principals', 'dana', enabled=0)
    view = ww.service.detail(admin, item['id'])
    assert view['owner_can_see'] is False and 'assign' in view['actions']
    assert [person['id'] for person in ww.service.assignees(admin, item['id'])] == ['admin']


def test_mailbox_settings_require_a_backup_who_sees_the_whole_mailbox(ww):
    admin = operator(ww, 'admin', 'installation', 'role_administrator')
    operator(ww, 'dana')
    ww.user('nobody')
    current = ww.service.settings(admin)
    front = next(row for row in current['mailboxes'] if row['mailbox_id'] == 'front')
    assert front['version'] == 0 and [person['id'] for person in front['people']] == ['admin', 'dana']
    with pytest.raises(WorkInputError, match='nobody cannot see every document in Front Desk'):
        ww.service.update_settings(admin, [{'mailbox_id': 'front', 'acknowledge_hours': 8,
                                            'backup_principal_id': 'nobody', 'version': 0}])
    with pytest.raises(WorkInputError, match='from 0 to 8760 hours'):
        ww.service.update_settings(admin, [{'mailbox_id': 'front', 'acknowledge_hours': -1, 'version': 0}])
    saved = ww.service.update_settings(admin, [{'mailbox_id': 'front', 'acknowledge_hours': 8,
                                                'backup_principal_id': 'dana', 'version': 0}])
    front = next(row for row in saved['mailboxes'] if row['mailbox_id'] == 'front')
    assert front['acknowledge_hours'] == 8 and front['backup'] == {'id': 'dana', 'name': 'dana'} and front['version'] == 1
    with pytest.raises(WorkConflict):
        ww.service.update_settings(admin, [{'mailbox_id': 'front', 'acknowledge_hours': 2, 'version': 0}])
    with pytest.raises(WorkForbidden):
        ww.service.settings(ww.user('dana-viewer'))


def test_a_mailbox_operator_from_before_the_upgrade_sees_that_mailboxes_work(database):
    """An installation upgraded from 0009: role holders gain work on what they could already see."""
    from api.tests.test_access_policy import World
    from api.tests.test_access_schema import at_revision
    from api.app.access.policy import AccessControl
    from api.app.access.store import AccessStore
    from api.app.schema import upgrade_schema
    at_revision(database, '0009_sip_call_records')
    before = World.__new__(World)  # the same synthetic rows, written into the older schema
    metadata = sa.MetaData()
    metadata.reflect(database)
    before.engine, before.tables = database, metadata.tables
    olive = before.user('olive')
    for mailbox in ('front', 'billing'):
        before.insert('mailboxes', id=mailbox, label=mailbox.title())
        before.resource('mailbox-' + mailbox, 'mailbox', 'installation', 'installation', mailbox_id=mailbox)
        moment = NOW - timedelta(minutes=30)
        before.insert('inbound_faxes', id='fax-' + mailbox, from_number='+15559990000', to_number='+15550100001',
                      status='received', backend='sip', pages=1, pdf_path=f'/synthetic/{mailbox}.pdf',
                      created_at=moment, received_at=moment, updated_at=moment)
        before.resource('resource-' + mailbox, 'inbound', 'mailbox-' + mailbox, 'mailbox', inbound_fax_id='fax-' + mailbox)
    before.assignment('olive', 'role_fax_operator', 'mailbox-front')
    upgrade_schema(database)
    store = AccessStore(database)
    work = WorkStore(database)
    assert work.feed(installation_hours=0, now=NOW) == 2
    service = WorkService(work, SimpleNamespace(store=store, control=AccessControl(store)),
                          values=lambda: SimpleNamespace(work_acknowledge_hours=0), clock=lambda: NOW)
    (item,) = service.list(olive)
    assert item['inbound_fax_id'] == 'fax-front' and item['mailbox'] == 'Front'
    assert item['actions'] == ['assign', 'done', 'document']
    assert [person['id'] for person in service.assignees(olive, item['id'])] == ['olive']
