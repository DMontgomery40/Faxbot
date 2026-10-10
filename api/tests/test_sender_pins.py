"""Registered senders (N17): a recipient that recognises faxes by their sending number gets them only from its
registered trunk, showing the registered caller ID and station ID, or they wait in Sent (SQLite, PostgreSQL).

The rules-delivery harness accepts faxes the way POST /fax does and runs the real worker, transport, planner and
holds; the provider double records which account each submission used. Synthetic numbers only. Not yet run against
a real recipient that checks the sending number.
"""
from datetime import datetime
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.config_values import ConfigurationValues
from api.app.outbound_worker import OutboundWorker
from api.app.routing import envelope as envelopes, sender_pins
from api.app.routing.holds import HoldStore
from api.app.routing.transport import RoutedTransport
from api.tests.test_rules_delivery import ANNE, BASE, Inner, accept, holds, installation, publish, rule
from api.tests.test_schema import database  # noqa: F401 (fixture)


BANK = '+902122220000'
REGISTERED, OTHER = '+13035550100', '+13035550142'
# An installation in Turkey, so the bank is a national number the dialing guard (routing/guard.py) lets it dial.
TRUNK = {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': 'sip.telnyx.com',
         'SIP_TRUNK_DIDS': f'{REGISTERED},{OTHER}', 'FAX_DEFAULT_COUNTRY': 'TR'}
WITH_TRUNK = {**BASE, **TRUNK, 'SIP_TRUNK_CALLER_ID': REGISTERED, 'FAX_OUTBOUND_ROUTES': 'signalwire, sip'}


@pytest.fixture
def trunked(database, tmp_path):  # noqa: F811
    return installation(database, tmp_path, WITH_TRUNK)


def values(**extra):
    return ConfigurationValues.from_environment({**WITH_TRUNK, **extra})


def pin(env, *, caller=REGISTERED, station=None, settings=None):
    return sender_pins.record(env.engine, settings or values(), BANK, account='sip', caller_id=caller,
                              station_id=station, note='Registered with the bank, form 7.4', actor={'name': 'Anne'})


def test_a_pin_needs_a_trunk_that_shows_the_registered_number_now(trunked):
    with pytest.raises(sender_pins.PinError, match='sets its own sending number'):
        sender_pins.record(trunked.engine, values(), BANK, account='signalwire', caller_id=REGISTERED)
    with pytest.raises(sender_pins.PinError, match=re.escape(f'shows {REGISTERED} as caller ID now, not {OTHER}')):
        pin(trunked, caller=OTHER)
    with pytest.raises(sender_pins.PinError, match='station ID now, not'):
        pin(trunked, station='+90 212 000 0000')
    with pytest.raises(sender_pins.PinError, match='country code'):
        sender_pins.record(trunked.engine, values(), '2122220000', account='sip', caller_id=REGISTERED)
    saved = pin(trunked, station=REGISTERED)
    assert (saved.account, saved.caller_id, saved.station, saved.recorded_by) == ('sip', REGISTERED, REGISTERED, 'Anne')
    # A trunk whose caller ID is now its other number cannot carry the pin: the check follows what the call shows.
    with pytest.raises(sender_pins.PinError, match=re.escape(f'shows {OTHER} as caller ID now')):
        pin(trunked, settings=values(SIP_TRUNK_CALLER_ID=OTHER))
    removed = sender_pins.remove(trunked.engine, BANK, note='Bank account closed')
    assert removed.caller_id == REGISTERED and sender_pins.current(trunked.engine, BANK) is None
    with pytest.raises(sender_pins.PinError):
        sender_pins.remove(trunked.engine, BANK)
    with trunked.engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM registered_sender_pins').scalar() == 2


def test_with_no_rules_a_pinned_recipient_gets_only_its_trunk_by_a_plain_strict_call(trunked):
    automatic = envelopes.load(trunked.engine, accept(trunked, to=BANK))
    assert automatic.envelope.mode == 'automatic' and 'signalwire' in automatic.envelope.accounts
    pin(trunked)
    pinned = envelopes.load(trunked.engine, accept(trunked, to=BANK))
    envelope = pinned.envelope
    assert pinned.decision.outcome == 'route' and pinned.strict
    assert (envelope.mode, envelope.accounts, envelope.local, envelope.direct, envelope.dial, envelope.alternate,
            envelope.page_layout) == ('one', ('sip',), False, False, None, 'never', 'one_per_sheet')
    other = envelopes.load(trunked.engine, accept(trunked))
    assert other.envelope.mode == 'automatic'  # every other recipient: unchanged


