"""Take the fax lines out of a POTS-replacement order (N23): the counter-quote from a synthetic line inventory.

The published Ooma AirDial price is TouchTone's partner sheet (revision 25 August 2023, read 2026-10-10); the trunk
prices are the shipped, dated carrier prices. Not yet run against a real POTS-replacement quote.
"""
from datetime import date, datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from api.app import schema, schema_pots_quote
from api.app.routing import inventory, pots_quote
from api.app.schema import upgrade_schema
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_line_inventory import INVENTORY, _values
from api.tests.test_schema import database, snapshot  # noqa: F401 (fixture)

NOW = datetime(2026, 10, 10, 12)


def _inventory(engine, text=INVENTORY):
    inventory.import_inventory(engine, inventory.parse_inventory(text.encode()).items, file_name='lines.csv', now=NOW)


def test_the_counter_quote_takes_fax_lines_out_and_prices_one_shared_trunk(database):  # noqa: F811
    upgrade_schema(database)
    _inventory(database)
    pots_quote.record_published(database, 'ooma-airdial', lines_quoted=7, term_months=36, actor={'name': 'Ada'})
    [found] = pots_quote.view(database, _values())['quotes']
    assert found['counts'] == {'lines': 7, 'fax': 5, 'keep': 1, 'unsure': 1}
    assert found['source_url'].startswith('https://touchtone.net/') and found['source_date'] == '2023-08-25'
    assert found['sentences'][0] == ('Your inventory has 7 lines: 5 fax, 1 alarm, elevator or emergency, and 1 other '
                                     'or not known. Fax lines are 71% of the 7 lines quoted.')
    assert found['sentences'][1] == ('Take the 5 fax lines out of the Ooma AirDial order: at $39.95 a line a month, '
                                     'that is $199.75 a month ($7,191.00 over the 36-month term). With 4 ports to a '
                                     'device, that is 1 fewer device.')
    # Your trunk is Telnyx: its published number price, no trunk fee, and its price per received minute.
    assert found['sentences'][2] == ("Faxbot can send and receive them on one shared trunk instead: at Telnyx's "
                                     'published prices, 5 numbers at $1.00 a month and no trunk fee, so $5.00 a '
                                     'month, plus $0.0032 a minute for each received call.')
    assert found['sentences'][3] == 'That is $194.75 a month less than keeping them in the order, before the calls.'
    assert found['sentences'][-2].startswith('Keep the 1 alarm, elevator and emergency line in the order')
    assert found['sentences'][-1].startswith('1 line has no use recorded')
    assert found['trunk']['sources'][0]['read_on'] == '2026-10-05'


def test_a_rate_card_you_saved_for_the_trunk_comes_before_published_prices(database):  # noqa: F811
    from api.app.routing.costs import RateCard, parse_amount
    from api.app.routing.store import RouteStore
    upgrade_schema(database)
    _inventory(database)
    routes = RouteStore(database, sip_preset=lambda: 'telnyx')
    routes.replace_cards([
        RateCard(None, 'sip', 'outbound', 'Telnyx under contract', 'USD', parse_amount('0.005'), 0, 0, 60, 60,
                 'https://example.com/contract.pdf', datetime(2026, 9, 1), monthly_fee_micros=parse_amount('20')),
        RateCard(None, 'sip', 'inbound', 'Telnyx received', 'USD', parse_amount('0.004'), 0, 0, 60, 60,
                 'https://example.com/contract.pdf', datetime(2026, 9, 1))])
    pots_quote.record_published(database, 'ooma-airdial', lines_quoted=7)
    [found] = pots_quote.view(database, _values(), routes=routes)['quotes']
    assert found['trunk']['rate_card'] and found['trunk']['monthly'] == '$25.00'
    assert found['sentences'][2] == ('Faxbot can send and receive them on one shared trunk instead: at your rate card '
                                     'for Telnyx and its published number price, 5 numbers at $1.00 a month and a '
                                     'trunk fee of $20.00 a month, so $25.00 a month, plus $0.004 a minute for each '
                                     'received call.')
    assert found['trunk']['sources'][0]['label'].startswith('Your rate card: Telnyx')
    assert found['net_monthly'] == '$174.75'


def test_without_a_trunk_the_cheapest_published_carrier_is_shown_and_unknown_fees_are_said(database):  # noqa: F811
    upgrade_schema(database)
    _inventory(database)
    pots_quote.record_quote(database, 'Box', per_line='45', ports_per_device=8, device_price='250',
                            source_url='https://example.com/quote.pdf', source_date=date(2026, 9, 1))
    [found] = pots_quote.view(database, None)['quotes']
    # Only Telnyx publishes its trunk fee; the others' numbers are cheaper but their trunk fee is not published.
    assert found['trunk']['carrier'] == 'telnyx'
    option = pots_quote._trunk_option('anveo', 5, 'USD')
    assert option['monthly'] is None and "its trunk fee, which AnveoDirect does not publish" in option['sentence']
    assert '$250.00 less for them' not in found['sentences'][1]  # 7 lines on 8 ports: still one device
    euro = pots_quote.record_quote(database, 'Euro box', per_line='30', currency='EUR')
    assert euro['currency'] == 'EUR'
    euros = next(item for item in pots_quote.view(database, None)['quotes'] if item['name'] == 'Euro box')
    assert any('publishes a price for a number in EUR' in sentence for sentence in euros['sentences'])


