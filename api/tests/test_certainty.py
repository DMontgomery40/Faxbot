"""Uncertain sent faxes over migrated SQLite and PostgreSQL: one owned item each, checks by cost, settled by a person."""
import asyncio
from datetime import timedelta
import itertools
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database  # noqa: F401 - fixture
from api.tests.test_access_policy import NOW, World
from api.app.work import certainty_checks as checks
from api.app.work.certainty import CertaintyStore, CertaintyWorker, PartnerQuestion, query_id, resend_id
from api.app.work.certainty_service import CertaintyConflict, CertaintyForbidden, CertaintyInputError, CertaintyService


NUMBER = '+12025550123'


class CertaintyWorld(World):
    def __init__(self, engine, folder):
        super().__init__(engine)
        self.folder = folder
        self.certainty = CertaintyStore(engine)
        self.values = SimpleNamespace(direct_organization='Riverside Clinic', fax_data_dir=str(folder),
                                      fax_reply_number='', fax_station_id='', fax_default_country='US')
        self.accepted = []
        self.service = self.service_at(NOW)
        names = ('work_mailbox_settings', 'fax_job_rule_decisions', 'direct_peers', 'direct_deliveries',
                 'sip_call_records', 'carrier_charges', 'outbound_attempts', 'outbound_deliveries', 'fax_jobs',
                 'mailboxes', 'direct_call_repairs')
        metadata = sa.MetaData()
        self.tables = {**self.tables, **{name: sa.Table(name, metadata, autoload_with=engine) for name in names}}
        self.insert('mailboxes', id='front', label='Front Desk')
        self.resource('mailbox-front', 'mailbox', 'installation', 'installation', mailbox_id='front')

    def service_at(self, moment):
        ticks = itertools.count()
        return CertaintyService(self.certainty, SimpleNamespace(store=self.store, control=self.control),
                                values=lambda: self.values,
                                clock=lambda: moment + timedelta(milliseconds=next(ticks)))

    def sent(self, job_id, *, sender=None, state='reconciliation_required', phase='uncertain',
             category='transport_ambiguous', dispatch='normal', pages=3, number=NUMBER, minutes_ago=20):
        moment = NOW - timedelta(minutes=minutes_ago)
        parent, kind = ('personal-' + sender, 'personal') if sender else ('legacy', 'legacy')
        self.insert('fax_jobs', id=job_id, to_number=number, file_name='referral.pdf', tiff_path='',
                    status='queued', backend='phaxio', pages=pages, created_at=moment, updated_at=moment)
        self.resource('resource-' + job_id, 'outbound', parent, kind, fax_job_id=job_id)
        tables = self.certainty
        with self.engine.begin() as connection:
            connection.execute(tables.attempts.insert().values(
                id='attempt-' + job_id, job_id=job_id, sequence=1, phase=phase, error_category=category,
                created_at=moment, submitted_at=moment))
            connection.execute(tables.deliveries.insert().values(
                id=job_id, dispatch_mode=dispatch, state=state, version=3, attempt_id='attempt-' + job_id,
                created_at=moment, updated_at=moment))
        (self.folder / f'{job_id}.pdf').write_bytes(b'%PDF-1.4 synthetic referral')

    def operator(self, name, role='role_fax_operator', resource='installation'):
        actor = self.user(name)
        self.assignment(name, role, resource)
        return actor

    def feed(self, now=NOW):
        return self.certainty.feed(self.control, now=now)

    def item_for(self, job_id):
        with self.engine.connect() as connection:
            row = connection.execute(sa.select(self.certainty.items).where(
                self.certainty.items.c.job_id == job_id)).mappings().first()
        return dict(row) if row is not None else None

    def events(self, item_id):
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(sa.select(self.certainty.events).where(
                self.certainty.events.c.item_id == item_id).order_by(self.certainty.events.c.created_at))
                .mappings()]

    def job_count(self):
        with self.engine.connect() as connection:
            return connection.execute(sa.select(sa.func.count()).select_from(self.tables['fax_jobs'])).scalar()

    def accept(self, sender):
        """A stand-in for ``accept_generated_fax`` bound to ``sender``: records the fax as queued for them."""
        def accept(*, to_number, document, file_name, pages, job_id):
            if any(entry[0] == job_id for entry in self.accepted):
                return False
            self.accepted.append((job_id, to_number, document, file_name, pages))
            self.insert('fax_jobs', id=job_id, to_number=to_number, file_name=file_name, tiff_path='',
                        status='queued', backend='phaxio', pages=pages)
            self.resource('r-' + job_id, 'outbound', 'personal-' + sender, 'personal', fax_job_id=job_id)
            return True
        return accept


