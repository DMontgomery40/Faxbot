"""Case ledger: send only what the recipient acknowledged it has, expire and invalidate that, and repair.

Fax success makes a document "sent". Only the recipient's acknowledgement (a
partner's signed receipt, the receiving team's acknowledgement of a packet
delivered here, a received acknowledgement fax, or a person's note) makes it
"accepted", and only accepted documents may be left out of the next packet.
"""
from datetime import timedelta
from io import BytesIO
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app import main
from app.direct.store import DirectStore
from app.routing.database import utcnow
from api.tests.test_routing_http import ADMIN, BOOTSTRAP, scoped_key


TO = '+12025550123'
CASE = 'claim-2026-117'


def pdf(text, pages=1):
    from reportlab.pdfgen import canvas
    output = BytesIO()
    document = canvas.Canvas(output)
    for number in range(pages):
        document.drawString(72, 720, f'{text} page {number + 1}')
        document.showPage()
    document.save()
    return output.getvalue()


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0',
                        'DIRECT_ORGANIZATION': 'Valley Hospital'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def engine():
    return main.app.state.configuration_runtime.manager.store.engine


def send(client, documents, *, preview=False, headers=ADMIN, purpose='', versions=(), sources=()):
    files = [('documents', (name, data, 'application/pdf')) for name, data in documents]
    return client.post(f'/cases/{CASE}/faxes', headers=headers, files=files,
                       data={'to': '+1 202 555 0123', 'titles': [name for name, _ in documents],
                             'preview': 'true' if preview else 'false', 'purpose': purpose,
                             'versions': list(versions), 'sources': list(sources)})


def finish(job_id, state='success'):
    with engine().begin() as connection:
        connection.execute(sa.text('UPDATE outbound_deliveries SET state=:state WHERE id=:id'),
                           {'id': job_id, 'state': state})


def documents(client):
    response = client.get(f'/cases/{CASE}/documents', headers=ADMIN, params={'to': TO})
    assert response.status_code == 200, response.text
    return response.json()


def by_title(client):
    return {item['title']: item for item in documents(client)['documents']}


def approve(client):
    approved = client.patch(f'/routing/destinations/{TO}', headers=ADMIN, json={'accepts_references': True})
    assert approved.status_code == 200 and approved.json()['accepts_references'] is True


def confirm(client, titles, note='Spoke with their intake desk; they have it.', **extra):
    ids = [by_title(client)[title]['id'] for title in titles]
    return client.post(f'/cases/{CASE}/accept', headers=ADMIN, json={'to': TO, 'documents': ids, 'note': note, **extra})


def invalidate(client, titles, note='They could not find it in their system.'):
    ids = [by_title(client)[title]['id'] for title in titles]
    return client.post(f'/cases/{CASE}/invalidate', headers=ADMIN, json={'to': TO, 'documents': ids, 'note': note})


def partner_receipt(job_id, state='accepted'):
    """What direct delivery records when a partner returns its signed receipt for the packet."""
    store, message = DirectStore(engine()), f'message-{job_id}'
    store.record_outbound(message_id=message, peer_id=None, job_id=job_id, attempt_id=None, recipient_number=TO,
                          digest='0' * 64, size=1, manifest='{}')
    if state == 'accepted':
        store.mark_outbound(message, 'accepted', receipt={'type': 'receipt', 'status': 'accepted'})
    else:
        store.mark_outbound(message, state)


def delivered_here(job_id, *, acknowledged):
    """What local delivery and the work queue record for a packet faxed to one of this installation's numbers."""
    now, inbound = utcnow(), uuid4().hex
    with engine().begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO inbound_faxes (id, from_number, to_number, status, backend, created_at, received_at, "
            "updated_at) VALUES (:id, '+15555550100', :to, 'received', 'local', :now, :now, :now)"),
            {'id': inbound, 'to': TO, 'now': now})
        connection.execute(sa.text(
            "INSERT INTO inbound_imports (id, source, account, operation_id, revision, state, attempts, imported_at, "
            "acquired_at, artifact_digest, artifact_size, artifact_media_type, report, inbound_fax_id, created_at, "
            "updated_at) VALUES (:id, 'local', 'local:installation', :job, '', 'received', 1, :now, :now, :digest, 10, "
            "'application/pdf', '{}', :inbound, :now, :now)"),
            {'id': uuid4().hex, 'job': job_id, 'now': now, 'digest': 'c' * 64, 'inbound': inbound})
        connection.execute(sa.text(
            "INSERT INTO work_items (id, inbound_fax_id, state, available_at, acknowledged_at, version, created_at, "
            "updated_at) VALUES (:id, :inbound, :state, :now, :acknowledged, 1, :now, :now)"),
            {'id': uuid4().hex, 'inbound': inbound, 'state': 'acknowledged' if acknowledged else 'open', 'now': now,
             'acknowledged': now if acknowledged else None})


