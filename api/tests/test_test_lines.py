"""Public test lines from Diagnostics (research N10, test_lines.py).

The real POST /diagnostics/test-lines/{line}/send path runs on an installation with a SIP trunk and sending turned
off, so a test fax is accepted (sending rules, dialing guard, the acceptance transaction) but never dialed. The
dialing guard is never bypassed: a line in a country Faxbot may not dial is refused before anything is accepted,
and allowed only through the guard's own change. A test fax a person answered, or whose station differed, opens
no Work item (the real FaxResult handler and certainty feed beside an ordinary fax that does). Replies are labelled
only from the line's own number, or by a person; Faxbeep's public API is replaced by a stand-in. Synthetic numbers
for this installation; the test lines' numbers are the operators' published ones and nothing calls them.
"""
from datetime import datetime, timedelta
from uuid import uuid4

import phonenumbers
import pytest
import sqlalchemy as sa

from app import test_lines
from api.tests.test_reply_number import DID_A, DID_B, client  # noqa: F401 - fixture


def test_the_curated_lines_are_the_ones_whose_operators_invite_test_faxes_each_with_its_source():
    assert [line.id for line in test_lines.LINES] == ['faxbeep-us', 'faxbeep-gb', 'faxbeep-au', 'hp-us',
                                                      'gotfreefax-us', 'interpage-us', 'tokonet-jp']
    for line in test_lines.LINES:
        parsed = phonenumbers.parse(line.number, None)
        assert phonenumbers.is_valid_number(parsed), line.id
        assert phonenumbers.region_code_for_number(parsed) == line.country, line.id
        assert line.source_url.startswith(('https://', 'http://')) and len(line.read_on) == 10
        assert datetime.strptime(line.read_on, '%Y-%m-%d') <= datetime(2026, 10, 10)
        assert line.kind in ('public', 'reply', 'echo')
        assert (line.reply_minutes is not None) == (line.kind != 'public')
    hp = test_lines.BY_ID['hp-us']
    # No verbatim invitation was kept for HP: none is invented, and the note says so.
    assert hp.invitation is None and 'exact words were not kept' in hp.note and 'often busy' in hp.note
    assert test_lines.BY_ID['interpage-us'].max_pages == 5
    assert {line.receipt for line in test_lines.LINES if line.operator == 'Faxbeep'} == {'faxbeep'}


def _b():
    from api.tests.test_access_management_http import B
    return B


def _engine():
    from app.main import app
    return app.state.configuration_runtime.manager.store.engine


def _count(table):
    with _engine().connect() as connection:
        return connection.execute(sa.select(sa.func.count()).select_from(sa.table(table))).scalar()


def test_the_list_says_what_the_guard_allows_and_whether_a_reply_reaches_faxbot(client):  # noqa: F811
    found = client.get('/diagnostics/test-lines', headers=_b())
    assert found.status_code == 200, found.text
    body = found.json()
    assert body['reply']['reaches'] is True and DID_A in body['reply']['sentence']
    guards = {line['id']: line['guard'] for line in body['lines']}
    assert guards['faxbeep-us']['allowed'] and guards['hp-us']['allowed']
    assert not guards['faxbeep-gb']['allowed'] and guards['faxbeep-gb']['class'] == 'country:GB'
    assert guards['faxbeep-gb']['sentence'] == ('Faxbot has not sent to numbers in the United Kingdom before, so it '
                                                'does not dial this line until you allow that country.')
    assert not guards['tokonet-jp']['allowed'] and guards['tokonet-jp']['class'] == 'country:JP'
    assert body['sends'] == []


def test_a_line_the_guard_holds_is_never_sent_until_you_allow_its_country_through_the_guard(client):  # noqa: F811
    before = _count('fax_jobs')
    held = client.post('/diagnostics/test-lines/faxbeep-gb/send', headers=_b())
    assert held.status_code == 200, held.text
    assert held.json()['sent'] is False and held.json()['needs_allow']['class'] == 'country:GB'
    assert _count('fax_jobs') == before and _count('test_line_sends') == 0
    # Your one-click allow is the guard's own change, recorded as yours.
    allowed = client.put('/routing/dialing/country:GB', headers=_b(), json={'state': 'allowed'})
    assert allowed.status_code == 200, allowed.text
    sent = client.post('/diagnostics/test-lines/faxbeep-gb/send', headers=_b())
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body['sent'] is True and body['send']['line_id'] == 'faxbeep-gb'
    assert _count('fax_jobs') == before + 1
    with _engine().connect() as connection:
        row = connection.execute(sa.text('SELECT job_id, number, reply_number, reply_minutes FROM test_line_sends')
                                 ).mappings().one()
        to_number = connection.execute(sa.text('SELECT to_number FROM fax_jobs WHERE id = :id'),
                                       {'id': row['job_id']}).scalar()
    assert row['number'] == to_number == '+442038089463' and row['reply_number'] == DID_A
    assert row['reply_minutes'] is None and row['job_id'] == body['send']['fax_id']
    listed = client.get('/diagnostics/test-lines', headers=_b()).json()
    assert [item['line_id'] for item in listed['sends']] == ['faxbeep-gb']
    assert client.post('/diagnostics/test-lines/nowhere/send', headers=_b()).status_code == 400


