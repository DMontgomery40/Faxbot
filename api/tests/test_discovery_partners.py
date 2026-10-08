"""Introductions between partners and verified directory lookups (M16, D13), on SQLite and PostgreSQL.

Installation B is the running application (TestClient). Installations A and C
are direct delivery services on their own databases, enrolled with B and
verified both ways by the challenge codes. B's requests to A and C are
answered in-process; a directory's DNS is a stand-in resolver. Every number,
domain, card and record is synthetic.
"""
import asyncio
from datetime import datetime, timedelta
import json
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from api.app.config_values import ConfigurationValues
from api.app.direct import discovery, dnstxt
from api.app.direct.crypto import Identity, signed, timestamp
from api.app.direct.discovery import DiscoveryService, Fetched, LookupRefused
from api.app.direct.identity import load_identity
from api.app.direct.service import DirectService, DirectUnavailable
from api.app.direct.store import DirectConflict, DirectStore
from api.app.schema import create_database_engine, upgrade_schema
from api.tests.test_direct_delivery import A_NUMBER, B_NUMBER, ToB
from api.tests.test_peer_fax import _namespaces
from api.tests.test_routing_http import ADMIN, BOOTSTRAP


C_NUMBER = '+15550100003'
PUBLIC = '93.184.215.14'


def values_for(data, organization, number, host, **changes):
    environment = {'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false', 'FAX_DATA_DIR': str(data),
                   'PUBLIC_API_URL': f'https://{host}', 'DIRECT_DELIVERY_ENABLED': 'true',
                   'DIRECT_ORGANIZATION': organization, 'DIRECT_FAX_NUMBER': number}
    environment.update(changes)
    return ConfigurationValues.from_environment(environment)


