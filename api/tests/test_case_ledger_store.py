"""The case ledger's own queries on SQLite and PostgreSQL: sent, acknowledged, expired, invalidated, repaired."""
from datetime import datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.cases.ledger import CaseConflict, CaseInputError, CaseLedger, document
from api.app.direct.store import DirectStore
from api.app.routing.database import utcnow
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 - fixture


NOW = datetime(2026, 10, 7, 12)
TO = '+12025550123'
CASE = 'claim-7'


def pdf(text, pages=1):
    from reportlab.pdfgen import canvas
    output = BytesIO()
    document_ = canvas.Canvas(output)
    for number in range(pages):
        document_.drawString(72, 720, f'{text} page {number + 1}')
        document_.showPage()
    document_.save()
    return output.getvalue()


@pytest.fixture
def ledger(database, tmp_path):
    upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO delivery_destinations (id, phone_number, accepts_references, version, created_at, updated_at)"
            " VALUES ('d-1', :to, 1, 1, :now, :now)"), {'to': TO, 'now': NOW})
    return CaseLedger(database, str(tmp_path))


def job(engine, state='queued'):
    identity = uuid4().hex
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO fax_jobs (id, to_number, file_name, tiff_path, status, backend, created_at, updated_at) "
            "VALUES (:id, :to, 'case.pdf', '', 'queued', 'phaxio', :now, :now)"), {'id': identity, 'to': TO, 'now': NOW})
        connection.execute(sa.text(
            "INSERT INTO outbound_deliveries (id, dispatch_mode, state, version, created_at, updated_at) "
            "VALUES (:id, 'normal', :state, 1, :now, :now)"), {'id': identity, 'state': state, 'now': NOW})
    return identity


def deliver(engine, identity, state='success'):
    with engine.begin() as connection:
        connection.execute(sa.text('UPDATE outbound_deliveries SET state = :state WHERE id = :id'),
                           {'state': state, 'id': identity})


def packet(ledger, documents, **kwargs):
    plan = ledger.plan(CASE, TO, documents)
    kept = ledger.keep(CASE, plan.included)
    from api.app.cases.ledger import PacketPlan
    plan = PacketPlan(tuple(kept), plan.referenced, plan.references_allowed, plan.why)
    identity = job(ledger.engine)
    ledger.record(CASE, TO, plan, identity, **kwargs)
    return plan, identity


def states(ledger, now=None):
    return {view['title']: view['state'] for view in ledger.entries(CASE, TO, now=now)}


def test_acknowledgements_not_fax_success_decide_what_a_packet_leaves_out(ledger):
    engine = ledger.engine
    record = document('Medical record', pdf('Record', 6), 6, source='Valley EHR', purpose='Appeal')
    letter = document('Cover letter', pdf('Letter'), 1, purpose='Appeal')
    plan, first = packet(ledger, [record, letter], purpose='Appeal')
    assert plan.pages == 7 and states(ledger) == {'Medical record': 'waiting', 'Cover letter': 'waiting'}
    deliver(engine, first)
    assert states(ledger) == {'Medical record': 'sent', 'Cover letter': 'sent'}
    assert ledger.plan(CASE, TO, [record, letter]).referenced == ()

    # A partner's signed receipt for that packet accepts both, once, however often it is read.
    store = DirectStore(engine)
    store.record_outbound(message_id='m-1', peer_id=None, job_id=first, attempt_id=None, recipient_number=TO,
                          digest='0' * 64, size=1, manifest='{}')
    assert states(ledger) == {'Medical record': 'sent', 'Cover letter': 'sent'}  # still sending
    store.mark_outbound('m-1', 'accepted', receipt={'status': 'accepted'})
    for _ in range(2):
        assert states(ledger) == {'Medical record': 'accepted', 'Cover letter': 'accepted'}
    with engine.connect() as connection:
        assert connection.execute(sa.text('SELECT COUNT(*) FROM case_entry_events')).scalar_one() == 2
    labs = document('Labs', pdf('Labs', 2), 2, purpose='Appeal')
    plan = ledger.plan(CASE, TO, [record, letter, labs])
    assert [item.title for item in plan.included] == ['Labs'] and plan.pages == 3 and plan.pages_saved == 6

    # The receiving team acknowledging a packet delivered here also accepts it.
    _, second = packet(ledger, [labs], purpose='Appeal')
    deliver(engine, second)
    inbound = uuid4().hex
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO inbound_faxes (id, from_number, to_number, status, backend, created_at, received_at, "
            "updated_at) VALUES (:id, '+15555550100', :to, 'received', 'local', :now, :now, :now)"),
            {'id': inbound, 'to': TO, 'now': NOW})
        connection.execute(sa.text(
            "INSERT INTO inbound_imports (id, source, account, operation_id, revision, state, attempts, imported_at, "
            "acquired_at, artifact_digest, artifact_size, artifact_media_type, report, inbound_fax_id, created_at, "
            "updated_at) VALUES ('i-1', 'local', 'local:installation', :job, '', 'received', 1, :now, :now, :digest, "
            "10, 'application/pdf', '{}', :inbound, :now, :now)"),
            {'job': second, 'now': NOW, 'digest': 'c' * 64, 'inbound': inbound})
        connection.execute(sa.text(
            "INSERT INTO work_items (id, inbound_fax_id, state, available_at, done_at, version, created_at, updated_at)"
            " VALUES ('w-1', :inbound, 'done', :now, :now, 1, :now, :now)"), {'inbound': inbound, 'now': NOW})
    labs_view = {view['title']: view for view in ledger.entries(CASE, TO)}['Labs']
    assert (labs_view['state'], labs_view['accepted_how'], labs_view['accepted_at']) == (
        'accepted', 'work_acknowledged', NOW)
    recent = ledger.recent()
    assert [(row['case_id'], row['documents'], row['accepted'], row['sent'], row['accepts_references'])
            for row in recent] == [(CASE, 3, 3, 3, True)]

    # Listing many cases takes a fixed handful of queries under the shared write lock, not some per case.
    from api.app.cases.ledger import PacketPlan
    for number in range(4):
        other, recipient = f'claim-{number + 10}', f'+1202555019{number}'
        referral = document(f'Referral {number}', pdf(f'Referral {number}'), 1)
        identity = job(engine)
        ledger.record(other, recipient, PacketPlan((referral,), (), False), identity)
    statements = []

    def count(connection, cursor, statement, *args):
        statements.append(statement)
    sa.event.listen(engine, 'before_cursor_execute', count)
    try:
        recent = ledger.recent()
    finally:
        sa.event.remove(engine, 'before_cursor_execute', count)
    assert sorted((row['case_id'], row['documents'], row['accepted'], row['sent']) for row in recent) == sorted(
        [(CASE, 3, 3, 3)] + [(f'claim-{number + 10}', 1, 0, 0) for number in range(4)])
    assert len(statements) <= 16, len(statements)


