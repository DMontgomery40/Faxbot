"""Peer fax calls (M1b): the partner's endpoint in Asterisk, the check made in Asterisk's own network before each
call, and the call itself going inside the tunnel instead of to the carrier. Synthetic partners and addresses only
(10.20.0.0/24 and fd00::/8 stand for tunnel addresses; 93.184.215.14 for a public one).

The loopback proof with two Asterisks and a real WireGuard tunnel is in test_t38_loopback.py.
"""
import asyncio

import pytest

from app import ami, sip_trunk
from app.config_values import ConfigurationValues
from app.direct import peer_call
from api.tests.test_peer_fax import ADMIN, peer_pair  # noqa: F401,F811 - fixture

PEER = 'a' * 32
OTHER = 'b' * 32
NUMBER = '+13035550160'


def partner(**changes):
    return {'id': PEER, 'organization': 'Valley Hospital', 'phone_number': NUMBER, 'state': 'verified',
            'expires_at': None, 'partner_receives_fax_images': None, 'partner_peer_calls': 1,
            'receive_peer_calls': None, 'peer_call_address': '10.20.0.2', **changes}


def trunk_values(**extra):
    return ConfigurationValues.from_environment({'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                                                 'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1',
                                                 'SIP_TRUNK_CALLER_ID': '+13035550100', **extra})


# -- the endpoint ---------------------------------------------------------------------------------------------

def test_a_partner_that_takes_our_calls_gets_an_endpoint_on_its_own_transport_and_ours_an_identify():
    text = peer_call.render_peers([partner(), partner(id=OTHER, receive_peer_calls=1, partner_peer_calls=None,
                                                      peer_call_address='[fd00::2]:5071')])
    assert text.count('[transport-peer]') == 1 and 'bind=0.0.0.0:5070' in text
    assert f'[peer-{PEER}-endpoint]' in text and 'contact=sip:10.20.0.2:5070' in text
    assert f'set_var=FAXBOT_PEER={PEER}' in text and 'set_var=FAXBOT_IAF=peer' in text
    assert 'transport=transport-peer' in text and 't38_udptl=yes' in text and 'context=faxbot-inbound' in text
    # Only a partner whose calls you take is identified by its tunnel address.
    assert f'[peer-{PEER}-identify]' not in text
    assert f'[peer-{OTHER}-identify]' in text and 'match=fd00::2' in text and 'contact=sip:[fd00::2]:5071' in text
    # Nothing here is a carrier's: no credentials, no registration, no public address.
    assert 'auth' not in text and 'registration' not in text and 'external_' not in text


@pytest.mark.parametrize('changes', [
    {'state': 'pending'}, {'state': 'revoked'}, {'peer_call_address': '93.184.215.14'},
    {'peer_call_address': 'partner.example'}, {'peer_call_address': None},
    {'partner_peer_calls': None, 'receive_peer_calls': None}, {'id': 'not-an-id'},
])
def test_no_endpoint_without_a_verified_partner_a_private_address_and_calls_one_way(changes):
    assert peer_call.render_peers([partner(**changes)]) == ''


def test_the_file_asterisk_reads_has_the_trunks_then_the_partners(tmp_path):
    values = trunk_values(FAX_DATA_DIR=str(tmp_path))
    path = sip_trunk.write_asterisk_configuration(values, peers=[partner()])
    text = path.read_text()
    assert text.index('[trunk-endpoint]') < text.index(f'[peer-{PEER}-endpoint]')
    assert sip_trunk.write_asterisk_configuration(values, peers=[]).read_text() == sip_trunk.render_pjsip(values)


@pytest.mark.parametrize('text, stored', [('10.20.0.2', '10.20.0.2:5070'), ('10.20.0.2:5080', '10.20.0.2:5080'),
                                          ('fd00::2', '[fd00::2]:5070')])
def test_a_tunnel_address_is_stored_with_its_port(text, stored):
    assert peer_call.tunnel_address(text) == stored


@pytest.mark.parametrize('text, words', [('93.184.215.14', 'public internet'), ('partner.example', 'such as 10.20.0.2')])
def test_a_public_or_unreadable_address_is_refused_with_a_sentence(text, words):
    from app.direct.store import DirectConflict
    with pytest.raises(DirectConflict, match=words):
        peer_call.tunnel_address(text)


# -- the call -------------------------------------------------------------------------------------------------

