"""faxbot recipients partners discover|introduce|may-introduce|publish: finding partners on the command line."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app.direct.crypto import Identity, card as make_card
from api.app.direct.discovery import DiscoveryStore
from api.tests.test_cli import Cli, _serve, cli, server  # noqa: F401 - fixtures


DIRECTORY = 'faxdirectory.example.org'


def engine_of(cli):
    return cli.client.app.state.configuration_runtime.manager.store.engine


def suggest(cli, organization, number, host):
    identity = Identity.generate()
    card = make_card(identity, organization=organization, fax_number=number, endpoint=f'https://{host}')
    return DiscoveryStore(engine_of(cli)).suggest(number=number, source='call', organization=organization,
                                                  signing_key=card['signing_key'], endpoint=card['endpoint'],
                                                  card=card)


def verified_partner(cli, organization, number, host):
    identity = Identity.generate()
    card = make_card(identity, organization=organization, fax_number=number, endpoint=f'https://{host}')
    added = cli.json('recipients', 'partners', 'add', '-', input=__import__('json').dumps(card))
    peers = sa.Table('direct_peers', sa.MetaData(), autoload_with=engine_of(cli))
    with engine_of(cli).begin() as connection:
        connection.execute(peers.update().where(peers.c.id == added['id']).values(state='verified',
                                                                                  verified_at=datetime.utcnow()))
    return added


class Told:
    """The partners' Faxbots, answering introductions."""

    def __init__(self):
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if url.endswith('/direct/introductions'):
            return 200, {'recorded': True}
        return 404, {'detail': 'Not found.'}


def test_discover_lists_suggestions_and_enrolls_or_dismisses_them(cli):
    empty = cli('recipients', 'partners', 'discover', 'list')
    assert empty.exit_code == 0, empty.stdout
    assert 'No recipient that runs Faxbot has been found yet.' in empty.stdout
    assert 'Faxbot looks numbers up only in directories you trust. None is listed, so nothing is looked up.' in \
        empty.stdout
    suggest(cli, 'Valley Hospital', '+15550100001', 'valley.example')
    suggest(cli, 'Mountain Clinic', '+15550100003', 'mountain.example')
    listed = cli('recipients', 'partners', 'discover', 'list')
    assert 'Valley Hospital' in listed.stdout and 'Found from a fax call with this number.' in listed.stdout
    assert ('This recipient runs Faxbot. Enroll as a direct partner to send to them without a phone call.'
            in listed.stdout)
    enrolled = cli('recipients', 'partners', 'discover', 'enroll', 'Valley Hospital')
    assert enrolled.exit_code == 0, enrolled.stdout
    assert enrolled.stdout.strip() == 'Valley Hospital added. Send them a code by fax to confirm their number.'
    assert [item['state'] for item in cli.json('recipients', 'partners', 'list')] == ['pending']
    dismissed = cli('recipients', 'partners', 'discover', 'dismiss', '+1 555 010 0003')
    assert dismissed.stdout.strip() == 'Dismissed. Faxbot will not suggest this recipient again.'
    assert cli.json('recipients', 'partners', 'discover', 'list')['suggestions'] == []
    missing = cli('recipients', 'partners', 'discover', 'enroll', 'Nobody')
    assert missing.exit_code != 0
    assert "No single suggestion matches 'Nobody'." in (missing.stdout + missing.stderr)


def test_discover_settings_and_a_lookup_in_trusted_directories(cli):
    shown = cli('recipients', 'partners', 'discover', 'settings')
    assert 'Answer Faxbot lookups' in shown.stdout and 'Look for Faxbot on calls' in shown.stdout
    changed = cli.json('recipients', 'partners', 'discover', 'settings', '--from-calls', 'off', '--directory',
                       DIRECTORY)
    assert changed['settings']['from_calls'] is False and changed['settings']['directories'] == [DIRECTORY]
    human = cli('recipients', 'partners', 'discover', 'settings')
    assert f'Faxbot looks up the numbers you fax in {DIRECTORY}.' in human.stdout
    assert 'Faxbot does not look up the other side of your fax calls.' in human.stdout
    asked = []
    cli.client.app.state.discovery_txt = lambda name: asked.append(name) or []
    try:
        looked = cli('recipients', 'partners', 'discover', 'lookup', '+15550100009')
    finally:
        cli.client.app.state.discovery_txt = None
    assert looked.stdout.strip() == 'No trusted directory lists +15550100009.'
    assert asked == ['_faxbot.9.0.0.0.0.1.0.5.5.5.1.' + DIRECTORY]
    bad = cli('recipients', 'partners', 'discover', 'settings', '--directory', 'not a domain')
    assert bad.exit_code != 0
    assert ('"not a domain" is not a domain name. Enter a directory such as faxdirectory.example.org.'
            in bad.stdout + bad.stderr)
    cleared = cli.json('recipients', 'partners', 'discover', 'settings', '--no-directories', '--from-calls', 'on')
    assert cleared['settings']['directories'] == [] and cleared['settings']['from_calls'] is True


