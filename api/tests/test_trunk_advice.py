"""Trunk advice from synthetic bills (B6): whether one trunk's traffic fits on another and what that saves.

Two trunks: the first (Telnyx, 2 lines) and Leeds (Gamma, 1 line, with a saved rate card whose monthly fee is
£22). The calls and charges are synthetic records of the kind Faxbot keeps for every call. Advice only:
nothing here changes or cancels anything.
"""
from datetime import datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa

from api.app.config_profiles import ConfigurationDocument
from api.app.config_values import ConfigurationValues
from api.app.routing import trunk_advice
from api.app.routing.costs import RateCard, parse_amount
from api.app.routing.store import RouteStore
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)


NOW = datetime(2026, 10, 8, 12, 0)
LEEDS = {'provider': 'sip', 'label': 'Leeds trunk', 'receives': True, 'numbers': ['+441132000000'],
         'settings': {'preset': 'gamma', 'auth': 'ip', 'host': '192.0.2.40', 'caller_id': '+441132000000'},
         'limits': {'at_once': 1}}


def values():
    return ConfigurationValues.from_environment({
        'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_MAX_CALLS': '2',
        'SIP_TRUNK_DIDS': '+13035550100', 'FAX_DEFAULT_COUNTRY': 'US'}).with_provider_accounts(
        ConfigurationDocument({'sip-leeds': LEEDS}))


def call(connection, trunk, start, minutes, *, direction='outbound', status='SUCCESS'):
    table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=connection)
    connection.execute(table.insert().values(
        id=uuid4().hex, direction=direction, call_id=uuid4().hex, started_at=start, answered_at=start,
        ended_at=start + timedelta(minutes=minutes), disposition='answered', t38='yes', fax_preference=0,
        fax_status=status, trunk_key=None if trunk == 'sip' else trunk, created_at=start, updated_at=start))


def sent(connection, route, destination, cost_micros, currency, when):
    tables = {name: sa.Table(name, sa.MetaData(), autoload_with=connection)
              for name in ('fax_jobs', 'outbound_attempts', 'delivery_attempt_costs')}
    job, attempt = uuid4().hex, uuid4().hex
    connection.execute(tables['fax_jobs'].insert().values(
        id=job, to_number=destination, file_name='synthetic.pdf', tiff_path='', status='SUCCESS', pages=1,
        backend='sip', created_at=when, updated_at=when))
    connection.execute(tables['outbound_attempts'].insert().values(
        id=attempt, job_id=job, sequence=1, phase='success', created_at=when, submitted_at=when,
        completed_at=when + timedelta(minutes=2)))
    connection.execute(tables['delivery_attempt_costs'].insert().values(
        id=attempt, job_id=job, destination=destination, route=route, route_reason='rule', provider_id='sip',
        estimated_cost_micros=cost_micros, currency=currency, billing_checks=0, outcome='success',
        created_at=when, updated_at=when))


def test_one_row_per_trunk_and_peak_lines_from_overlapping_calls(database):  # noqa: F811
    upgrade_schema(database)
    with database.begin() as connection:
        call(connection, 'sip', NOW - timedelta(days=2), 5)
        call(connection, 'sip', NOW - timedelta(days=2, minutes=-2), 5)       # overlaps the first: two at once
        call(connection, 'sip-leeds', NOW - timedelta(days=3), 4, direction='inbound')
        call(connection, 'sip-leeds', NOW - timedelta(days=40), 4)            # outside the window
    uses = {use.key: use for use in trunk_advice.trunk_uses(values(), database, now=NOW)}
    assert (uses['sip'].peak_lines, uses['sip'].lines) == (2, 2)
    assert (uses['sip-leeds'].peak_lines, uses['sip-leeds'].lines, uses['sip-leeds'].received) == (1, 1, 1)
    # Telnyx publishes a $0 trunk fee; Gamma publishes none, so the Leeds trunk's fee is unknown.
    assert (uses['sip'].monthly_micros, uses['sip-leeds'].monthly_micros) == (0, None)


def test_a_trunk_whose_faxes_fit_on_another_says_what_cancelling_it_would_save(database, monkeypatch):  # noqa: F811
    upgrade_schema(database)
    routes = RouteStore(database, sip_preset=lambda: 'telnyx')
    routes.replace_cards([RateCard(None, 'sip-gamma', 'outbound', 'Leeds trunk', 'GBP', parse_amount('0.010'), 0, 0,
                                   60, 60, None, datetime(2026, 10, 3), parse_amount('22.00', whole_digits=4))])
    with database.begin() as connection:
        for day in range(14):
            start = NOW - timedelta(days=day + 1)
            call(connection, 'sip-leeds', start, 2)
            sent(connection, 'sip-leeds', '+442079460000', 20000, 'GBP', start)
        call(connection, 'sip', NOW - timedelta(days=1, minutes=30), 3)
    # The first trunk would carry those faxes for the same price (a synthetic quote, as the predictor gives).
    from api.app.routing import pricing
    monkeypatch.setattr(pricing, 'price', lambda routes, values, key, destination, pages, provider=None, **_:
                        pricing.Price(key, 20000, 'GBP'))
    found = trunk_advice.advice(values(), database, now=NOW, routes=routes)
    [item] = found['items']
    assert (item['trunk'], item['into'], item['faxes']) == ('sip-leeds', 'sip', 14)
    assert item['saving_per_month']['micros'] == 22_000_000 and item['saving_per_month']['currency'] == 'GBP'
    assert item['sentence'] == (
        'Leeds trunk carried 14 faxes in the last 30 days and costs 22.00 GBP a month by itself. Telnyx had free lines '
        'at those times and charges the same or less for those numbers. Sending those faxes over Telnyx and '
        'cancelling Leeds trunk would save about 22.00 GBP a month.')
    assert item['limit_suggestion']['then'] == {'never': ['sip-leeds']}
    rows = {row['key']: row for row in found['trunks']}
    assert rows['sip-leeds']['cost_per_delivered'] == '0.02 GBP' and rows['sip-leeds']['monthly'] == '22.00 GBP'


def test_no_advice_when_the_other_trunk_has_no_room_or_moving_saves_nothing(database, monkeypatch):  # noqa: F811
    upgrade_schema(database)
    with database.begin() as connection:
        start = NOW - timedelta(days=1)
        # The first trunk's two lines are busy exactly when the Leeds trunk's call is up.
        call(connection, 'sip', start, 10)
        call(connection, 'sip', start, 10)
        call(connection, 'sip-leeds', start + timedelta(minutes=1), 2)
        sent(connection, 'sip-leeds', '+442079460000', 20000, 'GBP', start + timedelta(minutes=1))
    from api.app.routing import pricing
    monkeypatch.setattr(pricing, 'price', lambda routes, values, key, destination, pages, provider=None, **_:
                        pricing.Price(key, 10000, 'GBP'))
    found = trunk_advice.advice(values(), database, now=NOW)
    assert found['items'] == []
    assert found['sentence'] == 'Each trunk carries traffic the others could not take as cheaply.'
    alone = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx',
                                                  'SIP_TRUNK_AUTH': 'ip'})
    assert trunk_advice.advice(alone, database, now=NOW)['sentence'] == trunk_advice.NO_TRUNKS
