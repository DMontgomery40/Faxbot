"""Junk senders turned away before answer (M14): the dialplan step, the list, Asterisk's copy and the log.

Synthetic numbers only (555 exchange, UK drama numbers). Asterisk's database is a fake that answers
DBGetTree, DBPut and DBDel as Asterisk 22 does; the native proof (a blocked caller gets 603 and is never
answered) is in test_t38_loopback.py.
"""
import asyncio
from calendar import timegm
from datetime import datetime, timedelta
from pathlib import Path
import re

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.inbound import screening
from app.schema import create_database_engine, upgrade_schema

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 7, 12, 0)
CALLER = '+13035550142'


# -- the dialplan ---------------------------------------------------------------------------

def _context(name):
    text = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    match = re.search(rf'^\[{re.escape(name)}\]\n(.*?)(?=^\[|\Z)', text, re.S | re.M)
    assert match, name
    return [line for line in match.group(1).splitlines() if line.strip() and not line.startswith(';')]


def test_the_screen_runs_after_the_caller_is_read_and_before_anything_can_answer():
    lines = _context('faxbot-inbound-receive')
    position = {name: next(i for i, line in enumerate(lines) if marker in line)
                for name, marker in (('caller', 'Set(FAXBOT_CALLER='), ('screen', 'Gosub(faxbot-screen,s,1)'),
                                     ('options', 'Gosub(faxbot-options,s,1)'), ('engine', 'Gosub(faxbot-engine-in'),
                                     ('answer', 'Answer()'), ('receive', 'ReceiveFAX('))}
    assert position['caller'] < position['screen'] < position['options'] < position['engine'] < position['answer']
    assert position['answer'] < position['receive']
    # A screened call jumps to the reject, which declines it (603) without answering.
    assert '"${FAXBOT_SCREENED}" = "1"]?screened' in lines[position['screen'] + 1]
    rejected = next(i for i, line in enumerate(lines) if '(screened)' in line)
    assert 'PJSIPHangup(603)' in lines[rejected] and 'Hangup(21)' in lines[rejected + 1]
    assert not any('Answer' in line or 'Dial(' in line for line in lines[rejected:])


def test_the_screen_skips_callers_without_a_number_and_withheld_numbers_and_honours_expiry():
    lines = _context('faxbot-screen')
    text = '\n'.join(lines)
    assert 'FAXBOT_SCREEN_KEY=${FILTER(0123456789,${FAXBOT_CALLER})}' in text
    assert '${LEN(${FAXBOT_SCREEN_KEY})} < 3]?done' in text  # anonymous: no digits
    assert '"${CALLERID(num-pres):0:6}" = "prohib"]?done' in text  # withheld: never screened
    assert 'DB_EXISTS(faxbot-screen/${FAXBOT_SCREEN_KEY})' in text
    assert '$[0${FILTER(0123456789,${DB_RESULT})} <= ${EPOCH}]?done' in text  # expired in Asterisk itself
    # The rejection is queued before it is announced, so it is recorded even if Faxbot is not connected.
    queue = next(i for i, line in enumerate(lines) if 'Set(DB(faxbot-screened/' in line)
    event = next(i for i, line in enumerate(lines) if 'UserEvent(FaxScreened' in line)
    assert queue < event


# -- numbers and keys -----------------------------------------------------------------------

def test_each_entry_is_keyed_every_way_a_carrier_presents_the_number():
    assert screening.keys_for('+13035550142') == {'13035550142', '3035550142'}
    assert screening.keys_for('+441632960001') == {'441632960001', '01632960001', '1632960001'}
    assert screening.read_number('(303) 555-0142') == CALLER
    assert screening.read_number('+1 303 555 0142') == CALLER


@pytest.mark.parametrize('presented', ['', 'anonymous', 'Restricted', '   '])
def test_a_caller_with_no_number_can_never_be_blocked(presented):
    with pytest.raises(screening.ScreeningRefused, match="sent no number"):
        screening.read_number(presented)


def test_the_latest_expiry_wins_when_two_entries_share_a_key():
    entries = [{'number': CALLER, 'expires_at': NOW + timedelta(days=1)},
               {'number': CALLER, 'expires_at': NOW + timedelta(days=90)}]
    keys = screening.desired_keys(entries)
    assert set(keys) == {'13035550142', '3035550142'}
    assert keys['3035550142'] == keys['13035550142'] == str(timegm((NOW + timedelta(days=90)).timetuple()))


def test_a_queued_rejection_is_read_and_anything_else_is_skipped():
    at = timegm(NOW.timetuple())
    rejection = screening.parse_queued(f'/faxbot-screened/{at}.{at}.17',
                                       '+13035550142:+13035550100:Y2FsbC0xQGV4YW1wbGU=')
    assert (rejection.number, rejection.called, rejection.call_id) == (CALLER, '+13035550100', 'call-1@example')
    assert rejection.at == NOW
    assert screening.parse_queued('/faxbot-screened/bad key', 'x') is None
    assert screening.parse_queued(f'/faxbot-screened/{at}.1', '') is None


