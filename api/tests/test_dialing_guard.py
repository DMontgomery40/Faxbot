"""Where Faxbot may dial (routing/guard.py): classes of numbers, the defaults, and the hold in Sent.

The HTTP tests run the real POST /fax acceptance path with sending turned off (FAX_DISABLED), so every
accepted fax stays held and nothing is submitted to a provider; the real approval flow decides the holds.
Numbers are synthetic or from public numbering-plan examples; none is called.
"""
from datetime import datetime
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app import main
from app.routing import guard
from app.routing.background import installation_engine


# -- classes ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('home, number, expected', [
    ('US', '+13035550100', guard.GEOGRAPHIC),          # a synthetic 555 number: possible, not valid, national
    ('US', '+17208565062', guard.GEOGRAPHIC),
    ('US', '+18884732963', guard.TOLL_FREE),
    ('US', '+19005550100', guard.PREMIUM),
    ('US', '+18765551234', 'country:JM'),              # Jamaica inside +1: a classic wrong-number fraud range
    ('US', '+18095551234', 'country:DO'),
    ('US', '+16135550100', 'country:CA'),
    ('US', '+442079460000', 'country:GB'),
    ('US', '+447012345678', guard.SPECIAL),            # UK 070 personal numbers
    ('US', '+448712345678', guard.PREMIUM),            # UK 0871
    ('US', '+881612345678', guard.SATELLITE),          # global mobile satellite
    ('US', '+870772001234', guard.SATELLITE),          # Inmarsat
    ('US', '+88213000000', guard.SATELLITE),           # international networks
    ('US', '+80012345678', guard.SPECIAL),             # international freephone
    ('GB', '+442079460000', guard.GEOGRAPHIC),
    ('GB', '+443001234567', guard.GEOGRAPHIC),         # UK 03: charged like a geographic number
    ('GB', '+448001234567', guard.TOLL_FREE),
    ('GB', '+447400123456', guard.MOBILE),
    ('GB', '+447624000000', 'country:IM'),             # Isle of Man mobile inside +44
    ('GB', '+447700900123', guard.GEOGRAPHIC),         # an Ofcom drama number: possible, not valid, national
    ('GB', '+13035550100', 'country:US'),
    ('CA', '+18884732963', guard.TOLL_FREE),           # NANP toll-free is toll-free from Canada too
    ('AU', '+61412345678', guard.MOBILE),
    ('AU', '+61291234567', guard.GEOGRAPHIC),
])
def test_each_number_gets_its_class(home, number, expected):
    found = guard.dial_class(number, home)
    assert found is not None and found.key == expected
    if expected.startswith('country:'):
        assert found.region == expected.split(':')[1]


def test_a_class_is_named_by_word_country_or_calling_code():
    assert guard.parse_class('premium') == guard.PREMIUM
    assert guard.parse_class('national-mobile') == guard.MOBILE
    assert guard.parse_class('special-service') == guard.SPECIAL
    assert guard.parse_class('gb') == 'country:GB'
    assert guard.parse_class('+44') == 'country:GB'
    for wrong in ('US', '+1', '+881', 'XX', 'everything'):
        with pytest.raises(guard.GuardInputError):
            guard.parse_class(wrong, 'US')


def test_held_sentences_are_one_sentence_each_with_where_to_change_it():
    fenced = guard.held_sentence(guard.PREMIUM, 'fenced')
    assert fenced.startswith('Faxbot never dials premium-rate numbers unless you allow them in Delivery setup → Providers & accounts')
    assert guard.held_sentence('country:GB', 'not_allowed').startswith(
        'Faxbot has not sent to numbers in the United Kingdom before')
    over = guard.held_sentence('country:GB', 'over_ceiling', rate=120000, ceiling=50000, currency='USD',
                               route='SignalWire')
    assert over == ('Calls to numbers in the United Kingdom cost $0.12 a minute by SignalWire, over the $0.05 a '
                    'minute you set, so this fax waits for your approval. Nothing was sent.')


# -- the real acceptance path ------------------------------------------------------------------------------------

