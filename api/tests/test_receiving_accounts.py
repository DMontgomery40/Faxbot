"""Receiving with several provider accounts (provider-rules design §3.4, WP-B).

The first test pins the route gate exactly as it behaved before provider accounts existed. The gate is
kept unchanged until a second receiving account exists, so this table must keep passing after the change.
Provider APIs are a synthetic ``httpx.MockTransport``; nothing leaves the test.
"""
import base64
import hashlib
import hmac
import json
import re
from types import SimpleNamespace

import httpx
import pytest

from app.inbound import fetch as inbound_fetch
from app.inbound import http as inbound_http
from api.tests.test_inbound_acquisition import (ADMIN, client, environment, pdf_bytes, providers,  # noqa: F401
                                                rows, step)


@pytest.fixture(autouse=True)
def _no_inbound_read_limits(monkeypatch):
    """The per-minute limits on reading received faxes count across tests in one process; these tests read
    /inbound to check what was stored, so they leave the limits off rather than use up later tests' minute."""
    monkeypatch.setenv('INBOUND_LIST_RPM', '0')
    monkeypatch.setenv('INBOUND_GET_RPM', '0')


def _blocked_audits(monkeypatch):
    seen = []
    original = inbound_http.audit_event

    def record(event, **fields):
        if event == 'inbound_route_blocked':
            seen.append(fields['route'])
        return original(event, **fields)
    monkeypatch.setattr(inbound_http, 'audit_event', record)
    return seen


def _statuses(http):
    """The status each provider's receiving address answers with a notification that names no fax."""
    return {
        '/sinch-inbound': http.post('/sinch-inbound', json={'event': 'INCOMING_FAX'}).status_code,
        '/phaxio-inbound': http.post('/phaxio-inbound', data={'direction': 'received'}).status_code,
        '/_internal/asterisk/inbound': http.post('/_internal/asterisk/inbound', json={'tiff_path': '/nowhere.tiff'},
                                                 headers={'X-Internal-Secret': 'wrong'}).status_code,
    }


# (environment, FAX_INBOUND_BACKEND in the process environment or None, routes answering 404)
GATE = [
    # Receiving provider saved in configuration, no FAX_INBOUND_BACKEND variable: every route stays open and
    # each checks its own secret (the design's "appears to leave every provider's route open", pinned).
    ({'FAX_BACKEND': 'sip'}, None, set()),
    ({'FAX_BACKEND': 'phaxio'}, None, set()),
    # The variable names the receiving provider: only that provider's route is open.
    ({'FAX_BACKEND': 'phaxio'}, 'phaxio', {'/sinch-inbound', '/_internal/asterisk/inbound'}),
    ({'FAX_BACKEND': 'sip'}, 'sinch', {'/phaxio-inbound', '/_internal/asterisk/inbound'}),
    # No receiving provider set up yet: no provider's route is open.
    ({'FAX_BACKEND': ''}, None, {'/sinch-inbound', '/phaxio-inbound', '/_internal/asterisk/inbound'}),
]


@pytest.mark.parametrize('extra, variable, closed', GATE)
def test_the_route_gate_is_unchanged_while_no_extra_account_receives(isolated_installation, monkeypatch, providers,
                                                                     extra, variable, closed):
    environment(monkeypatch, **extra)
    if variable is None:
        monkeypatch.delenv('FAX_INBOUND_BACKEND', raising=False)
    else:
        monkeypatch.setenv('FAX_INBOUND_BACKEND', variable)
    blocked = _blocked_audits(monkeypatch)
    with client() as http:
        statuses = _statuses(http)
    assert {route for route, status in statuses.items() if status == 404} == closed
    # Each closed route wrote one audit row; an open one went on to check its own secret.
    assert sorted(blocked) == sorted(closed)
    assert all(status in (200, 400, 401) for route, status in statuses.items() if route not in closed)


def test_receiving_turned_off_closes_every_route_without_an_audit_row(isolated_installation, monkeypatch, providers):
    environment(monkeypatch, INBOUND_ENABLED='false')
    blocked = _blocked_audits(monkeypatch)
    with client() as http:
        assert set(_statuses(http).values()) == {404}
    assert blocked == []


