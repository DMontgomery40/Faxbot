"""The answer cap's switch and its sentences (routing/stations.py, asterisk patch 0007).

Each trunk has its own switch, on by default. Faxbot uses it only where the trunk's calls are billed by the minute
in steps of 60 seconds or more; the sentence beside the switch says which, and Sent details say when the cap ended a
call, with the billed step the call fitted in, from the billing step kept when the result arrived. Synthetic
numbers and documentation addresses only.
"""
from datetime import datetime

import pytest

from app import ami
from app.routing import stations
from app.routing.costs import RateCard
from api.tests.test_reply_number import DID_A, client  # noqa: F401 - fixture
from api.tests.test_sip_trunks import JOB, ATTEMPT, trunk_values


def card(per_minute=5000, per_page=0, increment=60, minimum=60):
    return RateCard(None, 'sip-telnyx', 'outbound', 'Telnyx', 'USD', per_minute, per_page, 0, increment, minimum,
                    None, datetime(2026, 10, 7))


@pytest.mark.parametrize('found, on_text, off_text', [
    (card(), 'and the call is billed as one minute instead of two.', 'is billed as two minutes.'),
    (card(increment=120, minimum=120), 'before a longer wait is billed.', 'is billed for that whole wait.'),
    (card(increment=6, minimum=6), 'your carrier bills its calls in 6-second steps', 'Off: Faxbot waits'),
    (card(per_minute=0, per_page=70000), 'does not bill its calls by the minute', 'Off: Faxbot waits'),
    (None, 'it has no call prices for this trunk', 'Off: Faxbot waits'),
])
def test_the_switch_sentence_says_whether_faxbot_uses_the_cap_and_why(monkeypatch, found, on_text, off_text):
    monkeypatch.setattr(stations, 'trunk_card', lambda values, engine: found)
    on = stations.cap_view(trunk_values(), None)
    assert on['on'] is True and on['applies'] is stations.bills_by_minute(found)
    assert on_text in on['sentence'] and on['sentence'] == on['on_sentence']
    off = stations.cap_view(trunk_values(SIP_FAX_ANSWER_CAP='false'), None)
    assert off['on'] is False and off['applies'] is False
    assert off_text in off['sentence'] and off['sentence'] == off['off_sentence'] == on['off_sentence']
    if found is None:
        # Unknown cost is never called free.
        assert 'nothing' not in on['sentence'] and 'little' not in on['sentence']


def _variables(fields):
    return dict(item.split('=', 1) for item in fields['Variable'].split(',') if '=' in item and '"' not in item)


def test_each_trunk_has_its_own_switch_and_a_trunk_switched_off_waits_the_usual_time(monkeypatch):
    # No database here: the cap reads only the trunk's shipped prices (Telnyx: by the minute in 60-second steps).
    monkeypatch.setattr(ami, '_database', lambda: None)
    second = {'provider': 'sip', 'label': 'Second Telnyx trunk', 'receives': True, 'numbers': ['+15555550111'],
              'settings': {'preset': 'telnyx', 'auth': 'registration', 'username': 'faxbotwest',
                           'caller_id': '+15555550111', 'answer_cap': False},
              'credentials': {'password': 'synthetic-West-Pass!7'}}
    values = trunk_values({'sip-west': second})
    first = _variables(ami.originate_fields_for(values, JOB, '+13035550150', '/faxdata/x.tiff', attempt_id=ATTEMPT))
    assert first['FAXBOT_T0_MS'] == str(stations.T0_CAP_MS)
    west = _variables(ami.originate_fields_for(values, JOB, '+13035550150', '/faxdata/x.tiff', attempt_id=ATTEMPT,
                                               trunk='sip-west'))
    assert 'FAXBOT_T0_MS' not in west
    # Switched off on the first trunk only: the second, now on, carries it.
    values = trunk_values({'sip-west': {**second, 'settings': {**second['settings'], 'answer_cap': True}}},
                          SIP_FAX_ANSWER_CAP='false')
    assert 'FAXBOT_T0_MS' not in _variables(ami.originate_fields_for(values, JOB, '+13035550150', '/faxdata/x.tiff',
                                                                     attempt_id=ATTEMPT))
    assert _variables(ami.originate_fields_for(values, JOB, '+13035550150', '/faxdata/x.tiff', attempt_id=ATTEMPT,
                                               trunk='sip-west'))['FAXBOT_T0_MS'] == str(stations.T0_CAP_MS)


