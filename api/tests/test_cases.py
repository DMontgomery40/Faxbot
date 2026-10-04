"""Case ledger: send only new or revised documents to recipients that accept references."""
from io import BytesIO

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app import main
from api.tests.test_routing_http import ADMIN, BOOTSTRAP, scoped_key


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


def send(client, documents, *, preview=False, headers=ADMIN):
    files = [('documents', (name, data, 'application/pdf')) for name, data in documents]
    return client.post('/cases/claim-2026-117/faxes', headers=headers, files=files,
                       data={'to': '+1 202 555 0123', 'titles': [name for name, _ in documents],
                             'preview': 'true' if preview else 'false'})


def finish(job_id):
    engine = main.app.state.configuration_runtime.manager.store.engine
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE outbound_deliveries SET state='success' WHERE id=:id"), {'id': job_id})


def test_everything_is_sent_until_the_recipient_accepts_references(client):
    record, letter = pdf('Medical record', pages=40), pdf('Cover letter')
    first = send(client, [('Medical record', record), ('Cover letter', letter)])
    assert first.status_code == 202, first.text
    assert first.json()['pages'] == 41 and first.json()['pages_saved'] == 0
    finish(first.json()['fax_id'])
    # References are not approved yet, so the whole file goes again.
    again = send(client, [('Medical record', record), ('Update', pdf('Update', pages=4))], preview=True)
    assert again.json()['pages'] == 44 and again.json()['fax_id'] is None


def test_accepted_documents_are_referenced_by_a_one_page_index(client):
    record = pdf('Medical record', pages=40)
    first = send(client, [('Medical record', record), ('Cover letter', pdf('Cover letter'))])
    finish(first.json()['fax_id'])
    approved = client.patch('/routing/destinations/+12025550123', headers=ADMIN, json={'accepts_references': True})
    assert approved.status_code == 200 and approved.json()['accepts_references'] is True
    update = pdf('New labs', pages=4)
    second = send(client, [('Medical record', record), ('New labs', update)])
    body = second.json()
    assert body['pages'] == 5 and body['pages_saved'] == 39
    assert [(item['title'], item['status']) for item in body['documents']] == [
        ('New labs', 'included'), ('Medical record', 'referenced')]
    packet = client.get(f"/admin/fax-jobs/{body['fax_id']}/pdf", headers=ADMIN)
    reader = PdfReader(BytesIO(packet.content))
    assert len(reader.pages) == 5
    index = reader.pages[0].extract_text()
    assert 'Case claim-2026-117' in index and 'Medical record (40 pages), accepted' in index
    assert 'New labs (4 pages), starting on page 2' in index
    assert 'New labs page 1' in reader.pages[1].extract_text()
    # A revised record is a different document and is sent in full.
    revised = send(client, [('Medical record', pdf('Medical record corrected', pages=40))], preview=True)
    assert revised.json()['pages'] == 40
    # Nothing new: refuse rather than fax an index alone.
    finish(body['fax_id'])
    assert send(client, [('Medical record', record), ('New labs', update)], preview=True).status_code == 409
    record_view = client.get('/cases/claim-2026-117/documents', headers=ADMIN,
                             params={'to': '+12025550123'}).json()
    assert record_view['accepts_references'] is True
    assert [(item['title'], item['accepted']) for item in record_view['documents']] == [
        ('Medical record', True), ('Cover letter', True), ('New labs', True)]


def test_case_inputs_and_permissions(client):
    bad = client.post('/cases/bad id!/faxes', headers=ADMIN, data={'to': '+12025550123'},
                      files=[('documents', ('a.pdf', pdf('x'), 'application/pdf'))])
    assert bad.status_code in {400, 404}
    text = send(client, [('Not a PDF', b'plain text')])
    assert text.status_code == 400 and text.json()['detail'] == 'Each case document must be a PDF.'
    reader = scoped_key(client, ['fax:read'])
    assert send(client, [('Letter', pdf('Letter'))], headers=reader).status_code == 403
    assert client.get('/cases/claim-2026-117/documents', headers=reader,
                      params={'to': '+12025550123'}).status_code == 403


def test_recent_cases_list_each_recipient_and_delivered_packets_count_the_pages_left_out(client):
    assert client.get('/cases', headers=ADMIN).json() == {'cases': []}
    record = pdf('Medical record', pages=40)
    first = send(client, [('Medical record', record), ('Cover letter', pdf('Cover letter'))])

    def listed():
        response = client.get('/cases', headers=ADMIN)
        assert response.status_code == 200, response.text
        return [(case['case_id'], case['to'], case['documents'], case['accepted'], case['pages'],
                 case['accepts_references']) for case in response.json()['cases']]
    assert listed() == [('claim-2026-117', '+12025550123', 2, 0, 41, False)]
    finish(first.json()['fax_id'])
    # Accepted as soon as the fax finished, without anyone opening the case first.
    assert listed() == [('claim-2026-117', '+12025550123', 2, 2, 41, False)]
    client.patch('/routing/destinations/+12025550123', headers=ADMIN, json={'accepts_references': True})
    second = send(client, [('Medical record', record), ('New labs', pdf('New labs', pages=4))])
    assert client.get('/routing/savings', headers=ADMIN).json()['case_packets']['packets'] == 0  # not delivered yet
    finish(second.json()['fax_id'])
    assert listed() == [('claim-2026-117', '+12025550123', 3, 3, 45, True)]
    packets = client.get('/routing/savings', headers=ADMIN).json()['case_packets']
    assert (packets['packets'], packets['documents_left_out'], packets['pages_not_resent'], packets['pages_saved']) == (
        1, 1, 40, 39)
    assert packets['estimate'] is True and packets['priced'] + packets['in_plan'] + packets['unpriced'] == 1
    assert packets['earlier_not_counted'] is False and packets['counted_from_sentence'] is None
    assert packets['sentence'].startswith('1 case packet left out 1 document the recipient already had: 39 pages')
    # A packet sent before Faxbot recorded what packets leave out is not counted, and the count says from when.
    engine = main.app.state.configuration_runtime.manager.store.engine
    with engine.begin() as connection:
        connection.execute(sa.text('DELETE FROM case_packet_sends WHERE id = :id'), {'id': first.json()['fax_id']})
    packets = client.get('/routing/savings', headers=ADMIN).json()['case_packets']
    assert packets['earlier_not_counted'] is True and packets['packets'] == 1
    assert packets['counted_from_sentence'].startswith('Counted from ')
    assert packets['counted_from_sentence'].endswith(', when Faxbot started recording what each packet left out.')
    reader = scoped_key(client, ['fax:read'])
    assert client.get('/cases', headers=reader).status_code == 403
    assert client.get('/cases', headers=ADMIN, params={'limit': 0}).status_code == 422
