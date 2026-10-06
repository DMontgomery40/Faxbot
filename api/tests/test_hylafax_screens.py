"""What the screens and the command line read from the SSL Fax engine's records: Sent details, Recipients,
Costs → Savings, and the recipient limits route."""
import base64
from datetime import datetime, timedelta

import pytest

from api.app import schema
from app import hylafax_engine, hylafax_records, sip_calls
from api.tests.test_schema import database  # noqa: F401 - fixture

JOB, ATTEMPT = 'a' * 32, 'b' * 32
NOW = datetime(2026, 10, 5, 1, 0, 0)
PEER = '+15555550199'


@pytest.fixture
def installation(database):
    schema.upgrade_schema(database)
    return sip_calls.SipCallRecords(database), hylafax_records.records_for(database)


def epoch(moment):
    return str(int((moment - datetime(1970, 1, 1)).total_seconds()))


def submit(calls, job, attempt):
    calls.record_submission({'JobID': job, 'AttemptID': attempt, 'Called': PEER, 'CallerID': '+15555550100',
                             'Preset': 'telnyx', 'FaxPreference': 'no'}, now=NOW)


def test_sent_details_say_how_the_pages_went_or_why_the_built_in_engine_carried_the_fax(installation):
    calls, records = installation
    submit(calls, JOB, ATTEMPT)
    calls.record_engine_result(JOB, ATTEMPT, success=True, pages=6, now=NOW)
    records.record_result(direction='outbound', call_key=ATTEMPT, job_id=JOB, number=PEER, now=NOW,
                          details={'engine_ref': 'e:1', 'sslfax': True, 'sslfax_offered': True, 'transfer_seconds': 12})
    detail = records.sent_detail(JOB)
    assert detail.pop('negotiation')['sentence'] == (
        'The pages went over the internet instead of the phone line; compression, resolution and error correction '
        'are not reported by this engine; 6 pages in 12 s.')
    assert detail == {'engine': 'hylafax', 'sslfax': True, 'sentence':
                      'The pages were sent faster during the call: 12 seconds instead of about 48.'}
    other = 'c' * 32
    records.record_call(direction='outbound', call_key='d' * 32, job_id=other, engine='builtin', now=NOW,
                        reason=hylafax_engine.SENDING_TOGETHER)
    assert records.sent_detail(other)['sentence'] == hylafax_engine.SENDING_TOGETHER
    assert records.sent_detail('e' * 32) is None


def test_recipient_detail_reports_sslfax_and_limits(installation):
    _, records = installation
    assert records.recipient_detail(PEER) == {'accepts_sslfax': None, 'accepts_sslfax_at': None, 'max_rate': None,
                                              'ecm': None}
    records.record_result(direction='inbound', call_key='engine.1', number=PEER, now=NOW,
                          details={'engine_ref': 'e:2', 'sslfax': True, 'sslfax_offered': True})
    records.set_recipient_settings(PEER, max_rate=9600, ecm=False, now=NOW)
    assert records.recipient_detail(PEER) == {'accepts_sslfax': True, 'accepts_sslfax_at': '2026-10-05T01:00:00Z',
                                              'max_rate': 9600, 'ecm': False}


class Routes:
    def __init__(self, card):
        self.card = card

    def card_for(self, provider, direction='outbound'):
        assert provider == 'sip'
        return self.card