class Installation:
    """A standalone installation: its direct delivery, its discovery and a stand-in fetcher."""

    def __init__(self, engine, tmp_path, name, organization, number, client):
        data = tmp_path / f'{name}-data'
        data.mkdir()
        self.host = f'{name}.example'
        self.values = values_for(data, organization, number, self.host)
        self.data = data
        self.engine = engine
        self.direct = DirectService(engine, values=lambda: self.values,
                                    environment={'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / f'{name}.key')},
                                    http=ToB(client), resolver=lambda host, port: [PUBLIC])
        self.direct.identity(create=True)
        self.fetcher = Answers()
        self.discovery = DiscoveryService(self.direct, fetcher=self.fetcher, txt=Directory())


class Answers:
    """Well-known answers by host; every GET is recorded."""

    def __init__(self):
        self.routes, self.calls = {}, []

    def serve(self, host, document):
        self.routes[host] = document

    async def get(self, host, *, allow_private):
        self.calls.append(host)
        if host not in self.routes:
            raise LookupRefused('unreachable')
        return Fetched(200, json.dumps(self.routes[host]).encode(), None)


class Directory:
    """A stand-in DNS: TXT values by name; every question is recorded."""

    def __init__(self):
        self.records, self.asked = {}, []

    def __call__(self, name):
        self.asked.append(name)
        if name == 'down':
            raise dnstxt.DnsError('The directory could not be reached.')
        return list(self.records.get(name, []))


class Partners:
    """B's transport to A and C, answered in-process; a host in ``down`` cannot be reached."""

    def __init__(self):
        self.installations, self.down, self.introductions = {}, set(), []

    async def request(self, method, url, **kwargs):
        parts = urlsplit(url)
        if parts.hostname in self.down:
            raise httpx.ConnectTimeout('partner offline')
        installation = self.installations[parts.hostname]
        body = kwargs.get('json')
        if parts.path == '/direct/verifications':
            return await asyncio.to_thread(installation.direct.confirm, body['statement'], body['signature'])
        if parts.path == '/direct/capabilities':
            return await asyncio.to_thread(installation.direct.note, body['statement'], body['signature'])
        assert parts.path == '/direct/introductions', url
        self.introductions.append((parts.hostname, body))
        try:
            return await asyncio.to_thread(installation.discovery.receive_introduction, body)
        except DirectUnavailable:
            return 404, {'detail': 'Not found.'}


@pytest.fixture(params=['sqlite', 'postgresql'])
def trio(request, isolated_installation, monkeypatch, tmp_path):
    scoped = _namespaces(request, 3)
    b_url = scoped[2].render_as_string(hide_password=False) if scoped else None
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0', 'DIRECT_DELIVERY_ENABLED': 'true',
                        'DIRECT_ORGANIZATION': 'County Clinic', 'DIRECT_FAX_NUMBER': B_NUMBER,
                        'DIRECT_ALLOW_PRIVATE_PEERS': 'true', 'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / 'b-direct.key'),
                        'FAXBOT_CONSOLE_ORIGINS': 'https://testserver', **({'DATABASE_URL': b_url} if b_url else {})}.items():
        monkeypatch.setenv(name, value)
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        engines = [create_database_engine(scoped[index].render_as_string(hide_password=False)) if scoped
                   else create_database_engine('sqlite:///' + str(tmp_path / f'{name}.db'))
                   for index, name in enumerate(('a', 'c'))]
        for engine in engines:
            upgrade_schema(engine)
        a = Installation(engines[0], tmp_path, 'a', 'Valley Hospital', A_NUMBER, client)
        c = Installation(engines[1], tmp_path, 'c', 'Mountain Clinic', C_NUMBER, client)
        partners = Partners()
        partners.installations = {'a.example': a, 'c.example': c}
        main.app.state.direct_http = partners
        b_card = client.get('/direct/card', headers=ADMIN).json()['card']
        b_engine = main.app.state.configuration_runtime.manager.store.engine
        ids = {}
        for name, installation in (('a', a), ('c', c)):
            ids[name] = befriend(client, b_engine, b_card, installation)
        try:
            yield {'client': client, 'a': a, 'c': c, 'partners': partners, 'ids': ids, 'b_engine': b_engine,
                   'b_identity': load_identity(tmp_path / 'b-direct.key'), 'tmp': tmp_path}
        finally:
            main.app.state.direct_http = None
            for engine in engines:
                engine.dispose()


def befriend(client, b_engine, b_card, installation):
    """Enroll an installation with B and verify both ways with challenge codes; returns (its id at B, B's id at it)."""
    enrolled = client.post('/direct/peers', headers=ADMIN, json={'card': installation.direct.own_card()})
    assert enrolled.status_code == 201, enrolled.text
    on_b = enrolled.json()['id']
    b_on_it = installation.direct.enroll(b_card)['id']
    # B faxes it a code; it proves the code to B.
    DirectStore(b_engine).start_challenge(on_b, code='11112222', job_id=None)
    asyncio.run(installation.direct.send_confirmation(b_on_it, '1111 2222'))
    # It faxes B a code; B proves it.
    installation.direct.store.start_challenge(b_on_it, code='33334444', job_id=None)
    confirmed = client.post(f'/direct/peers/{on_b}/confirm', headers=ADMIN, json={'code': '3333 4444'})
    assert confirmed.json()['confirmed'] is True, confirmed.text
    return on_b, b_on_it


def rows(engine, table):
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(sa.Table(table, sa.MetaData(),
                                                                            autoload_with=engine))).mappings()]


def allow(client, peer_id, allowed=True):
    return client.post(f'/direct/discovery/partners/{peer_id}/may-introduce', headers=ADMIN, json={'allowed': allowed})


def introduce(client, ids):
    return client.post('/direct/discovery/introductions', headers=ADMIN,
                       json={'first': ids['a'][0], 'second': ids['c'][0]})


# -- introductions --------------------------------------------------------------------------------------

