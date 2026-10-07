"""The reply number (M13): which number a sent fax shows, checks on it, advice, caller ID and 47 CFR 68.318(d).

All numbers are synthetic (555 exchange or the reserved 800-555 range). Mailbox rules are
given as routes for the unit tests and created through the real access API for the HTTP ones.
"""
import base64
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import ami, hylafax_engine
from app.config_values import ConfigurationValues
from app.routing import own_numbers, reply_number
from app.routing.costs import RateCard
from app.routing.reply_number import Route

DID_A, DID_B, TOLL_FREE = '+13035550101', '+13035550102', '+18005550199'
HUMBLE, SIGNALWIRE = '+17205550103', '+17205550104'


def values(**extra):
    environment = {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                   'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1', 'SIP_TRUNK_CALLER_ID': DID_A,
                   'SIP_TRUNK_DIDS': f'{DID_A},{DID_B},{TOLL_FREE}', 'INBOUND_ENABLED': 'true',
                   'FAX_HEADER': 'Example Clinic'}
    environment.update(extra)
    return ConfigurationValues.from_environment(environment)


ROUTES = [Route(DID_A, 'mbx-front', 'Front desk'), Route(DID_B, 'mbx-billing', 'Billing'),
          Route(TOLL_FREE, 'mbx-front', 'Front desk')]
LABELS = {'mbx-front': 'Front desk', 'mbx-billing': 'Billing'}


class Store:
    """Rate cards as RouteStore.card_for returns them."""

    def __init__(self, cards):
        self.cards = cards

    def card_for(self, provider, direction='outbound'):
        return self.cards.get((provider, direction))


def card(provider, *, per_minute=0, per_page=0, monthly=None, direction='inbound'):
    return RateCard(None, provider, direction, f'{provider} test card', 'USD', per_minute, per_page, 0, 60, 60,
                    None, datetime(2026, 10, 7), monthly)


# -- which number a fax shows ----------------------------------------------------------------

def test_the_choice_order_is_mailbox_then_organization_then_station_id_then_cheapest_then_the_line():
    both = values(FAX_REPLY_NUMBER=DID_A, FAX_REPLY_NUMBERS=f'mbx-billing={DID_B}', FAX_LOCAL_STATION_ID='+1 303 555 0177')
    choice = reply_number.choose(both, routes=ROUTES, mailbox_id='mbx-billing')
    assert (choice.number, choice.source) == (DID_B, 'mailbox')
    # Another mailbox, or a send that names none, shows the organization's number.
    assert reply_number.choose(both, routes=ROUTES, mailbox_id='mbx-front').source == 'organization'
    assert reply_number.choose(both, routes=ROUTES).number == DID_A
    # Without a reply number, the station ID setting as typed, as before this feature.
    station = values(FAX_LOCAL_STATION_ID='+1 303 555 0177')
    assert (reply_number.choose(station, routes=ROUTES).number, reply_number.choose(station, routes=ROUTES).source) \
        == ('+1 303 555 0177', 'station')
    # Neither: the cheapest number that reaches a mailbox (both DIDs cost the same; the first local one wins).
    automatic = reply_number.choose(values(), routes=ROUTES)
    assert automatic.source == 'automatic' and automatic.number == DID_A
    # No number reaches a mailbox: the line's own number.
    line = reply_number.choose(values(), routes=[])
    assert (line.number, line.source) == (None, 'line')


def test_a_saved_number_that_no_longer_reaches_its_mailbox_is_skipped_and_the_reason_is_kept():
    saved = values(FAX_REPLY_NUMBER=DID_B, FAX_REPLY_NUMBERS=f'mbx-front={DID_B}')
    moved = [Route(DID_A, 'mbx-front', 'Front desk'), Route(DID_B, 'mbx-front', 'Front desk')]
    choice = reply_number.choose(saved, routes=[ROUTES[0]], mailbox_id='mbx-front')
    # The mailbox's number lost its rule, and so did the organization's: the next in the order is used.
    assert choice.source == 'automatic' and choice.number == DID_A
    assert any('No rule under Numbers sends faxes for +13035550102' in note for note in choice.notes)
    assert reply_number.choose(saved, routes=moved, mailbox_id='mbx-front').source == 'mailbox'