def test_a_peer_call_goes_to_the_partners_endpoint_with_iaf_and_never_through_a_trunk_name():
    fields = ami.prepare_originate_fields('job1', NUMBER, '/faxdata/a.tiff', caller_id='+13035550100',
                                          peer=f'peer-{PEER}-endpoint', iaf='peer')
    assert fields['Channel'] == f'PJSIP/{NUMBER}@peer-{PEER}-endpoint' and 'FAXBOT_IAF=peer' in fields['Variable']
    for bad in ('trunk-endpoint', f'peer-{PEER}-endpoint;x', 'peer-zz-endpoint'):
        with pytest.raises(ValueError):
            ami.prepare_originate_fields('job1', NUMBER, '/faxdata/a.tiff', caller_id='+13035550100', peer=bad)
    # The trunk path never takes a partner's endpoint.
    with pytest.raises(ValueError):
        ami.prepare_originate_fields('job1', NUMBER, '/faxdata/a.tiff', caller_id='+13035550100',
                                     endpoint=f'peer-{PEER}-endpoint')


def test_with_a_peer_call_the_trunk_fax_dials_the_partner_inside_the_tunnel(monkeypatch):
    monkeypatch.setattr(ami, '_database', lambda: None)
    values = trunk_values(SIP_FAX_PREFERENCE_HEADER='true')
    call = peer_call.PeerCall(PEER, f'peer-{PEER}-endpoint', None)
    fields = ami.originate_fields_for(values, 'job1', NUMBER, '/faxdata/a.tiff', choice=ami.reply_choice(values),
                                      peer=call)
    assert fields['Channel'] == f'PJSIP/{NUMBER}@peer-{PEER}-endpoint'
    assert 'FAXBOT_IAF=peer' in fields['Variable'] and ami.FAX_PREFERENCE_VARIABLE not in fields['Variable']
    plain = ami.originate_fields_for(values, 'job1', NUMBER, '/faxdata/a.tiff', choice=ami.reply_choice(values))
    assert plain['Channel'].endswith('@trunk-endpoint')


class FakeEngine:
    """Asterisk's answers to the peer call check."""

    def __init__(self, route=('wg0', 'wireguard'), status='Reachable', fail=None):
        self.route, self.status, self.fail, self.asked = route, status, fail, []

    async def peer_route(self, address):
        self.asked.append(address)
        if self.fail:
            raise self.fail
        return self.route

    async def status_query(self, fields, *, collect=False):
        return {'response': 'Success'}, [{'URI': 'sip:10.20.0.2:5070', 'Status': self.status}]


@pytest.mark.parametrize('engine, loaded, reason', [
    (FakeEngine(), True, 'tunnel'),
    (FakeEngine(route=('eth0', 'none')), True, 'no_tunnel'),
    (FakeEngine(route=None), True, 'no_tunnel'),
    (FakeEngine(status='Unreachable'), True, 'unreachable'),
    (FakeEngine(), False, 'not_loaded'),
    (FakeEngine(fail=TimeoutError('slow')), True, 'engine'),
    (FakeEngine(fail=ConnectionError('gone')), True, 'engine'),
])
def test_the_check_is_made_in_asterisks_network_and_needs_the_partner_answering(monkeypatch, engine, loaded, reason):
    monkeypatch.setattr(peer_call, 'loaded', lambda values, peer_id: loaded)
    decision = asyncio.run(peer_call.check(engine, trunk_values(), partner()))
    assert decision.reason == reason, decision
    assert decision.applies is (reason == 'tunnel')
    assert engine.asked == ['10.20.0.2']
    assert decision.sentence.endswith('.') and 'Valley Hospital' in decision.sentence or reason == 'engine'


def test_a_public_address_is_never_even_checked(monkeypatch):
    engine = FakeEngine()
    decision = asyncio.run(peer_call.check(engine, trunk_values(), partner(peer_call_address='93.184.215.14')))
    assert decision.reason == 'public_address' and engine.asked == []


def test_the_partner_is_found_by_its_number_and_anyone_else_goes_by_the_carrier(monkeypatch):
    monkeypatch.setattr(peer_call, 'installation_peers', lambda engine: [partner()])
    monkeypatch.setattr(peer_call, 'loaded', lambda values, peer_id: True)
    found = asyncio.run(peer_call.call_for(FakeEngine(), trunk_values(), object(), NUMBER))
    assert found.peer_id == PEER and found.endpoint == f'peer-{PEER}-endpoint' and found.decision.applies
    assert asyncio.run(peer_call.call_for(FakeEngine(), trunk_values(), object(), '+13035550199')) is None
    assert asyncio.run(peer_call.call_for(FakeEngine(route=('eth0', 'none')), trunk_values(), object(), NUMBER)) is None
    assert asyncio.run(peer_call.call_for(FakeEngine(), trunk_values(), None, NUMBER)) is None


