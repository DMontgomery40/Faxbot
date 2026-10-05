"""Faxbot checks Telnyx's fax over IP (T.38) setting on the trunk numbers and offers one fix.

Every Telnyx answer comes from a fake Telnyx on an httpx MockTransport that keeps
its own numbers and connections, so the tests see exactly what Faxbot reads and
changes. No real Telnyx account is ever reached.
"""
import json

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main, sip_http, sip_network, stun, telnyx_t38
from app.sip_network import Discovery

BOOTSTRAP = 'synthetic-telnyx-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
KEY = 'KEYsynthetic0telnyx'
FIRST, SECOND = '+15555550100', '+15555550101'
TRUNK = {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser', 'SIP_TRUNK_PASSWORD': 'synthetic-Pass!42',
         'SIP_TRUNK_CALLER_ID': FIRST, 'SIP_TRUNK_DIDS': f'{FIRST},{SECOND}', 'TELNYX_API_KEY': KEY}
OTHER_FEATURES = {'rtp_auto_adjust_enabled': True, 'accept_any_rtp_packets_enabled': False}


class FakeTelnyx:
    """Numbers, voice settings and connections as Telnyx API v2 returns them; records every request."""

    def __init__(self):
        self.numbers = {FIRST: {'id': '1293384261075731499', 'connection_id': '1494404757140276705',
                                'media_features': {**OTHER_FEATURES, 't38_fax_gateway_enabled': False}},
                        SECOND: {'id': '1293384261075731500', 'connection_id': '1494404757140276705',
                                 'media_features': {**OTHER_FEATURES, 't38_fax_gateway_enabled': True}}}
        self.connections = {'1494404757140276705': {'user_name': 'faxbotuser',
                                                    'outbound': {'t38_reinvite_source': 'telnyx'}}}
        self.requests = []
        self.refuse_reads = self.refuse_changes = self.ignore_changes = self.down = False

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == 'api.telnyx.com' and request.headers['Authorization'] == f'Bearer {KEY}'
        if self.down:
            raise httpx.ConnectError('offline')
        path = request.url.path.removeprefix('/v2')
        if request.method == 'PATCH':
            if self.refuse_changes:
                return httpx.Response(403, json={'errors': [{'title': 'Forbidden'}]})
            number = next(item for item in self.numbers.values() if path == f'/phone_numbers/{item["id"]}/voice')
            body = json.loads(request.content)
            if not self.ignore_changes:
                number['media_features'] = body['media_features']
            return httpx.Response(200, json={'data': {'id': number['id'], 'media_features': number['media_features']}})
        if self.refuse_reads:
            return httpx.Response(401, json={'errors': [{'title': 'Unauthorized'}]})
        if path == '/phone_numbers':
            wanted = request.url.params['filter[phone_number]']
            found = [{'id': item['id'], 'phone_number': number, 'connection_id': item['connection_id']}
                     for number, item in self.numbers.items() if number == wanted]
            return httpx.Response(200, json={'data': found, 'meta': {'total_results': len(found)}})
        for number, item in self.numbers.items():
            if path == f'/phone_numbers/{item["id"]}/voice':
                return httpx.Response(200, json={'data': {'id': item['id'], 'phone_number': number,
                                                          'connection_id': item['connection_id'],
                                                          'media_features': item['media_features']}})
        for identifier, connection in self.connections.items():
            if path == f'/credential_connections/{identifier}':
                return httpx.Response(200, json={'data': {'id': identifier, **connection}})
        return httpx.Response(404, json={'errors': [{'title': 'Not found'}]})


@pytest.fixture
def telnyx(monkeypatch):
    fake = FakeTelnyx()
    monkeypatch.setattr(telnyx_t38, 'TRANSPORT', httpx.MockTransport(fake))
    # Apply also checks the network; nothing here reaches it.
    probe = stun.Probe(public_ip='198.51.100.7', local_ip='172.18.0.5', local_port=4000,
                       mapped=(('a', 4000), ('b', 4000)))
    monkeypatch.setattr(stun, 'probe', lambda servers, **_: probe)
    monkeypatch.setattr(sip_http, '_probes', {})

    async def discover(*, fresh=False):
        return Discovery()
    monkeypatch.setattr(sip_network, 'discover', discover)
    return fake


def _client(monkeypatch, extra):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'MAX_REQUESTS_PER_MINUTE': '0', **extra}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    return TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'})


@pytest.fixture
def client(isolated_installation, monkeypatch, telnyx):
    with _client(monkeypatch, TRUNK) as client:
        yield client


def _values(client):
    return client.app.state.configuration_runtime.manager.store.read().active.values


def _check(client):
    return telnyx_t38.check(_values(client))


# -- the check -------------------------------------------------------------------------------------------------