def test_a_limit_on_the_pinned_trunk_blocks_the_fax_instead_of_using_another(trunked):
    pin(trunked)
    publish(trunked, {'format': 1, 'limits': [rule('l-trunk', {'never': ['sip']})]})
    job = accept(trunked, to=BANK)
    decision = envelopes.load(trunked.engine, job).decision
    assert (decision.outcome, decision.reason, decision.envelope.accounts) == ('blocked', 'no_allowed_account', ())
    assert [item['kind'] for item in holds(trunked, job)] == ['no_route']


@pytest.mark.asyncio
async def test_a_trunk_that_would_not_show_the_registered_number_holds_the_fax_and_no_other_account_sends(trunked):
    """No rules published. The fax was accepted while the trunk showed the registered number; then the trunk's caller
    ID moved to its other number. Dispatch refuses every route, so the fax waits in Sent with one sentence and
    "send anyway" offers nothing: never another number."""
    pin(trunked)
    job = accept(trunked, to=BANK)
    revision, _ = trunked.configuration.outbound_context(job)
    moved = revision.values.with_patch({'sip_trunk_caller_id': OTHER})
    assert sender_pins.dispatch_refusal(trunked.engine, moved, BANK, 'sip') == sender_pins.SKIP
    assert sender_pins.dispatch_refusal(trunked.engine, revision.values, BANK, 'signalwire') == sender_pins.SKIP
    assert sender_pins.dispatch_refusal(trunked.engine, revision.values, BANK, 'sip') is None
    assert sender_pins.dispatch_refusal(trunked.engine, moved, '+12025550123', 'signalwire') is None
    from api.app.routing import transport
    original = transport.RoutedTransport._assign

    def assign(self, claim, plan, revision):  # the fax's settings as they would be after the caller ID moved
        return original(self, claim, plan, SimpleNamespace(values=moved))
    transport.RoutedTransport._assign = assign
    try:
        inner = Inner(trunked.delivery)
        assert await OutboundWorker(trunked.delivery, RoutedTransport(inner, direct=None)).step() is False
    finally:
        transport.RoutedTransport._assign = original
    assert inner.used == [] and trunked.delivery.get(job)['state'] == 'ready'
    [hold] = holds(trunked, job)
    assert hold['kind'] == 'no_route'
    assert 'does not show the caller ID and station ID this recipient has registered' in hold['reason']
    view = HoldStore(trunked.delivery).view(dict(hold, to_number=BANK), ANNE, approver=True)
    assert view['options'] == []


@pytest.mark.asyncio
async def test_when_the_trunk_is_not_ready_send_anyway_offers_only_the_registered_trunk(trunked, monkeypatch):
    from api.app.routing import transport
    pin(trunked)
    job = accept(trunked, to=BANK)
    monkeypatch.setattr(transport, 'route_ready', lambda configuration, ami=None: False)
    inner = Inner(trunked.delivery)
    assert await OutboundWorker(trunked.delivery, RoutedTransport(inner, direct=None)).step() is False
    assert inner.used == []
    [hold] = holds(trunked, job)
    view = HoldStore(trunked.delivery).view(dict(hold, to_number=BANK), ANNE, approver=True)
    assert [item['account'] for item in view['options']] == ['sip']
    assert sender_pins.pinned_options(trunked.engine, job, [{'account': 'signalwire'}, {'account': 'sip'}]) == [
        {'account': 'sip'}]


def _call(env, job, *, station='BANK CSI 1', caller=REGISTERED):
    table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=env.engine)
    now = datetime.utcnow()
    with env.engine.begin() as connection:
        connection.execute(table.insert().values(
            id=uuid4().hex, direction='outbound', call_id=uuid4().hex, job_id=job, attempt_id=None,
            trunk_preset='telnyx', did=None, caller=caller, called=BANK, started_at=now, answered_at=now,
            ended_at=now, disposition='answered', connected_seconds=61, t38='no', pages=3, fax_status='success',
            remote_station_id=station, error_cause=None, fax_preference=0, created_at=now, updated_at=now))