# -- the list ---------------------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path):
    database = create_database_engine('sqlite:///' + str(tmp_path / 'screening.db'))
    upgrade_schema(database)
    yield database
    database.dispose()


def test_entries_expire_after_90_days_and_are_removed_once_with_who_and_when(engine):
    store = screening.ScreeningStore(engine)
    entry = store.add(CALLER, '  Junk   offers ', actor_id='p1', actor_name='Dana Admin', inbound_fax_id='fax-1', now=NOW)
    assert entry['reason'] == 'Junk offers' and entry['expires_at'] == NOW + timedelta(days=90)
    assert [row['id'] for row in store.active(NOW + timedelta(days=89))] == [entry['id']]
    assert store.active(NOW + timedelta(days=90)) == []  # expired
    removed = store.remove(entry['id'], actor_id='p2', actor_name='Lee Admin', now=NOW + timedelta(hours=1))
    assert (removed['removed_by'], removed['removed_by_name']) == ('p2', 'Lee Admin')
    again = store.remove(entry['id'], actor_id='p3', actor_name='Someone Else', now=NOW + timedelta(hours=2))
    assert again['removed_by_name'] == 'Lee Admin' and again['removed_at'] == NOW + timedelta(hours=1)
    assert store.active(NOW + timedelta(hours=3)) == []
    # The row stays: who blocked whom, and who removed it, is history.
    assert [row['id'] for row in store.entries()] == [entry['id']]
    with pytest.raises(screening.ScreeningRefused, match='Say why'):
        store.add(CALLER, '   ', now=NOW)


def test_each_rejected_call_is_recorded_once_and_linked_to_the_entry_it_matched(engine):
    store = screening.ScreeningStore(engine)
    entry = store.add(CALLER, 'Junk', now=NOW)
    rejection = screening.Rejection('1791374400.1791374400.17', '3035550142', '+13035550100', 'call-1', NOW)
    assert store.record_rejection(rejection) is True
    assert store.record_rejection(rejection) is False
    [row] = store.rejections()
    assert (row['entry_id'], row['number'], row['called']) == (entry['id'], '3035550142', '+13035550100')
    assert store.counts() == {entry['id']: 1}


# -- Asterisk's copy --------------------------------------------------------------------------

class FakeAsterisk:
    """Asterisk's database over the manager connection: DBGetTree, DBPut, DBDel."""

    def __init__(self, database=None):
        self.database = dict(database or {})
        self.calls = []

    async def status_query(self, fields, *, collect=False):
        assert fields['Action'] == 'DBGetTree' and collect
        family = fields['Family']
        events = [{'Key': key, 'Val': value} for key, value in sorted(self.database.items())
                  if key.startswith(f'/{family}/')]
        return {'response': 'Success', 'value': '', 'message': ''}, events

    async def db_put(self, family, key, value):
        self.calls.append(('put', family, key))
        self.database[f'/{family}/{key}'] = value

    async def db_del(self, family, key):
        self.calls.append(('del', family, key))
        self.database.pop(f'/{family}/{key}', None)


def test_sync_makes_asterisks_keys_exactly_the_active_entries_and_restores_them_after_a_restart(engine):
    store = screening.ScreeningStore(engine)
    now = datetime.utcnow()
    store.add(CALLER, 'Junk', now=now)
    expired = store.add('+13035550143', 'Junk', days=1, now=now - timedelta(days=2))
    removed = store.add('+13035550144', 'Junk', now=now)
    store.remove(removed['id'], now=now)
    # Left over from before: an expired key and a removed one; and the engine family is never touched.
    asterisk = FakeAsterisk({'/faxbot-screen/13035550143': '1', '/faxbot-screen/13035550144': '9999999999',
                             '/faxbot-engine/1234567890123456': 'plan'})
    assert expired['expires_at'] < now
    result = asyncio.run(screening.sync(asterisk, store))
    screen = {key.rsplit('/', 1)[-1] for key in asterisk.database if key.startswith('/faxbot-screen/')}
    assert screen == {'13035550142', '3035550142'} and result == {'added': 2, 'removed': 2}
    assert '/faxbot-engine/1234567890123456' in asterisk.database
    # Nothing to change: nothing written.
    asterisk.calls.clear()
    assert asyncio.run(screening.sync(asterisk, store)) == {'added': 0, 'removed': 0} and asterisk.calls == []
    # Asterisk restarted with an empty database: the next sync puts everything back.
    asterisk.database = {}
    asyncio.run(screening.sync(asterisk, store))
    assert {key.rsplit('/', 1)[-1] for key in asterisk.database} == {'13035550142', '3035550142'}