def test_sslfax_savings_price_the_shorter_call_with_the_carriers_own_billing(installation):
    from app.routing.costs import RateCard
    calls, records = installation
    for index, (connected, pages, transfer) in enumerate(((20, 6, 12), (200, 30, 40))):
        job, attempt = f'{index:032x}', f'{index + 10:032x}'
        submit(calls, job, attempt)
        calls.record_engine_call({'Direction': 'out', 'JobID': job, 'AttemptID': attempt, 'T38Session': '0',
                                  'Started': epoch(NOW), 'Answered': epoch(NOW),
                                  'Ended': epoch(NOW + timedelta(seconds=connected)), 'Cause': '16',
                                  'CallID64': base64.b64encode(f'call-{index}'.encode()).decode()}, now=NOW)
        calls.record_engine_result(job, attempt, success=True, pages=pages, now=NOW)
        records.record_result(direction='outbound', call_key=attempt, job_id=job, number=PEER, now=NOW,
                              details={'engine_ref': f'e:{index}', 'sslfax': True, 'transfer_seconds': transfer})
    # Telnyx bills whole minutes with a one-minute minimum (the shipped rate card cites its pricing page).
    whole_minutes = RateCard(None, 'sip-telnyx', 'outbound', 'Telnyx', 'USD', 5000, 0, 0, 60, 60, None, NOW)
    found = hylafax_records.sslfax_savings(Routes(whole_minutes), calls.engine, since=NOW - timedelta(days=1), days=30)
    # Ordinary calls: 20 - 12 + 48 = 56 s (one minute either way); 200 - 40 + 240 = 400 s (7 minutes, not 4).
    assert (found['faxes'], found['seconds_saved'], found['priced'], found['same_cost']) == (2, 236, 2, 1)
    assert found['saved'] == {'USD': 15000}
    assert found['sentence'] == '2 faxes had their pages sent faster: about 4 minutes less on the phone and about $0.015 saved.'
    short = hylafax_records.savings_sentence({'faxes': 1, 'seconds_saved': 36, 'saved': {}, 'priced': 1,
                                               'same_cost': 1}, 30)
    assert short == ('1 fax had its pages sent faster: about 1 minute less on the phone. Your carrier charges whole '
                     'minutes, so it cost the same.')
    nothing = hylafax_records.sslfax_savings(Routes(None), calls.engine, since=NOW + timedelta(days=1), days=30)
    assert nothing['sentence'] == 'No faxes were sent faster in the last 30 days.'


def test_recipient_limits_route_saves_and_refuses_what_it_cannot_use(isolated_installation, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setenv('REQUIRE_API_KEY', 'true')
    monkeypatch.setenv('API_KEY', 'bootstrap_admin_only')
    admin = {'X-API-Key': 'bootstrap_admin_only'}
    url = '/routing/destinations/' + PEER + '/fax-limits'
    with TestClient(main.app) as client:
        assert client.get(url).status_code == 401
        first = client.get(url, headers=admin)
        assert first.status_code == 200, first.text
        assert first.json()['accepts_sslfax'] is None and first.json()['sslfax_sentence'] is None
        saved = client.put(url, json={'max_rate': 9600, 'ecm': False}, headers=admin)
        assert saved.status_code == 200 and saved.json()['max_rate'] == 9600 and saved.json()['ecm'] is False
        assert client.get(url, headers=admin).json()['max_rate'] == 9600
        assert client.put(url, json={'max_rate': 12000, 'ecm': None}, headers=admin).status_code == 400
        assert client.put(url, json={'max_rate': None, 'ecm': 'yes'}, headers=admin).status_code == 422
        assert client.put(url, json={'max_rate': None, 'ecm': None}, headers=admin).json()['max_rate'] is None
        assert client.get('/routing/destinations/not-a-number/fax-limits', headers=admin).status_code == 400


def test_the_command_line_shows_the_consoles_sentence_or_not_known_yet():
    from app.cli.commands import sslfax
    unknown = {'sslfax_sentence': None, 'max_rate': None, 'ecm': None}
    assert sslfax.limits_fields(unknown) == [('Faster pages', 'not known yet'), ('Highest speed', 'as set for all faxes'),
                                             ('Error correction', 'as set for all faxes')]
    known = {'sslfax_sentence': 'This fax machine can take pages faster, so faxes to it are quicker.', 'max_rate': 9600,
             'ecm': False}
    assert sslfax.limits_fields(known) == [('Faster pages', known['sslfax_sentence']),
                                           ('Highest speed', '9600 bits per second'), ('Error correction', 'off')]