def test_an_introduction_needs_both_partners_to_agree_and_each_learns_only_the_hint(trio):
    client, ids, partners = trio['client'], trio['ids'], trio['partners']
    view = client.get('/direct/discovery', headers=ADMIN).json()
    assert {(item['organization'], item['verified'], item['may_introduce']) for item in view['partners']} == {
        ('Valley Hospital', True, False), ('Mountain Clinic', True, False)}
    refused = introduce(client, ids)
    assert refused.status_code == 409
    assert refused.json()['detail'] == ('Valley Hospital has not agreed to be introduced. Turn on "May be introduced" '
                                        'for Valley Hospital once they agree.')
    agreed = allow(client, ids['a'][0])
    assert agreed.json() == {'may_introduce': True,
                             'detail': 'Valley Hospital may be introduced to your other partners.'}
    assert introduce(client, ids).json()['detail'] == ('Mountain Clinic has not agreed to be introduced. Turn on "May '
                                                       'be introduced" for Mountain Clinic once they agree.')
    assert partners.introductions == []
    allow(client, ids['c'][0])
    done = introduce(client, ids)
    assert done.status_code == 200, done.text
    assert done.json()['detail'] == ('Valley Hospital and Mountain Clinic were introduced. Each can now enroll the '
                                     "other and confirm the other's number with a code by fax.")
    # Each learned the other's organization, number, address and key, signed by B; nothing else, and no partner
    # was made: each still has to enroll the other and run the challenge.
    for installation, other, number in ((trio['a'], 'Mountain Clinic', C_NUMBER), (trio['c'], 'Valley Hospital',
                                                                                  A_NUMBER)):
        suggestions = rows(installation.engine, 'direct_discovery_suggestions')
        assert [(row['source'], row['organization'], row['number'], row['card']) for row in suggestions] == [
            ('introduction', other, number, None)]
        statement = json.loads(json.loads(suggestions[0]['introduction'])['statement'])
        assert set(statement['introduced']) == {'organization', 'fax_number', 'endpoint', 'signing_key'}
        assert {peer['organization'] for peer in rows(installation.engine, 'direct_peers')} == {'County Clinic'}
        view = installation.discovery.store.open_suggestions()
        assert discovery.suggestion_source_text(view[0], 'County Clinic') == 'Your partner County Clinic introduced them.'
    assert len(rows(trio['b_engine'], 'direct_introductions')) == 1
    turned_off = allow(client, ids['a'][0], allowed=False)
    assert turned_off.json()['detail'] == 'Valley Hospital is not introduced to anyone.'


def test_a_consent_counts_only_while_verified_and_after_the_current_verification(trio):
    client, ids = trio['client'], trio['ids']
    allow(client, ids['a'][0])
    allow(client, ids['c'][0])
    # Mountain Clinic is removed and enrolled again: its earlier "yes" no longer counts.
    assert client.post(f"/direct/peers/{ids['c'][0]}/revoke", headers=ADMIN).status_code == 200
    gone = introduce(client, ids)
    assert gone.status_code == 409 and gone.json()['detail'] == 'This partner is not enrolled.'
    b_card = client.get('/direct/card', headers=ADMIN).json()['card']
    again = client.post('/direct/peers', headers=ADMIN, json={'card': trio['c'].direct.own_card()})
    assert again.status_code == 201, again.text
    pending = allow(client, ids['c'][0])
    assert pending.status_code == 409
    assert pending.json()['detail'] == ('Mountain Clinic is not verified yet. Only a verified partner can be '
                                        'introduced.')
    DirectStore(trio['b_engine']).start_challenge(ids['c'][0], code='55556666', job_id=None)
    asyncio.run(trio['c'].direct.send_confirmation(ids['c'][1], '5555 6666'))
    assert b_card['fax_number'] == B_NUMBER
    refused = introduce(client, ids)
    assert refused.status_code == 409 and 'Mountain Clinic has not agreed' in refused.json()['detail']
    assert trio['partners'].introductions == []


