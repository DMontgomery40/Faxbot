"""Caller-name lookup on the trunk's Telnyx numbers: read with the T.38 check, priced, and one audited switch.

Every Telnyx answer comes from a fake Telnyx (the T.38 tests' fake, extended
with ``caller_id_name_enabled``). No real Telnyx account is ever reached.
"""
import json

import httpx
import pytest
import sqlalchemy as sa

from app import telnyx_numbers, telnyx_t38
from api.tests.test_telnyx_t38 import ADMIN, FIRST, SECOND, TRUNK, FakeTelnyx, _client, _values


class FakeNames(FakeTelnyx):
    """The T.38 fake, with each number's caller-name lookup in its voice settings."""

    def __init__(self):
        super().__init__()
        self.lookup = {FIRST: True, SECOND: False}

    def __call__(self, request):
        path = request.url.path.removeprefix('/v2')
        number = next((key for key, item in self.numbers.items() if path == f'/phone_numbers/{item["id"]}/voice'), None)
        if request.method == 'PATCH' and number is not None:
            self.requests.append(request)
            if self.down:
                raise httpx.ConnectError('offline')
            if self.refuse_changes:
                return httpx.Response(403, json={'errors': [{'title': 'Forbidden'}]})
            body = json.loads(request.content)
            if not self.ignore_changes:
                self.lookup[number] = body['caller_id_name_enabled']
            return httpx.Response(200, json={'data': {'id': self.numbers[number]['id']}})
        response = super().__call__(request)
        if number is not None and response.status_code == 200:
            data = response.json()['data']
            return httpx.Response(200, json={'data': {**data, 'caller_id_name_enabled': self.lookup[number]}})
        return response


@pytest.fixture
def telnyx(monkeypatch):
    fake = FakeNames()
    monkeypatch.setattr(telnyx_t38, 'TRANSPORT', httpx.MockTransport(fake))
    return fake


@pytest.fixture
def client(isolated_installation, monkeypatch, telnyx):
    with _client(monkeypatch, TRUNK) as client:
        yield client


def _audit_rows(client):
    store = client.app.state.configuration_runtime.manager.store
    with store.engine.connect() as connection:
        rows = connection.execute(sa.text(
            "SELECT details, outcome FROM access_audit WHERE operation = 'telnyx.caller_name_lookup' "
            'ORDER BY created_at'))
        return [json.loads(row.details) | {'outcome': row.outcome} for row in rows]


def _row(result, outcome='allowed'):
    return {'request': 'POST /admin/sip/telnyx/numbers/{number}/name-lookup-off', 'number': FIRST,
            'shown': '+1 555-555-0100', 'result': result, 'outcome': outcome}


def test_the_t38_check_reads_name_lookup_with_no_extra_telnyx_call(client, telnyx):
    assert client.get('/admin/sip/telnyx/names', headers=ADMIN).json()['text'] == (
        'Faxbot has not checked caller-name lookup on your Telnyx numbers yet.')
    telnyx_t38.check(_values(client))
    assert {request.method for request in telnyx.requests} == {'GET'}
    assert len([request for request in telnyx.requests if request.url.path.endswith('/voice')]) == 2
    report = client.get('/admin/sip/telnyx/names', headers=ADMIN).json()
    first, second = report['numbers']
    assert (first['lookup'], first['can_turn_off'], second['lookup'], second['can_turn_off']) == (True, True, False, False)
    assert first['text'] == ('Telnyx looks up callers\' names on +1 555-555-0100, at $0.40 a month for each number. '
                             'Faxbot never shows callers\' names, so turning it off changes nothing in Faxbot.')
    assert second['text'] == 'Caller-name lookup is off for +1 555-555-0101.'
    assert report['text'] == first['text'] and report['monthly_total'] == {'currency': 'USD', 'amount': '0.40'}
    assert report['price'] == {'text': '$0.40 a month for each number', 'monthly': {'currency': 'USD', 'amount': '0.40'},
                               'source_url': 'https://support.telnyx.com/en/articles/4366901-your-number-lookup-guide',
                               'read_on': '2026-10-07'}
    # The T.38 answer is unchanged by the extra field.
    assert client.get('/admin/sip/telnyx', headers=ADMIN).json()['numbers'][0]['state'] == 'off'