def test_the_check_reads_each_number_and_its_connection_with_gets_only(client, telnyx):
    _check(client)
    assert {request.method for request in telnyx.requests} == {'GET'}
    report = client.get('/admin/sip/telnyx', headers=ADMIN).json()
    assert report['applies'] is True and report['ready'] is False and report['checked_at'].endswith('Z')
    first, second = report['numbers']
    assert (first['number'], first['display'], first['state'], first['fixable']) == (FIRST, '+1 555-555-0100', 'off', True)
    assert first['text'] == ('Telnyx has fax over IP (T.38) turned off for +1 555-555-0100, so received faxes there '
                             'arrive as audio.')
    assert (second['state'], second['fixable']) == ('on', False)
    assert second['text'] == 'Telnyx has fax over IP (T.38) turned on for +1 555-555-0101.'
    assert report['text'] == first['text'] and report['connection_texts'] == []
    # Read-only means only these reads, and the account's internal ids never reach the page.
    assert sorted({request.url.path for request in telnyx.requests}) == [
        '/v2/credential_connections/1494404757140276705', '/v2/phone_numbers',
        '/v2/phone_numbers/1293384261075731499/voice', '/v2/phone_numbers/1293384261075731500/voice']
    assert '1293384261075731499' not in json.dumps(report)


def test_all_numbers_on_and_the_connection_ready(client, telnyx):
    telnyx.numbers[FIRST]['media_features']['t38_fax_gateway_enabled'] = True
    _check(client)
    report = client.get('/admin/sip/telnyx', headers=ADMIN).json()
    assert report['ready'] is True
    assert report['text'] == 'Telnyx has fax over IP (T.38) turned on for all your trunk numbers.'


@pytest.mark.parametrize('source, matches, sentence', [
    ('customer', True, 'Telnyx\'s SIP connection has T.38 Re-invite Initiated By set to "customer", so Telnyx does not '
                       'switch calls to fax over IP (T.38) itself. In the Telnyx portal, edit the SIP connection, open '
                       'Outbound and set T.38 Re-invite Initiated By to Telnyx.'),
    ('telnyx', False, 'This number is on a different Telnyx SIP connection than the one Faxbot signs in with, so calls '
                      'to it may not reach Faxbot.'),
])
def test_the_connections_reinvite_source_and_user_name_are_reported_with_the_portal_step(client, telnyx, source,
                                                                                         matches, sentence):
    telnyx.connections['1494404757140276705'] = {'user_name': 'faxbotuser' if matches else 'someoneelse',
                                                 'outbound': {'t38_reinvite_source': source}}
    _check(client)
    assert client.get('/admin/sip/telnyx', headers=ADMIN).json()['connection_texts'] == [sentence]


def test_a_key_that_cannot_read_numbers_gets_the_portal_steps(client, telnyx):
    telnyx.refuse_reads = True
    _check(client)
    report = client.get('/admin/sip/telnyx', headers=ADMIN).json()
    assert [entry['state'] for entry in report['numbers']] == ['unreadable', 'unreadable']
    assert report['text'] == ('Faxbot\'s Telnyx API key cannot read +1 555-555-0100, so Faxbot cannot check its fax over '
                              'IP (T.38). In the Telnyx portal, open Numbers → My Numbers, select the gear next to '
                              '+1 555-555-0100, open Expert Configuration and tick Enable T.38 Fax Gateway.')
    assert not any(entry['fixable'] for entry in report['numbers'])


def test_an_unreachable_telnyx_and_a_number_not_on_the_account_are_one_sentence_each(client, telnyx):
    del telnyx.numbers[SECOND]
    _check(client)
    numbers = client.get('/admin/sip/telnyx', headers=ADMIN).json()['numbers']
    assert numbers[1]['text'] == ('+1 555-555-0101 is not a number on this Telnyx account, so Faxbot cannot check its fax '
                                  'over IP (T.38).')
    telnyx.down = True
    _check(client)
    numbers = client.get('/admin/sip/telnyx', headers=ADMIN).json()['numbers']
    assert numbers[0]['text'] == ('Faxbot could not reach Telnyx to check fax over IP (T.38) for +1 555-555-0100; select '
                                  'Check again to retry.')


def test_without_a_key_or_on_another_carrier_nothing_is_read(isolated_installation, monkeypatch, telnyx):
    with _client(monkeypatch, {**TRUNK, 'TELNYX_API_KEY': ''}) as client:
        assert telnyx_t38.check(_values(client)) is None
        assert client.get('/admin/sip/telnyx', headers=ADMIN).json() == {
            'applies': False, 'numbers': [], 'connection_texts': [], 'text': None}
        assert client.get('/admin/sip/status', headers=ADMIN).json()['telnyx_t38'] is None
    assert telnyx.requests == []


def test_a_test_run_never_reaches_telnyx_without_a_fake(client, monkeypatch, telnyx):
    monkeypatch.setattr(telnyx_t38, 'TRANSPORT', None)
    assert telnyx_t38.check(_values(client)) is None
    assert telnyx_t38.enable(_values(client), FIRST)[0] == 'unavailable'
    assert telnyx.requests == []


# -- when the check runs ------------------------------------------------------------------------------------------

