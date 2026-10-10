"""Dated exchange rates for comparing route prices in different currencies (brief 99, item 1).

The ECB rate file here is a synthetic copy of the published format (namespaces, one dated cube); nothing is read
from the ECB. Rate cards and amounts are synthetic; numbers are 555 numbers.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from api.app.routing import exchange
from api.app.routing.exchange import Conversion, FetchError, RateError
from api.app.schema import upgrade_schema
from api.tests.test_routing_multiprovider import accept, card, multi  # noqa: F401 (fixture)
from api.tests.test_schema import database  # noqa: F401 (fixture)

ECB_FILE = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <gesmes:subject>Reference rates</gesmes:subject>
  <gesmes:Sender><gesmes:name>European Central Bank</gesmes:name></gesmes:Sender>
  <Cube>
    <Cube time='2026-10-09'>
      <Cube currency='USD' rate='1.1200'/>
      <Cube currency='JPY' rate='177.00'/>
      <Cube currency='GBP' rate='0.8400'/>
      <Cube currency='XXQ' rate='not-a-number'/>
    </Cube>
  </Cube>
</gesmes:Envelope>"""
NOW = datetime(2026, 10, 10, 18, 0)


def _ecb(body=ECB_FILE):
    return lambda: body


def test_the_ecb_file_is_read_with_its_date_and_only_valid_rates():
    day, rates = exchange.parse_ecb(ECB_FILE)
    assert day == date(2026, 10, 9)
    assert rates == {'USD': Decimal('1.1200'), 'JPY': Decimal('177.00'), 'GBP': Decimal('0.8400')}
    for body, sentence in ((b'<html>busy</html>', 'no dated rates'), (b'not xml', 'not its rate file'),
                           (ECB_FILE.replace(b"time='2026-10-09'", b"time='Friday'"), 'date Faxbot does not read')):
        with pytest.raises(FetchError, match=sentence):
            exchange.parse_ecb(body)


def test_refresh_stores_a_new_day_once_and_a_failure_keeps_the_rates_and_says_why(database):  # noqa: F811
    upgrade_schema(database)
    assert exchange.due(database, NOW) is True
    assert exchange.refresh(database, fetch=_ecb(), now=NOW)['outcome'] == 'stored'
    assert exchange.reference_rates(database) == (date(2026, 10, 9), {'USD': Decimal('1.12'), 'JPY': Decimal('177'),
                                                                       'GBP': Decimal('0.84')})
    assert exchange.due(database, NOW + timedelta(hours=5)) is False
    assert exchange.refresh(database, fetch=_ecb(), now=NOW + timedelta(hours=6))['outcome'] == 'unchanged'

    def unreachable():
        raise FetchError('Faxbot could not reach the ECB from this server.')
    failed = exchange.refresh(database, fetch=unreachable, now=NOW + timedelta(hours=12))
    assert failed == {'outcome': 'failed', 'day': None, 'rates': 0,
                      'error': 'Faxbot could not reach the ECB from this server.'}
    # The rates already stored stay in use, and Faxbot tries again an hour later.
    assert exchange.reference_rates(database)[0] == date(2026, 10, 9)
    assert exchange.due(database, NOW + timedelta(hours=12, minutes=59)) is False
    assert exchange.due(database, NOW + timedelta(hours=13)) is True
    assert exchange.reference_sentence(database, now=NOW + timedelta(hours=12), zone_name='UTC') == (
        'Faxbot could not read the ECB reference rates at 11 Oct 6:00 AM UTC: Faxbot could not reach the ECB from '
        'this server. It uses the rates of 9 October 2026 until it can, and tries again every hour.')
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT COUNT(*) FROM exchange_rates')).scalar() == 3
        assert [row[0] for row in connection.execute(sa.text(
            'SELECT outcome FROM exchange_rate_fetches ORDER BY attempted_at'))] == ['stored', 'unchanged', 'failed']
    # Turned off: never read in the background, and the sentence beside the switch says what is still used.
    exchange.set_reference(database, False, now=NOW + timedelta(hours=13))
    assert exchange.due(database, NOW + timedelta(days=2)) is False and exchange.refresh_step(
        database, fetch=lambda: pytest.fail('read while off'), now=NOW + timedelta(days=2)) is False
    assert exchange.reference_sentence(database, now=NOW).startswith(
        'Faxbot does not read the ECB reference rates; the last ones it read, of 9 October 2026, are used')


