"""The station check (N5) and the T0 cap: asterisk patch 0007 and routing/stations.py.

The C steps live in asterisk/patches/faxbot_t38_gateway.h; check() here follows the same rules with the same
examples. The HTTP tests run the real POST /fax path of an installation with a SIP trunk and sending turned off,
so nothing is dialed. Numbers are synthetic.
"""
import base64
from datetime import datetime
from pathlib import Path

import pytest

from app import ami, sip_calls
from app.outbound_store import NO_FALLBACK_CATEGORIES
from app.routing import stations
from app.routing.costs import RateCard
from api.tests.test_reply_number import DID_A, DID_B, client  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[2]
TO = '+13035550150'


@pytest.mark.parametrize('csi, expected, found', [
    ('+1 303 555 0150', ['13035550150'], stations.MATCHES),
    ('303-555-0150', ['13035550150'], stations.MATCHES),
    ('5550150', ['13035550150'], stations.MATCHES),
    ('555015', ['13035550150'], stations.NO_SIGNAL),          # under seven digits: no signal
    ('', ['13035550150'], stations.NO_SIGNAL),
    (None, ['13035550150'], stations.NO_SIGNAL),
    ('FAX MACHINE', ['13035550150'], stations.NO_SIGNAL),
    ('+1 720 555 0199', ['13035550150'], stations.DIFFERS),
    ('+1 720 555 0199', ['13035550150', '17205550199'], stations.MATCHES),
    ('+1 720 555 0199', [], stations.NO_SIGNAL),
    ('+1 720 555 0199', ['12345'], stations.NO_SIGNAL),
    ('+44 20 7946 0000', ['442079460000'], stations.MATCHES),
    ('020 7946 0000', ['442079460000'], stations.MATCHES),    # the UK's trunk prefix 0
    ('06 1234 5678', ['390612345678'], stations.MATCHES),     # Italy keeps its 0 abroad
    ('00 44 20 7946 0001', ['442079460000'], stations.DIFFERS),
])
def test_the_check_matches_the_engine_steps(csi, expected, found):
    assert stations.check(csi, expected) == found


def test_patch_0007_carries_the_same_steps_and_applies_after_0006():
    header = (ROOT / 'asterisk' / 'patches' / 'faxbot_t38_gateway.h').read_text()
    patch = (ROOT / 'asterisk' / 'patches' / '0007-faxbot-station-check-t0-cap.patch').read_text()
    for step in ('#define FAXBOT_CSI_MIN_DIGITS 7', 'static inline int faxbot_csi_check(', 'for (; alen > 0 && *a == \'0\'',
                 '#define FAXBOT_T0_MIN_MS 40000', '#define FAXBOT_T0_MAX_MS 60000', 'T1 = 35 +-5 s'):
        assert step in header, step
    assert '--- a/res/res_fax_spandsp.c' in patch
    assert '+\tt30_set_phase_b_handler(p->t30_state, faxbot_phase_b_handler, s);' in patch
    assert '+\treturn p->faxbot_csi_refuse ? T30_ERR_IDENT_UNACCEPTABLE : T30_ERR_OK;' in patch
    assert '+\t\tt30_terminate(p->t30_state);' in patch
    assert '"FAXBOT_CSI_CHECK"' in patch and '"FAXBOT_T0_CAPPED"' in patch
    names = sorted(path.name for path in (ROOT / 'asterisk' / 'patches').glob('*.patch'))
    assert names[-1] == '0007-faxbot-station-check-t0-cap.patch' and names[-2].startswith('0006-')
    dialplan = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    assert 'CsiCheck:${FILTER(abcdefghijklmnopqrstuvwxyz,${FAXBOT_CSI_CHECK})},T0Capped:${FILTER(1,${FAXBOT_T0_CAPPED})}' \
        in dialplan


def card(per_minute=0, per_page=0, increment=60):
    return RateCard(None, 'sip-telnyx', 'outbound', 'Telnyx', 'USD', per_minute, per_page, 0, increment, increment,
                    None, datetime(2026, 10, 7))


