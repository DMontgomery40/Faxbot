"""Two installations exchange registered forms: values and page hashes, never the pages, unless a person decides.

Installation B is the running application (TestClient); installation A is a
direct delivery service on its own database, as in test_direct_delivery.py.
Both directions are exercised: A sends to B's HTTP endpoints, and B sends
through its own console routes to A.
"""
import asyncio
from dataclasses import replace
import json

import httpx
import pytest
import sqlalchemy as sa

from app import main
from api.app.forms.exchange import FormExchange as AFormExchange
from api.app.forms import model as a_model, renderer as a_renderer
from api.tests.forms_fixtures import SVG, VALUES, positions_json
from api.tests.test_direct_delivery import A_NUMBER, B_NUMBER, b_client, pair  # noqa: F401 - fixtures
from api.tests.test_routing_http import ADMIN


OTHER_NUMBER = '+15550100009'


class ToAForms:
    """B's transport to installation A: direct deliveries, status questions and the forms partner protocol."""

    def __init__(self, a):
        self.a = a
        self.mode = 'normal'
        self.fetches = 0
        self.posts = 0

    async def request(self, method, url, **kwargs):
        from app.direct.service import PartnerUnreachable
        assert url.startswith('https://a.example/'), url
        path = url[len('https://a.example'):]
        headers = kwargs.get('headers') or {}
        signer = dict(signer=headers.get('X-Faxbot-Direct-Key'), request_time=headers.get('X-Faxbot-Direct-Time'),
                      signature=headers.get('X-Faxbot-Direct-Signature'))
        if self.mode == 'unreachable':
            raise PartnerUnreachable()
        if method == 'POST' and path == '/direct/deliveries':
            if self.mode == 'drop':
                raise httpx.ReadTimeout('request lost before reaching the partner')
            files = kwargs['files']
            self.posts += 1
            answer = await asyncio.to_thread(self.a.receive, files['manifest'][1], files['signature'][1].decode('ascii'),
                                             files['document'][1])
            if self.mode == 'lose_answer':
                raise httpx.ReadTimeout('answer lost after the partner received it')
            return answer
        if method == 'POST' and path == '/direct/verifications':
            body = kwargs['json']
            return await asyncio.to_thread(self.a.confirm, body['statement'], body['signature'])
        if method == 'GET' and path.startswith('/direct/deliveries/'):
            return await asyncio.to_thread(lambda: self.a.status(path.rsplit('/', 1)[1], **signer))
        if method == 'GET' and path == '/forms/partner/holdings':
            return await asyncio.to_thread(lambda: AFormExchange(self.a).holdings(**signer))
        if method == 'GET' and path.startswith('/forms/partner/forms/'):
            self.fetches += 1
            return await asyncio.to_thread(lambda: AFormExchange(self.a).bundle(path.rsplit('/', 1)[1], **signer))
        raise AssertionError((method, path))


@pytest.fixture
def forms(pair):  # noqa: F811
    """Both installations verified to each other, with B's transport able to reach A's form routes."""
    a, client = pair['a'], pair['b_client']
    to_a = ToAForms(a)
    main.app.state.direct_http = to_a
    from app.direct.store import DirectStore
    b_store = DirectStore(b_engine())
    b_store.start_challenge(pair['a_on_b']['id'], code='73104528', job_id=None)
    assert asyncio.run(a.send_confirmation(pair['b_on_a']['id'], '7310 4528')) == 'The partner confirmed the code.'
    assert b_store.get_peer(pair['a_on_b']['id'])['state'] == 'verified'
    return {**pair, 'to_a': to_a, 'client': client}


def b_engine():
    return main.app.state.configuration_runtime.manager.store.engine


def rows(engine, query):
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.text(query)).mappings()]


def import_on_b(client, name='Referral'):
    response = client.post('/forms', headers=ADMIN, data={'name': name},
                           files={'file': ('referral.svg', SVG, 'image/svg+xml'),
                                  'positions': ('positions.json', positions_json(), 'application/json')})
    assert response.status_code == 201, response.text
    return response.json()['version']


def import_on_a(a):
    from api.app.forms.importer import from_template
    exchange = AFormExchange(a)
    version, _ = exchange.store.add_version(from_template(SVG, file_name='referral.svg', positions=positions_json()),
                                           name='Referral')
    return exchange, version


def a_sends(a, values=VALUES, *, tamper=False):
    exchange, version = import_on_a(a)
    normalized, rendered = exchange.prepare(version, values)
    if tamper:
        rendered = replace(rendered, hashes=('0' * 64,) + rendered.hashes[1:])
    peer = exchange.direct.store.peer_by_key(a_peer_key(a))
    return exchange, asyncio.run(exchange.send_direct(version, normalized, rendered, peer, actor_id=None,
                                                      actor_name=None))


