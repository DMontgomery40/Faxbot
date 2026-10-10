"""Completed attempts use the actual destination's tariff; all calls and bills here are synthetic."""
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from api.app import config
from api.app.config_profiles import ConfigurationDocument, ProviderConfiguration
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.routing.costs import RateCard
from api.app.routing.origin_rates import OriginRate, save_rows
from api.app.routing.store import CaptureTarget, RouteStore
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)


START = datetime(2026, 10, 9, 12)
US = '+12025550123'
UK = '+442079460123'
AU = '+61293744000'


def _card(provider='sip-telnyx', minute=5000, currency='USD'):
    return RateCard(None, provider, 'outbound', 'Synthetic tariff', currency, minute, 0, 0, 60, 60, None, START)


@pytest.fixture
def capture_installation(database, tmp_path, monkeypatch):
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'true', 'SIP_TRUNK_PRESET': 'telnyx',
        'FAX_DEFAULT_COUNTRY': 'US', 'FAX_DATA_DIR': str(tmp_path / 'faxdata')})
    configuration.initialize(values, actor='test', providers={
        'outbound': ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-key'})})
    monkeypatch.setattr(config, '_source', configuration)
    routes = RouteStore(database)
    routes.replace_cards([_card()])
    return configuration, routes


def _attempt(installation, destination=US, *, dialed=None, route='sip', provider='sip', phase='success'):
    configuration, routes = installation
    job, attempt = uuid4().hex, uuid4().hex
    end = None if phase == 'uncertain' else START + timedelta(seconds=90)
    configuration.accept_outbound(configuration.read().active, {
        'id': job, 'to_number': destination, 'file_name': 'synthetic.txt', 'tiff_path': '', 'status': 'queued',
        'pages': 3, 'created_at': START, 'updated_at': START})
    with routes.engine.begin() as connection:
        connection.execute(routes.attempts.insert().values(
            id=attempt, job_id=job, sequence=1, phase=phase, created_at=START,
            submitted_at=START, completed_at=end, dialed_number=dialed))
    return CaptureTarget(attempt, job, destination, provider, 'synthetic-call', phase, 3, START, end, False,
                         dialed=dialed, route=route)


@pytest.mark.parametrize('destination', [UK, AU])
@pytest.mark.parametrize('phase', ['success', 'uncertain'])
def test_unpriced_international_capture_never_uses_the_domestic_tariff(capture_installation, destination, phase):
    _, routes = capture_installation
    target = _attempt(capture_installation, destination, phase=phase)
    routes.capture(target, observed_seconds=54)
    row = routes.decision(target.attempt_id)
    assert row['estimated_cost_micros'] is None
    assert row['currency'] is None and row['cost_basis'] is None and row['rate_card_id'] is None
    assert row['billed_seconds'] is None
    assert row['billed_pages'] == (3 if phase == 'success' else 0)
    assert (row['started_at'], row['ended_at'], row['outcome']) == (START, target.completed_at, phase)


@pytest.mark.parametrize('destination,dialed,want', [
    (US, None, 5000), (UK, US, 5000), (US, UK, None), (UK, '+18005550100', 0),
])
def test_capture_prices_the_number_actually_called(capture_installation, destination, dialed, want):
    _, routes = capture_installation
    target = _attempt(capture_installation, destination, dialed=dialed)
    routes.capture(target, observed_seconds=54)
    row = routes.decision(target.attempt_id)
    assert row['estimated_cost_micros'] == want
    assert row['destination'] == destination
    assert row['billed_pages'] == 3
    if want is not None:
        assert row['currency'] == 'USD' and row['cost_basis'] == 'measured'


@pytest.mark.parametrize('seconds,billed,want', [(54, 60, 5000), (61, 120, 10000)])
def test_domestic_capture_rounds_each_measured_call(capture_installation, seconds, billed, want):
    _, routes = capture_installation
    target = _attempt(capture_installation)
    routes.capture(target, observed_seconds=seconds)
    row = routes.decision(target.attempt_id)
    assert (row['billed_seconds'], row['estimated_cost_micros'], row['cost_basis']) == (billed, want, 'measured')


def test_explicit_account_destination_row_prices_only_that_account(capture_installation):
    _, routes = capture_installation
    base, account = routes.replace_cards([_card(), _card('sip-london', 7000, 'GBP')])
    account = next(card for card in (base, account) if card.provider_id == 'sip-london')
    save_rows(routes.engine, account.id, [OriginRate(
        'sip-london', 'any', '44', 'GBP', 12000, 0, 2500, 6, 6, None, START)])
    target = _attempt(capture_installation, UK, route='sip-london')
    routes.capture(target, observed_seconds=61)
    row = routes.decision(target.attempt_id)
    # 61 seconds rounds to 66: 1.1 minutes at 0.012 GBP plus the 0.0025 call fee.
    assert (row['billed_seconds'], row['estimated_cost_micros'], row['currency']) == (66, 15700, 'GBP')
    assert (row['route'], row['provider_id'], row['cost_basis']) == ('sip-london', 'sip', 'measured')
    other = _attempt(capture_installation, UK)
    assert routes.capture(other, observed_seconds=61)['estimated_cost_micros'] is None


@pytest.mark.parametrize('own_card,want', [(True, 14000), (False, 38000)])
def test_extra_trunk_uses_its_own_card_before_its_own_carrier(capture_installation, own_card, want):
    configuration, routes = capture_installation
    snapshot = configuration.read()
    configuration.apply(snapshot, snapshot.active.values, actor='test', restart_required=False,
                        accounts=ConfigurationDocument({'sip-london': {
                            'provider': 'sip', 'settings': {'preset': 'gamma', 'auth': 'ip', 'host': '192.0.2.40'}}}))
    cards = [_card(), _card('sip-gamma', 19000, 'GBP')]
    if own_card:
        cards.append(_card('sip-london', 7000, 'GBP'))
    routes.replace_cards(cards)
    target = _attempt(capture_installation, route='sip-london')
    routes.capture(target, observed_seconds=61)
    row = routes.decision(target.attempt_id)
    assert (row['estimated_cost_micros'], row['currency'], row['billed_seconds']) == (want, 'GBP', 120)


def test_unknown_estimate_survives_recapture_without_erasing_reported_or_settled_charges(capture_installation):
    _, routes = capture_installation
    target = _attempt(capture_installation, UK, phase='uncertain')
    routes.capture(target)
    routes.ingest_charge(target.attempt_id, provider_id='sip', charge_id='synthetic-charge',
                         amount_micros=23000, currency='GBP', billed_seconds=54, final=True)
    completed = replace(target, phase='success', completed_at=START + timedelta(seconds=90))
    routes.capture(completed, observed_seconds=54)
    row = routes.decision(target.attempt_id)
    assert row['estimated_cost_micros'] is None and row['currency'] is None
    assert (row['reported_cost_micros'], row['reported_currency'], row['settled_cost_micros']) == (23000, 'GBP', 23000)
    assert row['settled_at'] is not None
    assert (row['outcome'], row['billed_pages']) == ('success', 3)


def test_capture_uses_the_accepted_country_after_configuration_changes(capture_installation):
    configuration, routes = capture_installation
    target = _attempt(capture_installation, UK)
    snapshot = configuration.read()
    configuration.apply(snapshot, snapshot.active.values.with_patch({'fax_default_country': 'GB'}),
                        actor='test', restart_required=False)
    assert routes.capture(target, observed_seconds=54)['estimated_cost_micros'] is None