def test_the_t0_cap_applies_only_to_calls_billed_by_the_minute_in_steps_of_60_seconds_or_more():
    assert stations.bills_by_minute(card(per_minute=5000, increment=60))
    assert stations.bills_by_minute(card(per_minute=5000, increment=120))
    assert not stations.bills_by_minute(card(per_minute=5000, increment=6))
    assert not stations.bills_by_minute(card(per_page=70000))
    assert not stations.bills_by_minute(None)
    assert stations.T0_CAP_MS == 50000
    # The originate fields carry it only inside T.30's bounds: never under T1's longest (40 s), never over T0.
    fields = ami.prepare_originate_fields('job1', TO, '/faxdata/job1.tiff', caller_id=DID_A, t0_ms=50000)
    assert 'FAXBOT_T0_MS=50000' in fields['Variable']
    for wrong in (39999, 60001, 50000.0, '50000'):
        with pytest.raises(ValueError):
            ami.prepare_originate_fields('job1', TO, '/faxdata/job1.tiff', caller_id=DID_A, t0_ms=wrong)
    fields = ami.prepare_originate_fields('job1', TO, '/faxdata/job1.tiff', caller_id=DID_A,
                                          csi_expect='13035550150.17205550199', csi_refuse=True)
    assert 'FAXBOT_CSI_EXPECT=13035550150.17205550199' in fields['Variable']
    assert 'FAXBOT_CSI_REFUSE=yes' in fields['Variable']
    with pytest.raises(ValueError):
        ami.prepare_originate_fields('job1', TO, '/faxdata/job1.tiff', caller_id=DID_A, csi_expect='1303;rm')


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def test_a_refused_station_is_a_definite_failure_no_route_takes_and_the_cap_reads_as_no_fax_answer():
    refused = {'Answered': '1760000000', 'Status': 'FAILED', 'Pages': '0', 'Mode': 'T38', 'CsiCheck': 'refused',
               'Station64': _b64('+1 720 555 0199'), 'Error': "Far end's ident is not acceptable"}
    assert sip_calls.verdict(refused) == sip_calls.WRONG_STATION
    assert sip_calls.category_for(sip_calls.WRONG_STATION) == 'wrong_station' in NO_FALLBACK_CATEGORIES
    assert sip_calls.verdict_sentence(sip_calls.WRONG_STATION) == sip_calls.STATION
    # Faxbot ended the call at the cap with sound coming back: like T0 running out, not a person who hung up.
    capped = {'Answered': '1760000000', 'Status': 'FAILED', 'Pages': '0', 'Mode': 'audio', 'RtpRx': '2400',
              'T0Capped': '1', 'Error': 'The call dropped prematurely'}
    assert sip_calls.verdict(capped) == 'no_fax_answer'
    assert sip_calls.verdict({**capped, 'T0Capped': ''}) == sip_calls.PERSON_ANSWERED


def test_the_work_item_asks_to_confirm_the_number_with_another_machine_in_the_script():
    from app.work import certainty_checks, certainty_service
    assert certainty_service.CATEGORY_TEXT['wrong_station'].startswith('This number answered as another fax machine')
    found = certainty_checks.number_check({'reference': 'ACD34X'}, organization='Example Clinic',
                                          number='+1 303 555 0150', when_text='October 10', station=True)
    assert 'answered as another fax machine' in ' '.join(found['script'])


# -- the real paths --------------------------------------------------------------------------------------------------

def _send(test_client, **fields):
    from api.tests.test_access_management_http import B
    sent = test_client.post('/fax', headers=B, data={'to': TO, **fields},
                            files={'file': ('note.txt', b'Synthetic page', 'text/plain')})
    assert sent.status_code == 202, sent.text
    return sent.json()['id']


def _variables(fields):
    return dict(item.split('=', 1) for item in fields['Variable'].split(',') if '=' in item and '"' not in item)


def test_a_call_expects_the_dialled_number_and_learned_stations_and_refuses_only_when_chosen(client):  # noqa: F811
    from api.tests.test_access_management_http import B
    from app.main import app
    engine = app.state.configuration_runtime.manager.store.engine
    running = app.state.configuration_runtime.manager.store.read().active.values
    first = _send(client)
    fields = _variables(ami.originate_fields_for(running, first, TO, '/faxdata/first.tiff'))
    assert fields['FAXBOT_CSI_EXPECT'] == '13035550150' and 'FAXBOT_CSI_REFUSE' not in fields
    # Telnyx's shipped card bills by the minute in 60 s steps: the cap is on.
    assert fields['FAXBOT_T0_MS'] == '50000'
    # A successful call that answered as another station teaches it; a short one teaches nothing.
    assert stations.after_call(engine, job_id=first, attempt_id=None, station='+1 720 555 0199', succeeded=True) is None
    stations.after_call(engine, job_id=first, attempt_id=None, station='FAX', succeeded=True)
    saved = client.put(f'/routing/stations/{TO}', headers=B, json={'mode': 'refuse', 'station': '303 555 0177'})
    assert saved.status_code == 200, saved.text
    assert saved.json()['sentence'] == ('Saved. When this number answers as another fax machine, Faxbot hangs up '
                                        'before any page. Faxbot expects this number to answer as that station too.')
    assert {item['source'] for item in saved.json()['stations']} == {'call', 'person'}
    second = _send(client)
    fields = _variables(ami.originate_fields_for(running, second, TO, '/faxdata/second.tiff'))
    assert set(fields['FAXBOT_CSI_EXPECT'].split('.')) == {'13035550150', '17205550199', '3035550177'}
    assert fields['FAXBOT_CSI_REFUSE'] == 'yes'


