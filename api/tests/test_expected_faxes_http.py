"""Expected faxes over the real application: email intake matching, import, outage mode, evidence and access.

The email path is the real one: a TLS IMAP stand-in, the intake poller, the
generic import, the work feed and the expectation worker. Synthetic mailboxes
(example.com), people and 555 numbers only.
"""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.work.expectations import ExpectationStore
from app.work.store import WorkStore
from api.tests.imap_fake import FakeImap
from api.tests.test_access_management_http import B, ORIGIN, _environment, assign, ready_user
from api.tests.test_intake_sources import (add, check, database_url, email, engine, idle,  # noqa: F401 - fixture
                                           mail_settings, poller)
from api.tests.test_work_http import hold_worker, mailbox


@pytest.fixture
def client(monkeypatch, tmp_path, database_url):  # noqa: F811
    _environment(monkeypatch, tmp_path)
    if database_url:
        monkeypatch.setenv('DATABASE_URL', database_url)
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    monkeypatch.setattr('app.intake.sources.http.repeat', idle)
    hold_worker(monkeypatch)
    # The lifespan's own expectation worker waits; each test runs it when the arrivals are in.
    monkeypatch.setattr('app.work.expectations.ExpectationWorker.step', lambda self, now=None: False)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


@pytest.fixture
def imap(tmp_path):
    server = FakeImap(tmp_path, password='synthetic-mailbox-password')
    yield server
    server.close()


def match_now():
    """The work feed, then one real expectation pass (the held lifespan step is bypassed)."""
    WorkStore(engine()).feed()
    store = ExpectationStore(engine())
    store.examine()
    store.look_back()