def test_introduce_needs_may_introduce_for_both(cli):
    valley = verified_partner(cli, 'Valley Hospital', '+15550100001', 'valley.example')
    mountain = verified_partner(cli, 'Mountain Clinic', '+15550100003', 'mountain.example')
    refused = cli('recipients', 'partners', 'introduce', 'Valley Hospital', 'Mountain Clinic')
    assert refused.exit_code != 0
    assert ('Valley Hospital has not agreed to be introduced. Turn on "May be introduced" for Valley Hospital once '
            'they agree.') in refused.stdout + refused.stderr
    on = cli('recipients', 'partners', 'may-introduce', 'Valley Hospital', 'on')
    assert on.stdout.strip() == 'Valley Hospital may be introduced to your other partners.'
    cli('recipients', 'partners', 'may-introduce', valley['fax_number'], 'on')
    cli('recipients', 'partners', 'may-introduce', mountain['id'], 'on')
    told = Told()
    cli.client.app.state.direct_http = told
    done = cli('recipients', 'partners', 'introduce', 'Valley Hospital', 'Mountain Clinic')
    assert done.exit_code == 0, done.stdout + done.stderr
    assert done.stdout.strip() == ('Valley Hospital and Mountain Clinic were introduced. Each can now enroll the other '
                                   "and confirm the other's number with a code by fax.")
    assert [url for _, url in told.calls] == ['https://valley.example/direct/introductions',
                                              'https://mountain.example/direct/introductions']
    off = cli('recipients', 'partners', 'may-introduce', 'Mountain Clinic', 'off')
    assert off.stdout.strip() == 'Mountain Clinic is not introduced to anyone.'
    assert cli('recipients', 'partners', 'may-introduce', 'Mountain Clinic', 'maybe').exit_code != 0


def test_publish_refuses_a_number_this_faxbot_does_not_receive_on(cli):
    refused = cli('recipients', 'partners', 'publish', 'add', '+15550006666', '--directory', DIRECTORY)
    assert refused.exit_code != 0
    assert ('Faxbot does not receive faxes on +15550006666, so it cannot be published. Only a number your trunk or '
            'receiving account delivers to this Faxbot can be published.') in refused.stdout + refused.stderr
    listed = cli('recipients', 'partners', 'publish', 'list')
    assert ('Faxbot does not receive faxes on +15550006666, the number on your partner card, so it cannot be '
            'published.') in listed.stdout
    assert 'Nothing is published.' in listed.stdout


@pytest.fixture
def trunk_receiving_cli(monkeypatch, tmp_path):
    for client in _serve(monkeypatch, tmp_path, FAX_INBOUND_BACKEND='sip', SIP_TRUNK_DIDS='+15550006666'):
        yield Cli(client)


def test_publish_gives_the_record_checks_it_and_withdraws_it(trunk_receiving_cli):
    cli = trunk_receiving_cli
    added = cli('recipients', 'partners', 'publish', 'add', '+15550006666', '--directory', DIRECTORY)
    assert added.exit_code == 0, added.stdout + added.stderr
    name = '_faxbot.6.6.6.6.0.0.0.5.5.5.1.' + DIRECTORY
    assert (f'Add this record to the DNS for {DIRECTORY}. Senders who trust {DIRECTORY} then find this Faxbot for '
            '+15550006666.') in added.stdout
    assert f'{name}. 3600 IN TXT "v=faxbot1; n=+15550006666;' in added.stdout
    (item,) = cli.json('recipients', 'partners', 'publish', 'list')
    records = {}
    cli.client.app.state.discovery_txt = lambda asked: records.get(asked, [])
    try:
        missing = cli('recipients', 'partners', 'publish', 'check', DIRECTORY)
        assert missing.stdout.strip() == f'The record is not in the DNS for {DIRECTORY} yet.'
        records[name] = [item['value']]
        live = cli('recipients', 'partners', 'publish', 'check', '+15550006666')
        assert live.stdout.strip() == f'The record is in place in {DIRECTORY}.'
    finally:
        cli.client.app.state.discovery_txt = None
    withdrawn = cli('recipients', 'partners', 'publish', 'withdraw', DIRECTORY)
    assert withdrawn.stdout.strip() == f'Withdrawn. Delete the record {name} from the DNS for {DIRECTORY} too.'
    assert cli.json('recipients', 'partners', 'publish', 'list') == []