def test_cross_rates_go_through_the_euro_and_your_own_rate_wins_while_it_is_current(database):  # noqa: F811
    upgrade_schema(database)
    exchange.refresh(database, fetch=_ecb(), now=NOW)
    today = NOW.date()
    found, clause = exchange.conversion(database, 'GBP', 'USD', today=today)
    assert clause is None and found.source == 'ecb' and found.day == date(2026, 10, 9)
    assert found.rate == Decimal('1.12') / Decimal('0.84')
    assert found.phrase() == 'the ECB rate of 9 October 2026'
    assert found.convert(700_000) == 933_333  # 0.70 GBP is about 0.93 USD
    # Your own rate, entered in either direction, wins while it is no older than 35 days.
    exchange.set_own(database, 'USD', 'GBP', '0.8', rate_date='2026-09-30', note='September invoice', now=NOW)
    mine, _ = exchange.conversion(database, 'GBP', 'USD', today=today)
    assert (mine.source, mine.rate, mine.day) == ('own', Decimal('1.25'), date(2026, 9, 30))
    assert mine.phrase() == 'your rate of 30 September 2026'
    # Past 35 days the current ECB rate is used instead; past both limits nothing is compared, and it says why.
    assert exchange.conversion(database, 'GBP', 'USD', today=date(2026, 11, 5))[0] is None
    assert exchange.conversion(database, 'GBP', 'USD', today=date(2026, 11, 4))[0].source == 'own'
    assert exchange.conversion(database, 'GBP', 'USD', today=date(2026, 10, 16))[0].source == 'own'
    later = date(2026, 11, 5)
    assert exchange.conversion(database, 'GBP', 'USD', today=later) == (
        None, 'the newest exchange rate between GBP and USD is from 9 October 2026, too old to compare prices with')
    assert exchange.clear_own(database, 'gbp', 'usd', now=NOW + timedelta(seconds=1)) is True
    assert exchange.clear_own(database, 'GBP', 'USD', now=NOW + timedelta(seconds=2)) is False
    assert exchange.conversion(database, 'GBP', 'USD', today=date(2026, 10, 16))[0].source == 'ecb'
    assert exchange.conversion(database, 'GBP', 'USD', today=date(2026, 10, 17)) == (
        None, 'the ECB rate between GBP and USD is from 9 October 2026, more than 7 days old')
    assert exchange.conversion(database, 'GBP', 'CHF', today=today) == (
        None, 'no exchange rate between GBP and CHF is set')
    # The cleared rate stays in the history.
    with database.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM exchange_rates WHERE source = 'own'")).scalar() == 2


@pytest.mark.parametrize('args, sentence', [
    (('US', 'GBP', '1'), 'three-letter code'), (('USD', 'USD', '1'), 'two different currencies'),
    (('USD', 'GBP', 'lots'), 'as a number'), (('USD', 'GBP', '0'), 'above zero'),
    (('USD', 'GBP', 'NaN'), 'above zero'),
])
def test_impossible_rates_are_refused_in_one_sentence(database, args, sentence):  # noqa: F811
    upgrade_schema(database)
    with pytest.raises(RateError, match=sentence):
        exchange.set_own(database, *args, now=NOW)
    with pytest.raises(RateError, match='cannot be in the future'):
        exchange.set_own(database, 'USD', 'GBP', '0.8', rate_date='2026-12-01', now=NOW)
    with pytest.raises(RateError, match='year-month-day'):
        exchange.set_own(database, 'USD', 'GBP', '0.8', rate_date='1 May', now=NOW)


def test_amounts_and_rates_read_as_people_read_them():
    assert exchange.amount_text(840_000, 'USD') == '0.84 USD'
    assert exchange.amount_text(4_200, 'GBP') == '0.0042 GBP'
    assert exchange.amount_text(5_000_000, 'JPY') == '5.00 JPY'
    assert exchange.rate_text(Decimal('1.333333333')) == '1.3333'
    assert exchange.rate_text(Decimal('0.00563')) == '0.00563'
    assert Conversion('GBP', 'USD', Decimal('1.25'), date(2026, 10, 9), 'ecb').inverse().rate == Decimal('0.8')


# Through the server and the command line --------------------------------------------------------------------------

@pytest.fixture
def rates_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    monkeypatch.setattr(exchange, 'fetch_ecb', _ecb())
    for client in _serve(monkeypatch, tmp_path):
        yield Cli(client)


