"""Expected faxes over migrated SQLite and PostgreSQL: matching, proposals, escalation, imports, outages and scope.

Synthetic references, people and 555 numbers only. Received faxes go through the
real inbound access service (with the subaddress the sender stated), the real
work feed and the real expectation worker.
"""
from datetime import timedelta
import io
import itertools
import json
import threading
from types import SimpleNamespace
import zipfile

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database  # noqa: F401 - fixture
from api.tests.test_access_policy import NOW
from api.tests.test_work_store import WorkWorld, operator
from api.app.access.receiving_rules import ReceivedFacts
from api.app.work.expectation_service import (ExpectationService, ExpectedConflict, ExpectedForbidden,
                                              ExpectedInputError, ExpectedNotFound)
from api.app.work.expectations import ExpectationStore, ExpectationWorker
from api.app.work.store import WorkStore


SUPPLIER = '+15550104444'


class ExpectedWorld(WorkWorld):
    def __init__(self, engine):
        super().__init__(engine)
        self.expected = ExpectationStore(engine)
        self.zone = ''
        self.expect_service = self.expected_at(NOW)

    def expected_at(self, moment):
        ticks = itertools.count()
        return ExpectationService(self.expected, SimpleNamespace(store=self.store, control=self.control),
                                  values=lambda: SimpleNamespace(work_acknowledge_hours=self.hours,
                                                                 fax_default_country='US', time_zone=self.zone),
                                  clock=lambda: moment + timedelta(milliseconds=next(ticks)))

    def later(self, moment):
        """A service at a later time, with every synthetic session still current then."""
        self.at(moment)
        return self.expected_at(moment)

    def arrive(self, identity, to_number='+15550100001', *, sub=None, sender='+15559990000', minutes_ago=10,
               now=NOW):
        moment = now - timedelta(minutes=minutes_ago)
        self.inbound.accept(dict(id=identity, from_number=sender, to_number=to_number, status='received',
                                 backend='sip', pages=1, size_bytes=10, sha256=None,
                                 pdf_path='/synthetic/' + identity + '.pdf', created_at=moment, received_at=moment,
                                 updated_at=moment),
                            now=now, facts=ReceivedFacts(to_number=None, subaddress=sub, received_at=moment))

    def step(self, now=None):
        """The work feed, then the expectation worker, as the background tasks run them (a minute on)."""
        now = now or NOW + timedelta(minutes=1)
        self.work.feed(installation_hours=self.hours, now=now)
        return ExpectationWorker(self.expected).step(now=now)

    def row(self, code):
        with self.engine.connect() as connection:
            return dict(connection.execute(sa.select(self.expected.expectations).where(
                self.expected.expectations.c.code == code)).mappings().one())

    def kinds(self, code):
        table = self.expected.events
        with self.engine.connect() as connection:
            return [row.kind for row in connection.execute(sa.select(table.c.kind).where(
                table.c.expectation_id == self.row(code)['id']).order_by(table.c.created_at, table.c.id))]

    def count(self, name):
        with self.engine.connect() as connection:
            return connection.execute(sa.text(f'SELECT COUNT(*) FROM {name}')).scalar()


@pytest.fixture
def xw(database):  # noqa: F811
    return ExpectedWorld(database)


def expect(xw, actor, reference='483', **extra):
    entry = {'reference': reference, 'kind': 'Signed acknowledgement', 'mailbox_id': 'front',
             'counterparty': 'Acme Supply', 'fax_numbers': [SUPPLIER], **extra}
    return xw.expect_service.add(actor, entry)


# -- matching ------------------------------------------------------------------------------