@pytest.fixture
def cw(database, tmp_path):  # noqa: F811
    return CertaintyWorld(database, tmp_path)


def test_each_uncertain_outcome_becomes_one_item_owned_by_the_sender(cw):
    cw.operator('dana')
    cw.sent('fax-1', sender='dana')
    cw.sent('fax-partly', sender='dana', state='failed', phase='failed', category='partly_sent')
    cw.sent('fax-ok', sender='dana', state='success', phase='success', category=None)
    cw.sent('fax-legacy', dispatch='legacy')
    cw.sent('fax-held', sender='dana', dispatch='held')
    assert cw.feed() == 2 and cw.feed() == 0
    item = cw.item_for('fax-1')
    assert (item['state'], item['owner_principal_id'], item['owner_source']) == ('open', 'dana', 'sender')
    # The deadline counts from when Faxbot made the item: an old uncertain fax does not arrive overdue.
    assert item['due_hours'] == 24 and item['due_at'] == NOW + timedelta(hours=24)
    assert len(item['reference']) == 6 and set(item['reference']) <= set('ACDEFGHJKMNPQRTUVWXY34679')
    assert cw.item_for('fax-partly')['category'] == 'partly_sent'
    assert cw.item_for('fax-ok') is None and cw.item_for('fax-legacy') is None and cw.item_for('fax-held') is None
    assert [event['kind'] for event in cw.events(item['id'])] == ['opened']
    view = cw.service.detail(cw.operator('admin', 'role_administrator'), item['id'])
    assert view['state_text'].startswith('Assigned to dana; settle by ')
    assert view['why'] == 'The fax service did not confirm it accepted the fax.'
    assert view['to_number'] == '********0123' and view['number'] == NUMBER


def test_owner_falls_back_to_the_mailbox_backup_then_the_fallback_person(cw):
    cw.user('sam')  # sent it, but has no role: cannot see sent faxes
    cw.operator('bea', 'role_fax_viewer')
    cw.operator('fran', 'role_fax_viewer')
    cw.insert('work_mailbox_settings', mailbox_id='front', acknowledge_hours=None, backup_principal_id='bea')
    cw.sent('from-mailbox', sender='sam')
    cw.sent('no-mailbox', sender='sam')
    cw.insert('fax_job_rule_decisions', job_id='from-mailbox', sequence=1, revisions='{}',
              facts=json.dumps({'mailbox_id': 'front'}), facts_digest='0' * 64, decision='{}', outcome='route')
    admin = cw.operator('admin', 'role_administrator')
    cw.service.update_settings(admin, fallback_principal_id='fran', settle_hours=8, version=0)
    cw.feed()
    by_mailbox, by_fallback = cw.item_for('from-mailbox'), cw.item_for('no-mailbox')
    assert (by_mailbox['owner_principal_id'], by_mailbox['owner_source'], by_mailbox['mailbox_id']) == (
        'bea', 'mailbox', 'front')
    assert (by_fallback['owner_principal_id'], by_fallback['owner_source']) == ('fran', 'fallback')
    assert by_fallback['due_hours'] == 8
    with pytest.raises(CertaintyInputError, match='cannot see every sent fax'):
        cw.service.update_settings(admin, fallback_principal_id='sam', settle_hours=8, version=1)
    # Nobody who can see it: the item waits for a person to assign it.
    cw.service.update_settings(admin, fallback_principal_id=None, settle_hours=0, version=1)
    cw.sent('nobody', sender='sam')
    cw.feed()
    alone = cw.item_for('nobody')
    assert alone['owner_principal_id'] is None and alone['due_at'] is None
    assert cw.service.detail(admin, alone['id'])['state_text'] == 'Waiting for an owner.'