def test_the_same_mailbox_check_refuses_a_number_that_reaches_another_mailbox():
    refused = reply_number.refusal(values(), DID_B, routes=ROUTES, mailbox_id='mbx-front', labels=LABELS)
    assert refused == ('Faxes for +13035550102 go to the Billing mailbox, not Front desk. Choose a number that '
                       'reaches Front desk, or change its rule under Numbers.')
    assert reply_number.refusal(values(), DID_A, routes=ROUTES, mailbox_id='mbx-front', labels=LABELS) is None
    # For the whole organization any mailbox in use will do, but there must be one.
    assert reply_number.refusal(values(), DID_B, routes=ROUTES) is None
    assert reply_number.refusal(values(), DID_B, routes=[]).startswith('No rule under Numbers sends faxes for')


def test_a_number_the_organization_does_not_own_or_that_does_not_receive_here_is_refused():
    stranger = '+13035550999'
    assert reply_number.refusal(values(), stranger, routes=ROUTES) == (
        '+13035550999 is not one of your numbers. Use a number one of your fax accounts gives you '
        '(Providers lists them).')
    # HumbleFax gives you the number, but HumbleFax does not receive into Faxbot yet.
    humble = values(HUMBLEFAX_FROM_NUMBER=HUMBLE)
    routes = ROUTES + [Route(HUMBLE, 'mbx-front', 'Front desk')]
    assert reply_number.refusal(humble, HUMBLE, routes=routes).startswith(
        'Faxes sent to +17205550103 do not reach this Faxbot.')
    with pytest.raises(reply_number.ReplyNumberRefused, match='is not a fax number Faxbot can read'):
        reply_number.read_number('not a number', values())


def test_the_cheapest_number_to_receive_on_is_a_flat_plan_first_and_never_toll_free_or_per_page(monkeypatch):
    # Once HumbleFax receives into Faxbot (it registers its account numbers), its flat plan wins.
    monkeypatch.setitem(own_numbers.RECEIVING_ACCOUNTS, 'humblefax', lambda values: [values.humblefax_from_number])
    settings = values(HUMBLEFAX_FROM_NUMBER=HUMBLE, FAX_INBOUND_BACKEND='humblefax')
    routes = ROUTES + [Route(HUMBLE, 'mbx-front', 'Front desk')]
    store = Store({('humblefax', 'outbound'): card('humblefax', monthly=10_000_000, direction='outbound')})
    found = reply_number.candidates(settings, store=store, routes=routes)
    by_number = {candidate.number: candidate for candidate in found}
    assert by_number[HUMBLE].flat and by_number[HUMBLE].cost_micros == 0
    assert by_number[TOLL_FREE].kind == 'toll_free' and by_number[TOLL_FREE].avoid
    assert by_number[DID_A].cost_micros > 0  # Telnyx local, by the minute (shipped price)
    assert reply_number.cheapest(found).number == HUMBLE
    assert reply_number.choose(settings, store=store, routes=routes).number == HUMBLE
    # Without HumbleFax the toll-free number is never chosen, even though it reaches a mailbox.
    assert reply_number.cheapest([c for c in found if c.number in (TOLL_FREE,)]) is None