# -- several accounts receiving at once --------------------------------------------------------------------------

MAIN, UK = ('synthetic-project', 'main-user', 'main-pass'), ('uk-project', 'uk-user', 'uk-pass')


class Sinch:
    """Sinch's received-fax API for any project; records which project and key each request used."""

    def __init__(self):
        self.faxes, self.files, self.requests = {}, {}, []

    def add(self, project, fax_id, document):
        self.faxes[(project, fax_id)] = {'id': fax_id, 'direction': 'INBOUND', 'from': '+15555550100',
                                         'to': '+442071234567', 'numberOfPages': 1, 'status': 'COMPLETED',
                                         'completedTime': '2026-10-07T14:00:00Z', 'projectId': project}
        self.files[(project, fax_id)] = document

    def handler(self, request):
        match = re.fullmatch(r'/v3/projects/([A-Za-z0-9_-]+)/faxes/([A-Za-z0-9_-]+)(/file)?', request.url.path)
        if request.url.host != 'fax.api.sinch.com' or match is None:
            return httpx.Response(404)
        key = (match.group(1), match.group(2))
        credentials = request.headers.get('authorization', 'Basic ').split(' ', 1)[1]
        user = base64.b64decode(credentials).decode() if credentials else ''
        self.requests.append((match.group(1), user.partition(':')[0], bool(match.group(3))))
        if key not in self.faxes:
            return httpx.Response(404, json={'message': 'not found'})
        if match.group(3):
            return httpx.Response(200, content=self.files[key], headers={'content-type': 'application/pdf'})
        return httpx.Response(200, json=self.faxes[key])


@pytest.fixture
def sinch(monkeypatch, providers):  # noqa: F811
    fake = Sinch()
    monkeypatch.setattr(inbound_fetch, '_TRANSPORT', httpx.MockTransport(fake.handler))
    return fake


def basic(user, password):
    return {'Authorization': 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()}


def add_account(http, **body):
    state = http.get('/admin/providers/accounts', headers=ADMIN).json()
    result = http.post('/admin/providers/accounts', headers=ADMIN, json={
        'label': None, 'site': None, 'sends': True, 'receives': True, 'numbers': [], 'limits': None,
        'settings': {}, 'credentials': {}, **body, 'expected_generation': state['generation']})
    assert result.status_code == 200, result.text
    return result.json()


def patch_account(http, key, **body):
    state = http.get('/admin/providers/accounts', headers=ADMIN).json()
    result = http.patch(f'/admin/providers/accounts/{key}', headers=ADMIN,
                        json={**body, 'expected_generation': state['generation']})
    assert result.status_code == 200, result.text
    return result.json()


def sinch_environment(monkeypatch):
    environment(monkeypatch, FAX_BACKEND='sinch', SINCH_INBOUND_BASIC_USER=MAIN[1], SINCH_INBOUND_BASIC_PASS=MAIN[2])


def add_uk(http, **changes):
    return add_account(http, key='sinch-uk', provider='sinch', label='Sinch (UK)', numbers=['+442071234567'],
                       settings={'project_id': UK[0], 'inbound_basic_user': UK[1]},
                       credentials={'api_key': 'uk-key', 'api_secret': 'uk-secret', 'inbound_basic_pass': UK[2]},
                       **changes)


def notification(fax_id):
    return {'event': 'INCOMING_FAX', 'fax': {'id': fax_id, 'direction': 'INBOUND', 'from': '+15555550100',
                                             'to': '+442071234567', 'numberOfPages': 1}}