def test_checks_come_cheapest_first_and_nothing_is_sent_by_the_worker(cw):
    dana = cw.operator('dana')
    cw.sent('fax-1', sender='dana')
    worker = CertaintyWorker(cw.certainty, control=lambda: cw.control)
    jobs = cw.job_count()
    worker.step(now=NOW)
    worker.step(now=NOW + timedelta(minutes=5))
    assert cw.job_count() == jobs  # no receipt query, no new fax: only a person sends those
    item = cw.item_for('fax-1')
    view = cw.service.detail(dana, item['id'])
    assert [check['kind'] for check in view['checks']] == list(checks.ORDER)
    assert [check['cost'] for check in view['checks']] == ['Free', 'Free', 'One page', 'A few minutes']
    partner, record, query, phone = view['checks']
    assert partner['result'] == 'unavailable' and partner['text'].startswith('This number is not one of your partners')
    # Phaxio reports no call duration (its fax object, read 2026-10-07): said plainly, not guessed.
    assert record['result'] == 'unavailable' and 'Phaxio does not report' in record['text']
    assert query['action'] == 'send_query' and 'Faxbot never sends it on its own' in query['text']
    assert phone['script'][0] == f'Call {NUMBER}.' and item['reference'] in phone['script'][1]
    assert view['actions'] == ['settle', 'send_query'] and view['suggestion'] is None


def test_a_partners_signed_answer_settles_it_and_the_person_who_decides_is_recorded(cw):
    dana = cw.operator('dana')
    cw.insert('direct_peers', id='peer-1', organization='Lakeside Hospital', phone_number=NUMBER,
              endpoint_url='https://lakeside.example', signing_key='a' * 64, exchange_key='b' * 64,
              state='verified', challenge_failures=0)
    cw.sent('fax-1', sender='dana')
    cw.insert('direct_deliveries', direction='outbound', message_id='m' * 32, peer_id='peer-1', job_id='fax-1',
              attempt_id='attempt-fax-1', recipient_number=NUMBER, digest='d' * 64, size_bytes=10, manifest='{}',
              state='uncertain')
    cw.feed()
    item = cw.item_for('fax-1')
    asking = cw.service.detail(dana, item['id'])['checks'][0]
    assert asking['result'] == 'asking' and 'Lakeside Hospital' in asking['text']
    record = cw.service.detail(dana, item['id'])['checks'][1]
    assert record['result'] == 'unavailable' and 'over the internet' in record['text']
    # The direct path's own question gets the answer; this item only reads it, and never asks again.
    with cw.engine.begin() as connection:
        connection.execute(cw.tables['direct_deliveries'].update().values(state='accepted'))
    view = cw.service.detail(dana, item['id'])
    assert view['checks'][0]['result'] == 'delivered' and view['checks'][0]['strength'] == 'proof'
    assert view['suggestion'] == 'delivered'
    CertaintyWorker(cw.certainty, control=lambda: cw.control).step(now=NOW)
    assert [event['kind'] for event in cw.events(item['id'])] == ['opened', 'probe']
    with pytest.raises(CertaintyInputError, match='how you know'):
        cw.service.settle(dana, item['id'], outcome='delivered', reason=' ', version=1)
    settled = cw.service.settle(dana, item['id'], outcome='delivered', reason='Lakeside signed a receipt', version=1)
    assert settled['state'] == 'settled' and settled['outcome'] == 'delivered' and settled['settled_by'] == 'dana'
    assert settled['state_text'] == 'Settled as delivered by dana.'
    last = cw.events(item['id'])[-1]
    details = json.loads(last['details'])
    assert last['kind'] == 'settled' and last['actor_principal_id'] == 'dana'
    assert details['evidence'][0] == {'kind': 'partner', 'result': 'delivered',
                                      'text': 'Lakeside Hospital signed a receipt for this document.'}
    # The fax's own delivery record is never changed by settling.
    with cw.engine.connect() as connection:
        assert connection.execute(sa.select(cw.certainty.deliveries.c.state).where(
            cw.certainty.deliveries.c.id == 'fax-1')).scalar() == 'reconciliation_required'
    with pytest.raises(CertaintyConflict, match='already settled'):
        cw.service.settle(dana, item['id'], outcome='unknown', reason='second thoughts', version=2)


