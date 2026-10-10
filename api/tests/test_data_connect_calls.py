"""NTT Data Connect in route choice and on the call (brief 99, item 2; asterisk patch 0010).

One predicate (``data_connect_store.places``) decides that a fax goes as a Data Connect call: the predictor's facts
that rank and price it, the Originate the built-in engine places, and the attempt's recorded estimate (the Data
Connect price only for a call that really went as one). A 488 refusal is called again as an ordinary fax call for
the same attempt, before any page. The AMI stream here is a fake; no call is placed and NTT is never contacted.
"""
import asyncio
from datetime import datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app import ami
from app.config_values import ConfigurationValues
from app.hylafax_engine import CallSettings
from app.routing import data_connect, data_connect_store as store
from app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)

NTT = {'SIP_TRUNK_PRESET': 'ntt-hikari', 'SIP_TRUNK_HOST': '192.0.2.60', 'SIP_TRUNK_USERNAME': '0312345678',
       'SIP_TRUNK_PASSWORD': 'synthetic-Trunk-Pass!42', 'FAX_DEFAULT_COUNTRY': 'JP', 'FAX_BACKEND': 'sip',
       'SIP_TRUNK_CALLER_ID': '+81312345678'}
NUMBER = '+81355550123'
NOW = datetime(2026, 10, 10, 3, 0)


def _values(**extra):
    return ConfigurationValues.from_environment({**NTT, **extra})


# The price -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('seconds', [0, 1, 29, 30, 31, 59, 60, 61, 179, 180, 181, 600, 1234])
def test_the_card_bills_exactly_ntts_charge_at_the_sessions_tier(seconds):
    from app.routing.costs import terms_cost, RateTerms
    _, micros = terms_cost(RateTerms(data_connect.card()), seconds=seconds, pages=1)
    assert Decimal(micros) / 1_000_000 == data_connect.charge(seconds, data_connect.SESSION_KBPS)
    # Never more than an ordinary call to a fixed line, for any length.
    assert data_connect.charge(seconds, data_connect.SESSION_KBPS) <= data_connect.voice_charge(seconds)


def test_the_session_fits_the_tier_it_is_priced_at():
    assert 50 < data_connect.session_bound_kbps() < 50.2 < data_connect.SESSION_KBPS
    assert data_connect.tier(data_connect.SESSION_KBPS).yen == Decimal('1.1')
    assert data_connect.price_sentence() == (
        "A Data Connect call, because this number's fax machine takes them: NTT charges ¥1.1 per started 30 seconds "
        'for a session up to 64 kbit/s, against ¥8.8 per started 3 minutes for an ordinary call.')


# The one predicate -------------------------------------------------------------------------------------------------

def test_a_capable_number_on_an_ntt_trunk_goes_as_data_connect_and_nothing_else_does(database):  # noqa: F811
    upgrade_schema(database)
    values = _values()
    assert store.ntt_trunks(values)[0][0] == 'sip'
    assert store.places(database, values, 'sip', NUMBER) is False  # nothing known about the number
    store.set_fact(database, NUMBER, True, now=NOW)
    assert store.places(database, values, 'sip', NUMBER) is True
    # Another carrier, a trunk without T.38, or Data Connect turned off: an ordinary call.
    assert store.places(database, _values(SIP_TRUNK_PRESET='telnyx'), 'sip', NUMBER) is False
    assert store.places(database, _values(SIP_T38_ENABLED='false'), 'sip', NUMBER) is False
    store.set_trunk(database, values, 'sip', False, now=NOW)
    assert store.places(database, values, 'sip', NUMBER) is False
    store.set_trunk(database, values, 'sip', True, now=NOW.replace(minute=1))
    assert store.places(database, values, 'sip', NUMBER) is True
    with pytest.raises(store.DataConnectError, match='only for a trunk on NTT Hikari Denwa'):
        store.set_trunk(database, _values(SIP_TRUNK_PRESET='telnyx'), 'sip', True)