BOOTSTRAP = 'synthetic-dialing-guard-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
HUMBLEFAX = {'provider_id': 'humblefax', 'label': 'HumbleFax', 'monthly_fee': '10', 'captured_on': '2026-10-05'}
SIGNALWIRE = {'provider_id': 'signalwire', 'label': 'SignalWire', 'per_minute': '0.0095', 'captured_on': '2026-10-05'}


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_ROUTES': 'humblefax, signalwire',
                        'HUMBLEFAX_ACCESS_KEY': 'synthetic-access', 'HUMBLEFAX_SECRET_KEY': 'synthetic-secret',
                        'SIGNALWIRE_SPACE_URL': 'example.signalwire.com', 'SIGNALWIRE_PROJECT_ID': 'project-1',
                        'SIGNALWIRE_API_TOKEN': 'synthetic-token', 'SIGNALWIRE_FAX_FROM_E164': '+13035550111',
                        'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        saved = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [HUMBLEFAX, SIGNALWIRE]})
        assert saved.status_code == 200, saved.text
        yield client


def send(client, to):
    response = client.post('/fax', headers=ADMIN, data={'to': to},
                           files={'file': ('note.txt', b'Synthetic page\n', 'text/plain')})
    assert response.status_code == 202, response.text
    return response.json()['id']


def holds_for(client, job):
    return [hold for hold in client.get('/routing/holds', headers=ADMIN).json()['holds'] if hold['job_id'] == job]


def publish(client, document):
    current = client.get('/routing/rules', headers=ADMIN).json()
    saved = client.put('/routing/rules/draft', headers=ADMIN, json={
        'document': document, 'expected_version': current['draft']['version'] if current['draft'] else 0})
    assert saved.status_code == 200, saved.text
    active = current['active']['number'] if current['active'] else None
    published = client.post('/routing/rules/publish', headers=ADMIN, json={
        'expected_active_revision': active, 'expected_draft_version': saved.json()['version'], 'note': 'Synthetic'})
    assert published.status_code == 200, published.text


def change(client, what, **body):
    response = client.put(f'/routing/dialing/{what}', headers=ADMIN, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def guard_rows(job):
    engine, _ = installation_engine(main.app)
    table = sa.table('dialing_guard_holds', sa.column('id'), sa.column('job_id'), sa.column('class_key'),
                     sa.column('why'), sa.column('dialed_number'), sa.column('rate_micros'))
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table).where(table.c.job_id == job)).mappings()]


def test_national_numbers_are_never_held(client):
    for number in ('+13035550100', '+18884732963'):
        job = send(client, number)
        assert holds_for(client, job) == [] and guard_rows(job) == []


def test_premium_is_held_and_never_released_by_approval_until_its_class_is_allowed(client):
    job = send(client, '+19005550100')
    [hold] = holds_for(client, job)
    assert hold['kind'] == 'approval'
    assert hold['reason'] == guard.held_sentence(guard.PREMIUM, 'fenced')
    assert [(row['class_key'], row['why'], row['dialed_number']) for row in guard_rows(job)] == [
        (guard.PREMIUM, 'fenced', '+19005550100')]
    refused = client.post(f"/routing/holds/{hold['id']}/approve", headers=ADMIN, json={'version': hold['version']})
    assert refused.status_code == 400
    assert 'unless you allow them first' in refused.json()['detail']
    assert holds_for(client, job)[0]['state'] == 'open'          # still held: nothing is dialed
    assert change(client, 'premium', state='allowed')['sentence'] == 'Faxbot may dial premium-rate numbers.'
    approved = client.post(f"/routing/holds/{hold['id']}/approve", headers=ADMIN, json={'version': hold['version']})
    assert approved.status_code == 200, approved.text
    assert holds_for(client, job) == []
    # Allowed now: the next premium-rate fax is not held.
    assert holds_for(client, send(client, '+19005550100')) == []


def test_a_new_country_waits_for_approval_and_one_approval_sends_only_that_fax(client):
    job = send(client, '+442079460000')
    [hold] = holds_for(client, job)
    assert hold['reason'] == guard.held_sentence('country:GB', 'not_allowed')
    approved = client.post(f"/routing/holds/{hold['id']}/approve", headers=ADMIN, json={'version': hold['version']})
    assert approved.status_code == 200, approved.text
    assert len(holds_for(client, send(client, '+442079460001'))) == 1