def expected(client, code, headers=B):
    response = client.get(f'/expected-faxes/{code}', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_an_email_subject_closes_only_the_revision_it_names(client, imap):
    box = mailbox(client, 'Purchasing', '+15550100001')
    created = client.post('/expected-faxes', headers=B, json={
        'reference': 'PO 483', 'kind': 'Signed acknowledgement', 'mailbox_id': box['id'],
        'counterparty': 'Acme Supply', 'required_revision': 'B', 'email_subject': 'PO 483', 'due_hours': 48})
    assert created.status_code == 201, created.text
    code = created.json()['code']
    assert created.json()['keys']['subaddress'] is None  # "PO 483" cannot travel as a subaddress
    source = add(client, {'name': 'Supplier replies', 'kind': 'email', 'direction': 'receive',
                          'settings': mail_settings(imap, mailbox_id=box['id']),
                          'secret': {'password': 'synthetic-mailbox-password'}})
    runner = poller(imap)
    imap.add(email(subject='Re: PO 483 rev A acknowledged', message_id='<ack-a@example.com>', results=None))
    assert check(runner, source['id'])[0]
    match_now()
    view = expected(client, code)
    assert view['state'] == 'proposed_match'
    assert view['proposals'][0]['text'] == ('It carries the reference in its email subject, but says revision A; '
                                            'you expect revision B.')
    assert view['proposals'][0]['fax']['mailbox'] == 'Purchasing'
    rejected = client.post(f'/expected-faxes/{code}/reject', headers=B, json={
        'proposal_id': view['proposals'][0]['id'], 'version': view['version'], 'note': 'Old revision'})
    assert rejected.status_code == 200 and rejected.json()['state'] == 'open'

    imap.add(email(subject='PO 483 rev B signed', message_id='<ack-b@example.com>', results=None))
    assert check(runner, source['id'])[0]
    match_now()
    view = expected(client, code)
    assert view['state'] == 'matched' and view['match']['signal'] == 'email_subject'
    events = client.get(f'/expected-faxes/{code}/history', headers=B).json()['events']
    assert [event['kind'] for event in events] == ['created', 'proposed', 'proposal_rejected', 'matched']
    assert events[-1]['text'] == 'A received fax answered it, matched by its email subject.'
    report = client.get('/expected-faxes/report', headers=B).json()
    # The rejected revision A email answered nothing, and is counted apart from the match.
    assert (report['expected'], report['matched'], report['arrived'], report['arrived_unmatched']) == (1, 1, 2, 1)


def test_import_outage_and_evidence_over_http_with_access_checked(client):
    front = mailbox(client, 'Front Desk', '+15550100001')
    billing = mailbox(client, 'Billing', '+15550100002')
    saved = client.post('/expected-faxes/sources', headers=B, json={
        'name': 'Open purchase orders', 'format': 'json',
        'mapping': {'reference': 'po', 'counterparty': 'supplier', 'mailbox': 'team', 'due': 'due'},
        'mailbox_id': front['id'], 'subject_template': 'PO {reference}'})
    assert saved.status_code == 200, saved.text
    rows = [{'po': '100', 'supplier': 'Acme', 'team': 'Front Desk', 'due': '2099-01-01'},
            {'po': '200', 'supplier': 'Beta', 'team': 'Billing'}]
    upload = lambda data, full=True: client.post(
        '/expected-faxes/imports', headers=B, data={'source': 'Open purchase orders', 'full_export': str(full).lower()},
        files={'file': ('open.json', json.dumps(data).encode(), 'application/json')})
    first = upload(rows)
    assert first.status_code == 200, first.text
    assert first.json()['summary'] == ('2 rows: 2 new, 0 unchanged, 0 new revisions, 0 changed without a new '
                                       'revision, 0 with problems. Every expected fax still waiting is in this export.')
    assert upload(rows).json()['replay'] is True
    listing = client.get('/expected-faxes', headers=B).json()['expected']
    assert sorted(entry['reference'] for entry in listing) == ['100', '200']

    # A Front Desk operator sees only Front Desk, cannot import, and cannot reach Billing's.
    operator, principal = ready_user(client, 'olive')
    assign(client, principal['id'], 'role_fax_operator', front['resource_id'])
    mine = operator.get('/expected-faxes').json()['expected']
    assert [entry['reference'] for entry in mine] == ['100']
    assert operator.get('/expected-faxes/counts').json()['waiting'] == 1
    hidden = next(entry for entry in listing if entry['reference'] == '200')
    for path in (f"/expected-faxes/{hidden['code']}", f"/expected-faxes/{hidden['code']}/history",
                 f"/expected-faxes/{hidden['code']}/export"):
        response = operator.get(path)
        assert response.status_code == 404 and response.json() == {'detail': 'This expected fax was not found.'}
    assert operator.get('/expected-faxes/sources').status_code == 403
    assert operator.get('/expected-faxes/outages').status_code == 403

    # Outage mode: start, record, end, import, three lists.
    outage = client.post('/expected-faxes/outages', headers=B, json={'source': 'Open purchase orders'})
    assert outage.status_code == 201, outage.text
    outage = outage.json()
    recorded = client.post(f"/expected-faxes/outages/{outage['code']}/actions", headers=B, json={
        'operation_id': '100', 'action': 'Order confirmed with Acme by phone', 'channel': 'phone'})
    assert recorded.status_code == 200, recorded.text
    ended = client.post(f"/expected-faxes/outages/{outage['code']}/end", headers=B,
                        json={'version': outage['version']})
    assert ended.status_code == 200 and ended.json()['open'] is False
    after = upload(rows + [{'po': '300', 'supplier': 'Gamma', 'team': 'Billing'}])
    assert after.json()['reconciled_outage'] == outage['code']
    lists = client.get(f"/expected-faxes/outages/{outage['code']}", headers=B).json()['reconciliation']
    assert [entry['operation_id'] for entry in lists['already_done']] == ['100']
    assert sorted(entry['operation_id'] for entry in lists['new']) == ['200', '300']

    # Evidence for the operator's own expected fax.
    own = mine[0]
    response = operator.get(f"/expected-faxes/{own['code']}/export")
    assert response.status_code == 403  # a fax operator reads and manages, but does not export
    response = client.get(f"/expected-faxes/{own['code']}/export", headers=B)
    assert response.status_code == 200 and response.headers['content-type'] == 'application/zip'
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(response.content)).read('manifest.json'))
    assert manifest['expected']['source']['operation_id'] == '100'
    assert manifest['outage_actions'][0]['action'] == 'Order confirmed with Acme by phone'
    # Cancelling needs a note and the version last seen.
    closed = operator.post(f"/expected-faxes/{own['code']}/close", {'outcome': 'cancelled', 'note': 'Withdrawn',
                                                                    'version': own['version']})
    assert closed.status_code == 200 and closed.json()['state'] == 'cancelled'
    stale = operator.post(f"/expected-faxes/{own['code']}/close", {'outcome': 'cancelled', 'note': 'Again',
                                                                   'version': own['version']})
    assert stale.status_code == 409
    assert billing['id'] != front['id']