def test_the_sent_sentence_names_the_billed_step_kept_with_the_call():
    row = {'cap_seconds': 50, 'increment_seconds': 60, 'minimum_seconds': 60}
    assert stations.cap_sentence(row) == ('Faxbot hung up 50 seconds after the call was answered because no fax '
                                          'machine answered, so it is billed as one minute instead of two.')
    # On 120-second steps both fit in one step: no saving is claimed.
    assert stations.cap_sentence({**row, 'increment_seconds': 120, 'minimum_seconds': 0}) == (
        'Faxbot hung up 50 seconds after the call was answered because no fax machine answered.')
    assert stations.cap_sentence({**row, 'increment_seconds': None, 'minimum_seconds': None}).endswith('answered.')


def _send(test_client):
    from api.tests.test_access_management_http import B
    sent = test_client.post('/fax', headers=B, data={'to': '+13035550150'},
                            files={'file': ('note.txt', b'Synthetic page', 'text/plain')})
    assert sent.status_code == 202, sent.text
    return sent.json()['id']


def test_the_switch_reads_and_saves_over_http_and_a_capped_call_is_said_in_sent_details(client):  # noqa: F811
    from api.tests.test_access_management_http import B
    from app.main import app
    import sqlalchemy as sa
    view = client.get('/routing/stations/answer-cap', headers=B)
    assert view.status_code == 200, view.text
    (trunk,) = view.json()['trunks']
    assert trunk['account'] == 'sip' and trunk['on'] and trunk['applies']
    assert trunk['sentence'].startswith('On: your carrier bills this trunk by the minute')
    settings = client.get('/admin/settings', headers=B).json()
    assert settings['sip']['trunk']['fax_answer_cap'] is True
    saved = client.put('/admin/settings', headers=B, json={
        'sip_fax_answer_cap': False, 'expected_revision_id': settings['_meta']['desired_revision_id']})
    assert saved.status_code == 200, saved.text
    (trunk,) = client.get('/routing/stations/answer-cap', headers=B).json()['trunks']
    assert not trunk['on'] and trunk['sentence'].startswith('Off: Faxbot waits the usual 60 seconds')
    # A call the cap ended, recorded as the built-in engine's result arrives, then read in Sent details.
    store = app.state.configuration_runtime.manager.store
    job = _send(client)
    attempts = sa.table('outbound_attempts', sa.column('id'), sa.column('job_id'), sa.column('sequence'),
                        sa.column('phase'), sa.column('created_at'))
    with store.engine.begin() as connection:
        connection.execute(attempts.insert().values(id='c' * 32, job_id=job, sequence=1, phase='failed',
                                                    created_at=datetime(2026, 10, 10, 9, 0)))
    stations.after_call(store.engine, job_id=job, attempt_id='c' * 32, station='', succeeded=False,
                        t0_capped=True, configuration=store)
    sentences = client.get(f'/routing/stations/faxes/{job}', headers=B).json()['sentences']
    assert sentences == ['Faxbot hung up 50 seconds after the call was answered because no fax machine answered, '
                         'so it is billed as one minute instead of two.']


def test_the_command_line_shows_and_changes_the_switch(client):  # noqa: F811
    from typer.testing import CliRunner
    from app.cli.main import app as cli_app
    from api.tests.test_access_management_http import BOOTSTRAP

    def run(*args):
        return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args],
                                  obj={'client_factory': lambda address, timeout: (client, False)},
                                  env={'COLUMNS': '220', 'TZ': 'UTC'})
    shown = run('providers', 'trunk', 'answer-cap')
    assert shown.exit_code == 0, (shown.stdout, shown.stderr)
    assert 'On: your carrier bills this trunk by the minute' in ' '.join(shown.stdout.split())
    off = run('providers', 'trunk', 'answer-cap', 'off')
    assert off.exit_code == 0, (off.stdout, off.stderr)
    assert 'Off: Faxbot waits the usual 60 seconds' in ' '.join(off.stdout.split())
    assert run('providers', 'trunk', 'answer-cap', 'maybe').exit_code != 0
    assert run('providers', 'trunk', 'answer-cap', '--account', 'nowhere').exit_code != 0