def test_advice_names_the_numbers_not_to_publish_for_receiving():
    settings = values(SIGNALWIRE_FAX_FROM_E164=SIGNALWIRE, FAX_INBOUND_BACKEND='signalwire')
    routes = ROUTES + [Route(SIGNALWIRE, 'mbx-front', 'Front desk')]
    store = Store({('signalwire', 'inbound'): card('signalwire', per_page=45_000)})
    advice = reply_number.advice(reply_number.candidates(settings, store=store, routes=routes))
    sentences = dict((candidate.number, sentence) for candidate, sentence in advice['avoid'])
    assert sentences[TOLL_FREE] == ("Don't publish +18005550199 for receiving: on a toll-free number you pay for "
                                    'every minute of every fax sent to you.')
    assert sentences[SIGNALWIRE].startswith("Don't publish +17205550104 for receiving: SignalWire charges for every "
                                            'page you receive (about $0.225 for a 5-page fax)')
    assert advice['suggest'].number == DID_A


def test_an_unpriced_number_is_never_called_the_cheapest():
    settings = values(SIP_TRUNK_PRESET='custom', SIP_TRUNK_HOST='sip.example.test', SIP_TRUNK_AUTH='ip')
    found = reply_number.candidates(settings, store=Store({}), routes=ROUTES)
    assert all(candidate.cost_micros is None for candidate in found)
    assert reply_number.cheapest(found) is None
    assert reply_number.choose(settings, store=Store({}), routes=ROUTES).source == 'line'


def test_the_mailbox_setting_round_trips_and_ignores_what_it_cannot_read():
    text = reply_number.encode_mailbox_numbers({'mbx-front': DID_A, 'mbx-billing': DID_B})
    assert text == f'mbx-billing={DID_B};mbx-front={DID_A}'
    assert reply_number.mailbox_numbers(values(FAX_REPLY_NUMBERS=text)) == {'mbx-front': DID_A, 'mbx-billing': DID_B}
    with pytest.raises(Exception):
        values(FAX_REPLY_NUMBERS='front desk=3035550101')


# -- the station ID (TSI) and caller ID of each call, on both engines ----------------------------

def _decoded(fields, name):
    variables = dict(item.split('=', 1) for item in fields['Variable'].split(',') if '=' in item)
    return base64.b64decode(variables[name]).decode()


def test_the_built_in_engine_sends_the_reply_number_as_station_id_and_caller_id_only_on_the_same_trunk(monkeypatch):
    settings = values(FAX_REPLY_NUMBER=DID_B)
    choice = reply_number.choose(settings, routes=ROUTES)
    fields = ami.originate_fields_for(settings, 'job1', '+13035550150', '/faxdata/job1.tiff', choice=choice)
    assert _decoded(fields, 'FAXSTATION64') == DID_B  # localstationid: the TSI
    assert _decoded(fields, 'FAXHEADER64') == 'Example Clinic'  # headerinfo, printed with the date and the TSI
    assert fields['CallerID'] == DID_B  # a number on the same Telnyx trunk
    # A number from another account (HumbleFax) is printed and sent as TSI, but caller ID stays the line's.
    monkeypatch.setitem(own_numbers.RECEIVING_ACCOUNTS, 'humblefax', lambda values: [values.humblefax_from_number])
    humble = values(FAX_REPLY_NUMBER=HUMBLE, HUMBLEFAX_FROM_NUMBER=HUMBLE, FAX_INBOUND_BACKEND='humblefax')
    routes = ROUTES + [Route(HUMBLE, 'mbx-front', 'Front desk')]
    choice = reply_number.choose(humble, routes=routes)
    fields = ami.originate_fields_for(humble, 'job1', '+13035550150', '/faxdata/job1.tiff', choice=choice)
    assert _decoded(fields, 'FAXSTATION64') == HUMBLE and fields['CallerID'] == DID_A
    status = reply_number.caller_id(humble, HUMBLE)
    assert status[0]['provider'] == 'Telnyx' and status[0]['shows'] is False
    assert status[0]['sentence'].startswith('Telnyx calls keep showing +13035550101 as caller ID: ')


def test_with_no_reply_number_a_trunk_call_keeps_the_trunk_caller_id_as_before():
    settings = values()
    line = reply_number.Choice(None, 'line', '')
    fields = ami.originate_fields_for(settings, 'job1', '+13035550150', '/faxdata/job1.tiff', choice=line)
    assert fields['CallerID'] == DID_A and _decoded(fields, 'FAXSTATION64') == DID_A