def test_a_partner_that_cannot_be_reached_is_named_and_the_other_is_still_told(trio):
    client, ids = trio['client'], trio['ids']
    allow(client, ids['a'][0])
    allow(client, ids['c'][0])
    trio['partners'].down.add('c.example')
    done = introduce(client, ids)
    assert done.json()['detail'] == ('Valley Hospital was told about Mountain Clinic. Faxbot could not reach Mountain '
                                     'Clinic; introduce them again later.')
    assert discovery.introduction_text('A', 'unsupported', 'C', 'refused') == (
        "A's Faxbot does not take introductions yet. C did not accept the introduction.")


@pytest.mark.asyncio
async def test_enrolling_an_introduced_partner_reads_its_card_and_refuses_another_key(trio):
    client, ids, a, c = trio['client'], trio['ids'], trio['a'], trio['c']
    allow(client, ids['a'][0])
    allow(client, ids['c'][0])
    await asyncio.to_thread(introduce, client, ids)
    suggestion = a.discovery.store.open_suggestions()[0]
    # Mountain Clinic's address answers with a different key: refused, nothing enrolled.
    a.fetcher.serve('c.example', {'faxbot_direct': 1, 'card': discovery_card(Identity.generate(), C_NUMBER)})
    with pytest.raises(DirectConflict) as refused:
        await a.discovery.enroll(suggestion['id'])
    assert str(refused.value) == ("Faxbot could not read Mountain Clinic's card from their Faxbot. Exchange cards with "
                                  'them instead.')
    assert [row['outcome'] for row in rows(a.engine, 'direct_discovery_lookups')] == ['key_mismatch']
    # Its own card: enrolled, still to be verified by the challenge fax.
    a.fetcher.serve('c.example', c.discovery.well_known())
    peer, sentence = await a.discovery.enroll(suggestion['id'])
    assert peer['state'] == 'pending' and peer['phone_number'] == C_NUMBER
    assert sentence == 'Mountain Clinic added. Send them a code by fax to confirm their number.'
    assert a.fetcher.calls == ['c.example', 'c.example']


def discovery_card(identity, number, endpoint='https://c.example'):
    from api.app.direct.crypto import card
    return card(identity, organization='Mountain Clinic', fax_number=number, endpoint=endpoint)


def test_a_forged_stale_or_unknown_introduction_is_refused(trio):
    a, b_identity = trio['a'], trio['b_identity']
    own = a.direct.identity().signing_key
    introduced = {'organization': 'Mountain Clinic', 'fax_number': C_NUMBER, 'endpoint': 'https://c.example',
                  'signing_key': trio['c'].direct.identity().signing_key}

    def envelope(identity=b_identity, **changes):
        statement = {'type': 'introduction', 'recipient': own, 'introduced_at': timestamp(), 'introduced': introduced}
        statement.update(changes)
        return signed(identity, statement)
    receive = a.discovery.receive_introduction
    assert receive(envelope()) == (200, {'recorded': True})
    assert receive(envelope()) == (200, {'recorded': True})  # the same hint again changes nothing
    assert len(rows(a.engine, 'direct_discovery_suggestions')) == 1
    unchecked = (400, {'recorded': False, 'detail': 'This introduction could not be checked.'})
    forged = envelope()
    forged['statement'] = forged['statement'].replace('Mountain Clinic', 'Mountain Clinlc')
    assert receive(forged) == unchecked
    assert receive(envelope(introduced_at=timestamp(datetime.utcnow() - timedelta(days=2)))) == unchecked
    assert receive(envelope(recipient=trio['c'].direct.identity().signing_key)) == unchecked
    assert receive(envelope(introduced={**introduced, 'signing_key': own})) == unchecked
    assert receive(envelope(introduced={**introduced, 'endpoint': 'ftp://c.example'})) == unchecked
    stranger = Identity.generate()
    assert receive(envelope(identity=stranger)) == (
        403, {'recorded': False, 'detail': 'Introductions are taken only from verified partners.'})
    # Over the network: a stranger's introduction to B is refused, and a Faxbot with direct delivery off has none.
    answer = trio['client'].post('/direct/introductions', json=signed(stranger, {
        'type': 'introduction', 'recipient': 'x', 'introduced_at': timestamp(), 'introduced': introduced}))
    assert answer.status_code == 400
    a.values = a.values.model_copy(update={'direct_delivery_enabled': False})
    with pytest.raises(DirectUnavailable):
        receive(envelope())


