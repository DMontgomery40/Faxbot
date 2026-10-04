"""The work queue over the real HTTPS application, access policy and lifespan."""
from datetime import datetime, timedelta
import hashlib
from io import BytesIO
import json
import os
import uuid
import zipfile

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.main import app
from app.work.store import WorkStore
from api.tests.test_access_management_http import (B, ORIGIN, _environment, assign, policy_version,
                                                   ready_user)


def pdf(text):
    from reportlab.pdfgen import canvas
    output = BytesIO()
    document = canvas.Canvas(output)
    document.drawString(72, 720, text)
    document.showPage()
    document.save()
    return output.getvalue()


@pytest.fixture
def client(monkeypatch, tmp_path):
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


def engine():
    return app.state.configuration_runtime.manager.store.engine


def mailbox(client, label, number):
    created = client.post('/access/mailboxes', headers=B, json={'label': label, 'enabled': True,
                                                                'expected_policy_version': policy_version(client)})
    assert created.status_code == 200, created.text
    box = created.json()['mailbox']
    rule = client.post('/access/inbound-rules', headers=B, json={'to_number': number, 'mailbox_id': box['id'],
                                                                 'expected_policy_version': policy_version(client)})
    assert rule.status_code == 200, rule.text
    return box


def receive(tmp_path, to_number, *, content=None, status='received', minutes_ago=30):
    """A received fax stored on disk, placed by the real inbound access service."""
    identity = uuid.uuid4().hex
    path = None
    digest = None
    if content is not None:
        path = tmp_path / f'{identity}.pdf'
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
    moment = datetime.utcnow() - timedelta(minutes=minutes_ago)
    app.state.access_runtime.inbound.accept(dict(
        id=identity, from_number='+15559990000', to_number=to_number, status=status, backend='sip', pages=1,
        size_bytes=len(content) if content else None, sha256=digest, pdf_path=str(path) if path else None,
        created_at=moment, received_at=moment, updated_at=moment), country='US')
    return identity


def feed(hours=0):
    return WorkStore(engine()).feed(installation_hours=hours)


def item_of(client, inbound_id, headers=B):
    items = client.get('/work', headers=headers).json()['items']
    return next(item for item in items if item['inbound_fax_id'] == inbound_id)


def test_queue_lifecycle_versions_and_console_navigation(client, tmp_path):
    front = mailbox(client, 'Front Desk', '+15550100001')
    dana, dana_principal = ready_user(client, 'dana')
    assign(client, dana_principal['id'], 'role_fax_operator', front['resource_id'])
    saved = client.put('/work/settings', headers=B, json={'mailboxes': [
        {'mailbox_id': front['id'], 'acknowledge_hours': 24, 'version': 0}]})
    assert saved.status_code == 200, saved.text
    fax = receive(tmp_path, '+15550100001', content=pdf('Synthetic referral'))
    feed()
    item = item_of(client, fax)
    assert item['state_text'] == 'Waiting for an owner.' and item['mailbox'] == 'Front Desk'
    assert item['due_text'] == 'Acknowledge within 24 hours of the document arriving (mailbox setting)'
    people = client.get(f"/work/{item['id']}/assignees", headers=B).json()['people']
    assert [person['name'] for person in people] == ['Dana']
    assigned = client.post(f"/work/{item['id']}/assign", headers=B,
                           json={'principal_id': dana_principal['id'], 'version': item['version']})
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()['state_text'].startswith('Assigned to Dana; acknowledge by ')
    stale = client.post(f"/work/{item['id']}/assign", headers=B,
                        json={'principal_id': dana_principal['id'], 'version': item['version']})
    assert stale.status_code == 409 and stale.json()['detail'] == 'This item changed; reload and try again.'
    context = dana.get('/auth/context').json()
    assert context['navigation']['work'] is True
    assert dana.get('/work/counts').json()['mine'] == 1
    acknowledged = dana.post(f"/work/{item['id']}/acknowledge", {'version': 2})
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()['state_text'] == 'Acknowledged by Dana.'
    done = dana.post(f"/work/{item['id']}/done", {'note': 'Filed in the case system', 'version': 3})
    assert done.json()['state_text'] == 'Done: Filed in the case system.'
    history = dana.get(f"/work/{item['id']}/history").json()['events']
    assert [event['kind'] for event in history] == ['received', 'assigned', 'acknowledged', 'done']
    assert history[1]['text'].endswith('assigned it to Dana.')
    assert 'no-store' in done.headers['cache-control']