def test_expiry_invalidation_and_a_persons_confirmation(ledger):
    engine, now = ledger.engine, utcnow()
    record = document('Medical record', pdf('Record', 3), 3)
    _, first = packet(ledger, [record])
    entry = ledger.entries(CASE, TO)[0]['id']
    with pytest.raises(CaseConflict):
        ledger.confirm(CASE, TO, [entry], note='They have it.')  # not delivered yet
    deliver(engine, first)
    with pytest.raises(CaseInputError):
        ledger.confirm(CASE, TO, [entry], note=' ')
    with pytest.raises(CaseInputError):
        ledger.confirm(CASE, TO, ['someone-else'], note='They have it.')
    ledger.confirm(CASE, TO, [entry], note='Confirmed by phone with intake.', now=now)
    assert states(ledger, now=now + timedelta(days=89)) == {'Medical record': 'accepted'}
    assert states(ledger, now=now + timedelta(days=91)) == {'Medical record': 'expired'}
    assert ledger.set_reuse_days(TO, 0)['reuse_days'] == 0
    assert states(ledger, now=now + timedelta(days=900)) == {'Medical record': 'accepted'}
    with pytest.raises(CaseConflict):
        ledger.set_reuse_days(TO, 30, expected_version=0)
    with pytest.raises(CaseInputError):
        ledger.set_reuse_days(TO, 4000)
    ledger.invalidate(CASE, TO, [entry], note='Not in their system.', now=now + timedelta(days=1))
    view = ledger.entries(CASE, TO)[0]
    assert (view['state'], view['invalidated_note']) == ('invalidated', 'Not in their system.')
    assert ledger.plan(CASE, TO, [record]).why == ('invalidated',)


def test_a_full_packet_repair_uses_kept_originals_and_records_why(ledger, tmp_path):
    engine = ledger.engine
    record = document('Medical record', pdf('Record', 3), 3, purpose='Appeal')
    letter = document('Cover letter', pdf('Letter'), 1, purpose='Appeal')
    _, first = packet(ledger, [record, letter], purpose='Appeal')
    # The same bytes for another purpose is another entry; the repair faxes the bytes once.
    _, second = packet(ledger, [document('Medical record', record.data, 3, purpose='Second opinion')],
                       purpose='Second opinion')
    assert len(ledger.entries(CASE, TO)) == 3
    assert set(ledger.in_flight(CASE, TO)) == {first, second}
    documents, missing = ledger.full_packet(CASE, TO)
    assert missing == [] and [(item.title, item.purpose) for item in documents] == [
        ('Medical record', 'Appeal'), ('Cover letter', 'Appeal'), ('Medical record', 'Second opinion')]
    from api.app.cases.ledger import PacketPlan
    repair = PacketPlan(tuple(documents), (), False)
    assert repair.pages == 4
    identity = job(engine)
    ledger.record(CASE, TO, repair, identity, kind='repair', reason='Their system lost the file.')
    with engine.connect() as connection:
        rows = connection.execute(sa.text(
            'SELECT e.purpose, s.first_page, s.last_page FROM case_entry_sends s JOIN case_entries e '
            'ON e.id = s.entry_id WHERE s.job_id = :job ORDER BY e.purpose, s.first_page'), {'job': identity}).all()
        assert [tuple(row) for row in rows] == [('Appeal', 1, 3), ('Appeal', 4, 4), ('Second opinion', 1, 3)]
        assert connection.execute(sa.text('SELECT kind, reason FROM case_packets WHERE id = :id'),
                                  {'id': identity}).one() == ('repair', 'Their system lost the file.')
    # A kept copy that changed on disk is never sent.
    (tmp_path / 'cases' / f'{letter.digest}.pdf').write_bytes(b'%PDF-changed')
    with pytest.raises(CaseConflict, match='has changed'):
        ledger.full_packet(CASE, TO)
    # The ledger never wrote case_documents.accepted_at.
    with engine.connect() as connection:
        assert connection.execute(sa.text(
            'SELECT COUNT(*) FROM case_documents WHERE accepted_at IS NOT NULL')).scalar_one() == 0