def test_the_rates_through_the_server_and_the_command_line(rates_cli):
    from api.tests.test_cli import BOOTSTRAP
    cli, admin = rates_cli, {'X-API-Key': BOOTSTRAP}
    empty = cli.client.get('/routing/exchange-rates', headers=admin).json()
    assert empty['reference']['enabled'] is True and empty['own'] == [] and empty['reference']['rates'] == []
    assert cli.client.get('/routing/exchange-rates').status_code in (401, 403)
    # Read now (the ECB file is the synthetic copy), then list.
    refreshed = cli.json('savings', 'plans', 'rates', 'refresh')
    assert refreshed['refreshed']['outcome'] == 'stored' and refreshed['reference']['date_text'] == '9 October 2026'
    shown = ' '.join(cli('savings', 'plans', 'rates', 'list').stdout.split())
    assert 'ECB reference rates of 9 October 2026' in shown and 'You have not set any rates of your own.' in shown
    assert 'GBP 0.84' in shown
    assert 'The ECB has published nothing newer than the rates of 9 October 2026.' in ' '.join(
        cli('savings', 'plans', 'rates', 'refresh').stdout.split())
    # Your own rate: set, shown, refused when impossible, cleared.
    saved = cli('savings', 'plans', 'rates', 'set', 'gbp', 'usd', '1.27', '--date', '2026-10-01', '--note', 'Invoice')
    assert saved.exit_code == 0, saved.stdout
    assert ' '.join(saved.stdout.split()).startswith('1 GBP = 1.27 USD, your rate of 1 October 2026.')
    own = cli.client.get('/routing/exchange-rates', headers=admin).json()['own']
    assert [(item['base'], item['quote'], item['rate'], item['note']) for item in own] == [('GBP', 'USD', '1.27',
                                                                                            'Invoice')]
    refused = cli.client.put('/routing/exchange-rates/own', headers=admin,
                             json={'base': 'GBP', 'quote': 'GBP', 'rate': '1'})
    assert refused.status_code == 400 and refused.json()['detail'] == 'Choose two different currencies.'
    assert cli('savings', 'plans', 'rates', 'set', 'GBP', 'USD', 'lots').exit_code != 0
    cleared = cli('savings', 'plans', 'rates', 'clear', 'USD', 'GBP')
    assert cleared.exit_code == 0 and 'no longer uses your rate between USD and GBP' in ' '.join(cleared.stdout.split())
    assert cli.client.post('/routing/exchange-rates/own/clear', headers=admin,
                           json={'base': 'USD', 'quote': 'GBP'}).status_code == 404
    # The reference rates off, and on again.
    off = cli('savings', 'plans', 'rates', 'reference', 'off')
    assert off.exit_code == 0 and 'does not read the ECB reference rates' in ' '.join(off.stdout.split())
    assert cli.client.get('/routing/exchange-rates', headers=admin).json()['reference']['enabled'] is False
    assert cli('savings', 'plans', 'rates', 'reference', 'maybe').exit_code != 0
    assert cli.json('savings', 'plans', 'rates', 'reference', 'on')['reference']['enabled'] is True


# Route choice ---------------------------------------------------------------------------------------------------

def _gbp(provider, **rates):
    from dataclasses import replace
    return replace(card(provider, **rates), currency='GBP')


def _plan(multi, *, today):  # noqa: F811
    from api.app.routing.plan import RoutePlanner, explain
    _, _, routes, snapshot = multi
    plan = RoutePlanner(routes).plan(to_number='+12025550123', bound='phaxio', values=snapshot.active.values,
                                     pages=3, alternates=True, now=datetime.combine(today, datetime.min.time()))
    return plan, [(choice.route.key, choice.reason, explain(choice, plan.destination)) for choice in plan.choices]