def test_a_direct_reply_closes_the_expectation_for_the_request_it_names(client):
    from types import SimpleNamespace
    from app.digital.direct_message import Received, replies_to
    from app.digital.worker import filer
    from api.tests.test_work_http import pdf
    assert replies_to({'in-reply-to': '<request-1@faxbot.example>',
                       'references': '<older@faxbot.example> <request-1@faxbot.example>'}) == (
        '<request-1@faxbot.example>', '<older@faxbot.example>')
    box = mailbox(client, 'Records', '+15550100003')
    created = client.post('/expected-faxes', headers=B, json={
        'reference': 'Records request 882', 'kind': 'Signed records', 'mailbox_id': box['id'],
        'message_id': '<request-1@faxbot.example>'})
    assert created.status_code == 201, created.text
    values = app.state.configuration_runtime.manager.store.read().active.values
    account = SimpleNamespace(key='hisp', setting=lambda name: box['id'] if name == 'mailbox_id' else None)
    received = Received('<reply-9@hisp.example>', 'records@hisp.example', [], replies_to=(
        '<request-1@faxbot.example>',))
    filed = filer(lambda: app.state.access_runtime, values)(account, received, 1, 'records.pdf', 'application/pdf',
                                                             pdf('Synthetic records'))
    assert filed['status'] == 'received', filed
    match_now()
    view = expected(client, created.json()['code'])
    assert view['state'] == 'matched' and view['match']['signal'] == 'digital_message'
    assert view['state_text'].startswith('Arrived and matched by its Direct message ID on ')


def test_a_partners_registered_form_closes_the_expectation_only_with_the_revision_it_states(client, tmp_path):
    from app.forms.store import FormStore
    from app.inbound.acquisition import ImportStore
    from app.intake.store import IntakeStore
    from api.tests.test_work_http import pdf
    mailbox(client, 'Purchasing', '+15550100001')
    box = client.get('/expected-faxes/mailboxes', headers=B).json()['mailboxes'][0]
    codes = {}
    for reference, revision in (('PO 483', 'B'), ('PO 484', 'C')):
        created = client.post('/expected-faxes', headers=B, json={
            'reference': reference, 'kind': 'Signed acknowledgement', 'mailbox_id': box['id'],
            'form_field': 'po_number', 'revision_field': 'rev', 'required_revision': revision})
        assert created.status_code == 201, created.text
        codes[reference] = created.json()['code']
    document = tmp_path / 'form.pdf'
    document.write_bytes(pdf('Synthetic acknowledgement form'))
    for message_id, values in (('a' * 32, {'po_number': 'PO 483', 'rev': 'B'}),
                               ('b' * 32, {'po_number': 'po  484', 'rev': 'B'})):
        # What forms/exchange.py records for a partner's form that matched, then direct/filing.py's filing.
        FormStore(engine()).record(direction='inbound', route='direct', message_id=message_id, peer_id='peer-1',
                                   partner='Acme Supply', form_address='f' * 64, renderer='synthetic',
                                   resolution='fine', fax_number='+15550104444', field_values=json.dumps(values),
                                   page_hashes='[]', pages=1, digest='d' * 64, state='matched')
        IntakeStore(engine(), None).add_fax_image(
            ImportStore(app.state.access_runtime.inbound), account='direct:peer-1', message_id=message_id,
            image_path=str(document), from_number='+15550104444', to_number='+15550100001', pages=1,
            received_at=None, report={'route': 'direct', 'kind': 'form', 'partner': 'Acme Supply',
                                      'message_id': message_id}, original=True)
    match_now()
    closed = expected(client, codes['PO 483'])
    assert closed['state'] == 'matched' and closed['match']['signal'] == 'form_field'
    wrong = expected(client, codes['PO 484'])
    assert wrong['state'] == 'proposed_match'
    assert wrong['proposals'][0]['text'] == ("It carries the reference in the partner's registered form, but says "
                                             'revision B; you expect revision C.')