def test_the_senders_evidence_is_kept_with_the_answering_station_and_requests_for_the_original(trunked):
    before = accept(trunked, to=BANK)
    pin(trunked)
    job = accept(trunked, to=BANK)
    (trunked.tmp / f'{job}.tiff').write_bytes(b'II*\x00synthetic')
    _call(trunked, job)
    assert sender_pins.kept_jobs(trunked.engine, [before, job]) == {job}
    found = sender_pins.evidence(trunked.engine, job, fax_data_dir=str(trunked.tmp))
    assert found['pin']['caller_id'] == REGISTERED and found['kept'] == ['fax image']
    [call] = found['calls']
    assert (call['answering_station'], call['caller_id'], call['matches_pin']) == ('BANK CSI 1', REGISTERED, True)
    assert sender_pins.evidence(trunked.engine, before) is None
    with pytest.raises(sender_pins.PinError, match='request for the original first'):
        sender_pins.record_original(trunked.engine, job, 'sent')
    sender_pins.record_original(trunked.engine, job, 'requested', note='Bank asked by phone')
    with pytest.raises(sender_pins.PinError, match='already requested'):
        sender_pins.record_original(trunked.engine, job, 'requested')
    done = sender_pins.record_original(trunked.engine, job, 'sent', note='Couriered')
    assert [item['state'] for item in done['originals']] == ['requested', 'sent'] and done['original'] == 'sent'
    with pytest.raises(sender_pins.PinError, match='registered-sender recipient'):
        sender_pins.record_original(trunked.engine, before, 'requested')
    # Removing the pin later keeps what was sent under it as evidence.
    sender_pins.remove(trunked.engine, BANK)
    assert sender_pins.kept_jobs(trunked.engine, [job]) == {job}


@pytest.fixture
def pins_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    environment = {**TRUNK, 'SIP_TRUNK_CALLER_ID': REGISTERED, 'FAX_OUTBOUND_ROUTES': 'sip'}
    for client in _serve(monkeypatch, tmp_path, FAX_BACKEND='sip', FAX_OUTBOUND_BACKEND='sip', **environment):
        yield Cli(client)


def test_the_console_routes_and_the_command_line_register_list_and_remove_a_sender(pins_cli):
    from api.tests.test_cli import BOOTSTRAP
    client, admin = pins_cli.client, {'X-API-Key': BOOTSTRAP}
    empty = client.get('/routing/sender-pins', headers=admin).json()
    assert empty['pins'] == [] and empty['trunks'][0]['caller_id'] == REGISTERED
    refused = pins_cli('providers', 'trunk', 'register-sender', BANK, '--caller-id', OTHER)
    assert refused.exit_code != 0 and f'not {OTHER}' in refused.stdout + refused.stderr
    saved = pins_cli('providers', 'trunk', 'register-sender', BANK, '--caller-id', REGISTERED, '--note', 'Form 7.4')
    assert saved.exit_code == 0, saved.stdout + saved.stderr
    assert f'Faxes to {BANK} go only by' in saved.stdout and f'showing {REGISTERED}' in saved.stdout
    listed = pins_cli.json('providers', 'trunk', 'registered-senders')
    assert [(item['recipient'], item['ready'], item['note']) for item in listed['pins']] == [(BANK, True, 'Form 7.4')]
    missing = client.get(f'/routing/faxes/{"c" * 32}/sender-evidence', headers=admin)
    assert missing.status_code == 404
    assert pins_cli('providers', 'trunk', 'unregister-sender', BANK).exit_code == 0
    assert client.get('/routing/sender-pins', headers=admin).json()['pins'] == []
    assert client.delete(f'/routing/sender-pins/{BANK}', headers=admin).status_code == 404
    assert client.get('/routing/sender-pins', headers={'X-API-Key': 'wrong'}).status_code in (401, 403)


@pytest.fixture
def companyflex_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    from api.tests.test_sip_access import COMPANYFLEX
    environment = {key: value for key, value in COMPANYFLEX.items() if key != 'FAX_BACKEND'}
    for client in _serve(monkeypatch, tmp_path, FAX_BACKEND='sip', FAX_OUTBOUND_BACKEND='sip', **environment):
        yield Cli(client)


def test_the_command_line_saves_the_telekom_line_and_says_why_calls_are_encrypted(companyflex_cli):
    before = companyflex_cli.json('providers', 'trunk', 'own-access')
    assert (before['own_access'], before['access'], before['media_encryption']) == ('', 'unknown', 'sdes')
    shown = companyflex_cli('providers', 'trunk', 'own-access', '198.51.100.7')
    assert shown.exit_code == 0, shown.stdout + shown.stderr
    assert 'Your own line: 198.51.100.7.' in shown.stdout and 'has not seen your Telekom line yet' in shown.stdout