def test_reply_choice_never_raises_and_falls_back_to_the_line(monkeypatch):
    monkeypatch.setattr(ami, '_database', lambda: object())  # not a database: every read fails
    assert ami.reply_choice(values()).source in {'line', 'automatic'}


def test_the_ssl_fax_engine_job_sends_the_reply_number_as_tsi_and_prints_68_318_d_fields_on_each_page(tmp_path):
    from api.tests.test_hylafax_engine import ATTEMPT, JOB, FakeEngine, trunk_values
    import threading
    settings = trunk_values(tmp_path)
    password = hylafax_engine.engine_secrets(settings)['submit_password']
    server = FakeEngine(password)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        image = tmp_path / 'fax.tiff'
        image.write_bytes(b'II*\x00synthetic')
        hylafax_engine.create_job(settings, tag=hylafax_engine.new_tag(), job_id=JOB, attempt_id=ATTEMPT,
                                  tiff_path=str(image), header='Example Clinic | 100% local', station=DID_B,
                                  host='127.0.0.1', port=server.server_address[1]).discard()
    finally:
        server.shutdown()
        server.server_close()
    parms = [command for command in server.commands if command.startswith('JPARM')]
    assert f'JPARM TSI "{DID_B}"' in parms
    tagline = next(command for command in parms if command.startswith('JPARM TAGLINE'))
    # Date and time (strftime), who sends, the station ID the job sends (%l), and the page: every page.
    assert tagline == 'JPARM TAGLINE "%d %b %Y %H:%M|Example Clinic 100%%%% local|%%l|Page %%P of %%T"'


def test_the_header_line_is_printed_only_with_header_text_and_faxbot_says_so():
    assert reply_number.header_problem(values()) is None
    assert reply_number.header_problem(values(FAX_HEADER='  ')) == reply_number.HEADER_MISSING
    # spandsp 0.0.6 prints "date time  header  TSI  p.N" on each page only when the header text is set
    # (t4_tx.c make_header); an empty header sends pages with no header line at all.
    fields = ami.originate_fields_for(values(FAX_HEADER=''), 'job1', '+13035550150', '/faxdata/job1.tiff',
                                      choice=reply_number.Choice(DID_A, 'organization', ''))
    assert _decoded(fields, 'FAXHEADER64') == ''


# -- the Numbers screen's API ---------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from api.tests.test_access_management_http import ORIGIN, _environment
    from app.main import app
    _environment(monkeypatch, tmp_path)
    for name, value in {'FAX_BACKEND': 'sip', 'FAX_OUTBOUND_BACKEND': 'sip', 'FAX_INBOUND_BACKEND': 'sip',
                        'INBOUND_ENABLED': 'true', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                        'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1', 'SIP_TRUNK_CALLER_ID': DID_A,
                        'SIP_TRUNK_DIDS': f'{DID_A},{DID_B},{TOLL_FREE}', 'AMI_PASSWORD': 'synthetic-ami-pass',
                        'FAX_HEADER': 'Example Clinic'}.items():
        monkeypatch.setenv(name, value)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