def test_turning_it_off_changes_only_that_field_reads_it_back_and_is_audited(client, telnyx):
    telnyx_t38.check(_values(client))
    telnyx.requests.clear()
    response = client.post('/admin/sip/telnyx/numbers/%2B15555550100/name-lookup-off', headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body['outcome'], body['message']) == ('off', 'Telnyx now has caller-name lookup off for +1 555-555-0100.')
    assert body['numbers'][0]['lookup'] is False and body['monthly_total'] is None
    assert body['text'] == 'Caller-name lookup is off for all your trunk numbers, so Telnyx charges nothing for it.'
    patches = [request for request in telnyx.requests if request.method == 'PATCH']
    assert len(patches) == 1 and json.loads(patches[0].content) == {'caller_id_name_enabled': False}
    assert telnyx.requests[-1].method == 'GET' and telnyx.requests[-1].url.path.endswith('/voice')
    # Nothing else on the number changed: its T.38 setting is as it was.
    assert telnyx.numbers[FIRST]['media_features']['t38_fax_gateway_enabled'] is False
    assert _audit_rows(client) == [_row('turned_off')]


@pytest.mark.parametrize('mode, outcome, result, start', [
    ('refuse_changes', 'refused', 'refused', 'Telnyx did not let Faxbot change +1 555-555-0100, because the API key '
                                             'may not change numbers. In the Telnyx portal, open Numbers → My Numbers, '
                                             'select the business card icon next to +1 555-555-0100, turn off CNAM '
                                             'Caller ID Lookup and save.'),
    ('ignore_changes', 'still_on', 'still_on', 'Telnyx accepted the change but still shows caller-name lookup on for '
                                               '+1 555-555-0100.'),
    ('down', 'unavailable', 'unreachable', 'Faxbot could not reach Telnyx, so nothing changed for +1 555-555-0100; '
                                           'try again.'),
])
def test_a_refusing_or_unconfirming_telnyx_is_said_plainly_and_audited(client, telnyx, mode, outcome, result, start):
    telnyx_t38.check(_values(client))
    setattr(telnyx, mode, True)
    body = client.post('/admin/sip/telnyx/numbers/%2B15555550100/name-lookup-off', headers=ADMIN).json()
    assert body['outcome'] == outcome and body['message'].startswith(start)
    assert body['numbers'][0]['lookup'] is True
    assert _audit_rows(client) == [_row(result)]


def test_turning_it_off_needs_providers_write_and_a_trunk_number(client, telnyx):
    telnyx_t38.check(_values(client))
    telnyx.requests.clear()
    sender = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': ['fax:send']})
    key = {'X-API-Key': sender.json()['token']}
    assert client.post('/admin/sip/telnyx/numbers/%2B15555550100/name-lookup-off', headers=key).status_code == 403
    assert client.get('/admin/sip/telnyx/names', headers=key).status_code == 403
    assert telnyx.requests == []
    assert _audit_rows(client) == [_row('forbidden', 'denied')]
    other = client.post('/admin/sip/telnyx/numbers/%2B15555550199/name-lookup-off', headers=ADMIN)
    assert other.status_code == 404 and telnyx.requests == [] and len(_audit_rows(client)) == 1


def test_a_test_run_never_reaches_telnyx_and_another_carrier_has_nothing_to_say(client, telnyx, monkeypatch):
    monkeypatch.setattr(telnyx_t38, 'TRANSPORT', None)
    assert telnyx_numbers.turn_off(_values(client), FIRST)[0] == 'unavailable'
    assert telnyx.requests == []
    values = _values(client).model_copy(update={'telnyx_api_key': ''})
    assert telnyx_numbers.report(values)['applies'] is False


def test_the_audit_log_names_the_change(client, telnyx):
    telnyx_t38.check(_values(client))
    client.post('/admin/sip/telnyx/numbers/%2B15555550100/name-lookup-off', headers=ADMIN)
    entries = [item for item in client.get('/access/audit', headers=ADMIN).json()['items']
               if item['operation'] == 'telnyx.caller_name_lookup']
    assert len(entries) == 1 and entries[0]['details']['result'] == 'turned_off'