def test_apply_and_check_again_run_the_check_and_status_shows_it(client, telnyx):
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert telnyx.requests and {request.method for request in telnyx.requests} == {'GET'}
    status = client.get('/admin/sip/status', headers=ADMIN).json()
    assert status['telnyx_t38']['numbers'][0]['state'] == 'off'
    telnyx.requests.clear()
    telnyx.numbers[FIRST]['media_features']['t38_fax_gateway_enabled'] = True
    assert client.post('/admin/sip/network/check', headers=ADMIN).status_code == 200
    assert telnyx.requests
    assert client.get('/admin/sip/telnyx', headers=ADMIN).json()['ready'] is True


# -- the fix ----------------------------------------------------------------------------------------------------------

def _audit_rows(client):
    store = client.app.state.configuration_runtime.manager.store
    with store.engine.connect() as connection:
        rows = connection.execute(sa.text(
            "SELECT details, outcome FROM access_audit WHERE operation = 'providers.write' ORDER BY created_at"))
        return [json.loads(row.details) | {'outcome': row.outcome} for row in rows]


def test_turning_it_on_changes_only_that_setting_for_that_number_reads_it_back_and_is_audited(client, telnyx):
    _check(client)
    telnyx.requests.clear()
    response = client.post('/admin/sip/telnyx/numbers/%2B15555550100/t38', headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body['outcome'], body['message']) == ('on', 'Telnyx now has fax over IP (T.38) turned on for +1 555-555-0100.')
    assert body['numbers'][0]['state'] == 'on' and body['ready'] is True
    patches = [request for request in telnyx.requests if request.method == 'PATCH']
    assert len(patches) == 1 and patches[0].url.path == '/v2/phone_numbers/1293384261075731499/voice'
    # The full existing media_features with exactly one field changed, then a fresh read.
    assert json.loads(patches[0].content) == {'media_features': {**OTHER_FEATURES, 't38_fax_gateway_enabled': True}}
    assert telnyx.requests[-1].method == 'GET' and telnyx.requests[-1].url.path.endswith('/voice')
    assert telnyx.numbers[SECOND]['media_features'] == {**OTHER_FEATURES, 't38_fax_gateway_enabled': True}
    rows = _audit_rows(client)
    assert rows == [{'request': 'POST /admin/sip/telnyx/numbers/+15555550100/t38', 'outcome': 'allowed'}]


@pytest.mark.parametrize('mode, outcome, message', [
    ('refuse_changes', 'refused', 'Telnyx did not let Faxbot change +1 555-555-0100, because the API key may not change '
                                  'numbers. In the Telnyx portal, open Numbers → My Numbers, select the gear next to '
                                  '+1 555-555-0100, open Expert Configuration and tick Enable T.38 Fax Gateway.'),
    ('ignore_changes', 'not_on', 'Telnyx accepted the change but still shows fax over IP (T.38) off for +1 555-555-0100. '
                                 'In the Telnyx portal, open Numbers → My Numbers, select the gear next to +1 555-555-0100, '
                                 'open Expert Configuration and tick Enable T.38 Fax Gateway.'),
    ('down', 'unavailable', 'Faxbot could not reach Telnyx, so nothing changed for +1 555-555-0100; try again.'),
])
def test_a_refused_or_unconfirmed_change_says_so_with_the_portal_steps(client, telnyx, mode, outcome, message):
    _check(client)
    setattr(telnyx, mode, True)
    body = client.post('/admin/sip/telnyx/numbers/%2B15555550100/t38', headers=ADMIN).json()
    assert (body['outcome'], body['message']) == (outcome, message)
    assert body['numbers'][0]['state'] == 'off'


def test_the_fix_needs_providers_write_and_a_trunk_number(client, telnyx):
    sender = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': ['fax:send']})
    key = {'X-API-Key': sender.json()['token']}
    assert client.post('/admin/sip/telnyx/numbers/%2B15555550100/t38', headers=key).status_code == 403
    assert client.get('/admin/sip/telnyx', headers=key).status_code == 403
    assert not any(request.method == 'PATCH' for request in telnyx.requests)
    assert _audit_rows(client)[-1]['outcome'] == 'denied'
    other = client.post('/admin/sip/telnyx/numbers/%2B15555550199/t38', headers=ADMIN)
    assert other.status_code == 404 and other.json()['detail'] == "That number is not one of this trunk's fax numbers."
    assert not any(request.method == 'PATCH' for request in telnyx.requests)


def test_diagnostics_shows_a_carrier_trunk_finding_with_the_fix(client, telnyx, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app import diagnostics_report as report
    _check(client)
    status = client.get('/admin/sip/status', headers=ADMIN).json()
    monkeypatch.setattr(report, '_uses_trunk', lambda request: True)

    async def fake_status(request, identity):
        return status
    monkeypatch.setattr(sip_http, 'status', fake_status)
    findings = {item.id: item for item in asyncio.run(report.carrier_trunk(SimpleNamespace(request=None, identity=None)))}
    finding = findings['engine.telnyx_t38']
    assert (finding.status, finding.title, finding.fix_label, finding.fix_page) == (
        report.ATTENTION, 'Fax over IP (T.38) at Telnyx', 'Turn on T.38 at Telnyx', 'providers/trunk')
    assert finding.sentence == ('Telnyx has fax over IP (T.38) turned off for +1 555-555-0100, so received faxes there '
                                'arrive as audio.')