def fax_count():
    with engine().connect() as connection:
        return connection.execute(sa.text('SELECT COUNT(*) FROM fax_jobs')).scalar_one()


def test_fax_success_makes_documents_sent_never_accepted(client):
    approve(client)
    record, letter = pdf('Medical record', pages=40), pdf('Cover letter')
    first = send(client, [('Medical record', record), ('Cover letter', letter)])
    assert first.status_code == 202, first.text
    assert first.json()['pages'] == 41 and first.json()['pages_saved'] == 0
    assert {item['state'] for item in documents(client)['documents']} == {'waiting'}
    finish(first.json()['fax_id'])
    held = by_title(client)
    assert {item['state'] for item in held.values()} == {'sent'}
    assert held['Medical record']['accepted'] is False and held['Medical record']['accepted_at'] is None
    assert held['Medical record']['sent_at'] is not None and held['Medical record']['kept'] is True
    # Delivered is not acknowledged: the whole file goes again, and says why.
    again = send(client, [('Medical record', record), ('Update', pdf('Update', pages=4))], preview=True)
    assert again.json()['pages'] == 44 and again.json()['fax_id'] is None
    assert [(item['title'], item['why']) for item in again.json()['documents']] == [
        ('Medical record', 'sent'), ('Update', 'new')]
    # A partner answer that is not a signed receipt, or a work item nobody acknowledged, is not acceptance either.
    partner_receipt(first.json()['fax_id'], state='uncertain')
    delivered_here(first.json()['fax_id'], acknowledged=False)
    assert {item['state'] for item in documents(client)['documents']} == {'sent'}


def test_the_case_example_188_pages_down_to_56_on_real_acknowledgements(client):
    """R07 section 5: a 40-page file and a cover, then three updates of four pages, each acknowledged."""
    approve(client)
    files = [('Medical record', pdf('Medical record', pages=40)), ('Cover letter', pdf('Cover letter'))]
    first = send(client, files)
    finish(first.json()['fax_id'])
    partner_receipt(first.json()['fax_id'])
    held = by_title(client)
    assert {(item['state'], item['accepted_how']) for item in held.values()} == {('accepted', 'partner_receipt')}
    sent, full = [first.json()['pages']], [first.json()['pages']]
    acknowledgements = [
        lambda job, title: confirm(client, [title]),
        lambda job, title: delivered_here(job, acknowledged=True),
        None,
    ]
    for number, acknowledge in enumerate(acknowledgements, start=1):
        files.append((f'Update {number}', pdf(f'Update {number}', pages=4)))
        result = send(client, files)
        assert result.status_code == 202, result.text
        body = result.json()
        sent.append(body['pages'])
        # A full resend carries every document: the new pages plus the ones the index listed instead.
        full.append(body['pages'] - 1 + sum(item['pages'] for item in body['documents']
                                            if item['status'] == 'referenced'))
        finish(body['fax_id'])
        if acknowledge is not None:
            outcome = acknowledge(body['fax_id'], f'Update {number}')
            assert outcome is None or outcome.status_code == 200, outcome.text
    assert sent == [41, 5, 5, 5] and sum(sent) == 56
    assert full == [41, 45, 49, 53] and sum(full) == 188
    held = by_title(client)
    assert [held[f'Update {n}']['accepted_how'] for n in (1, 2, 3)] == ['person', 'work_acknowledged', None]
    assert held['Update 1']['accepted_note'] == 'Spoke with their intake desk; they have it.'
    packets = client.get('/routing/savings', headers=ADMIN).json()['case_packets']
    assert (packets['packets'], packets['pages_saved']) == (3, 188 - 56)
    # The index page names the case and what the recipient acknowledged.
    last = client.get(f"/admin/fax-jobs/{body['fax_id']}/pdf", headers=ADMIN)
    reader = PdfReader(BytesIO(last.content))
    assert len(reader.pages) == 5
    index = reader.pages[0].extract_text()
    assert f'Case {CASE}' in index and 'Medical record, 40 pages, acknowledged' in index
    assert 'To receive every document again in full, contact Valley Hospital.' in index
    assert 'Update 3 (4 pages), starting on page 2' in index