def test_the_numbers_screen_saves_only_a_number_that_reaches_the_same_mailbox(client):
    from api.tests.test_access_management_http import B
    from api.tests.test_work_http import mailbox
    front = mailbox(client, 'Front desk', DID_A)
    billing = mailbox(client, 'Billing', DID_B)
    view = client.get('/numbers/reply', headers=B)
    assert view.status_code == 200, view.text
    body = view.json()
    assert body['source'] == 'automatic' and body['shows'] == DID_A
    assert body['suggestion']['number'] == DID_A
    assert {item['number'] for item in body['avoid']} == {TOLL_FREE}
    # The toll-free number has no rule: refused with the reason, nothing saved.
    refused = client.put('/numbers/reply', headers=B, json={'number': TOLL_FREE})
    assert refused.status_code == 400 and refused.json()['detail'].startswith('No rule under Numbers sends faxes')
    saved = client.put('/numbers/reply', headers=B, json={'number': '303-555-0102'})
    assert saved.status_code == 200, saved.text
    assert saved.json()['number'] == DID_B
    assert client.get('/numbers/reply', headers=B).json()['source'] == 'organization'
    # A mailbox's own number must reach that mailbox.
    wrong = client.put(f"/numbers/reply/mailboxes/{front['id']}", headers=B, json={'number': DID_B})
    assert wrong.status_code == 400 and 'go to the Billing mailbox, not Front desk' in wrong.json()['detail']
    right = client.put(f"/numbers/reply/mailboxes/{billing['id']}", headers=B, json={'number': DID_B})
    assert right.status_code == 200, right.text
    listed = client.get('/numbers/reply', headers=B).json()['mailboxes']
    assert listed == [{'mailbox_id': billing['id'], 'mailbox': 'Billing', 'number': DID_B, 'problem': None}]
    assert client.delete(f"/numbers/reply/mailboxes/{billing['id']}", headers=B).status_code == 200
    assert client.get('/numbers/reply', headers=B).json()['mailboxes'] == []
    # Empty: Faxbot chooses again.
    assert client.put('/numbers/reply', headers=B, json={'number': ''}).json()['number'] is None


def test_reading_and_changing_the_reply_number_need_settings_permissions(client):
    unauthenticated = client.get('/numbers/reply')
    assert unauthenticated.status_code in (401, 403)
    assert client.put('/numbers/reply', json={'number': DID_A}).status_code in (401, 403)


# -- faxbot numbers reply -----------------------------------------------------------------------

def test_the_command_line_shows_sets_and_clears_the_reply_number(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for client in _serve(monkeypatch, tmp_path, FAX_BACKEND='sip', FAX_OUTBOUND_BACKEND='sip',
                         FAX_INBOUND_BACKEND='sip', INBOUND_ENABLED='true', SIP_TRUNK_PRESET='telnyx',
                         SIP_TRUNK_USERNAME='faxbotuser', SIP_TRUNK_PASSWORD='Synthetic-Password-1',
                         SIP_TRUNK_CALLER_ID=DID_A, SIP_TRUNK_DIDS=f'{DID_A},{DID_B}',
                         AMI_PASSWORD='synthetic-ami-pass'):
        cli = Cli(client)
        for label, number in (('Front desk', DID_A), ('Billing', DID_B)):
            added = cli('numbers', 'mailboxes', 'add', label)
            assert added.exit_code == 0, (added.stdout, added.stderr)
            routed = cli('numbers', 'add', number, '--mailbox', label)
            assert routed.exit_code == 0, (routed.stdout, routed.stderr)
        shown = cli('numbers', 'reply', 'show')
        assert shown.exit_code == 0, (shown.stdout, shown.stderr)
        assert 'Faxes show +13035550101, your cheapest number to receive on that reaches a mailbox.' in shown.stdout
        refused = cli('numbers', 'reply', 'set', DID_A, '--mailbox', 'Billing')
        assert refused.exit_code != 0 and 'go to the Front desk mailbox, not Billing' in refused.stderr
        assert 'Faxes from Billing now show +13035550102.' in cli('numbers', 'reply', 'set', DID_B, '--mailbox',
                                                                  'Billing').stdout
        assert 'Faxes now show +13035550102.' in cli('numbers', 'reply', 'set', '3035550102').stdout
        listed = cli.json('numbers', 'reply', 'numbers')
        mailboxes = {row['number']: row['mailbox'] for row in listed}
        assert (mailboxes[DID_A], mailboxes[DID_B]) == ('Front desk', 'Billing')
        assert 'Faxbot now chooses the number' in cli('numbers', 'reply', 'clear').stdout
        assert "show the organization's reply number again" in cli('numbers', 'reply', 'clear', '--mailbox',
                                                                    'Billing').stdout