def test_a_mailbox_choice_applies_until_the_recipient_has_its_own(client):  # noqa: F811
    from api.tests.test_access_management_http import B
    from api.tests.test_work_http import mailbox
    from app.main import app
    running = app.state.configuration_runtime.manager.store.read().active.values
    billing = mailbox(client, 'Billing', DID_B)
    chosen = client.put(f"/routing/stations/mailboxes/{billing['id']}", headers=B, json={'mode': 'refuse'})
    assert chosen.status_code == 200, chosen.text
    boxed = _send(client, mailbox=billing['id'])
    assert _variables(ami.originate_fields_for(running, boxed, TO, '/faxdata/boxed.tiff'))['FAXBOT_CSI_REFUSE'] == 'yes'
    assert client.put(f'/routing/stations/{TO}', headers=B, json={'mode': 'warn'}).status_code == 200
    again = _send(client, mailbox=billing['id'])
    assert 'FAXBOT_CSI_REFUSE' not in _variables(ami.originate_fields_for(running, again, TO, '/faxdata/again.tiff'))
    assert client.put('/routing/stations/mailboxes/nowhere', headers=B, json={'mode': 'refuse'}).status_code == 400


def test_sent_details_name_the_station_and_say_when_the_check_came_after_the_call(client):  # noqa: F811
    from api.tests.test_access_management_http import B
    from app.main import app
    import sqlalchemy as sa
    engine = app.state.configuration_runtime.manager.store.engine
    job = _send(client)
    attempts = sa.table('outbound_attempts', sa.column('id'), sa.column('job_id'), sa.column('sequence'),
                        sa.column('phase'), sa.column('created_at'))
    with engine.begin() as connection:
        connection.execute(attempts.insert().values(id='a' * 32, job_id=job, sequence=1, phase='completed',
                                                    created_at=datetime(2026, 10, 10, 9, 0)))
        connection.execute(attempts.insert().values(id='b' * 32, job_id=job, sequence=2, phase='completed',
                                                    created_at=datetime(2026, 10, 10, 9, 5)))
    assert stations.after_call(engine, job_id=job, attempt_id='a' * 32, station='+1 720 555 0199', succeeded=False,
                               check_result='refused') == 'refused'
    # The SSL Fax engine's call is checked after it; the station differed, so it is not learned either.
    assert stations.after_call(engine, job_id=job, attempt_id='b' * 32, station='+1 720 555 0188', succeeded=True,
                               engine_name='sslfax') == 'differs'
    sentences = client.get(f'/routing/stations/faxes/{job}', headers=B).json()['sentences']
    assert sentences == [
        'The number answered as +1 720-555-0199, a fax machine Faxbot did not expect there, so Faxbot hung up before '
        'any page. Check the number with the recipient.',
        'The number answered as +1 720-555-0188, not the fax machine Faxbot expected there. The SSL Fax engine can '
        'only check this after the call, so the fax was sent.']
    assert [item['station'] for item in client.get(f'/routing/stations/{TO}', headers=B).json()['stations']] == []


def test_the_command_line_sets_the_check_for_a_recipient_and_a_mailbox(client):  # noqa: F811
    from typer.testing import CliRunner
    from app.cli.main import app as cli_app
    from api.tests.test_access_management_http import BOOTSTRAP
    from api.tests.test_work_http import mailbox

    def run(*args):
        return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args],
                                  obj={'client_factory': lambda address, timeout: (client, False)},
                                  env={'COLUMNS': '220', 'TZ': 'UTC'})
    done = run('recipients', 'set', TO, '--station-check', 'refuse', '--expected-station', '+1 303 555 0177')
    assert done.exit_code == 0, (done.stdout, done.stderr)
    assert 'Faxbot expects this number to answer as that station too.' in ' '.join(done.stdout.split())
    mailbox(client, 'Billing', DID_B)
    boxed = run('numbers', 'reply', 'station-check', 'refuse', '--mailbox', 'Billing')
    assert boxed.exit_code == 0, (boxed.stdout, boxed.stderr)
    assert 'on a fax from Billing, Faxbot hangs up before any page.' in ' '.join(boxed.stdout.split())
    assert run('recipients', 'set', TO, '--station-check', 'maybe').exit_code != 0