def test_queued_rejections_are_recorded_once_then_taken_off_the_queue(engine):
    store = screening.ScreeningStore(engine)
    store.add(CALLER, 'Junk', now=datetime(2026, 10, 1))
    at = timegm(NOW.timetuple())
    asterisk = FakeAsterisk({f'/faxbot-screened/{at}.{at}.17': '13035550142:+13035550100:',
                             '/faxbot-screened/garbage': 'x'})
    assert asyncio.run(screening.drain(asterisk, store)) == 1
    assert not any(key.startswith('/faxbot-screened/') for key in asterisk.database)
    assert [row['number'] for row in store.rejections()] == ['13035550142']


# -- the console's and the command line's API ---------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from api.tests.test_access_management_http import ORIGIN, _environment
    from app.main import app
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


def _received(from_number):
    from app.main import app
    import uuid
    identity = uuid.uuid4().hex
    moment = datetime.utcnow()
    app.state.access_runtime.inbound.accept(dict(
        id=identity, from_number=from_number, to_number='+13035550100', status='received', backend='sip', pages=1,
        created_at=moment, received_at=moment, updated_at=moment), country='US')
    return identity


def test_mark_sender_as_junk_blocks_the_faxs_sender_with_an_audit_row_and_unblock_keeps_history(client):
    from api.tests.test_access_management_http import B
    from app.main import app
    fax = _received(CALLER)
    blocked = client.post('/screening/senders', headers=B, json={'inbound_id': fax, 'reason': 'Unsolicited offers'})
    assert blocked.status_code == 200, blocked.text
    entry = blocked.json()['entry']
    assert entry['number'] == CALLER and entry['active'] and entry['inbound_id'] == fax
    view = client.get('/screening', headers=B).json()
    assert [item['id'] for item in view['entries']] == [entry['id']]
    assert view['sentence'].startswith('Blocking works for faxes received over your SIP trunk.')
    engine = app.state.configuration_runtime.manager.store.engine
    audit = sa.Table('access_audit', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        rows = connection.execute(sa.select(audit.c.operation, audit.c.target_id, audit.c.details)
                                  .where(audit.c.operation.like('screening.%'))).all()
    assert [(row.operation, row.target_id) for row in rows] == [('screening.block', entry['id'])]
    assert '"reason":"Unsolicited offers"' in rows[0].details and CALLER in rows[0].details
    removed = client.delete(f"/screening/senders/{entry['id']}", headers=B)
    assert removed.status_code == 200 and removed.json()['entry']['active'] is False
    assert client.delete('/screening/senders/missing', headers=B).status_code == 404
    listed = client.get('/screening', headers=B).json()['entries']
    assert listed[0]['removed_at'] is not None


def test_a_fax_with_no_sender_number_cannot_be_marked_as_junk(client):
    from api.tests.test_access_management_http import B
    fax = _received('')
    refused = client.post('/screening/senders', headers=B, json={'inbound_id': fax, 'reason': 'Junk'})
    assert refused.status_code == 400 and 'sent no number' in refused.json()['detail']
    assert client.post('/screening/senders', headers=B, json={'number': CALLER, 'inbound_id': fax,
                                                               'reason': 'Junk'}).status_code == 400
    assert client.post('/screening/senders', headers=B, json={'number': CALLER, 'reason': 'Junk',
                                                               'days': 400}).status_code == 422


def test_blocked_senders_need_settings_permissions(client):
    assert client.get('/screening').status_code in (401, 403)
    assert client.post('/screening/senders', json={'number': CALLER, 'reason': 'Junk'}).status_code in (401, 403)


# -- faxbot numbers blocked / faxbot received block ---------------------------------------------

def test_the_command_line_blocks_lists_and_unblocks(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for client in _serve(monkeypatch, tmp_path, INBOUND_ENABLED='true'):
        cli = Cli(client)
        fax = _received(CALLER)
        blocked = cli('received', 'block', fax, '--reason', 'Unsolicited offers')
        assert blocked.exit_code == 0, (blocked.stdout, blocked.stderr)
        assert blocked.stdout.startswith('Calls from +13035550142 are turned away until ')
        added = cli('numbers', 'blocked', 'add', '+1 303 555 0143', '--reason', 'Junk', '--days', '7')
        assert added.exit_code == 0, (added.stdout, added.stderr)
        listed = cli('numbers', 'blocked', 'list')
        assert listed.exit_code == 0 and 'Unsolicited offers' in listed.stdout and '+13035550143' in listed.stdout
        removed = cli('numbers', 'blocked', 'remove', '303-555-0143')
        assert removed.exit_code == 0 and '+13035550143 is no longer blocked.' in removed.stdout
        assert [entry['number'] for entry in cli.json('numbers', 'blocked', 'list')['entries']] == [CALLER]
        assert len(cli.json('numbers', 'blocked', 'list', '--all')['entries']) == 2
        missing = cli('numbers', 'blocked', 'remove', '+13035550199')
        assert missing.exit_code != 0 and 'is not blocked' in missing.stderr