# -- the route check through the manager connection ---------------------------------------------------------------

def test_the_route_check_reads_asterisks_answer_and_places_no_call(monkeypatch):
    client = ami.AMIClient()
    sent = []

    async def answer(fields):
        sent.append(fields)
        token = fields['Variable'].split(',')[0].split('=')[1]
        client._dispatch({'Event': 'UserEvent', 'UserEvent': 'FaxPeerRoute', 'Check': token, 'Route': route})
    monkeypatch.setattr(client, '_send_action', answer)
    route = 'wg0/wireguard'
    assert asyncio.run(client.peer_route('10.20.0.2')) == ('wg0', 'wireguard')
    assert sent[0]['Channel'] == 'Local/s@faxbot-peer-route' and sent[0]['Application'] == 'Wait'
    assert 'FAXBOT_PEER_ADDRESS=10.20.0.2' in sent[0]['Variable']
    route = 'none'
    assert asyncio.run(client.peer_route('10.20.0.2')) is None
    with pytest.raises(ValueError):
        asyncio.run(client.peer_route('10.20.0.2,FAXBOT_IAF=peer'))
    assert client._route_waiters == {}


def test_no_answer_from_asterisk_in_time_means_no_tunnel(monkeypatch):
    client = ami.AMIClient()

    async def silent(fields):
        return None
    monkeypatch.setattr(client, '_send_action', silent)
    monkeypatch.setattr(ami, 'PEER_ROUTE_TIMEOUT_SECONDS', 0.05)
    assert asyncio.run(client.peer_route('10.20.0.2')) is None and client._route_waiters == {}


def test_a_peer_call_is_recorded_on_the_call_with_its_partner(tmp_path):
    import sqlalchemy as sa
    from app import sip_calls
    from app.schema import create_database_engine, upgrade_schema
    engine = create_database_engine('sqlite:///' + str(tmp_path / 'calls.db'))
    upgrade_schema(engine)
    records = sip_calls.SipCallRecords(engine)
    records.record_submission({'JobID': 'job1', 'AttemptID': 'attempt1', 'Called': NUMBER, 'CallerID': '+13035550100',
                               'Peer': PEER})
    records.record_inbound({'did': '+13035550100', 'caller': NUMBER, 'peer': OTHER, 'started_at': 1791391994},
                           call_id='1791391994.7')
    table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        found = dict(connection.execute(sa.select(table.c.direction, table.c.peer_id)).all())
    assert found == {'outbound': PEER, 'inbound': OTHER}
    engine.dispose()


# -- the partner's settings over HTTP ---------------------------------------------------------------------------

def test_taking_a_partners_tunnel_calls_tells_the_partner_and_refuses_a_public_address(peer_pair, monkeypatch):
    client, a_on_b = peer_pair['b_client'], peer_pair['a_on_b']
    path = f"/direct/peers/{a_on_b['id']}/peer-calls"
    from api.tests.test_routing_http import scoped_key
    assert client.post(path, headers=scoped_key(client, ['fax:send']), json={'accept': True}).status_code == 403
    refused = client.post(path, headers=ADMIN, json={'accept': True, 'address': '93.184.215.14'})
    assert refused.status_code == 409 and 'public internet' in refused.json()['detail']
    saved = client.post(path, headers=ADMIN, json={'accept': True, 'address': '10.20.0.2'})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body['receive_peer_calls'] is True and body['peer_call_address'] == '10.20.0.2:5070'
    assert body['peer_calls_text'] == 'You take their fax calls inside the tunnel from 10.20.0.2:5070.'
    assert body['partner_told'] is True
    # The partner's Faxbot heard it, signed: it may now place calls to this installation inside the tunnel.
    assert peer_pair['a'].store.get_peer(peer_pair['b_on_a']['id'])['partner_peer_calls'] == 1
    # Asked now, from this side: this side has not verified the partner yet (the fixture verifies the other way),
    # so nothing goes inside the tunnel, and the check says why in a sentence.
    monkeypatch.setattr(ami.ami_client, 'peer_route', FakeEngine().peer_route)
    checked = client.post(path + '/check', headers=ADMIN)
    assert checked.status_code == 200 and checked.json()['applies'] is False
    assert checked.json()['reason'] == 'not_verified' and checked.json()['sentence'].endswith('is not a verified partner.')
    assert 'WireGuard' in checked.json()['note']
    off = client.post(path, headers=ADMIN, json={'accept': False})
    assert off.json()['receive_peer_calls'] is False and off.json()['peer_call_address'] is None