def test_the_same_bytes_for_another_purpose_or_version_are_another_entry(client):
    approve(client)
    record = pdf('Medical record', pages=10)
    first = send(client, [('Medical record', record)], purpose='Prior authorization', sources=['Valley EHR'])
    finish(first.json()['fax_id'])
    assert confirm(client, ['Medical record']).status_code == 200
    labs = ('Labs', pdf('Labs'))
    same = send(client, [('Medical record', record), labs], preview=True, purpose='Prior authorization',
                sources=['Valley EHR'])
    assert [(item['title'], item['status']) for item in same.json()['documents']] == [
        ('Labs', 'included'), ('Medical record', 'referenced')]
    for changed in ({'purpose': 'Appeal', 'sources': ['Valley EHR']},
                    {'purpose': 'Prior authorization', 'sources': ['Mesa Clinic']},
                    {'purpose': 'Prior authorization', 'sources': ['Valley EHR'], 'versions': ['corrected']}):
        other = send(client, [('Medical record', record), labs], preview=True, **changed)
        assert [(item['title'], item['why']) for item in other.json()['documents']] == [
            ('Medical record', 'new'), ('Labs', 'new')], changed
    held = documents(client)['documents']
    assert [(item['purpose'], item['source'], item['version']) for item in held] == [
        ('Prior authorization', 'Valley EHR', '')]


def test_references_expire_and_a_cache_miss_sends_the_document_in_full_again(client):
    approve(client)
    record, letter = pdf('Medical record', pages=40), pdf('Cover letter')
    first = send(client, [('Medical record', record), ('Cover letter', letter)])
    # Nobody can acknowledge what has not been delivered.
    early = confirm(client, ['Medical record'])
    assert early.status_code == 409 and 'has not been delivered yet' in early.json()['detail']
    finish(first.json()['fax_id'])
    assert confirm(client, ['Medical record'], note='').status_code == 400
    assert client.post(f'/cases/{CASE}/accept', headers=ADMIN,
                       json={'to': TO, 'documents': ['not-a-document'], 'note': 'They have it.'}).status_code == 400
    assert confirm(client, ['Medical record', 'Cover letter']).status_code == 200
    view = documents(client)
    assert (view['reuse_days'], view['reuse_days_set'], view['recipient_version']) == (90, False, 0)
    record_view = by_title(client)['Medical record']
    assert record_view['state'] == 'accepted' and record_view['accepted_how'] == 'person'
    assert record_view['expires_at'].startswith((utcnow() + timedelta(days=90)).date().isoformat()[:7])

    # This recipient trusts references for 30 days; the record was acknowledged 31 days ago.
    changed = client.patch(f'/case-recipients/{TO}', headers=ADMIN, json={'reuse_days': 30, 'version': 0})
    assert changed.status_code == 200 and changed.json()['reuse_days'] == 30 and changed.json()['version'] == 1
    stale = client.patch(f'/case-recipients/{TO}', headers=ADMIN, json={'reuse_days': 60, 'version': 0})
    assert stale.status_code == 409
    with engine().begin() as connection:
        for statement in ('UPDATE case_entry_events SET occurred_at = :at WHERE entry_id = :id',
                          'UPDATE case_entry_sends SET created_at = :at WHERE entry_id = :id'):
            connection.execute(sa.text(statement), {'at': utcnow() - timedelta(days=31), 'id': record_view['id']})
    held = by_title(client)
    assert (held['Medical record']['state'], held['Cover letter']['state']) == ('expired', 'accepted')
    preview = send(client, [('Medical record', record), ('Cover letter', letter), ('Labs', pdf('Labs'))], preview=True)
    assert [(item['title'], item['why']) for item in preview.json()['documents']] == [
        ('Medical record', 'expired'), ('Labs', 'new'), ('Cover letter', 'accepted')]

    # The recipient says the cover letter is not in its system: stop referring to it. No fax is sent for that.
    before = fax_count()
    missed = invalidate(client, ['Cover letter'])
    assert missed.status_code == 200 and fax_count() == before
    letter_view = by_title(client)['Cover letter']
    assert letter_view['state'] == 'invalidated' and letter_view['invalidated_note'].startswith('They could not')
    preview = send(client, [('Medical record', record), ('Cover letter', letter)], preview=True)
    assert [(item['title'], item['why']) for item in preview.json()['documents']] == [
        ('Medical record', 'expired'), ('Cover letter', 'invalidated')]
    assert preview.json()['pages'] == 41

    # No limit: the 31-day-old acknowledgement is trusted again; the default comes back with null.
    assert client.patch(f'/case-recipients/{TO}', headers=ADMIN, json={'reuse_days': 0}).json()['reuse_days'] == 0
    assert by_title(client)['Medical record']['state'] == 'accepted'
    assert by_title(client)['Medical record']['expires_at'] is None
    restored = client.patch(f'/case-recipients/{TO}', headers=ADMIN, json={'reuse_days': None})
    assert restored.json() == {'to': TO, 'reuse_days': 90, 'reuse_days_default': 90, 'reuse_days_set': False,
                               'version': 3}
    assert client.patch(f'/case-recipients/{TO}', headers=ADMIN, json={'reuse_days': 9000}).status_code == 422