def test_a_call_far_shorter_than_the_fax_needs_reads_probably_not_delivered_never_proof():
    from api.app.routing.predict import SETUP_SECONDS
    short = checks.duration_reading(12, 180, 10, source="Telnyx's record of the call")
    assert short['result'] == 'probably_not_delivered' and short['strength'] == 'reading'
    assert short['meaning'] == 'The fax probably did not arrive. This reads the call record; it is not proof.'
    part = checks.duration_reading(60, 180, 10, source="Telnyx's record of the call")
    assert part['result'] == 'probably_partial' and 'not proof' in part['meaning']
    whole = checks.duration_reading(175, 180, 10, source="Faxbot's own record of the call")
    assert whole['result'] == 'consistent' and whole['meaning'] == 'That fits a delivered fax, but it does not prove it.'
    # A call can run shorter than predicted when the machines use a better coding: within that, it fits.
    fastest = SETUP_SECONDS + (180 - SETUP_SECONDS) * checks._fastest_share()
    assert checks.duration_reading(fastest + 1, 180, 10, source='x')['result'] == 'consistent'
    unknown = checks.duration_reading(30, None, 10, source="Faxbot's own record of the call")
    assert unknown['result'] == 'unknown' and unknown['strength'] is None
    assert checks.suggestion([short]) == 'not_delivered'
    assert checks.suggestion([whole]) is None


def test_the_call_record_check_reads_the_carriers_seconds_against_the_prediction(cw, monkeypatch):
    dana = cw.operator('dana')
    cw.sent('fax-1', sender='dana', pages=10)
    cw.insert('sip_call_records', id='call-1', direction='outbound', call_id='c-1', job_id='fax-1',
              attempt_id='attempt-fax-1', caller='+12025550100', called=NUMBER, started_at=NOW - timedelta(minutes=19),
              answered_at=NOW - timedelta(minutes=19), ended_at=NOW - timedelta(minutes=18, seconds=40),
              disposition='answered', connected_seconds=20, t38='yes', pages=None, fax_preference=1)
    with cw.engine.begin() as connection:
        connection.execute(cw.certainty.items.delete())
    monkeypatch.setattr(checks, 'predicted_seconds', lambda route, number, job, now=None: 180.0)
    cw.feed()
    item = cw.item_for('fax-1')
    record = cw.service.detail(dana, item['id'])['checks'][1]
    assert record['result'] == 'probably_not_delivered'
    assert record['text'].startswith("Faxbot's own record of the call shows the call lasted about 20 seconds")
    cw.insert('carrier_charges', call_record_id='call-1', provider_id='telnyx', record_id='r-1', version=1,
              amount_micros=5000, raw_amount='0.005', currency='USD', billed_seconds=60, call_seconds=170,
              match_method='call_id', effective_at=NOW, observed_at=NOW, applied=1, is_final=1)
    record = cw.service.detail(dana, item['id'])['checks'][1]
    assert record['result'] == 'consistent' and record['text'].startswith("Telnyx's record of the call shows")
    # How the call ended comes with the reading: the fax engine's record of the hang-up.
    cw.update('sip_call_records', 'call-1', fax_status='FAILED',
              error_cause='remote_fax_failed: The call dropped prematurely (cause 16)')
    record = cw.service.detail(dana, item['id'])['checks'][1]
    assert record['text'].endswith('The other fax machine answered but the fax failed: The call dropped prematurely.')
    unanswered = dict(disposition='no_answer', answered_at=None, connected_seconds=None)
    cw.update('sip_call_records', 'call-1', **unanswered)
    record = cw.service.detail(dana, item['id'])['checks'][1]
    assert record['result'] == 'probably_not_delivered' and record['text'] == 'Nobody answered the call.'