def test_keys_after_answer_or_sending_together_need_another_kind_of_call(database):  # noqa: F811
    upgrade_schema(database)
    values = _values()
    store.set_fact(database, NUMBER, True, now=NOW)
    assert store.places(database, values, 'sip', NUMBER) is True
    from app.routing import after_answer
    with database.begin() as connection:
        after_answer.set_on(connection, NUMBER, '2', now=NOW)
    assert store.places(database, values, 'sip', NUMBER) is False
    with database.begin() as connection:
        after_answer.set_on(connection, NUMBER, None, now=NOW.replace(minute=1))
    assert store.places(database, values, 'sip', NUMBER) is True
    from app.batching.store import BatchingSettings
    BatchingSettings(database).save(NUMBER, enabled=True, actor='principal:person-anne', recipient_agreed=True)
    assert store.places(database, values, 'sip', NUMBER) is False


def test_the_predictor_prices_a_data_connect_call_by_its_bandwidth_and_the_estimate_by_what_happened(database):  # noqa: F811
    from app.routing.predict_facts import facts_for
    from app.routing.seed import load_cards
    from app.routing.store import RouteStore
    upgrade_schema(database)
    RouteStore(database).seed_cards(load_cards())  # the shipped NTT Hikari Denwa voice card, ¥8.8 per 3 minutes
    values = _values()
    voice = facts_for('sip', NUMBER, engine=database, values=values)
    assert voice.data_connect is False and voice.terms.card.billing_increment_seconds == 180
    store.set_fact(database, NUMBER, True, now=NOW)
    found = facts_for('sip', NUMBER, engine=database, values=values)
    assert found.data_connect is True and found.terms.card.currency == 'JPY'
    assert (found.terms.card.billing_increment_seconds, found.terms.card.per_minute_micros) == (30, 2_200_000)
    # An attempt is priced by what its call did: the voice card for a refused offer, whatever the number's fact.
    assert facts_for('sip', NUMBER, engine=database, values=values, data_connect=False).data_connect is False
    from app.routing.pricing import price
    quoted = price(RouteStore(database), values, 'sip', NUMBER, 2)
    assert quoted.currency == 'JPY' and quoted.sentence == data_connect.price_sentence()


# The call ---------------------------------------------------------------------------------------------------------

def test_ntt_trunks_take_data_connect_faxes_and_other_trunks_do_not():
    from app import sip_trunk
    accept = 'set_var=FAXBOT_DATA_CONNECT_ACCEPT=yes'
    assert accept in sip_trunk.render_pjsip(_values())
    assert accept not in sip_trunk.render_pjsip(_values(SIP_T38_ENABLED='false'))
    assert accept not in sip_trunk.render_pjsip(ConfigurationValues.from_environment({
        'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_CALLER_ID': '+13035550100'}))