def test_a_reply_from_the_lines_number_is_labelled_another_waits_for_a_person_and_silence_is_no_answer(
        client):  # noqa: F811
    engine = _engine()
    sent = client.post('/diagnostics/test-lines/hp-us/send', headers=_b()).json()
    assert sent['sent'] is True and sent['send']['reply']['state'] == 'not_sent'
    job, send_id = sent['send']['fax_id'], sent['send']['id']
    now = datetime.utcnow()
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE outbound_deliveries SET state = 'success' WHERE id = :id"), {'id': job})
    view = client.get(f'/diagnostics/test-lines/sends/{send_id}', headers=_b()).json()
    assert view['fax_state'] == 'sent' and view['reply']['state'] == 'waiting'

    def received(from_number, to_number=DID_A, minutes=3):
        inbound_id = uuid4().hex
        moment = now + timedelta(minutes=minutes)
        with engine.begin() as connection:
            connection.execute(sa.table('inbound_faxes', *(sa.column(name) for name in (
                'id', 'from_number', 'to_number', 'status', 'backend', 'pages', 'created_at', 'received_at',
                'updated_at'))).insert().values(
                id=inbound_id, from_number=from_number, to_number=to_number, status='received', backend='sip',
                pages=1, created_at=moment, received_at=moment, updated_at=moment))
        return inbound_id
    # A fax from another number on another of your numbers is not offered; one on the reply number is offered only.
    received('+13035550177', to_number=DID_B)
    stranger = received('+13035550177')
    view = client.get(f'/diagnostics/test-lines/sends/{send_id}', headers=_b()).json()
    assert view['reply']['state'] == 'possible'
    assert [item['inbound_id'] for item in view['reply']['candidates']] == [stranger]
    assert client.get('/diagnostics/test-lines/replies', headers=_b()).json()['replies'] == []
    # From HP's own number: labelled by itself.
    reply = received('+1 888 473 2963', minutes=6)
    view = client.get(f'/diagnostics/test-lines/sends/{send_id}', headers=_b()).json()
    assert view['reply'] == {'state': 'replied', 'inbound_id': reply, 'candidates': [],
                             'sentence': "HP's fax test service faxed back. The reply is in Received, labelled as a "
                                         'test.'}
    (label,) = client.get('/diagnostics/test-lines/replies', headers=_b()).json()['replies']
    assert label['inbound_id'] == reply and label['how'] == 'number' and label['label'] == 'Test reply'


def _received(engine, from_number, moment, to_number=DID_A):
    inbound_id = uuid4().hex
    with engine.begin() as connection:
        connection.execute(sa.table('inbound_faxes', *(sa.column(name) for name in (
            'id', 'from_number', 'to_number', 'status', 'backend', 'pages', 'created_at', 'received_at',
            'updated_at'))).insert().values(
            id=inbound_id, from_number=from_number, to_number=to_number, status='received', backend='sip', pages=1,
            created_at=moment, received_at=moment, updated_at=moment))
    return inbound_id


def test_silence_is_no_answer_from_the_service_and_a_person_may_mark_a_fax_that_arrived(client):  # noqa: F811
    engine = _engine()
    other = client.post('/diagnostics/test-lines/interpage-us/send', headers=_b()).json()['send']
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE outbound_deliveries SET state = 'success' WHERE id = :id"),
                           {'id': other['fax_id']})
    late = test_lines.one_view(engine, other['id'], now=datetime.utcnow() + timedelta(minutes=31))
    assert late['reply']['state'] == 'no_answer'
    assert late['reply']['sentence'] == ('No answer from the service: Interpage ReFax did not fax back within 30 '
                                         'minutes. This is not a problem with receiving.')
    # A person marks a fax that arrived on the reply number while it waited; one from before is refused.
    before = _received(engine, '+13035550166', datetime.utcnow() - timedelta(hours=1))
    maybe = _received(engine, '+13035550166', datetime.utcnow() + timedelta(minutes=1))
    refused = client.post(f"/diagnostics/test-lines/sends/{other['id']}/reply", headers=_b(),
                          json={'inbound_id': before})
    assert refused.status_code == 400
    marked = client.post(f"/diagnostics/test-lines/sends/{other['id']}/reply", headers=_b(),
                         json={'inbound_id': maybe})
    assert marked.status_code == 200, marked.text
    assert marked.json()['reply']['state'] == 'replied'
    (label,) = client.get('/diagnostics/test-lines/replies', headers=_b()).json()['replies']
    assert label['inbound_id'] == maybe and label['how'] == 'person'