def test_introductions_from_one_partner_are_limited_per_day(trio, monkeypatch):
    a, b_identity = trio['a'], trio['b_identity']
    own = a.direct.identity().signing_key
    monkeypatch.setattr(discovery, 'INTRODUCTIONS_PER_DAY', 2)
    for index in range(3):
        key = Identity.generate().signing_key
        answer = a.discovery.receive_introduction(signed(b_identity, {
            'type': 'introduction', 'recipient': own, 'introduced_at': timestamp(),
            'introduced': {'organization': f'Clinic {index}', 'fax_number': f'+1555010020{index}',
                           'endpoint': f'https://clinic{index}.example', 'signing_key': key}}))
        expected = (200, {'recorded': True}) if index < 2 else (
            429, {'recorded': False, 'detail': 'Too many introductions today; try again tomorrow.'})
        assert answer == expected


# -- verified directories (D13) -----------------------------------------------------------------------------

DIRECTORY = 'faxdirectory.example.org'


def publisher(trio, **changes):
    """Installation C as a publisher that receives its card's number over its trunk."""
    c = trio['c']
    c.values = values_for(c.data, 'Mountain Clinic', C_NUMBER, c.host, INBOUND_ENABLED='true',
                          FAX_INBOUND_BACKEND='sip', SIP_TRUNK_DIDS=C_NUMBER, **changes)
    return c


def test_publishing_is_refused_for_a_number_this_faxbot_does_not_receive_on(trio):
    c = trio['c']
    # Its card's number alone is not a number it receives on.
    with pytest.raises(DirectConflict) as refused:
        c.discovery.publish(C_NUMBER, DIRECTORY)
    assert str(refused.value) == (f'Faxbot does not receive faxes on {C_NUMBER}, so it cannot be published. Only a '
                                  'number your trunk or receiving account delivers to this Faxbot can be published.')
    publisher(trio)
    with pytest.raises(DirectConflict) as refused:
        c.discovery.publish('+15550100009', DIRECTORY)
    assert str(refused.value) == (f'Partners deliver directly to {C_NUMBER}, the number on your partner card, so '
                                  'publish that number.')
    with pytest.raises(DirectConflict) as refused:
        c.discovery.publish(C_NUMBER, 'not a domain')
    assert str(refused.value) == ('Enter the directory as a domain name you control, such as '
                                  'faxdirectory.example.org.')
    c.discovery.store.save_settings(well_known=False)
    with pytest.raises(DirectConflict) as refused:
        c.discovery.publish(C_NUMBER, DIRECTORY)
    assert str(refused.value) == 'Turn on "Answer Faxbot lookups" first: senders read your partner card there.'
    assert rows(c.engine, 'direct_dns_publications') == []