def test_two_sinch_accounts_receive_at_their_own_addresses_with_their_own_auth_and_keys(
        isolated_installation, monkeypatch, sinch):
    sinch_environment(monkeypatch)
    sinch.add(MAIN[0], '01SAMEID', pdf_bytes('main account copy'))
    sinch.add(UK[0], '01SAMEID', pdf_bytes('uk account copy'))
    with client() as http:
        add_uk(http)
        # Each address checks its own account's user name and password.
        assert http.post('/sinch-inbound/sinch-uk', json=notification('01SAMEID'),
                         headers=basic(MAIN[1], MAIN[2])).status_code == 401
        assert http.post('/sinch-inbound', json=notification('01SAMEID'),
                         headers=basic(UK[1], UK[2])).status_code == 401
        assert http.post('/sinch-inbound', json=notification('01SAMEID'),
                         headers=basic(MAIN[1], MAIN[2])).json() == {'status': 'ok'}
        assert http.post('/sinch-inbound/sinch-uk', json=notification('01SAMEID'),
                         headers=basic(UK[1], UK[2])).json() == {'status': 'ok'}
        # The same Sinch fax ID under two accounts is two received faxes, each fetched with its own keys.
        assert step() is True and step() is True
        faxes = http.get('/inbound', headers=ADMIN).json()
        assert sorted((fax['account_key'], fax['status']) for fax in faxes) == [
            ('sinch', 'received'), ('sinch-uk', 'received')]
        assert {fax['account_label'] for fax in faxes} == {'Sinch', 'Sinch (UK)'}
    imports = rows(isolated_installation, 'inbound_imports')
    assert sorted((row['account'], row['account_key']) for row in imports) == [
        ('sinch:synthetic-project', 'sinch'), ('sinch:uk-project', 'sinch-uk')]
    assert sorted((project, user) for project, user, document in sinch.requests if document) == [
        (MAIN[0], 'synthetic-sinch-key'), (UK[0], 'uk-key')]
    # Each received fax is bound to the account that received it, so a later fetch can use it.
    bindings = rows(isolated_installation, 'inbound_fax_bindings')
    assert len(bindings) == 2 and len({row['profile_id'] for row in bindings}) == 2
    assert {row['inbound_backend'] for row in rows(isolated_installation, 'inbound_faxes')} == {'sinch'}


def test_an_address_whose_account_is_off_or_unknown_answers_404_with_an_audit_row(isolated_installation,
                                                                                  monkeypatch, sinch):
    sinch_environment(monkeypatch)
    blocked = []
    original = inbound_http.audit_event

    def record(event, **fields):
        if event == 'inbound_route_blocked':
            blocked.append(fields)
        return original(event, **fields)
    monkeypatch.setattr(inbound_http, 'audit_event', record)
    with client() as http:
        add_uk(http)
        patch_account(http, 'sinch-uk', enabled=False)
        assert http.post('/sinch-inbound/sinch-uk', json=notification('01OFF'),
                         headers=basic(UK[1], UK[2])).status_code == 404
        assert http.post('/sinch-inbound/nobody', json=notification('01OFF')).status_code == 404
        # A Sinch account cannot be reached through another provider's address.
        assert http.post('/phaxio-inbound/sinch-uk', data={'direction': 'received'}).status_code == 404
    assert [(item['route'], item.get('account')) for item in blocked] == [
        ('/sinch-inbound/sinch-uk', 'sinch-uk'), ('/sinch-inbound/nobody', 'nobody'),
        ('/phaxio-inbound/sinch-uk', 'sinch-uk')]
    assert rows(isolated_installation, 'inbound_faxes') == []


def test_once_an_extra_account_receives_each_original_address_serves_one_account(isolated_installation,
                                                                                 monkeypatch, sinch):
    """The pinned old gate leaves every provider's address open; with an extra receiving account it closes."""
    sinch_environment(monkeypatch)
    monkeypatch.delenv('FAX_INBOUND_BACKEND', raising=False)
    with client() as http:
        assert http.post('/phaxio-inbound', data={'direction': 'received'}).status_code != 404
        add_uk(http)
        # Phaxio has no receiving account now, so its address is closed; Sinch's original address serves the
        # first Sinch account, which is the receiving provider.
        assert http.post('/phaxio-inbound', data={'direction': 'received'}).status_code == 404
        sinch.add(MAIN[0], '01MAIN', pdf_bytes('main'))
        assert http.post('/sinch-inbound', json=notification('01MAIN'),
                         headers=basic(MAIN[1], MAIN[2])).json() == {'status': 'ok'}
    assert [row['account_key'] for row in rows(isolated_installation, 'inbound_imports')] == ['sinch']