def test_repair_sends_every_kept_document_again_as_a_new_fax_only_for_a_persons_reason(client):
    approve(client)
    record, letter = pdf('Medical record', pages=40), pdf('Cover letter')
    first = send(client, [('Medical record', record), ('Cover letter', letter)], purpose='Appeal')
    finish(first.json()['fax_id'])
    assert confirm(client, ['Medical record', 'Cover letter']).status_code == 200
    assert invalidate(client, ['Cover letter']).status_code == 200
    before = fax_count()
    preview = client.post(f'/cases/{CASE}/repair', headers=ADMIN, json={'to': TO, 'preview': True})
    assert preview.status_code == 202, preview.text
    body = preview.json()
    assert body['fax_id'] is None and body['pages'] == 41 and body['missing'] == []
    assert [(item['title'], item['status']) for item in body['documents']] == [
        ('Medical record', 'included'), ('Cover letter', 'included')]
    assert client.post(f'/cases/{CASE}/repair', headers=ADMIN, json={'to': TO}).status_code == 400
    assert fax_count() == before  # a preview, a refusal and an invalidation never send

    labs = send(client, [('Labs', pdf('Labs', pages=4))], purpose='Appeal')
    waiting = client.post(f'/cases/{CASE}/repair', headers=ADMIN, json={'to': TO, 'preview': True}).json()
    assert waiting['packets_in_flight'] == 1 and waiting['pages'] == 45
    reason = 'Their intake system lost the file after a migration.'
    repaired = client.post(f'/cases/{CASE}/repair', headers=ADMIN, json={'to': TO, 'reason': reason})
    assert repaired.status_code == 202, repaired.text
    job = repaired.json()['fax_id']
    assert job and job not in {first.json()['fax_id'], labs.json()['fax_id']} and fax_count() == before + 2
    packet = PdfReader(BytesIO(client.get(f'/admin/fax-jobs/{job}/pdf', headers=ADMIN).content))
    assert len(packet.pages) == 45 and 'Medical record page 1' in packet.pages[0].extract_text()
    with engine().connect() as connection:
        row = connection.execute(sa.text('SELECT kind, reason, purpose FROM case_packets WHERE id = :id'),
                                 {'id': job}).one()
    assert tuple(row) == ('repair', reason, '')
    finish(job)
    held = by_title(client)
    # Sent again, so it waits for a new acknowledgement; the record's acknowledgement still stands.
    assert (held['Cover letter']['state'], held['Medical record']['state']) == ('sent', 'accepted')
    assert held['Labs']['fax_id'] == job


def test_a_received_acknowledgement_fax_must_be_one_the_person_can_read(client):
    approve(client)
    first = send(client, [('Medical record', pdf('Medical record', pages=3))])
    finish(first.json()['fax_id'])
    unknown = confirm(client, ['Medical record'], note=None, received_fax_id=uuid4().hex)
    assert unknown.status_code == 404
    assert by_title(client)['Medical record']['state'] == 'sent'