def test_a_routing_rule_or_a_saved_recipient_names_a_country(client):
    publish(client, {'format': 1, 'routes': [
        {'id': 'r-uk', 'name': 'UK faxes', 'on': True, 'when': {'destination': {'countries': ['GB']}},
         'then': {'use': 'signalwire'}},
        {'id': 'r-ie', 'name': 'Dublin office', 'on': True, 'when': {'destination': {'prefixes': ['+3531']}},
         'then': {'use': 'signalwire'}}]})
    assert holds_for(client, send(client, '+442079460000')) == []
    assert holds_for(client, send(client, '+35315550100')) == []
    # A limit (or a rule that is off) never allows a country.
    publish(client, {'format': 1, 'limits': [
        {'id': 'l-de', 'name': 'German cap', 'on': True, 'when': {'destination': {'countries': ['DE']}},
         'then': {'cap_cost': {'currency': 'USD', 'amount': '5'}}}]})
    assert len(holds_for(client, send(client, '+4930123456'))) == 1
    saved = client.patch('/routing/destinations/+33123456789', headers=ADMIN, json={'display_name': 'Paris clinic'})
    assert saved.status_code == 200, saved.text
    assert holds_for(client, send(client, '+33198765432')) == []
    listed = client.get('/routing/dialing', headers=ADMIN).json()
    france = next(item for item in listed['countries'] if item['key'] == 'country:FR')
    assert france['allowed'] and france['sentence'] == 'Allowed: a saved recipient, +33123456789, is there.'


def _delivered(number, *, when):
    """A fax already delivered over a call before the guard existed: a job, its attempt and its cost row."""
    engine, _ = installation_engine(main.app)
    job, attempt = uuid.uuid4().hex, uuid.uuid4().hex
    jobs = sa.table('fax_jobs', *(sa.column(name) for name in (
        'id', 'to_number', 'file_name', 'tiff_path', 'status', 'pages', 'backend', 'created_at', 'updated_at')))
    attempts = sa.table('outbound_attempts', *(sa.column(name) for name in (
        'id', 'job_id', 'sequence', 'phase', 'created_at')))
    costs = sa.table('delivery_attempt_costs', *(sa.column(name) for name in (
        'id', 'job_id', 'destination', 'route', 'route_reason', 'provider_id', 'billing_checks', 'outcome',
        'created_at', 'updated_at')))
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id=job, to_number=number, file_name='old.pdf', tiff_path='',
                                                status='SUCCESS', pages=1, backend='signalwire', created_at=when,
                                                updated_at=when))
        connection.execute(attempts.insert().values(id=attempt, job_id=job, sequence=1, phase='completed',
                                                    created_at=when))
        connection.execute(costs.insert().values(id=attempt, job_id=job, destination=number, route='signalwire',
                                                 route_reason='default', provider_id='signalwire', billing_checks=0,
                                                 outcome='success', created_at=when, updated_at=when))


def test_countries_already_delivered_to_stay_allowed_with_that_reason(client):
    _delivered('+61291234567', when=datetime(2026, 3, 2, 15, 0))
    assert holds_for(client, send(client, '+61298765432')) == []
    listed = client.get('/routing/dialing', headers=ADMIN).json()
    australia = next(item for item in listed['countries'] if item['key'] == 'country:AU')
    assert australia['allowed'] and australia['source'] == 'delivered'
    assert australia['first_delivered_at'] == '2026-03-02T15:00:00'
    # Recorded once: a delivery after Faxbot started checking allows nothing by itself.
    _delivered('+64912345678', when=datetime(2026, 10, 9, 9, 0))
    assert len(holds_for(client, send(client, '+6493456789'))) == 1