def test_mailbox_operator_sees_only_their_mailbox_and_denials_disclose_nothing(client, tmp_path):
    front = mailbox(client, 'Front Desk', '+15550100001')
    mailbox(client, 'Billing', '+15550100002')
    operator, principal = ready_user(client, 'olive')
    assign(client, principal['id'], 'role_fax_operator', front['resource_id'])
    outsider, outsider_principal = ready_user(client, 'otto')
    mine = receive(tmp_path, '+15550100001', content=pdf('front'))
    other = receive(tmp_path, '+15550100002', content=pdf('billing'))
    unassigned = receive(tmp_path, '+15550109999', content=pdf('loose'))
    feed(1)
    listing = operator.get('/work').json()['items']
    assert [item['inbound_fax_id'] for item in listing] == [mine]
    assert operator.get('/work/counts').json() == {'open': 1, 'acknowledged': 0, 'done': 0, 'unassigned': 1,
                                                   'mine': 0, 'overdue': 0}
    hidden = item_of(client, other)
    for path in (f"/work/{hidden['id']}", f"/work/{hidden['id']}/history", f"/work/{hidden['id']}/export",
                 f"/work/{hidden['id']}/assignees", f"/work/{item_of(client, unassigned)['id']}"):
        response = operator.get(path)
        assert response.status_code == 404 and response.json() == {'detail': 'This work item was not found.'}, path
    refused = operator.post(f"/work/{listing[0]['id']}/assign", {'principal_id': outsider_principal['id'],
                                                                 'version': listing[0]['version']})
    assert refused.status_code == 400
    assert refused.json()['detail'] == 'Otto cannot see this document, so it cannot be assigned to them.'
    assert outsider.get('/work').json()['items'] == []
    assert outsider.get('/auth/context').json()['navigation']['work'] is False
    assert operator.get('/work/settings').status_code == 403
    assert operator.client.put('/work/settings', json={'mailboxes': [{'mailbox_id': front['id'], 'version': 0}]},
                               headers=operator.headers()).status_code == 403


def test_documents_without_bytes_wait_outside_the_queue(client, tmp_path):
    waiting = receive(tmp_path, '+15550100001', status='waiting')
    feed()
    assert client.get('/work', headers=B).json()['items'] == []
    inbox = client.get('/inbound', headers=B, params={'status': 'waiting'}).json()
    assert [row['id'] for row in inbox] == [waiting]


def test_duplicates_are_marked_and_deadlines_survive_restart(monkeypatch, tmp_path):
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    content = pdf('same bytes twice')
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as client:
        first = receive(tmp_path, '+15550100001', content=content, minutes_ago=50)
        second = receive(tmp_path, '+15550100001', content=content, minutes_ago=20)
        feed(2)
        one, two = item_of(client, first), item_of(client, second)
        assert two['duplicate_of']['id'] == one['id'] and one['duplicate_of']['id'] == two['id']
        due = (one['due_at'], two['due_at'])
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as restarted:
        feed(9)  # the installation target changed; existing deadlines stay
        assert (item_of(restarted, first)['due_at'], item_of(restarted, second)['due_at']) == due
        assert len(restarted.get('/work', headers=B).json()['items']) == 2


def _export(response):
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'application/zip'
    archive = zipfile.ZipFile(BytesIO(response.content))
    return {name: archive.read(name) for name in archive.namelist()}


def test_export_contains_manifest_original_and_history_and_names_what_is_missing(client, tmp_path):
    front = mailbox(client, 'Front Desk', '+15550100001')
    content = pdf('Synthetic evidence document')
    fax = receive(tmp_path, '+15550100001', content=content)
    feed(4)
    item = item_of(client, fax)
    first = _export(client.get(f"/work/{item['id']}/export", headers=B))
    assert set(first) == {'manifest.json', 'original.pdf', 'history.txt'}
    assert hashlib.sha256(first['original.pdf']).hexdigest() == hashlib.sha256(content).hexdigest()
    manifest = json.loads(first['manifest.json'])
    assert manifest['document']['sha256'] == manifest['document']['file_sha256'] == hashlib.sha256(content).hexdigest()
    assert manifest['item']['mailbox'] == 'Front Desk' and manifest['export']['snapshot_version'] == 1
    assert 'No provider receipt was retained for this fax.' in manifest['missing']
    assert 'No email delivery was recorded for this document.' in manifest['missing']
    assert [event['kind'] for event in manifest['history']] == ['received']
    assert b'The document arrived in Front Desk.' in first['history.txt'] and b' UTC' in first['history.txt']
    second = _export(client.get(f"/work/{item['id']}/export", headers=B))
    again = json.loads(second['manifest.json'])
    assert again['export']['id'] != manifest['export']['id']
    for document in (manifest, again):
        document['export'].pop('id'), document['export'].pop('created_at')
    assert again == manifest
    history = client.get(f"/work/{item['id']}/history", headers=B).json()['events']
    assert [event['kind'] for event in history] == ['received', 'exported', 'exported']
    os.remove(tmp_path / f'{fax}.pdf')
    gone = _export(client.get(f"/work/{item['id']}/export", headers=B))
    assert 'original.pdf' not in gone
    assert 'The original document could not be read from storage.' in json.loads(gone['manifest.json'])['missing']
    with engine().begin() as connection:
        connection.execute(sa.text('UPDATE inbound_faxes SET pdf_path = NULL WHERE id = :id'), {'id': fax})
    cleared = json.loads(_export(client.get(f"/work/{item['id']}/export", headers=B))['manifest.json'])
    assert 'The original document is no longer stored.' in cleared['missing']