def test_an_exact_subaddress_closes_the_expectation_and_links_the_fax(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    view = expect(xw, admin, due_hours=48)
    assert view['state_key'] == 'waiting' and view['keys']['subaddress'] == '483'
    assert view['due_text'] == ('Expected within 48 hours, from the due time given when it was added. This is an '
                                'operational target, not a legal deadline.')
    xw.arrive('fax-other', sub='999')
    xw.arrive('fax-po', sub='483', sender='+15559990001')
    xw.step()
    row = xw.row(view['code'])
    assert row['state'] == 'matched' and row['matched_inbound_fax_id'] == 'fax-po' and row['matched_by'] is None
    detail = xw.expect_service.detail(admin, view['code'])
    assert detail['state_text'].startswith('Arrived and matched by its subaddress on ')
    assert detail['match']['fax']['inbound_fax_id'] == 'fax-po' and detail['match']['signal'] == 'subaddress'
    # The received fax is linked by its id, never copied, and its work item is untouched.
    assert xw.count('inbound_faxes') == 2 and xw.item_for('fax-po')['state'] == 'open'
    assert xw.kinds(view['code']) == ['created', 'matched']
    # A second fax with the same subaddress is noted; the match does not change.
    xw.arrive('fax-again', sub='483', minutes_ago=5)
    xw.step(now=NOW + timedelta(minutes=2))
    assert xw.row(view['code'])['matched_inbound_fax_id'] == 'fax-po'
    assert xw.kinds(view['code'])[-1] == 'also_arrived'
    # Each received fax was examined once.
    assert xw.count('work_expectation_arrivals') == 3
    xw.step(now=NOW + timedelta(minutes=3))
    assert xw.count('work_expectation_arrivals') == 3 and len(xw.kinds(view['code'])) == 3


def test_a_known_sender_only_proposes_and_a_person_confirms_or_rejects(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    first = expect(xw, admin, reference='PO 900')
    xw.arrive('fax-a', sender=SUPPLIER, minutes_ago=-5)  # after it was expected
    xw.step()
    view = xw.expect_service.detail(admin, first['code'])
    assert view['state'] == 'proposed_match' and view['state_key'] == 'proposed'
    [proposal] = view['proposals']
    assert proposal['strength'] == 'weak' and proposal['can_decide']
    assert proposal['text'] == ('It came from a fax number you listed for Acme Supply, but nothing in it names '
                                'PO 900.')
    # A weak signal never closes; a person rejects it and the expectation waits again.
    rejected = xw.later(NOW + timedelta(minutes=2)).reject(admin, first['code'], proposal['id'],
                                                            version=view['version'],
                                                            note='A price list, not the acknowledgement')
    assert rejected['state'] == 'open' and rejected['proposals'] == []
    xw.arrive('fax-b', sender=SUPPLIER, minutes_ago=-6)
    xw.step(now=NOW + timedelta(minutes=3))
    service = xw.later(NOW + timedelta(minutes=4))
    view = service.detail(admin, first['code'])
    confirmed = service.confirm(admin, first['code'], view['proposals'][0]['id'], version=view['version'])
    assert confirmed['state'] == 'matched' and confirmed['state_text'].startswith('Matched by admin on ')
    assert xw.kinds(first['code']) == ['created', 'proposed', 'proposal_rejected', 'proposed', 'confirmed']
    with pytest.raises(ExpectedConflict):
        service.confirm(admin, first['code'], view['proposals'][0]['id'], version=confirmed['version'])


def test_a_generic_fax_never_satisfies_a_revision_or_required_parts(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    revision = expect(xw, admin, reference='4831', required_revision='B')
    parts = expect(xw, admin, reference='4832', required_parts=['Signature page'])
    xw.arrive('fax-rev', sub='4831')
    xw.arrive('fax-parts', sub='4832')
    xw.step()
    for code, wanted in ((revision['code'], 'does not say which revision; you expect revision B.'),
                         (parts['code'], 'check that it includes Signature page.')):
        view = xw.expect_service.detail(admin, code)
        assert view['state'] == 'proposed_match', code
        assert view['proposals'][0]['strength'] == 'strong' and view['proposals'][0]['text'].endswith(wanted)


def test_one_reference_that_fits_two_expectations_proposes_both(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    first = expect(xw, admin, reference='PO 1', subaddress='7777')
    second = expect(xw, admin, reference='PO 2', subaddress='7777')
    xw.arrive('fax-shared', sub='7777')
    xw.step()
    for code in (first['code'], second['code']):
        view = xw.expect_service.detail(admin, code)
        assert view['state'] == 'proposed_match'
        assert view['proposals'][0]['text'] == ('Its reference in its subaddress fits more than one expected fax; '
                                                'choose which one it answers.')


def test_a_fax_that_arrived_before_the_expectation_is_found_by_looking_back(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    xw.arrive('fax-early', sub='5150', minutes_ago=600)
    xw.step()
    view = expect(xw, admin, reference='5150')
    assert xw.row(view['code'])['examined_at'] is None
    xw.step()
    row = xw.row(view['code'])
    assert row['state'] == 'matched' and row['matched_inbound_fax_id'] == 'fax-early'
    assert row['examined_at'] is not None


def test_an_approved_extraction_only_proposes(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    view = expect(xw, admin, reference='PO 77')
    xw.arrive('fax-text')
    xw.step()
    with pytest.raises(ValueError):
        xw.expected.propose_from_extraction('fax-text', 'PO 77', extraction={'tool': 'ocr'})
    made = xw.expected.propose_from_extraction('fax-text', 'po  77', now=NOW, extraction={
        'tool': 'synthetic-ocr', 'version': '1.0', 'policy_revision': 'local-only-1', 'pages': [1],
        'confidence': 0.71})
    assert made == 1
    detail = xw.expect_service.detail(admin, view['code'])
    assert detail['state'] == 'proposed_match' and detail['proposals'][0]['signal'] == 'extraction'


# -- escalation and concurrency ------------------------------------------------------------

def test_overdue_escalates_once_across_restarts_to_a_backup_who_can_see_the_mailbox(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    operator(xw, 'backup')
    xw.setting('front', acknowledge_hours=None, backup_principal_id='backup')
    view = expect(xw, admin, due_hours=2)
    later = NOW + timedelta(hours=3)
    xw.at(later)
    # Two workers on fresh stores, as after a restart, and two more at once: one effect.
    assert WorkStore(xw.engine).escalate(xw.control, now=later) == 1
    assert WorkStore(xw.engine).escalate(xw.control, now=later + timedelta(minutes=1)) == 0
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        WorkStore(xw.engine).escalate(xw.control, now=later + timedelta(minutes=2)))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(results) == 0
    row = xw.row(view['code'])
    assert row['state'] == 'overdue' and row['owner_principal_id'] == 'backup'
    assert xw.kinds(view['code']).count('overdue') == 1
    detail = xw.later(later).detail(admin, view['code'])
    assert detail['state_text'].endswith('escalated to backup.') and detail['overdue']
    # A late strong match still closes an overdue expectation.
    xw.arrive('fax-late', sub='483', now=later, minutes_ago=1)
    xw.step(now=later)
    assert xw.row(view['code'])['state'] == 'matched'


def test_concurrent_confirm_and_cancel_cannot_lose_a_change(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    dana = operator(xw, 'dana')
    view = expect(xw, admin, reference='PO 55')
    xw.arrive('fax-maybe', sender=SUPPLIER, minutes_ago=-5)
    xw.step()
    view = xw.expect_service.detail(admin, view['code'])
    proposal, version = view['proposals'][0]['id'], view['version']
    outcomes = []
    barrier = threading.Barrier(2)

    def run(action):
        barrier.wait()
        try:
            outcomes.append(action()['state'])
        except ExpectedConflict:
            outcomes.append('conflict')
    threads = [threading.Thread(target=run, args=(lambda: xw.expect_service.confirm(admin, view['code'], proposal,
                                                                                    version=version),)),
               threading.Thread(target=run, args=(lambda: xw.expect_service.close(
                   dana, view['code'], outcome='cancelled', note='Order withdrawn', version=version),))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) in (['conflict', 'matched'], ['cancelled', 'conflict'])
    row = xw.row(view['code'])
    kinds = xw.kinds(view['code'])
    assert row['state'] in ('matched', 'cancelled') and row['version'] == version + 1
    assert kinds.count('confirmed') + kinds.count('cancelled') == 1


# -- import ---------------------------------------------------------------------------------

def csv_bytes(*rows, header='PO,Rev,Supplier,Fax,Due,Team'):
    return ('\n'.join([header, *rows]) + '\n').encode()


def save_source(xw, admin, **extra):
    return xw.expect_service.save_source(admin, {
        'name': 'Open purchase orders', 'format': 'csv',
        'mapping': {'reference': 'PO', 'revision': 'Rev', 'counterparty': 'Supplier', 'fax_numbers': 'Fax',
                    'due': 'Due', 'mailbox': 'Team'},
        'mailbox_id': 'front', 'subject_template': 'PO {reference}', **extra})


def test_import_creates_replays_conflicts_revises_and_reports_missing_rows(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    source = save_source(xw, admin)
    first = csv_bytes('483,A,Acme Supply,+1 555 010 4444,2026-10-10,Front Desk',
                      '484,,Beta Parts,,2026-10-10 15:00,Billing',
                      '485,,Gamma,,,')
    result = xw.expect_service.import_file(admin, 'Open purchase orders', first, file_name='po.csv', full=True)
    assert (result['created'], result['unchanged'], result['problem_count']) == (3, 0, 0)
    assert xw.count('work_expectations') == 3
    po = xw.expect_service.list(admin, search='483')[0]
    assert po['source'] == 'Open purchase orders' and po['revision'] == 'A' and po['mailbox'] == 'Front Desk'
    assert po['keys'] == {'subaddress': '483', 'email_subject': 'PO 483', 'message_id': None, 'form_field': None}
    assert po['fax_numbers'] == [SUPPLIER] and po['due_at'] == NOW.replace(day=10, hour=23, minute=59, second=59)

    # The identical file again resumes the same run and adds nothing.
    again = xw.expect_service.import_file(admin, source['id'], first, full=True)
    assert again['replay'] and (again['created'], again['unchanged']) == (0, 3)
    assert xw.count('work_expectations') == 3 and xw.count('work_expectation_imports') == 1

    # A changed row under the same revision is a conflict; a new revision replaces the open one.
    second = csv_bytes('483,B,Acme Supply,+1 555 010 4444,2026-10-11,Front Desk',
                       '484,,Beta Parts Ltd,,2026-10-10 15:00,Billing')
    result = xw.expect_service.import_file(admin, source['id'], second, full=True)
    assert (result['created'], result['revised'], result['conflicts']) == (0, 1, 1)
    assert [entry['reference'] for entry in result['missing']] == ['485']
    assert xw.count('work_expectations') == 4
    old, new = (xw.expect_service.list(admin, view='all', search='483')[index] for index in (1, 0))
    assert {old['state'], new['state']} == {'replaced', 'open'}
    revised = new if new['state'] == 'open' else old
    assert revised['revision'] == 'B' and revised['replaces'] is not None
    conflicted = xw.expect_service.list(admin, view='conflicts')[0]
    assert conflicted['reference'] == '484' and conflicted['counterparty'] == 'Beta Parts'
    applied = xw.expect_service.resolve_conflict(admin, conflicted['code'], choice='apply',
                                                 version=conflicted['version'])
    assert applied['counterparty'] == 'Beta Parts Ltd' and not applied['conflict']
    # Missing from the later full export: reported and kept open, never cancelled.
    missing = xw.expect_service.list(admin, view='missing')
    assert [entry['reference'] for entry in missing] == ['485'] and missing[0]['state'] == 'open'
    assert 'missing_from_export' in xw.kinds(missing[0]['code'])
    # Listed again: back in the export.
    xw.expect_service.import_file(admin, source['id'], csv_bytes('485,,Gamma,,,'), full=False)
    assert xw.expect_service.list(admin, view='missing') == []


def test_a_new_revision_leaves_a_matched_earlier_revision_matched(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    save_source(xw, admin)
    xw.expect_service.import_file(admin, 'Open purchase orders', csv_bytes('610,A,Acme,,,'))
    xw.arrive('fax-610', sub='610')
    xw.step()
    assert xw.expect_service.list(admin, view='all', search='610')[0]['state'] == 'matched'
    xw.expect_service.import_file(admin, 'Open purchase orders', csv_bytes('610,B,Acme,,,'))
    states = sorted(view['state'] for view in xw.expect_service.list(admin, view='all', search='610'))
    assert states == ['matched', 'open']


def test_import_rows_with_problems_are_reported_one_sentence_each(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    save_source(xw, admin, mailbox_id=None)
    data = csv_bytes(',A,Acme,,,Front Desk', '700,,Acme,,12/10/2026,Front Desk', '701,,Acme,,,Nowhere',
                     '702,,Acme,,,')
    result = xw.expect_service.import_file(admin, 'Open purchase orders', data, full=True)
    assert [problem['problem'] for problem in result['problems']] == [
        'It has no business reference.',
        'Write times as 2026-10-12, 2026-10-12 14:00 or 2026-10-12T14:00:00-06:00.',
        'There is no mailbox called Nowhere.',
        'It names no mailbox, and the import source has no mailbox set.']
    assert [problem['row'] for problem in result['problems']] == [2, 3, 4, 5]
    with pytest.raises(ExpectedInputError, match='business reference'):
        xw.expect_service.save_source(admin, {'name': 'Bad', 'format': 'csv', 'mapping': {'revision': 'Rev'}})


# -- outage recovery ---------------------------------------------------------------------------

def test_an_outage_sorts_the_next_export_into_three_lists_and_sends_nothing(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    save_source(xw, admin)
    before = csv_bytes('801,,Acme,,,', '802,A,Acme,,,', '803,,Acme,,,', '804,,Acme,,,', '805,,Acme,,,',
                       '806,,Acme,,,')
    xw.expect_service.import_file(admin, 'Open purchase orders', before)
    xw.outbound('job-sent')  # a sent fax with no confirmed delivery
    jobs = xw.count('fax_jobs')
    outage = xw.expect_service.start_outage(admin, 'Open purchase orders', started_at=NOW - timedelta(hours=2),
                                            note='ERP down for maintenance')
    record = lambda **entry: xw.expect_service.record_action(admin, outage['code'], {
        'channel': 'fax', 'action': 'Order sent to the supplier by fax', 'occurred_at': NOW - timedelta(hours=1),
        **entry})
    record(operation_id='801')                                   # done
    record(operation_id='802', revision='A')                     # the export now shows B
    record(operation_id='803', outcome='uncertain')              # may not have gone through
    record(operation_id='804', fax_job_id='job-sent')            # its fax is not confirmed
    record(operation_id='805')
    record(operation_id='805', channel='email', action='Order emailed')  # two actions
    record(operation_id='999', reference='New order taken by phone')    # not in the export
    xw.arrive('fax-806', sub='806', minutes_ago=30)              # answered during the outage, nothing recorded
    xw.step(now=NOW - timedelta(minutes=20))
    with pytest.raises(ExpectedConflict, match='End the outage first'):
        xw.expect_service.reconcile(admin, outage['code'])
    ended = xw.expect_service.end_outage(admin, outage['code'], version=outage['version'])
    assert ended['reconciliation'] is None
    after = csv_bytes('801,,Acme,,,', '802,B,Acme,,,', '803,,Acme,,,', '804,,Acme,,,', '805,,Acme,,,',
                      '806,,Acme,,,', '807,,Acme,,,')
    result = xw.expect_service.import_file(admin, 'Open purchase orders', after, full=True)
    assert result['reconciled_outage'] == outage['code']
    lists = xw.expect_service.outage(admin, outage['code'])['reconciliation']
    assert [entry['operation_id'] for entry in lists['already_done']] == ['801']
    assert [entry['operation_id'] for entry in lists['new']] == ['807']
    reasons = {entry['operation_id']: entry['reason'] for entry in lists['unresolved']}
    assert reasons == {'802': 'revision', '803': 'uncertain', '804': 'fax_not_confirmed', '805': 'several',
                       '806': 'answered', '999': 'not_in_export'}
    assert lists['already_done'][0]['text'] == ('Already done during the outage; record it in the source system '
                                                'and do not submit it again.')
    assert lists['summary'] == ('1 already done (record them; do not submit them again), 1 new (submit them '
                                'normally), 6 held for you to decide.')
    # Reconciling again keeps the first list and adds a new one; nothing is ever sent.
    xw.expect_service.reconcile(admin, outage['code'])
    assert xw.count('work_outage_reconciliations') == 2 and xw.count('fax_jobs') == jobs


# -- scope ---------------------------------------------------------------------------------------

def test_lists_counts_reports_exports_and_reconciliations_disclose_nothing_outside_your_mailboxes(xw):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    front = operator(xw, 'front')
    xw.user('billing-only')
    xw.assignment('billing-only', 'role_fax_operator', 'mailbox-billing')
    mine = expect(xw, admin, reference='PO 1')
    theirs = expect(xw, admin, reference='PO 2', mailbox_id='billing', subaddress='2222')
    xw.arrive('fax-billing', to_number='+15550100002', sub='2222')
    xw.step()
    service = xw.expect_service
    assert [view['code'] for view in service.list(front, view='all')] == [mine['code']]
    assert service.counts(front)['waiting'] == 1 and service.counts(admin)['waiting'] == 1
    assert service.counts(admin)['matched'] == 1 and service.counts(front)['matched'] == 0
    with pytest.raises(ExpectedNotFound):
        service.detail(front, theirs['code'])
    with pytest.raises(ExpectedNotFound):
        service.export(front, theirs['code'])
    report = service.report(front)
    assert report['expected'] == 1 and report['matched'] == 0 and report['arrived'] == 0
    assert [entry['reference'] for entry in report['unmatched_expected']] == ['PO 1']
    # Someone who may read but not manage sees it and cannot change it.
    viewer = xw.user('viewer')
    xw.assignment('viewer', 'role_fax_viewer', 'mailbox-front')
    assert service.detail(viewer, mine['code'])['actions'] == []
    with pytest.raises(ExpectedForbidden):
        service.close(viewer, mine['code'], outcome='cancelled', note='No', version=mine['version'])
    with pytest.raises(ExpectedInputError, match='Choose a mailbox'):
        service.add(front, {'reference': 'PO 3', 'kind': 'Reply', 'mailbox_id': 'billing'})
    with pytest.raises(ExpectedForbidden):
        service.sources(front)
    # Reconciliation lists show only the mailboxes the reader can see.
    save_source(xw, admin)
    xw.expect_service.import_file(admin, 'Open purchase orders', csv_bytes('901,,Acme,,,Front Desk',
                                                                           '902,,Beta,,,Billing'))
    outage = service.start_outage(admin, 'Open purchase orders', started_at=NOW - timedelta(hours=1))
    service.end_outage(admin, outage['code'], version=outage['version'])
    service.import_file(admin, 'Open purchase orders', csv_bytes('901,,Acme,,,Front Desk', '902,,Beta,,,Billing',
                                                                 '903,,Beta,,,Billing'))
    importer = xw.user('importer')  # imports for the installation, reads only the Front Desk mailbox
    xw.assignment('importer', xw.role('importer-role', ('work:import',)), 'installation')
    xw.assignment('importer', 'role_fax_viewer', 'mailbox-front')
    everything = service.outage(admin, outage['code'])['reconciliation']
    assert sorted(entry['operation_id'] for entry in everything['new']) == ['901', '902', '903']
    limited = service.outage(importer, outage['code'])['reconciliation']
    assert [entry['operation_id'] for entry in limited['new']] == ['901']
    assert limited['summary'].startswith('0 already done (record them; do not submit them again), 1 new')
    assert [entry['reference'] for entry in service.list(importer, view='all')] == ['901', 'PO 1']


def test_the_evidence_export_names_what_is_missing_and_the_work_export_names_the_expectation(xw, tmp_path):
    admin = operator(xw, 'admin', 'installation', 'role_administrator')
    waiting = expect(xw, admin, reference='PO 11')
    data, name = xw.expect_service.export(admin, waiting['code'])
    assert name == f"faxbot-expected-{waiting['code']}.zip"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        history = archive.read('history.txt').decode()
    assert manifest['expected']['reference'] == 'PO 11' and manifest['links'] == []
    assert 'Nothing that closes it has arrived.' in manifest['missing']
    assert 'No received fax has been linked to it.' in manifest['missing']
    assert manifest['expected']['due_rule'] == 'No due time is set.'
    assert 'operational target, not a legal deadline' in history
    assert xw.kinds(waiting['code'])[-1] == 'exported'
    # A matched expectation: its link and the evidence behind it.
    matched = expect(xw, admin, reference='3030')
    xw.arrive('fax-3030', sub='3030')
    xw.step()
    data, _ = xw.expect_service.export(admin, matched['code'])
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(data)).read('manifest.json'))
    [link] = manifest['links']
    assert link['state'] == 'automatic' and link['evidence'] == {'subaddress': '3030'}
    assert link['document']['inbound_fax_id'] == 'fax-3030'
    assert [entry['kind'] for entry in manifest['history']] == ['created', 'matched']
    # The received fax's own work evidence names the expected fax it answered, for people who may see it.
    from api.app.work.export import EvidenceExport
    work = xw.at(NOW + timedelta(minutes=5))
    item = xw.item_for('fax-3030')
    data, _ = EvidenceExport(work, values=lambda: SimpleNamespace()).create(admin, item['id'])
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(data)).read('manifest.json'))
    assert [(entry['code'], entry['link'], entry['signal']) for entry in manifest['expected']] == [
        (matched['code'], 'automatic', 'subaddress')]
    # An auditor of this one fax who cannot see the Front Desk mailbox gets no expected fax named.
    auditor = xw.user('fax-auditor')
    xw.assignment('fax-auditor', 'role_auditor', xw.resource_of('fax-3030')['id'])
    data, _ = EvidenceExport(work, values=lambda: SimpleNamespace()).create(auditor, item['id'])
    assert json.loads(zipfile.ZipFile(io.BytesIO(data)).read('manifest.json'))['expected'] == []