def test_blocking_a_country_and_the_default_back(client):
    _delivered('+61291234567', when=datetime(2026, 3, 2, 15, 0))
    assert change(client, 'AU', state='blocked')['sentence'] == \
        'Faxbot holds faxes to numbers in Australia for your approval.'
    job = send(client, '+61298765432')
    assert holds_for(client, job)[0]['reason'] == guard.held_sentence('country:AU', 'blocked')
    change(client, '+61', state='default')
    assert holds_for(client, send(client, '+61298765432')) == []


def test_a_rate_ceiling_holds_a_fax_over_it_and_lets_one_under_it_go(client):
    publish(client, {'format': 1, 'routes': [{'id': 'r-sw', 'name': 'Everything by SignalWire', 'on': True,
                                              'when': {}, 'then': {'use': 'signalwire'}}]})
    result = change(client, 'national', state='default', ceiling='0.005')
    assert result['sentence'] == ('Numbers in your country follow Faxbot’s default again. Calls costing more than '
                                  '$0.005 a minute wait for approval.')
    job = send(client, '+13035550100')
    [hold] = holds_for(client, job)
    assert hold['reason'] == ('Calls to numbers in your country cost $0.0095 a minute by SignalWire, over the $0.005 '
                              'a minute you set, so this fax waits for your approval. Nothing was sent.')
    assert [(row['why'], row['rate_micros']) for row in guard_rows(job)] == [('over_ceiling', 9500)]
    # Allowing the class keeps its ceiling unless the change names one.
    change(client, 'national', state='allowed')
    assert len(holds_for(client, send(client, '+13035550100'))) == 1
    change(client, 'national', state='allowed', ceiling='0.01')
    assert holds_for(client, send(client, '+13035550100')) == []


def test_the_list_shows_every_class_and_a_sender_without_settings_cannot_change_it(client):
    listed = client.get('/routing/dialing', headers=ADMIN)
    assert listed.status_code == 200
    body = listed.json()
    assert [item['key'] for item in body['classes']] == list(guard.FIXED)
    assert [item['allowed'] for item in body['classes']] == [True, True, True, False, False, False]
    sender = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': ['fax:send']})
    key = {'X-API-Key': sender.json()['token']}
    assert client.get('/routing/dialing', headers=key).status_code == 403
    assert client.put('/routing/dialing/premium', headers=key, json={'state': 'allowed'}).status_code == 403
    assert client.put('/routing/dialing/nowhere', headers=ADMIN, json={'state': 'allowed'}).status_code == 400
    assert client.put('/routing/dialing/premium', headers=ADMIN, json={'state': 'maybe'}).status_code == 400


def test_the_relay_check_sees_an_open_guard_hold(client):
    job = send(client, '+19005550100')
    engine, _ = installation_engine(main.app)
    with engine.connect() as connection:
        assert guard.open_guard_hold_on(connection, job)
        assert not guard.open_guard_hold_on(connection, send(client, '+13035550100'))


def test_the_command_line_lists_and_changes_through_the_real_server(client):
    from app.cli.main import app as cli_app
    from app.cli import output
    output_home = output.home_currency

    def run(*args):
        return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args],
                                  obj={'client_factory': lambda address, timeout: (client, False)},
                                  env={'COLUMNS': '220', 'TZ': 'UTC'})
    try:
        allowed = run('providers', 'rules', 'destinations', 'allow', 'GB', '--ceiling', '0.25')
        assert allowed.exit_code == 0, allowed.stdout
        assert ' '.join(allowed.stdout.split()) == ('Faxbot may dial numbers in the United Kingdom. Calls costing '
                                                     'more than $0.25 a minute wait for approval.')
        listed = run('providers', 'rules', 'destinations', 'list')
        assert listed.exit_code == 0, listed.stdout
        flat = ' '.join(listed.stdout.split())
        assert 'Premium-rate numbers No Blocked: Faxbot never dials these unless you allow them.' in flat
        assert 'United Kingdom Yes Allowed' in flat and '$0.25 a minute' in flat
        blocked = run('providers', 'rules', 'destinations', 'block', 'satellite')
        assert blocked.exit_code == 0
        assert 'Faxbot holds faxes to satellite and international network numbers' in blocked.stdout
    finally:
        output.home_currency = output_home