def test_a_phaxio_accounts_signature_covers_its_own_address(isolated_installation, monkeypatch, providers):  # noqa: F811
    environment(monkeypatch)
    token = 'second-phaxio-token'
    fields = [('fax', json.dumps({'id': 9100, 'direction': 'received', 'num_pages': 1, 'status': 'success',
                                  'completed_at': '2026-10-07T08:00:00.000-06:00',
                                  'from_number': '+15555550100', 'to_number': '+15555550123'})),
              ('direction', 'received')]
    document = pdf_bytes('second phaxio')

    def signed(url, key=token):
        message = url + ''.join(name + value for name, value in sorted(fields, key=lambda part: part[0]))
        message += 'file' + hashlib.sha1(document).hexdigest()
        return {'X-Phaxio-Signature': hmac.new(key.encode(), message.encode(), hashlib.sha1).hexdigest()}

    def post(headers):
        return http.post('/phaxio-inbound/phaxio-2', data=dict(fields), headers=headers,
                         files={'file': ('fax.pdf', document, 'application/pdf')})
    with client() as http:
        add_account(http, key='phaxio-2', provider='phaxio', label='Phaxio (second)',
                    settings={'inbound_verify_signature': True},
                    credentials={'api_key': 'p2-key', 'api_secret': 'p2-secret', 'callback_token': token})
        # Signed for the original address, or with the first account's token: refused.
        assert post(signed('https://testserver/phaxio-inbound')).status_code == 401
        assert post(signed('https://testserver/phaxio-inbound/phaxio-2',
                           'synthetic-phaxio-callback-token')).status_code == 401
        assert post(signed('https://testserver/phaxio-inbound/phaxio-2')).json() == {'status': 'ok'}
    [record] = rows(isolated_installation, 'inbound_imports')
    assert record['account_key'] == 'phaxio-2' and record['state'] == 'received'
    assert record['account'] == 'phaxio:' + hashlib.sha256(b'p2-key').hexdigest()[:12]


def test_fetching_uses_the_account_as_it_was_bound_when_its_settings_changed(isolated_installation, monkeypatch,
                                                                             sinch):
    sinch_environment(monkeypatch)
    sinch.add(UK[0], '01BOUND', pdf_bytes('bound copy'))
    with client() as http:
        add_uk(http)
        assert http.post('/sinch-inbound/sinch-uk', json=notification('01BOUND'),
                         headers=basic(UK[1], UK[2])).json() == {'status': 'ok'}
        # The account now points at another project; the fax arrived on the old one and is fetched there.
        patch_account(http, 'sinch-uk', settings={'project_id': 'uk-project-2', 'inbound_basic_user': UK[1]})
        assert step() is True
        assert http.get('/inbound', headers=ADMIN).json()[0]['status'] == 'received'
    assert [(project, document) for project, user, document in sinch.requests if document] == [(UK[0], True)]


# -- own numbers, pollers ------------------------------------------------------------------------------------------

def values_with(documents, **environment):
    from app.config_profiles import ConfigurationDocument
    from app.config_values import ConfigurationValues
    base = {'INBOUND_ENABLED': 'true', 'FAX_BACKEND': 'sip', 'SIP_TRUNK_HOST': 'sip.example.net',
            'SIP_TRUNK_USERNAME': 'trunk', 'SIP_TRUNK_PASSWORD': 'synthetic', 'SIP_TRUNK_DIDS': '+17208565062',
            **environment}
    return ConfigurationValues.from_environment(base).with_provider_accounts(ConfigurationDocument(documents))