def test_retention_removes_originals_unused_for_the_period_and_touches_nothing_outside_cases(ledger, tmp_path):
    """Kept originals hold patient data: once added or sent longer ago than the retention period, they go."""
    import os
    from api.app.cases.ledger import PacketPlan
    from api.app.cases.retention import remove_expired_originals
    engine, now = ledger.engine, utcnow()
    old = document('Old record', pdf('Old record', 2), 2)
    recent = document('Recent labs', pdf('Recent labs'), 1)
    shared = document('Shared referral', pdf('Shared referral'), 1)
    ledger.keep(CASE, [old, recent, shared])
    ledger.keep('claim-9', [shared])
    # The old record and the referral were kept and sent 40 days ago; the labs were sent yesterday.
    plan = PacketPlan((ledger.keep(CASE, [recent])[0],), (), False)
    identity = job(engine)
    ledger.record(CASE, TO, plan, identity)
    with engine.begin() as connection:
        connection.execute(sa.text('UPDATE case_originals SET created_at = :at'), {'at': now - timedelta(days=40)})
        connection.execute(sa.text("UPDATE case_originals SET created_at = :at WHERE case_id = 'claim-9'"),
                           {'at': now - timedelta(days=5)})
        connection.execute(sa.text('UPDATE case_entry_sends SET created_at = :at'), {'at': now - timedelta(days=1)})
    folder = tmp_path / 'cases'
    for name in (f'{old.digest}.pdf', f'{shared.digest}.pdf'):
        os.utime(folder / name, (0, (now - timedelta(days=40)).timestamp()))
    stray = folder / f'.{old.digest}.{"a" * 32}.part'
    stray.write_bytes(b'%PDF-half')
    os.utime(stray, (0, (now - timedelta(days=40)).timestamp()))
    outside = [tmp_path / f'{old.digest}.pdf', tmp_path / 'job.pdf', folder / 'notes.txt']
    for path in outside:
        path.write_bytes(b'not a kept original')
        os.utime(path, (0, (now - timedelta(days=400)).timestamp()))

    assert remove_expired_originals(engine, str(tmp_path), now - timedelta(days=30), now=now) == 2
    kept = {(row['case_id'], row['title']) for row in ledger.originals(CASE) + ledger.originals('claim-9')}
    assert kept == {(CASE, 'Recent labs'), ('claim-9', 'Shared referral')}
    # The old record's file goes; the referral's bytes stay while another case keeps them; the labs stay.
    assert not (folder / f'{old.digest}.pdf').exists() and not stray.exists()
    assert (folder / f'{shared.digest}.pdf').exists() and (folder / f'{recent.digest}.pdf').exists()
    assert all(path.exists() for path in outside)
    with engine.connect() as connection:
        rows = connection.execute(sa.text(
            'SELECT case_id, digest, reason FROM case_original_removals ORDER BY digest')).all()
    assert sorted(tuple(row) for row in rows) == sorted([(CASE, old.digest, 'retention'),
                                                          (CASE, shared.digest, 'retention')])
    # Nothing left to remove a second time.
    assert remove_expired_originals(engine, str(tmp_path), now - timedelta(days=30), now=now) == 0


def test_a_repair_says_which_documents_retention_removed(ledger):
    from api.app.cases.retention import remove_expired_originals
    engine, now = ledger.engine, utcnow()
    record = document('Medical record', pdf('Record', 2), 2)
    letter = document('Cover letter', pdf('Letter'), 1)
    packet(ledger, [record, letter])  # both kept and sent; the record was added today
    with engine.begin() as connection:
        connection.execute(sa.text('UPDATE case_entry_sends SET created_at = :at'), {'at': now - timedelta(days=60)})
        connection.execute(sa.text("UPDATE case_originals SET created_at = :at WHERE title = 'Cover letter'"),
                           {'at': now - timedelta(days=60)})
    assert remove_expired_originals(engine, ledger.data_dir, now - timedelta(days=30), now=now) == 1
    documents, missing = ledger.full_packet(CASE, TO)
    assert [item.title for item in documents] == ['Medical record']
    assert missing == [{'title': 'Cover letter', 'removed_at': now}]