def test_routes_in_different_currencies_are_compared_at_a_current_rate_and_say_how(multi):  # noqa: F811
    _, _, routes, _ = multi
    # Phaxio charges 0.05 GBP a page (synthetic), SignalWire 0.07 USD a page: 0.15 GBP and 0.21 USD for 3 pages.
    routes.replace_cards([_gbp('phaxio', page='0.05'), card('signalwire', page='0.07')])
    # No rate at all: your order decides between currencies, and the first route says why.
    _, choices = _plan(multi, today=NOW.date())
    assert [item[:2] for item in choices] == [('phaxio', 'mixed_currency'), ('signalwire', 'alternative')]
    assert choices[0][2] == ('Your routes charge in different currencies and no exchange rate between GBP and USD is '
                             'set, so Faxbot kept your order between them and compared prices only within each '
                             'currency.')
    exchange.refresh(routes.engine, fetch=_ecb(), now=NOW)
    plan, choices = _plan(multi, today=NOW.date())
    # Compared in the currency of the fax's own account (GBP): at the ECB rate 0.21 USD is about 0.1575 GBP, over
    # 0.15 GBP, so Phaxio is the cheapest; each estimate stays in its own currency.
    assert [item[:2] for item in choices] == [('phaxio', 'cheapest_converted'), ('signalwire', 'alternative')]
    first, second = plan.choices
    assert (first.estimated_cost_micros, first.currency, first.compared_in) == (150_000, 'GBP', 'GBP')
    assert (second.estimated_cost_micros, second.currency, second.compared_as) == (210_000, 'USD', 157_500)
    assert choices[0][2] == ('The cheapest route that works reliably for this number: 0.15 GBP, with the other '
                             "routes' prices converted to GBP at the ECB rate of 9 October 2026.")
    assert choices[1][2] == ('Used if the routes above it are unavailable; it costs 0.21 USD, about 0.16 GBP at the '
                             'ECB rate of 9 October 2026.')
    # Your own rate (the one on your invoice) overrides it: at 1 GBP = 1.50 USD, SignalWire is the cheaper.
    exchange.set_own(routes.engine, 'GBP', 'USD', '1.5', rate_date='2026-10-01', now=NOW)
    plan, choices = _plan(multi, today=NOW.date())
    assert [item[:2] for item in choices] == [('signalwire', 'cheapest_converted'), ('phaxio', 'alternative')]
    assert choices[0][2] == ('The cheapest route that works reliably for this number: 0.21 USD, about 0.14 GBP at '
                             'your rate of 1 October 2026.')
    assert choices[1][2] == 'Used if the routes above it are unavailable.'
    # A week later the ECB rate is too old and your rate still counts; past your 35 days nothing is compared.
    assert _plan(multi, today=date(2026, 11, 5))[1][0][:2] == ('signalwire', 'cheapest_converted')
    _, choices = _plan(multi, today=date(2026, 11, 6))
    assert choices[0][:2] == ('phaxio', 'mixed_currency')
    assert 'the newest exchange rate between GBP and USD is from 9 October 2026, too old' in choices[0][2]


def test_one_currency_reads_no_rates(multi, monkeypatch):  # noqa: F811
    _, _, routes, _ = multi
    routes.replace_cards([card('phaxio', page='0.05'), card('signalwire', page='0.07')])
    monkeypatch.setattr(exchange, 'for_ranking', lambda *args, **kwargs: pytest.fail('rates read for one currency'))
    assert _plan(multi, today=NOW.date())[1][0][:2] == ('phaxio', 'cheapest')


@pytest.mark.asyncio
async def test_a_sent_fax_keeps_how_its_route_was_compared(multi):  # noqa: F811
    from api.app.outbound_worker import OutboundWorker, SubmissionReceipt
    from api.app.routing.transport import RoutedTransport
    from api.tests.test_routing_multiprovider import Inner
    configuration, delivery, routes, _ = multi
    routes.replace_cards([_gbp('phaxio', page='0.08'), card('signalwire', page='0.07')])
    exchange.refresh(routes.engine, fetch=_ecb(), now=exchange.utcnow())
    job = accept(multi)
    inner = Inner(delivery, [SubmissionReceipt('FX1', 'in_progress')])
    assert await OutboundWorker(delivery, RoutedTransport(inner)).step() is True
    assert inner.used == ['signalwire']
    row = delivery.get(job)
    assert routes.decision(row['attempt_id'])['route_reason'] == 'cheapest_converted'
    sentence = exchange.sent_sentence(routes.engine, job, 'signalwire')
    assert sentence == ('The cheapest route then, with prices in different currencies compared: 0.21 USD, about '
                        '0.16 GBP at the ECB rate of 9 October 2026, against 0.24 GBP by Phaxio.')
    with routes.engine.connect() as connection:
        stored = connection.execute(sa.text(
            'SELECT route_key, currency, rate, source FROM exchange_rate_uses WHERE job_id = :job ORDER BY route_key'),
            {'job': job}).all()
    # Each amount stays in its own currency; only the USD one was converted (to the fax's own account's GBP).
    assert [(key, currency, source) for key, currency, _, source in stored] == [('phaxio', 'GBP', None),
                                                                               ('signalwire', 'USD', 'ecb')]