@pytest.mark.asyncio
async def test_a_directory_record_is_shown_only_after_its_signature_number_and_expiry_check(trio):
    a, c = trio['a'], publisher(trio)
    row = c.discovery.publish(C_NUMBER, DIRECTORY, actor_name='Clinic admin')
    name = '_faxbot.3.0.0.0.0.1.0.5.5.5.1.' + DIRECTORY
    assert row['record_name'] == name and row['record_value'].startswith(f'v=faxbot1; n={C_NUMBER}; e=https://c.example;')
    assert discovery.publication_text('created', row) == (
        f'Add this record to the DNS for {DIRECTORY}. Senders who trust {DIRECTORY} then find this Faxbot for '
        f'{C_NUMBER}.')
    # A zone line splits a value longer than 255 bytes (a long address) into several strings.
    long_value = row['record_value'] + ' ' + 'x' * 200
    strings = discovery.zone_strings(long_value)
    assert strings.count('" "') == 1 and strings.replace('" "', '').strip('"') == long_value
    with pytest.raises(DirectConflict):
        c.discovery.publish(C_NUMBER, DIRECTORY)
    # The sender trusts nothing yet: nothing is asked.
    with pytest.raises(DirectConflict) as refused:
        await a.discovery.look_up(C_NUMBER)
    assert str(refused.value) == ('Add a trusted directory first; Faxbot looks numbers up only in directories you '
                                  'trust.')
    assert a.discovery.txt.asked == []
    a.discovery.store.save_settings(directories=[DIRECTORY])
    # A record changed on the way (another address) or copied to another number is ignored.
    tampered = row['record_value'].replace('https://c.example', 'https://evil.example')
    a.discovery.txt.records[name] = [tampered]
    suggestion, sentence = await a.discovery.look_up(C_NUMBER)
    assert suggestion is None and sentence == (f'{DIRECTORY} lists {C_NUMBER}, but its record could not be trusted, '
                                               'so it was ignored.')
    assert a.fetcher.calls == []
    copied = '_faxbot.9.0.0.0.0.1.0.5.5.5.1.' + DIRECTORY
    a.discovery.txt.records[copied] = [row['record_value'].replace(f'n={C_NUMBER}', 'n=+15550100009')]
    assert (await a.discovery.look_up('+15550100009'))[0] is None
    # The genuine record: checked, then the card is read from the address it names.
    a.discovery.txt.records[name] = ['v=spf1 -all', row['record_value']]
    a.fetcher.serve('c.example', c.discovery.well_known())
    lookups_before = len(rows(a.engine, 'direct_discovery_lookups'))
    suggestion, sentence = await a.discovery.look_up(C_NUMBER, now=datetime.utcnow() + timedelta(days=2))
    assert sentence == f'Mountain Clinic runs Faxbot at {C_NUMBER}, listed in {DIRECTORY}.'
    assert suggestion['source'] == 'directory' and suggestion['directory'] == DIRECTORY
    assert a.fetcher.calls == ['c.example'] and len(rows(a.engine, 'direct_discovery_lookups')) == lookups_before + 2
    assert discovery.suggestion_source_text(suggestion) == f'Listed in {DIRECTORY}, a directory you trust.'
    # An expired record is ignored.
    assert discovery.read_record(row['record_value'], name=name, number=C_NUMBER,
                                 now=row['expires_at'] + timedelta(days=1)) is None
    assert discovery.read_record(row['record_value'], name=name, number=C_NUMBER, now=datetime.utcnow())


@pytest.mark.asyncio
async def test_a_publication_is_checked_in_dns_and_withdrawn(trio):
    c = publisher(trio)
    row = c.discovery.publish(C_NUMBER, DIRECTORY)
    _, state = await c.discovery.check_publication(row['id'])
    assert state == 'missing'
    assert discovery.publication_text(state, row) == f'The record is not in the DNS for {DIRECTORY} yet.'
    c.discovery.txt.records[row['record_name']] = ['v=faxbot1; n=old']
    _, state = await c.discovery.check_publication(row['id'])
    assert discovery.publication_text(state, row) == (f'{DIRECTORY} has a different record for {C_NUMBER}; replace '
                                                      'it with this one.')
    c.discovery.txt.records[row['record_name']] = [row['record_value']]
    _, state = await c.discovery.check_publication(row['id'])
    assert discovery.publication_text(state, row) == f'The record is in place in {DIRECTORY}.'
    withdrawn = c.discovery.withdraw(row['id'], actor_name='Clinic admin')
    assert discovery.publication_text('withdrawn', withdrawn) == (
        f"Withdrawn. Delete the record {row['record_name']} from the DNS for {DIRECTORY} too.")
    assert rows(c.engine, 'direct_dns_publications')[0]['withdrawn_by_name'] == 'Clinic admin'
    with pytest.raises(DirectConflict):
        c.discovery.withdraw(row['id'])
    assert discovery.publication_text('unreachable', row) == (f'Faxbot could not ask the DNS for {DIRECTORY}; check '
                                                              'again later.')