def test_case_inputs_and_permissions(client):
    bad = client.post('/cases/bad id!/faxes', headers=ADMIN, data={'to': '+12025550123'},
                      files=[('documents', ('a.pdf', pdf('x'), 'application/pdf'))])
    assert bad.status_code in {400, 404}
    text = send(client, [('Not a PDF', b'plain text')])
    assert text.status_code == 400 and text.json()['detail'] == 'Each case document must be a PDF.'
    reader = scoped_key(client, ['fax:read'])
    assert send(client, [('Letter', pdf('Letter'))], headers=reader).status_code == 403
    assert client.get(f'/cases/{CASE}/documents', headers=reader, params={'to': TO}).status_code == 403
    for path, body in ((f'/cases/{CASE}/accept', {'to': TO, 'documents': ['x'], 'note': 'They have it.'}),
                       (f'/cases/{CASE}/invalidate', {'to': TO, 'documents': ['x']}),
                       (f'/cases/{CASE}/repair', {'to': TO, 'reason': 'Lost'})):
        assert client.post(path, headers=reader, json=body).status_code == 403, path
    assert client.patch(f'/case-recipients/{TO}', headers=reader, json={'reuse_days': 5}).status_code == 403
    assert client.post(f'/cases/{CASE}/repair', headers=ADMIN,
                       json={'to': TO, 'reason': 'Nothing was ever sent'}).status_code == 409


def test_recent_cases_list_each_recipient_and_delivered_packets_count_the_pages_left_out(client):
    assert client.get('/cases', headers=ADMIN).json() == {'cases': []}
    record = pdf('Medical record', pages=40)
    first = send(client, [('Medical record', record), ('Cover letter', pdf('Cover letter'))])

    def listed():
        response = client.get('/cases', headers=ADMIN)
        assert response.status_code == 200, response.text
        return [(case['case_id'], case['to'], case['documents'], case['sent'], case['accepted'], case['pages'],
                 case['accepts_references']) for case in response.json()['cases']]
    assert listed() == [(CASE, TO, 2, 0, 0, 41, False)]
    finish(first.json()['fax_id'])
    assert listed() == [(CASE, TO, 2, 2, 0, 41, False)]
    # A partner's receipt is recorded without anyone opening the case first.
    partner_receipt(first.json()['fax_id'])
    assert listed() == [(CASE, TO, 2, 2, 2, 41, False)]
    approve(client)
    second = send(client, [('Medical record', record), ('New labs', pdf('New labs', pages=4))])
    assert client.get('/routing/savings', headers=ADMIN).json()['case_packets']['packets'] == 0  # not delivered yet
    finish(second.json()['fax_id'])
    assert listed() == [(CASE, TO, 3, 3, 2, 45, True)]
    packets = client.get('/routing/savings', headers=ADMIN).json()['case_packets']
    assert (packets['packets'], packets['documents_left_out'], packets['pages_not_resent'], packets['pages_saved']) == (
        1, 1, 40, 39)
    assert packets['estimate'] is True and packets['priced'] + packets['in_plan'] + packets['unpriced'] == 1
    assert packets['earlier_not_counted'] is False and packets['counted_from_sentence'] is None
    assert packets['sentence'].startswith('1 case packet left out 1 document the recipient already had: 39 pages')
    # A packet sent before Faxbot recorded what packets leave out is not counted, and the count says from when.
    with engine().begin() as connection:
        connection.execute(sa.text('DELETE FROM case_packet_sends WHERE id = :id'), {'id': first.json()['fax_id']})
    packets = client.get('/routing/savings', headers=ADMIN).json()['case_packets']
    assert packets['earlier_not_counted'] is True and packets['packets'] == 1
    assert packets['counted_from_sentence'].startswith('Counted from ')
    assert packets['counted_from_sentence'].endswith(', when Faxbot started recording what each packet left out.')
    reader = scoped_key(client, ['fax:read'])
    assert client.get('/cases', headers=reader).status_code == 403
    assert client.get('/cases', headers=ADMIN, params={'limit': 0}).status_code == 422