def a_peer_key(a):
    peers = a.store.list_peers()
    assert len(peers) == 1
    return peers[0]['signing_key']


def test_a_matching_form_is_filed_as_received_with_its_values(forms):
    a, client = forms['a'], forms['client']
    _, sent = a_sends(a)
    assert sent['state'] == 'delivered' and sent['route'] == 'direct' and sent['pages'] == 1
    # B fetched the form from A once, by its address, and filed the pages it drew itself.
    assert forms['to_a'].fetches == 1
    received = client.get('/forms/received', headers=ADMIN).json()['received']
    assert len(received) == 1
    item = received[0]
    assert item['state'] == 'matched' and item['form'] == 'Referral' and item['form_version'] == 1
    assert item['values']['patient'] == 'Ána Müller-Øster' and item['values']['amount'] == '1234.50'
    assert item['values']['urgent'] is True and item['partner'] == 'Valley Hospital'
    assert {field['name'] for field in item['fields']} >= {'patient', 'born', 'signature'}
    intake = client.get('/intake/items', headers=ADMIN).json()['items']
    assert [entry['id'] for entry in intake if entry['source'] == 'direct'] == [item['intake_item_id']]
    stored = rows(b_engine(), "SELECT document_path FROM direct_deliveries WHERE direction='inbound' AND state='accepted'")
    with open(stored[0]['document_path'], 'rb') as handle:
        assert handle.read().startswith(b'%PDF')
    held = rows(b_engine(), "SELECT f.origin, v.source, v.number FROM forms f JOIN form_versions v ON v.form_id = f.id")
    assert held == [{'origin': 'partner', 'source': 'partner', 'number': 1}]


def test_a_missing_form_is_fetched_once_by_its_address(forms):
    a = forms['a']
    a_sends(a)
    a_sends(a, {**VALUES, 'patient': 'Second Synthetic'})
    assert forms['to_a'].fetches == 1
    assert len(forms['client'].get('/forms/received', headers=ADMIN).json()['received']) == 2


def test_a_mismatch_files_nothing_and_asks_for_the_full_pages(forms):
    a, client = forms['a'], forms['client']
    _, sent = a_sends(a, tamper=True)
    assert sent['state'] == 'mismatch'
    assert sent['detail'] is None
    assert client.get('/forms/received', headers=ADMIN).json()['received'] == []
    assert [entry for entry in client.get('/intake/items', headers=ADMIN).json()['items'] if entry['source'] == 'direct'] == []
    assert rows(b_engine(), "SELECT count(*) AS n FROM direct_deliveries WHERE state='accepted'") == [{'n': 0}]
    kept = rows(b_engine(), "SELECT state, field_values, detail FROM form_deliveries WHERE direction='inbound'")
    assert kept == [{'state': 'mismatch', 'field_values': None,
                     'detail': 'The pages drawn here did not match; Faxbot asked the sender for the full pages.'}]
    # Nothing was faxed: only a person sends the pages (see the next test).
    assert rows(forms['a'].store.engine, 'SELECT count(*) AS n FROM fax_jobs') == [{'n': 0}]