def test_the_receipt_query_goes_only_when_a_person_sends_it_and_only_once(cw):
    dana = cw.operator('dana')
    cw.sent('fax-1', sender='dana')
    cw.feed()
    item = cw.item_for('fax-1')
    document, name, _ = cw.service.draft(dana, item['id'])
    assert document.startswith(b'%PDF') and name == f"receipt-query-{item['reference']}.pdf"
    assert cw.job_count() == 1  # drafting sends nothing: the original is the only fax
    accept = cw.accept('dana')
    sent = cw.service.send_query(dana, item['id'], version=1, send=accept)
    assert sent['query_fax_id'] == query_id(item['id']) and len(cw.accepted) == 1
    job_id, to_number, page, file_name, pages = cw.accepted[0]
    assert (to_number, pages) == (NUMBER, 1) and b'synthetic referral' not in page
    with pytest.raises(CertaintyConflict, match='already sent'):
        cw.service.send_query(dana, item['id'], version=sent['version'], send=accept)
    assert len(cw.accepted) == 1
    query = cw.service.detail(dana, item['id'])['checks'][2]
    assert query['result'] == 'sent' and query['fax_id'] == job_id
    # The receipt query is not itself an uncertain fax with an item of its own.
    cw.insert('outbound_attempts', id='attempt-q', job_id=job_id, sequence=1, phase='uncertain',
              error_category='transport_ambiguous', created_at=NOW)
    cw.insert('outbound_deliveries', id=job_id, dispatch_mode='normal', state='reconciliation_required',
              attempt_id='attempt-q')
    assert cw.feed() == 0
    assert [event['kind'] for event in cw.events(item['id'])] == ['opened', 'drafted', 'query_sent']


def test_not_delivered_send_again_is_a_new_fax_linked_to_the_first(cw):
    dana = cw.operator('dana')
    cw.sent('fax-1', sender='dana')
    cw.feed()
    item = cw.item_for('fax-1')
    with pytest.raises(CertaintyInputError, match='Only a fax that did not arrive'):
        cw.service.settle(dana, item['id'], outcome='delivered', reason='they have it', version=1, send_again=True,
                          send=cw.accept('dana'))
    settled = cw.service.settle(dana, item['id'], outcome='not_delivered', reason='Front desk says no fax came',
                                version=1, send_again=True, send=cw.accept('dana'))
    new_id = resend_id(item['id'])
    assert settled['resend_fax_id'] == new_id and settled['outcome'] == 'not_delivered'
    assert settled['state_text'] == 'Settled as not delivered by dana. The fax was sent again as a new fax.'
    assert cw.accepted[0][:3] == (new_id, NUMBER, b'%PDF-1.4 synthetic referral')
    linked = cw.service.for_fax(dana, new_id)
    assert linked == {'items': [], 'about': {'fax_id': 'fax-1', 'kind': 'resend'}}
    assert cw.service.for_fax(dana, 'fax-1')['items'][0]['resend_fax_id'] == new_id