def test_patch_0010_offers_the_priced_session_reads_the_answer_and_applies_after_0007():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    patches = root / 'asterisk' / 'patches'
    patch = (patches / '0010-faxbot-ntt-data-connect.patch').read_text()
    # The bandwidth it declares and the datagram cap that keeps it there are the numbers Faxbot prices by.
    assert f'+#define FAXBOT_DATA_CONNECT_KBPS {data_connect.SESSION_KBPS}' in patch
    assert f'+#define FAXBOT_DATA_CONNECT_DATAGRAM {data_connect.DATAGRAM_LIMIT}' in patch
    assert '--- a/channels/chan_pjsip.c' in patch and '--- a/res/res_pjsip_t38.c' in patch
    assert '+\tfaxbot_data_connect_media(session);\n \tres = ast_sip_session_create_invite(session, &tdata);' in patch
    assert '"FAXBOT_DATA_CONNECT"' in patch and 'UserEvent: FaxDataConnect' in patch and 'code == 488' in patch
    assert '+\tt38_change_state(session, session_media, state, T38_PEER_REINVITE);' in patch
    # The first offer's parameters are T.38 Annex D's, as data_connect.T38_SDP lists them.
    sdp = dict(data_connect.T38_SDP)
    assert sdp['T38FaxVersion'] == '0' and '+\t\tstate->our_parms.version = 0;' in patch
    assert sdp['T38MaxBitRate'] == '14400' and '+\t\tstate->our_parms.rate = AST_T38_RATE_14400;' in patch
    assert '+\t\tstate->our_parms.rate_management = AST_T38_RATE_MANAGEMENT_TRANSFERRED_TCF;' in patch
    names = sorted(path.name for path in patches.glob('*.patch'))
    assert names.index('0010-faxbot-ntt-data-connect.patch') > names.index('0007-faxbot-station-check-t0-cap.patch')
    dialplan = (root / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    assert ',DataConnect:${FILTER(abcdefghijklmnopqrstuvwxyz,${FAXBOT_DATA_CONNECT_RESULT})})' in dialplan

def _fields(**limits):
    return ami.prepare_originate_fields('a' * 32, '+81355550123', '/faxdata/' + 'a' * 32 + '.tiff',
                                        caller_id='+81312345678', attempt_id='b' * 32, dial='0355550123', **limits)


def test_the_originate_offers_data_connect_only_without_a_voice_call_first():
    fields = _fields(data_connect=True, t38_now=True)
    variables = fields['Variable'].split(',')
    assert ami.DATA_CONNECT_VARIABLE in variables and 'FAXBOT_T38_NOW=yes' not in variables
    assert ami.DATA_CONNECT_VARIABLE not in ami.without_data_connect(fields)['Variable'].split(',')
    assert ami.without_data_connect(fields)['ActionID'] == fields['ActionID']
    for refused in ({'dtmf': '2'}, {'audio': True}, {'peer': 'peer-ab12-endpoint'}):
        with pytest.raises(ValueError):
            _fields(data_connect=True, **refused)
    assert ami.DATA_CONNECT_VARIABLE not in _fields()['Variable'].split(',')


def test_originate_fields_follow_the_calls_data_connect_setting(monkeypatch):
    monkeypatch.setattr(ami, 'job_mailbox', lambda job_id: None)
    monkeypatch.setattr(ami, 'sender_identity', lambda job_id: None)
    monkeypatch.setattr(ami, 'fax_subaddress', lambda job_id: None)
    from app.routing import after_answer, stations
    monkeypatch.setattr(after_answer, 'for_number', lambda engine, number: None)
    monkeypatch.setattr(stations, 'call_guard', lambda *a, **k: stations.CallGuard())
    call = CallSettings(True, 14400, True, True, 'mmr', data_connect=True)
    fields = ami.originate_fields_for(_values(), 'a' * 32, NUMBER, '/faxdata/' + 'a' * 32 + '.tiff',
                                      attempt_id='b' * 32, call=call)
    assert ami.DATA_CONNECT_VARIABLE in fields['Variable'].split(',')
    monkeypatch.setattr(after_answer, 'for_number', lambda engine, number: '2')
    fields = ami.originate_fields_for(_values(), 'a' * 32, NUMBER, '/faxdata/' + 'a' * 32 + '.tiff',
                                      attempt_id='b' * 32, call=call)
    assert ami.DATA_CONNECT_VARIABLE not in fields['Variable'].split(',')


class FakeWriter:
    def __init__(self, client, answer='Success'):
        self.client, self.answer, self.sent = client, answer, []

    def write(self, raw):
        text = raw.decode()
        self.sent.append(text)
        action = next(line.split(': ', 1)[1] for line in text.split('\r\n') if line.startswith('ActionID: '))
        if self.answer is not None:
            asyncio.get_running_loop().call_soon(self.client._dispatch, {'Response': self.answer, 'ActionID': action})

    async def drain(self):
        return None


def _client(answer='Success'):
    client = ami.AMIClient()
    client._connected.set()
    client.writer = FakeWriter(client, answer)
    seen = {'OriginateResponse': [], 'FaxResult': [], 'FaxDataConnect': []}
    client.on_originate_response(seen['OriginateResponse'].append)
    client.on_fax_result(seen['FaxResult'].append)
    client.on_data_connect(seen['FaxDataConnect'].append)
    fields = _fields(data_connect=True)
    client._data_connect_offers[fields['ActionID']] = fields
    return client, fields, seen


def _refused(client):
    client._dispatch({'Event': 'UserEvent', 'UserEvent': 'FaxDataConnect', 'JobID': 'a' * 32, 'AttemptID': 'b' * 32,
                      'Outcome': 'refused', 'Code': '488', 'Warning64': 'MzA0IE5UVCAiSW5jb21wYXRpYmxlIG1lZGlhIg=='})


def _failure(client, fields):
    client._dispatch({'Event': 'OriginateResponse', 'Response': 'Failure', 'ActionID': fields['ActionID'],
                      'Reason': '8'})


async def _settle(client):
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_refusal_is_called_again_as_an_ordinary_call_and_its_failure_never_fails_the_attempt():
    client, fields, seen = _client()
    _refused(client)
    _failure(client, fields)  # the refused call's own failure: held
    await _settle(client)
    assert len(client.writer.sent) == 1 and ami.DATA_CONNECT_VARIABLE not in client.writer.sent[0]
    assert f"ActionID: {fields['ActionID']}" in client.writer.sent[0]
    assert seen['OriginateResponse'] == [] and len(seen['FaxDataConnect']) == 1
    # The ordinary call's own outcome goes through as always, and the state ends.
    client._dispatch({'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': 'a' * 32, 'AttemptID': 'b' * 32,
                      'Status': 'SUCCESS', 'Pages': '2'})
    assert len(seen['FaxResult']) == 1 and client._data_connect_retries == {}


@pytest.mark.asyncio
async def test_when_the_ordinary_call_is_accepted_first_the_refused_calls_failure_is_still_dropped():
    client, fields, seen = _client()
    _refused(client)
    await _settle(client)  # accepted before the refused call's failure arrives
    _failure(client, fields)
    assert seen['OriginateResponse'] == []
    _failure(client, fields)  # the ordinary call fails before answer: that failure goes through
    assert len(seen['OriginateResponse']) == 1 and client._data_connect_retries == {}


@pytest.mark.asyncio
async def test_when_the_ordinary_call_is_not_placed_the_refused_calls_failure_stands():
    client, fields, seen = _client(answer='Error')
    _refused(client)
    _failure(client, fields)
    await _settle(client)
    assert len(seen['OriginateResponse']) == 1 and seen['OriginateResponse'][0]['Response'] == 'Failure'
    assert client._data_connect_retries == {}


@pytest.mark.asyncio
async def test_an_unconfirmed_ordinary_call_never_reports_a_failure_before_any_page(monkeypatch):
    monkeypatch.setattr(ami, 'ORIGINATE_RESPONSE_TIMEOUT_SECONDS', 0.05)
    client, fields, seen = _client(answer=None)  # written, never acknowledged
    _refused(client)
    _failure(client, fields)
    await asyncio.wait_for(client._data_connect_retries[fields['ActionID']]['task'], 1)
    assert seen['OriginateResponse'] == []


@pytest.mark.asyncio
async def test_other_failures_and_unknown_offers_go_through_as_today():
    client, fields, seen = _client()
    _failure(client, fields)  # a 486 or 503: no refusal event, so nothing is called again
    assert len(seen['OriginateResponse']) == 1 and client.writer.sent == []
    other = ami.AMIClient()
    other._connected.set()
    other.writer = FakeWriter(other)
    found = []
    other.on_originate_response(found.append)
    _refused(other)  # an offer this process did not place (after a restart): its failure goes through
    _failure(other, fields)
    await _settle(other)
    assert len(found) == 1 and other.writer.sent == []


# What each call taught ------------------------------------------------------------------------------------------

def _attempts(database, tmp_path, count):  # noqa: F811
    """``count`` real attempts (accepted and claimed faxes), each decided for the NTT trunk and NUMBER."""
    from app.routing.store import RouteStore
    from api.tests.test_rules_delivery import BASE, accept, installation
    env = installation(database, tmp_path, BASE)
    found = []
    for index in range(count):
        job = accept(env, to=f'+1202555013{index}')  # one fax per number: a number takes one call at a time
        claim = env.delivery.claim('worker-test')
        RouteStore(database).record_decision(attempt_id=claim.attempt_id, job_id=job, destination=NUMBER,
                                             route='sip', reason='configured', provider_id='sip')
        found.append((job, claim.attempt_id))
    return found


def test_each_call_teaches_the_number_both_ways_and_sent_says_what_happened(database, tmp_path):  # noqa: F811
    (j1, a1), (j2, a2) = _attempts(database, tmp_path, 2)
    store.set_fact(database, NUMBER, True, now=NOW)
    assert store.record_call(database, j1, a1, 'refused', warning='304 NTT "Incompatible media"',
                             now=NOW.replace(minute=5)) is True
    assert store.record_call(database, j1, a1, 'refused') is False  # once per attempt
    learned = store.fact(database, NUMBER)
    assert (learned['capable'], learned['source'], learned['attempt_id']) == (0, 'learned', a1)
    assert 'Incompatible media' in learned['note']
    assert store.outcome(database, a1) == 'refused'
    assert store.sent_sentence(database, j1) == (
        "The recipient's machine doesn't take Data Connect; Faxbot called again as an ordinary fax call.")
    store.record_call(database, j2, a2, 'data_connect', now=NOW.replace(minute=9))
    assert store.fact(database, NUMBER)['capable'] == 1
    assert store.sent_sentence(database, j2) == (
        'Sent as a Data Connect call over NTT Hikari Denwa, priced by its bandwidth: ¥1.1 per started 30 seconds.')


def test_the_listeners_record_from_the_events(database, tmp_path):  # noqa: F811
    (j3, a3), (j4, a4) = _attempts(database, tmp_path, 2)
    client = ami.AMIClient()
    store.attach(client, database)
    client._emit('FaxDataConnect', {'Outcome': 'refused', 'JobID': j3, 'AttemptID': a3,
                                    'Warning64': 'MzA0IE5UVCAiSW5jb21wYXRpYmxlIG1lZGlhIg=='})
    client._emit('FaxResult', {'JobID': j4, 'AttemptID': a4, 'Status': 'SUCCESS',
                               'DataConnect': 'connected'})
    client._emit('FaxResult', {'JobID': j4, 'AttemptID': 'b4' * 16, 'Status': 'SUCCESS', 'DataConnect': ''})
    assert store.outcome(database, a3) == 'refused' and store.outcome(database, a4) == 'data_connect'
    assert store.outcome(database, 'b4' * 16) is None


# Through the server and the command line --------------------------------------------------------------------------

@pytest.fixture
def ntt_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    environment = {key: value for key, value in NTT.items() if key != 'FAX_BACKEND'}
    for client in _serve(monkeypatch, tmp_path, **environment):
        yield Cli(client)


def test_the_number_and_the_trunk_through_the_server_and_the_command_line(ntt_cli):
    from api.tests.test_cli import BOOTSTRAP
    cli, admin = ntt_cli, {'X-API-Key': BOOTSTRAP}
    path = '/routing/destinations/%2B81355550123/data-connect'
    first = cli.client.get(path, headers=admin).json()
    assert first['known'] is False and first['trunks'][0]['account'] == 'sip' and first['trunks'][0]['enabled']
    assert first['advice'] == list(data_connect.ADVICE) and first['not_run'] == store.NOT_RUN
    assert cli.client.get(path).status_code in (401, 403)
    saved = cli('recipients', 'set', NUMBER, '--data-connect', 'on')
    assert saved.exit_code == 0, saved.stdout
    assert "You recorded that this number's fax machine takes Data Connect calls." in ' '.join(saved.stdout.split())
    shown = ' '.join(cli('recipients', 'show', NUMBER).stdout.split())
    assert "NTT Data Connect You recorded that this number's fax machine takes Data Connect calls." in shown
    assert cli('recipients', 'set', NUMBER, '--data-connect', 'maybe').exit_code != 0
    off = cli('providers', 'trunk', 'data-connect', 'off')
    assert off.exit_code == 0 and 'only ordinary fax calls' in off.stdout
    assert cli.client.get(path, headers=admin).json()['trunks'][0]['enabled'] is False
    assert cli.json('providers', 'trunk', 'data-connect', 'on')['enabled'] is True
    refused = cli.client.put('/routing/data-connect/trunks/sinch', headers=admin, json={'enabled': True})
    assert refused.status_code == 400
    assert refused.json()['detail'] == 'Data Connect is only for a trunk on NTT Hikari Denwa.'