def test_faxbeeps_receipt_is_linked_only_when_exactly_one_fax_fits():
    row = {'created_at': datetime(2026, 10, 10, 6, 0)}

    def answer(*items):
        return lambda url, params: list(items)
    one = {'slug': 'fax_8c93f66b', 'source': 'phone', 'page_count': 1, 'received_at': '2026-10-10T06:03:15Z'}
    found = test_lines.faxbeep_receipt(row, get=answer(one, {**one, 'source': 'email', 'slug': 'fax_aaaaaaaa'},
                                                       {**one, 'page_count': 3, 'slug': 'fax_bbbbbbbb'},
                                                       {**one, 'received_at': '2026-10-10T08:00:00Z',
                                                        'slug': 'fax_cccccccc'}))
    assert found['url'] == 'https://faxbeep.com/faxtest/fax_8c93f66b'
    two = test_lines.faxbeep_receipt(row, get=answer(one, {**one, 'slug': 'fax_dddddddd'}))
    assert two['url'] is None and two['sentence'].startswith('Faxbeep shows 2 one-page faxes from that time')
    assert test_lines.faxbeep_receipt(row, get=answer())['sentence'].startswith('Faxbeep does not show it yet.')
    junk = test_lines.faxbeep_receipt(row, get=answer({**one, 'slug': '../../evil'}))
    assert junk['url'] is None

    def down(url, params):
        raise OSError('unreachable')
    assert test_lines.faxbeep_receipt(row, get=down) == {
        'url': None, 'sentence': 'Faxbeep could not be reached. Try again in a moment.'}


def test_the_reply_check_says_when_a_reply_cannot_reach_faxbot():
    from app.config_values import ConfigurationValues
    base = {'FAX_BACKEND': 'sip', 'FAX_OUTBOUND_BACKEND': 'sip', 'FAX_INBOUND_BACKEND': 'sip', 'INBOUND_ENABLED': 'true',
            'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser', 'SIP_TRUNK_PASSWORD': 'Synthetic-Pass-1',
            'SIP_TRUNK_CALLER_ID': '+13035550109', 'SIP_TRUNK_DIDS': '+13035550101'}
    found = test_lines.reply_check(ConfigurationValues.from_environment(base), None)
    assert found['reaches'] is False and found['sentence'].startswith('Your calls show +13035550109, and Faxbot '
                                                                      'does not receive faxes on it')
    cloud = test_lines.reply_check(ConfigurationValues.from_environment(
        {'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_BACKEND': 'phaxio'}), None)
    assert cloud['reaches'] is None and 'shows its own number' in cloud['sentence']



def test_the_command_line_lists_sends_and_asks_before_allowing_a_country(client):  # noqa: F811
    from typer.testing import CliRunner
    from app.cli.main import app as cli_app
    from api.tests.test_access_management_http import BOOTSTRAP

    def run(*args, given=None):
        return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args], input=given,
                                  obj={'client_factory': lambda address, timeout: (client, False)},
                                  env={'COLUMNS': '220', 'TZ': 'UTC'})
    listed = run('system', 'diagnostics', 'test-lines', 'list')
    assert listed.exit_code == 0, (listed.stdout, listed.stderr)
    text = ' '.join(listed.stdout.split())
    assert 'faxbeep-gb' in text and 'read 9 October 2026' in text and 'exact words were not kept' in text
    # The UK is not allowed yet: a "no" at the question sends nothing and changes nothing.
    before = _count('fax_jobs')
    declined = run('system', 'diagnostics', 'test-lines', 'send', 'faxbeep-gb', '--yes', given='n\n')
    assert declined.exit_code != 0 and _count('fax_jobs') == before
    assert client.get('/diagnostics/test-lines', headers=_b()).json()['lines'][1]['guard']['allowed'] is False
    done = run('system', 'diagnostics', 'test-lines', 'send', 'faxbeep-gb', '--yes', '--allow-country')
    assert done.exit_code == 0, (done.stdout, done.stderr)
    said = ' '.join(done.stdout.split())
    assert 'Faxbot may dial numbers in the United Kingdom.' in said and 'is on its way' in said
    assert _count('fax_jobs') == before + 1
    shown = run('system', 'diagnostics', 'test-lines', 'list')
    assert 'Recent test faxes:' in shown.stdout
    assert run('system', 'diagnostics', 'test-lines', 'send', 'nowhere', '--yes').exit_code != 0