def test_export_withholds_the_original_from_people_who_cannot_read_documents(client, tmp_path):
    front = mailbox(client, 'Front Desk', '+15550100001')
    auditor, auditor_principal = ready_user(client, 'audrey')
    assign(client, auditor_principal['id'], 'role_auditor', front['resource_id'])
    operator, operator_principal = ready_user(client, 'olive')
    assign(client, operator_principal['id'], 'role_fax_operator', front['resource_id'])
    fax = receive(tmp_path, '+15550100001', content=pdf('withheld'))
    feed()
    item = item_of(client, fax)
    assert item_of(client, fax, headers=auditor.headers(csrf=False))['actions'] == ['export']
    files = _export(auditor.get(f"/work/{item['id']}/export"))
    assert set(files) == {'manifest.json', 'history.txt'}
    withheld = 'The original document was withheld because you do not have permission to read documents.'
    assert withheld in json.loads(files['manifest.json'])['missing']
    assert withheld.encode() in files['history.txt']
    assert operator.get(f"/work/{item['id']}/export").status_code == 403  # no work:export
    assign(client, operator_principal['id'], 'role_auditor', front['resource_id'])
    files = _export(operator.get(f"/work/{item['id']}/export"))
    assert 'original.pdf' in files


def _import(client, content, manifest, headers=B):
    files = {'file': ('referral.pdf', content, 'application/pdf')} if content is not None else None
    data = {'manifest': manifest if isinstance(manifest, str) else json.dumps(manifest)} if manifest is not None else {}
    return client.post('/imports', headers=headers, files=files, data=data)


@pytest.mark.parametrize('content, manifest, message', [
    (None, {'source_system': 'case-system', 'operation_id': 'op-1'}, 'Attach the document as a PDF file.'),
    (b'not a pdf at all', {'source_system': 'case-system', 'operation_id': 'op-1'}, 'The file is not a PDF.'),
    (b'%PDF-1.4\n% broken\n%%EOF', {'source_system': 'case-system', 'operation_id': 'op-1'},
     'The file is not a valid PDF.'),
    ('pdf', None, 'Add a manifest describing the document.'),
    ('pdf', '{not json', 'The manifest is not valid JSON.'),
    ('pdf', {'operation_id': 'op-1'}, 'The manifest needs source_system.'),
    ('pdf', {'source_system': 'case-system', 'operation_id': 'op-1', 'priority': 'high'},
     'The manifest has fields Faxbot does not use: priority.'),
    ('pdf', {'source_system': 'case-system', 'operation_id': 'op-1', 'source_received_at': '2026-10-03 14:05'},
     'source_received_at must be a time like 2026-10-03T14:05:00Z, with its offset.'),
    ('pdf', {'source_system': 'case-system', 'operation_id': 'op-1', 'pages': 0},
     'pages must be a whole number from 1 to 10000.'),
])
def test_import_refuses_bad_input_with_one_sentence_and_records_nothing(client, content, manifest, message):
    body = _import(client, pdf('import') if content == 'pdf' else content, manifest)
    assert body.status_code == 400 and body.json() == {'detail': message}
    with engine().connect() as connection:
        assert connection.execute(sa.text('SELECT COUNT(*) FROM inbound_faxes')).scalar_one() == 0


def test_import_requires_the_import_permission(client):
    def key(scopes):
        response = client.post('/admin/api-keys', headers=B, json={'name': 'synthetic', 'scopes': scopes})
        assert response.status_code == 200, response.text
        return {'X-API-Key': response.json()['token']}
    denied = _import(client, pdf('import'), {'source_system': 'case-system', 'operation_id': 'op-1'},
                     headers=key(['inbound:list']))
    assert denied.status_code == 403
    importer = key(['work:import'])  # an integration key can carry the import scope
    assert _import(client, pdf('import'), {'operation_id': 'op-1'}, headers=importer).status_code == 400