def test_only_the_owner_or_someone_who_may_confirm_receipt_settles(cw):
    dana = cw.operator('dana')
    viewer = cw.operator('vic', 'role_fax_viewer')
    admin = cw.operator('admin', 'role_administrator')
    cw.sent('fax-1', sender='dana')
    cw.feed()
    item = cw.item_for('fax-1')
    assert cw.service.detail(viewer, item['id'])['actions'] == []
    assert 'number' not in cw.service.detail(viewer, item['id'])
    with pytest.raises(CertaintyForbidden):
        cw.service.settle(viewer, item['id'], outcome='unknown', reason='no idea', version=1)
    with pytest.raises(CertaintyForbidden):
        cw.service.assign(dana, item['id'], 'vic', version=1)
    moved = cw.service.assign(admin, item['id'], 'vic', version=1)
    assert moved['owner'] == {'id': 'vic', 'name': 'vic'}
    # Now the owner, the viewer may settle their own item.
    done = cw.service.settle(viewer, item['id'], outcome='unknown', reason='The line was busy all day', version=2)
    assert done['state_text'] == "Settled as can't tell by vic."
    assert [entry['text'] for entry in cw.service.history(admin, item['id'])][-2:] == [
        'admin moved it from dana to vic.', "vic settled it as can't tell: The line was busy all day."]
    assert cw.service.counts(admin) == {'open': 0, 'mine': 0, 'unassigned': 0, 'overdue': 0, 'settled': 1}


def test_people_with_a_temporary_password_still_own_and_settle_by_their_grants(cw):
    """The sender, the fallback and an assignee are chosen by who holds the sent fax, password set or not."""
    cw.operator('dana')
    cw.operator('fran', 'role_fax_viewer')
    cw.user('nell')
    admin = cw.operator('admin', 'role_administrator')
    for name in ('dana', 'fran', 'nell'):
        cw.update('access_users', name, password_change_required=1)
    cw.service.update_settings(admin, fallback_principal_id='fran', settle_hours=2, version=0)
    with pytest.raises(CertaintyInputError, match='cannot see every sent fax'):
        cw.service.update_settings(admin, fallback_principal_id='nell', settle_hours=2, version=1)
    cw.sent('fax-1', sender='dana')
    cw.feed()
    item = cw.item_for('fax-1')
    assert (item['owner_principal_id'], item['owner_source']) == ('dana', 'sender')
    assert [person['id'] for person in cw.service.assignees(admin, item['id'])] == ['admin', 'dana', 'fran']
    with pytest.raises(CertaintyInputError, match='nell cannot see this fax'):
        cw.service.assign(admin, item['id'], 'nell', version=1)
    assert cw.service.assign(admin, item['id'], 'fran', version=1)['owner'] == {'id': 'fran', 'name': 'fran'}


def test_an_item_past_its_deadline_is_escalated_once_to_the_fallback_person(cw):
    cw.operator('dana')
    cw.operator('fran', 'role_fax_viewer')
    admin = cw.operator('admin', 'role_administrator')
    cw.service.update_settings(admin, fallback_principal_id='fran', settle_hours=2, version=0)
    cw.sent('fax-1', sender='dana')
    cw.feed()
    item = cw.item_for('fax-1')
    later = NOW + timedelta(hours=3)
    assert cw.certainty.escalate(cw.control, now=later) == 1
    assert cw.certainty.escalate(cw.control, now=later + timedelta(hours=1)) == 0
    moved = cw.item_for('fax-1')
    assert (moved['owner_principal_id'], moved['owner_source']) == ('fran', 'fallback')
    assert cw.service.history(admin, item['id'])[-1]['text'] == 'Not settled in time; moved to fran.'