def test_own_numbers_are_the_union_of_the_receiving_accounts():
    from app.routing.own_numbers import account_numbers, receiving_numbers
    documents = {
        'sip-leeds': {'provider': 'sip', 'receives': True, 'numbers': ['+441132000000'],
                      'settings': {'host': 'leeds.example.net'}},
        'sinch-uk': {'provider': 'sinch', 'receives': True, 'numbers': ['+442071234567'],
                     'settings': {'project_id': 'p'}, 'credentials': {'api_key': 'k', 'api_secret': 's'}},
        'sinch-off': {'provider': 'sinch', 'receives': True, 'enabled': False, 'numbers': ['+442071234568'],
                      'settings': {'project_id': 'p'}, 'credentials': {'api_key': 'k', 'api_secret': 's'}},
        'humble-2': {'provider': 'humblefax', 'receives': True, 'numbers': ['+13034265098'],
                     'credentials': {'access_key': 'a', 'secret_key': 'b'}},
    }
    assert receiving_numbers(values_with({})) == {'+17208565062'}
    # A HumbleFax number still places a real call, as before; an account that is off receives nothing.
    assert receiving_numbers(values_with(documents)) == {'+17208565062', '+441132000000', '+442071234567'}
    assert '+13034265098' in account_numbers(values_with(documents))
    assert receiving_numbers(values_with(documents, INBOUND_ENABLED='false')) == set()


def test_an_extra_polling_account_gets_its_own_poller_and_loses_it_when_removed():
    from app.inbound.http import reconcile_receivers
    service = SimpleNamespace(store=None, runtime=None, kick=None, receivers={}, extra={})
    started = []

    def start(receiver):
        started.append(receiver.account_key)
        return SimpleNamespace(cancel=lambda: None)
    documents = {'humble-2': {'provider': 'humblefax', 'receives': True,
                              'credentials': {'access_key': 'a', 'secret_key': 'b'}},
                 'efax-uk': {'provider': 'efax', 'receives': True,
                             'credentials': {'app_id': 'x', 'api_key': 'y', 'user_id': 'z'}},
                 'sinch-uk': {'provider': 'sinch', 'receives': True, 'settings': {'project_id': 'p'}}}
    assert reconcile_receivers(service, values_with(documents), start=start) == (['efax-uk', 'humble-2'], [])
    assert started == ['efax-uk', 'humble-2']
    assert set(service.receivers) == {('efax', 'efax-uk'), ('humblefax', 'humble-2')}
    # Nothing changes while the accounts stay; a removed account's poller stops.
    assert reconcile_receivers(service, values_with(documents), start=start) == ([], [])
    del documents['efax-uk']
    assert reconcile_receivers(service, values_with(documents), start=start) == ([], ['efax-uk'])


def test_humblefax_and_efax_accounts_come_from_the_provider_accounts():
    from app import accounts
    from app.inbound import efax, humblefax
    documents = {'humble-2': {'provider': 'humblefax', 'receives': True, 'settings': {'poll_seconds': 120},
                              'credentials': {'access_key': 'hf-a', 'secret_key': 'hf-b'}},
                 'efax-uk': {'provider': 'efax', 'receives': True,
                             'credentials': {'app_id': 'app-uk', 'api_key': 'key-uk', 'user_id': 'user-uk'}}}
    values = values_with(documents, HUMBLEFAX_ACCESS_KEY='first-a', HUMBLEFAX_SECRET_KEY='first-b',
                         EFAX_APP_ID='app-1', EFAX_API_KEY='key-1', EFAX_USER_ID='user-1')
    listed = {account.key: account for account in humblefax.accounts(values)}
    assert (listed['humble-2'].access_key, listed['humble-2'].receives, listed['humble-2'].poll_seconds) == (
        'hf-a', True, 120)
    assert listed['humblefax'].access_key == 'first-a'
    own = efax.own_values(values, 'efax-uk')
    assert (own.efax_app_id, own.efax_user_id) == ('app-uk', 'user-uk')
    assert efax.receiving_active(own, accounts.account_named(values, 'efax-uk'))
    # The first eFax account receives only while eFax is the receiving provider, as before.
    assert not efax.receiving_active(values, accounts.account_named(values, 'efax'))
    assert efax.account_identities(values) == {efax.account_for(values), efax.account_for(own)}