@pytest.mark.asyncio
async def test_directory_lookups_are_limited_per_hour(trio):
    a = trio['a']
    a.discovery.store.save_settings(directories=[DIRECTORY])
    now = datetime.utcnow()
    for index in range(discovery.LOOKUPS_PER_HOUR):
        a.discovery.store.record_lookup(kind='call', host=f'h{index}.example', url='https://x/', outcome='unreachable',
                                        now=now - timedelta(minutes=5))
    with pytest.raises(DirectConflict) as refused:
        await a.discovery.look_up(C_NUMBER)
    assert str(refused.value) == 'Faxbot has made many lookups in the last hour; try again later.'
    assert a.discovery.txt.asked == []


# -- the DNS client ------------------------------------------------------------------------------------------

def answer_packet(identity, name, records, *, flags=0x8180, question=None):
    import struct
    header = struct.pack('!HHHHHH', identity, flags, 1, len(records) + 1, 0, 0)
    body = dnstxt.encode_name(question or name) + struct.pack('!HH', 16, 1)
    # A CNAME first (skipped), then each TXT record at a compression pointer to the question's name.
    cname = dnstxt.encode_name('other.' + name)
    body += b'\xc0\x0c' + struct.pack('!HHIH', 5, 1, 60, len(cname)) + cname
    for strings in records:
        rdata = b''.join(bytes([len(part)]) + part for part in strings)
        body += b'\xc0\x0c' + struct.pack('!HHIH', 16, 1, 60, len(rdata)) + rdata
    return header + body


def test_the_dns_client_joins_strings_checks_the_question_and_falls_back_to_tcp():
    name = '_faxbot.3.0.0.0.0.1.0.5.5.5.1.' + DIRECTORY
    long_value = b'v=faxbot1; ' + b'x' * 300
    packet = answer_packet(7, name, [(long_value[:255], long_value[255:]), (b'v=spf1 -all',)])
    truncated, records = dnstxt.parse(packet, 7, name)
    assert not truncated and records == [long_value.decode(), 'v=spf1 -all']
    with pytest.raises(dnstxt.DnsError):
        dnstxt.parse(packet, 8, name)
    with pytest.raises(dnstxt.DnsError):
        dnstxt.parse(answer_packet(7, name, [], question='other.example'), 7, name)
    assert dnstxt.parse(answer_packet(7, name, [], flags=0x8183), 7, name) == (False, [])
    sent = []

    def udp(server, packet, timeout):
        sent.append(('udp', server))
        identity = int.from_bytes(packet[:2], 'big')
        return answer_packet(identity, name, [], flags=0x8380)  # truncated

    def tcp(server, packet, timeout):
        sent.append(('tcp', server))
        identity = int.from_bytes(packet[:2], 'big')
        return answer_packet(identity, name, [(b'v=faxbot1; n=+15550100003',)])
    assert dnstxt.txt_records(name, servers=['192.0.2.53'], udp=udp, tcp=tcp) == ['v=faxbot1; n=+15550100003']
    assert sent == [('udp', '192.0.2.53'), ('tcp', '192.0.2.53')]
    with pytest.raises(dnstxt.DnsError):
        dnstxt.txt_records(name, servers=[])
    query = dnstxt.query(name, 9)
    assert query[:2] == b'\x00\x09' and query.endswith(b'\x00\x00\x29\x04\xd0\x00\x00\x00\x00\x00\x00')