def test_a_partner_is_asked_once_about_an_uncertain_call_and_its_signed_count_is_kept(cw):
    dana = cw.operator('dana')
    cw.insert('direct_peers', id='peer-1', organization='Lakeside Hospital', phone_number=NUMBER,
              endpoint_url='https://lakeside.example', signing_key='a' * 64, exchange_key='b' * 64,
              state='verified', challenge_failures=0)
    cw.sent('fax-1', sender='dana', pages=4, category='pages_unconfirmed')
    cw.insert('sip_call_records', id='call-1', direction='outbound', call_id='c-1', job_id='fax-1',
              attempt_id='attempt-fax-1', caller='+12025550100', called=NUMBER, started_at=NOW - timedelta(minutes=19),
              answered_at=NOW - timedelta(minutes=19), ended_at=NOW - timedelta(minutes=17),
              disposition='answered', connected_seconds=120, t38='yes', pages=None, fax_preference=1)
    cw.feed()
    item = cw.item_for('fax-1')
    asked = []

    async def ask(peer, call, total_pages):
        asked.append((peer['id'], call['caller'], total_pages))
        return {'status': 'found', 'pages_held': 4, 'total_pages': 4,
                'envelope': {'statement': '{"type":"call_pages"}', 'signature': 's' * 86}}
    question = PartnerQuestion(cw.certainty, service=None, asker=ask)
    asyncio.run(question.step())
    asyncio.run(question.step())
    assert asked == [('peer-1', '+12025550100', 4)]
    partner = cw.service.detail(dana, item['id'])['checks'][0]
    assert partner['result'] == 'delivered' and partner['strength'] == 'proof'
    assert partner['text'] == 'Lakeside Hospital signed that it holds all 4 pages of this call.'
    kept = [json.loads(event['details']) for event in cw.events(item['id']) if event['kind'] == 'probe']
    assert kept[0]['statement'] == '{"type":"call_pages"}'


def test_send_again_is_refused_once_the_fax_is_already_on_its_way_again(cw):
    dana = cw.operator('dana')
    cw.insert('direct_peers', id='peer-1', organization='Lakeside Hospital', phone_number=NUMBER,
              endpoint_url='https://lakeside.example', signing_key='a' * 64, exchange_key='b' * 64,
              state='verified', challenge_failures=0)
    cw.sent('fax-1', sender='dana')
    cw.insert('direct_deliveries', direction='outbound', message_id='m' * 32, peer_id='peer-1', job_id='fax-1',
              attempt_id='attempt-fax-1', recipient_number=NUMBER, digest='d' * 64, size_bytes=10, manifest='{}',
              state='uncertain')
    cw.feed()
    item = cw.item_for('fax-1')
    # The partner signs "not received", and the direct path puts the fax back for its next route by itself.
    with cw.engine.begin() as connection:
        connection.execute(cw.tables['direct_deliveries'].update().values(state='refused'))
        connection.execute(cw.certainty.deliveries.update().values(state='ready', attempt_id=None))
    view = cw.service.detail(dana, item['id'])
    assert view['checks'][0]['result'] == 'not_delivered'
    assert view['moved_on'] == {'kind': 'resent', 'text': 'Faxbot is already sending this fax again by another '
                                                          'route, so it is not sent again from here.'}
    assert view['suggestion'] is None
    with pytest.raises(CertaintyConflict, match='already sending this fax again'):
        cw.service.settle(dana, item['id'], outcome='not_delivered', reason='Partner said no', version=1,
                          send_again=True, send=cw.accept('dana'))
    assert cw.accepted == []
    settled = cw.service.settle(dana, item['id'], outcome='not_delivered', reason='Partner said no; resent by Faxbot',
                                version=1)
    assert settled['state'] == 'settled' and settled['moved_on'] is None
    # Later the fax service reports another fax delivered: said plainly, and it is never sent again from here.
    cw.sent('fax-2', sender='dana')
    cw.feed()
    with cw.engine.begin() as connection:
        connection.execute(cw.certainty.deliveries.update().where(cw.certainty.deliveries.c.id == 'fax-2').values(
            state='success'))
    other = cw.service.detail(dana, cw.item_for('fax-2')['id'])
    assert other['moved_on']['text'] == 'The fax service has since reported this fax delivered.'
    assert other['suggestion'] == 'delivered'