def send_from_b(client, version, to=A_NUMBER, values=None, **extra):
    body = {'version_id': version['id'], 'to': to, 'values': values or {**VALUES}, **extra}
    response = client.post('/forms/send', headers=ADMIN, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def b_jobs():
    return rows(b_engine(), 'SELECT id, to_number, pages, file_name FROM fax_jobs ORDER BY created_at')


def test_after_a_mismatch_only_a_person_sends_the_pages_and_only_once(forms, monkeypatch):
    client = forms['client']
    version = import_on_b(client)
    from app.forms import exchange as b_exchange
    prepare = b_exchange.FormExchange.prepare

    def different_renderer(self, version, values, resolution='fine'):
        normalized, rendered = prepare(self, version, values, resolution)
        return normalized, replace(rendered, hashes=('f' * 64,) + rendered.hashes[1:])
    monkeypatch.setattr(b_exchange.FormExchange, 'prepare', different_renderer)
    sent = send_from_b(client, version)
    assert sent['state'] == 'mismatch' and sent['can_fax'] is True and sent['fax_id'] is None
    assert b_jobs() == []
    # A fetched B's form and filed nothing.
    assert rows(forms['a'].store.engine, "SELECT state FROM form_deliveries WHERE direction='inbound'") == [{'state': 'mismatch'}]
    assert rows(forms['a'].store.engine, "SELECT count(*) AS n FROM intake_items") == [{'n': 0}]
    faxed = client.post(f"/forms/deliveries/{sent['id']}/fax", headers=ADMIN)
    assert faxed.status_code == 200, faxed.text
    assert faxed.json()['message'] == 'The pages are on their way as a fax.'
    assert faxed.json()['state'] == 'mismatch' and faxed.json()['can_fax'] is False
    jobs = b_jobs()
    assert len(jobs) == 1 and jobs[0]['to_number'] == A_NUMBER and jobs[0]['pages'] == 1
    assert faxed.json()['fax_id'] == jobs[0]['id']
    again = client.post(f"/forms/deliveries/{sent['id']}/fax", headers=ADMIN)
    assert again.json()['message'] == 'These pages were already sent as a fax.' and len(b_jobs()) == 1


def test_a_matching_form_sent_from_the_console_is_delivered(forms):
    client = forms['client']
    version = import_on_b(client)
    sent = send_from_b(client, version)
    assert sent['state'] == 'delivered' and sent['partner'] == 'Valley Hospital' and sent['form'] == 'Referral'
    assert b_jobs() == []
    filed = rows(forms['a'].store.engine, "SELECT state, field_values FROM form_deliveries WHERE direction='inbound'")
    assert filed[0]['state'] == 'matched' and json.loads(filed[0]['field_values'])['clinic'] == 'South'
    assert rows(forms['a'].store.engine, "SELECT source FROM intake_items") == [{'source': 'direct'}]
    listed = client.get('/forms/deliveries', headers=ADMIN).json()['deliveries']
    assert [(item['direction'], item['state']) for item in listed] == [('outbound', 'delivered')]


def test_a_form_to_a_number_without_a_partner_is_sent_as_a_fax(forms):
    client = forms['client']
    version = import_on_b(client)
    sent = send_from_b(client, version, to=OTHER_NUMBER)
    assert sent['route'] == 'fax' and sent['state'] == 'faxed' and sent['fax_id']
    jobs = b_jobs()
    assert len(jobs) == 1 and jobs[0]['id'] == sent['fax_id'] and jobs[0]['file_name'] == 'Referral v1.pdf'
    # The faxed pages are the pages a partner would have drawn.
    rendered = client.post(f"/forms/versions/{version['id']}/render?format=json", headers=ADMIN,
                           json={'values': VALUES}).json()
    stored = rows(b_engine(), f"SELECT page_hashes FROM form_deliveries WHERE id = '{sent['id']}'")
    assert json.loads(stored[0]['page_hashes']) == rendered['page_hashes']
    # Asking for the fax route sends a fax even to a partner.
    assert send_from_b(client, version, route='fax')['route'] == 'fax' and len(b_jobs()) == 2


def test_a_lost_answer_is_settled_by_asking_never_by_sending_again(forms):
    client = forms['client']
    version = import_on_b(client)
    forms['to_a'].mode = 'lose_answer'
    sent = send_from_b(client, version)
    assert sent['state'] == 'uncertain' and sent['can_fax'] is False
    forms['to_a'].mode = 'normal'
    from app.forms.http import exchange_for
    exchange = exchange_for(main.app)
    row = exchange.store.delivery(sent['id'])
    assert asyncio.run(exchange.reconcile(row)) == 'delivered'
    assert forms['to_a'].posts == 1
    # Lost on the way: the partner signs that it never had it, and fences the message.
    forms['to_a'].mode = 'drop'
    dropped = send_from_b(client, version)
    assert dropped['state'] == 'uncertain'
    forms['to_a'].mode = 'normal'
    assert asyncio.run(exchange.reconcile(exchange.store.delivery(dropped['id']))) == 'not_received'
    view = client.get(f"/forms/deliveries/{dropped['id']}", headers=ADMIN).json()
    assert view['can_fax'] is True and view['values']['patient'] == 'Ána Müller-Øster' and b_jobs() == []


def test_an_unreachable_partner_gets_nothing_and_a_person_decides(forms):
    client = forms['client']
    version = import_on_b(client)
    forms['to_a'].mode = 'unreachable'
    sent = send_from_b(client, version)
    assert sent['state'] == 'not_sent' and sent['can_fax'] is True and b_jobs() == []


def test_partner_routes_answer_only_an_enrolled_partners_signature(forms):
    client = forms['client']
    version = import_on_b(client)
    assert client.get('/forms/partner/holdings').status_code == 403
    assert client.get(f"/forms/partner/forms/{version['address']}").status_code == 403
    from api.app.forms.exchange import request_headers
    identity = forms['a'].identity()
    held = client.get('/forms/partner/holdings', headers=request_headers(identity, 'GET', '/forms/partner/holdings'))
    assert held.status_code == 200
    from api.app.direct.crypto import check_signed
    b_key = client.get('/direct/card', headers=ADMIN).json()['card']['signing_key']
    statement = check_signed(held.json(), b_key)
    assert statement['forms'] == [{'address': version['address'], 'title': 'Referral', 'version': 1}]
    path = f"/forms/partner/forms/{version['address']}"
    # A signature for another path does not open this one.
    assert client.get(path, headers=request_headers(identity, 'GET', '/forms/partner/holdings')).status_code == 403
    bundle = client.get(path, headers=request_headers(identity, 'GET', path)).json()
    assert bundle['address'] == version['address'] and bundle['version'] == 1
    missing = '/forms/partner/forms/' + 'e' * 64
    assert client.get(missing, headers=request_headers(identity, 'GET', missing)).status_code == 404


def test_the_console_asks_a_partner_which_forms_it_holds(forms):
    client = forms['client']
    import_on_a(forms['a'])
    answer = client.get(f"/forms/partners/{forms['a_on_b']['id']}", headers=ADMIN).json()
    assert answer['reached'] is True and answer['message'] == 'Valley Hospital holds 1 form version.'
    assert answer['forms'][0]['title'] == 'Referral' and answer['forms'][0]['also_here'] is False


def test_console_routes_read_import_preview_and_render(forms):
    client = forms['client']
    version = import_on_b(client)
    again = client.post('/forms', headers=ADMIN, data={'name': 'Copy'},
                        files={'file': ('referral.svg', SVG, 'image/svg+xml'),
                               'positions': ('positions.json', positions_json(), 'application/json')})
    assert again.json()['created'] is False
    assert again.json()['message'] == 'This form is already registered as Referral version 1.'
    listed = client.get('/forms', headers=ADMIN).json()
    assert [form['name'] for form in listed['forms']] == ['Referral'] and listed['renderer'] == 'faxbot-forms-1'
    detail = client.get(f"/forms/versions/{version['id']}", headers=ADMIN).json()
    assert [field['type_text'] for field in detail['field_list']][:3] == ['Text', 'Date', 'Checkbox']
    preview = client.get(f"/forms/versions/{version['id']}/pages/1?fields=true", headers=ADMIN)
    assert preview.headers['content-type'] == 'image/png' and preview.content.startswith(b'\x89PNG')
    pdf = client.post(f"/forms/versions/{version['id']}/render", headers=ADMIN, json={'values': VALUES})
    assert pdf.content.startswith(b'%PDF') and len(pdf.headers['X-Faxbot-Page-Hashes'].split(',')) == 1
    bad = client.post(f"/forms/versions/{version['id']}/render?format=json", headers=ADMIN,
                      json={'values': {'clinic': 'East'}})
    assert bad.status_code == 422
    assert bad.json()['detail'] == 'Patient name is required. Clinic must be one of: North, South.'
    template = client.get(f"/forms/versions/{version['id']}/template", headers=ADMIN)
    assert template.content == SVG
    assert client.get('/forms', headers={'X-API-Key': 'wrong'}).status_code in (401, 403)


def test_the_form_kind_is_a_strict_manifest_shape():
    from api.app.direct.crypto import FORM_TYPE, DirectProtocolError, Identity, kind_of, parse_manifest, seal
    from api.app.forms.exchange import parse_payload
    sender, recipient = Identity.generate(), Identity.generate()
    document = a_model.canonical({'faxbot_form_payload': 1, 'form': 'a' * 64, 'renderer': a_renderer.RENDERER,
                                  'resolution': 'fine', 'values': {'patient': 'A'}, 'pages': ['b' * 64]})
    common = dict(organization='Valley Hospital', fax_number=A_NUMBER, recipient_number=B_NUMBER,
                  recipient_signing_key=recipient.signing_key, recipient_exchange_key=recipient.exchange_key, pages=1)
    manifest, _, _ = seal(sender, message_id='c' * 32, document=document, form=True, **common)
    parsed = parse_manifest(manifest)
    assert kind_of(parsed) == 'form' and parsed['document']['media_type'] == FORM_TYPE
    assert parse_payload(document)['values'] == {'patient': 'A'}
    plain, _, _ = seal(sender, message_id='d' * 32, document=b'%PDF-1.4', **common)
    assert kind_of(parse_manifest(plain)) == 'original'
    # A form manifest must carry the form media type, and a PDF manifest must not claim the form kind.
    for broken in (manifest.replace(FORM_TYPE.encode(), b'application/pdf'),
                   plain.replace(b'"encryption"', b'"kind":"form","encryption"', 1)):
        with pytest.raises(DirectProtocolError):
            parse_manifest(broken)
    for bad in (document + b' ', document.replace(b'"fine"', b'"draft"'), b'{}'):
        with pytest.raises(a_model.FormError):
            parse_payload(bad)