def test_quotes_are_kept_as_history_and_impossible_ones_are_refused(database):  # noqa: F811
    upgrade_schema(database)
    assert pots_quote.view(database, None)['quotes'] == []
    pots_quote.record_quote(database, 'Box', per_line='45')
    [only] = pots_quote.view(database, None)['quotes']
    assert only['sentences'] == ["Import your line inventory first, with each line's use (fax, alarm, elevator, "
                                 'emergency or other), so Faxbot knows which lines are fax.']
    pots_quote.record_quote(database, 'Box', per_line='40')
    assert pots_quote.quotes(database)['Box']['per_line'] == '40'
    pots_quote.remove_quote(database, 'Box')
    assert pots_quote.quotes(database) == {}
    with database.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM pots_quotes').scalar() == 3
    for kwargs, message in (({'per_line': 'cheap'}, 'as a number'), ({'per_line': '1', 'currency': 'dollars'},
                                                                      'three letters'),
                            ({'per_line': '1', 'ports_per_device': 0}, 'from 1 to 64'),
                            ({'per_line': ''}, 'price per line')):
        with pytest.raises(pots_quote.QuoteError, match=message):
            pots_quote.record_quote(database, 'Box', **kwargs)
    with pytest.raises(pots_quote.QuoteError):
        pots_quote.record_published(database, 'nothing')
    with pytest.raises(pots_quote.QuoteError):
        pots_quote.remove_quote(database, 'Box')


def test_0079_is_the_head_and_its_downgrade_keeps_quotes(database):  # noqa: F811
    schema.upgrade_schema(database)
    assert schema_pots_quote.REVISION == '0079_pots_quote' == schema.HEAD
    assert schema.FAX_SERVER_RENEWAL == '0078_fax_server_renewal'
    assert schema_pots_quote.TABLES <= schema.STRICT_TABLES and schema_pots_quote.TABLES <= _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(_table(database, 'pots_quotes').insert().values(
                id='q1', name='Box', state='maybe', created_at=NOW))
    _downgrade(database, '0078_fax_server_renewal')
    assert 'pots_quotes' not in _tables(database)
    schema.upgrade_schema(database)
    pots_quote.record_quote(database, 'Box', per_line='45')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='quotes are recorded'):
        _downgrade(database, '0078_fax_server_renewal')
    assert snapshot(database) == before


BOOTSTRAP = 'synthetic-pots-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}


@pytest.fixture
def client(isolated_installation, monkeypatch):
    from app import main
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'FAX_DEFAULT_COUNTRY': 'US', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def test_the_counter_quote_through_the_server(client):
    assert client.get('/routing/pots-quotes', headers=ADMIN).json()['published'][0]['id'] == 'ooma-airdial'
    client.post('/routing/line-inventory/files', headers=ADMIN, files={'file': ('lines.csv', INVENTORY.encode(),
                                                                                'text/csv')})
    saved = client.put('/routing/pots-quotes', headers=ADMIN, json={'published': 'ooma-airdial', 'term_months': 36})
    assert saved.status_code == 200, saved.text
    assert saved.json()['quotes'][0]['removed_monthly'] == '$199.75'
    entered = client.put('/routing/pots-quotes', headers=ADMIN, json={'name': 'Box', 'per_line': '45',
                                                                     'source_date': '2026-09-01'})
    assert entered.status_code == 200 and len(entered.json()['quotes']) == 2
    bad = client.put('/routing/pots-quotes', headers=ADMIN, json={'name': 'Box', 'per_line': 'cheap'})
    assert bad.status_code == 400 and 'as a number' in bad.json()['detail']
    assert client.post('/routing/pots-quotes/remove', headers=ADMIN, json={'name': 'Box'}).status_code == 200
    assert client.post('/routing/pots-quotes/remove', headers=ADMIN, json={'name': 'Box'}).status_code == 404
    assert client.get('/routing/pots-quotes').status_code in (401, 403)


@pytest.fixture
def pots_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for served in _serve(monkeypatch, tmp_path, FAX_DEFAULT_COUNTRY='US'):
        yield Cli(served)


def test_the_counter_quote_from_the_command_line(pots_cli, tmp_path):
    lines = tmp_path / 'lines.csv'
    lines.write_text(INVENTORY, encoding='utf-8')
    assert pots_cli('numbers', 'move', 'import-inventory', lines).exit_code == 0
    shown = pots_cli('costs', 'recommendations', 'pots')
    assert shown.exit_code == 0 and '--published ooma-airdial' in shown.stdout
    saved = pots_cli('costs', 'recommendations', 'pots', '--published', 'ooma-airdial', '--term', '36')
    said = ' '.join(saved.stdout.split())
    assert saved.exit_code == 0 and 'Take the 5 fax lines out of the Ooma AirDial order' in said
    assert pots_cli('costs', 'recommendations', 'pots', '--name', 'Box').exit_code != 0
    assert pots_cli('costs', 'recommendations', 'pots', '--remove', '--name', 'Ooma AirDial').exit_code == 0