def test_a_fax_sent_again_names_the_earlier_fax_only_to_people_who_may_read_it(cw):
    dana = cw.operator('dana')
    cw.sent('fax-1', sender='dana')
    cw.feed()
    item = cw.item_for('fax-1')
    cw.service.settle(dana, item['id'], outcome='not_delivered', reason='No fax came', version=1, send_again=True,
                      send=cw.accept('dana'))
    new_id = resend_id(item['id'])
    # Someone who may read only the new fax (its own resource) learns nothing about the earlier one.
    cw.user('ola')
    cw.role('one-fax', ['fax:read'])
    cw.assignment('ola', 'one-fax', 'r-' + new_id)
    from api.app.access.types import PasswordSessionEvidence, PrincipalContext
    ola = PrincipalContext('ola', 1, PasswordSessionEvidence('session-ola', 1), 'principal:ola')
    assert cw.service.for_fax(ola, new_id) == {'items': [], 'about': None}
    assert cw.service.for_fax(dana, new_id)['about'] == {'fax_id': 'fax-1', 'kind': 'resend'}


def test_an_open_item_closes_by_itself_once_the_fax_is_delivered_by_any_path(cw):
    dana = cw.operator('dana')
    cw.insert('direct_peers', id='peer-1', organization='Lakeside Hospital', phone_number=NUMBER,
              endpoint_url='https://lakeside.example', signing_key='a' * 64, exchange_key='b' * 64,
              state='verified', challenge_failures=0)
    cw.sent('fax-repaired', sender='dana', state='failed', phase='failed', category='partly_sent', pages=10)
    cw.sent('fax-late', sender='dana')
    cw.sent('fax-person', sender='dana')
    cw.sent('fax-open', sender='dana', state='failed', phase='failed', category='partly_sent')
    worker = CertaintyWorker(cw.certainty, control=lambda: cw.control)
    worker.step(now=NOW)
    person = cw.item_for('fax-person')
    cw.service.settle(dana, person['id'], outcome='unknown', reason='Could not reach them', version=1)
    # The partner completes the broken call with only its missing pages: the repair's own attempt (named by the
    # repair's ID) succeeds and the fax is delivered, as OutboundStore.complete_repair records it.
    repair = 'r' * 32
    cw.insert('direct_call_repairs', role='sender', repair_id=repair, peer_id='peer-1', job_id='fax-repaired',
              attempt_id='attempt-fax-repaired', total_pages=10, pages_held=6, state='completed', statement='{}',
              signature='s' * 86)
    cw.insert('outbound_attempts', id=repair, job_id='fax-repaired', sequence=2, phase='success',
              created_at=NOW, submitted_at=NOW, completed_at=NOW)
    with cw.engine.begin() as connection:
        deliveries = cw.certainty.deliveries
        connection.execute(deliveries.update().where(deliveries.c.id == 'fax-repaired').values(
            state='success', attempt_id=repair))
        # A late result from the fax service for the uncertain attempt itself.
        connection.execute(deliveries.update().where(deliveries.c.id.in_(['fax-late', 'fax-person'])).values(
            state='success'))
    later = NOW + timedelta(minutes=1)
    assert cw.certainty.close_delivered(now=later) == 2
    worker.step(now=later + timedelta(minutes=1))  # a restart or a second worker: nothing more happens
    repaired, late = cw.item_for('fax-repaired'), cw.item_for('fax-late')
    assert (repaired['state'], repaired['outcome'], repaired['settled_by']) == ('settled', 'delivered', None)
    assert repaired['settled_reason'] == 'Closed: the fax was completed directly by Lakeside Hospital.'
    assert late['settled_reason'] == 'Closed: the fax was delivered.'
    view = cw.service.detail(dana, repaired['id'])
    assert view['state_text'] == 'Closed: the fax was completed directly by Lakeside Hospital.'
    assert view['actions'] == [] and view['suggestion'] is None
    assert [event['kind'] for event in cw.events(repaired['id'])].count('settled') == 1
    assert cw.service.history(dana, late['id'])[-1]['text'] == 'Closed: the fax was delivered.'
    # A person's decision stands, and a fax that is still not delivered stays open for a person.
    assert cw.item_for('fax-person')['settled_by'] == 'dana' and cw.item_for('fax-person')['outcome'] == 'unknown'
    assert cw.item_for('fax-open')['state'] == 'open'
