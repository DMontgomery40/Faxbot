"""A recipient's toll-free fax number, used only with a recorded approval: migration 0026, the store and the API.

Synthetic numbers only (555 and 800-555 numbers). The approval table is
append-only; every assertion on history reads the rows back as written.
"""
from datetime import datetime, timedelta
import json

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_tollfree
from api.app.routing.store import RouteStore, RoutingInputError
from api.app.routing.tollfree import (TollFreeApprovals, approval, approved_alternate, current_approval,
                                      toll_free_recommendations)
from api.tests.test_access_schema import at_revision
from api.tests.test_routing_http import ADMIN, client, scoped_key  # noqa: F401  (fixture)
from api.tests.test_schema import database, snapshot  # noqa: F401  (fixture: SQLite and PostgreSQL)


CLINIC = '+12025550123'
TOLL_FREE, OTHER_TOLL_FREE = '+18005550100', '+18885550111'
AGREED = datetime(2026, 10, 3)


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


# -- migration 0026 ------------------------------------------------------------------------------------------

def test_0026_is_head_after_negotiation_and_adds_one_empty_table(database):  # noqa: F811
    assert schema.HEAD == schema_tollfree.REVISION == '0026_tollfree_approval'
    assert schema.NEGOTIATION == '0023_negotiation' and schema_tollfree.TABLES == frozenset({'toll_free_approvals'})
    at_revision(database, '0023_negotiation')
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    assert after['toll_free_approvals'] == []
    assert {name: rows for name, rows in after.items() if name not in ('alembic_version', 'toll_free_approvals')} == {
        name: rows for name, rows in before.items() if name != 'alembic_version'}
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, '0023_negotiation')
    with database.connect() as connection:
        assert not sa.inspect(connection).has_table('toll_free_approvals')
        assert schema.validate_schema(connection, require_version=True) == '0023_negotiation'
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, '0023_negotiation')
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE toll_free_approvals (id VARCHAR(40) PRIMARY KEY)')
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)


# -- the store ---------------------------------------------------------------------------------------------

@pytest.fixture
def approvals(database):  # noqa: F811
    schema.upgrade_schema(database)
    return TollFreeApprovals(database)


def test_an_approval_needs_who_when_and_evidence_and_a_real_toll_free_number(approvals):
    with pytest.raises(RoutingInputError, match='not a toll-free number'):
        approvals.record(CLINIC, action='noted', alternate_number='+13035550100')
    with pytest.raises(RoutingInputError, match='must differ'):
        approvals.record(TOLL_FREE, action='noted', alternate_number=TOLL_FREE)
    with pytest.raises(RoutingInputError, match='who at the recipient agreed'):
        approvals.record(CLINIC, action='approved', alternate_number=TOLL_FREE, approved_on=AGREED, evidence='Email')
    with pytest.raises(RoutingInputError, match='the day the recipient agreed'):
        approvals.record(CLINIC, action='approved', alternate_number=TOLL_FREE, approved_by='Front desk',
                         evidence='Email')
    with pytest.raises(RoutingInputError, match='cannot be in the future'):
        approvals.record(CLINIC, action='approved', alternate_number=TOLL_FREE, approved_by='Front desk',
                         approved_on=datetime.utcnow() + timedelta(days=2), evidence='Email')
    with pytest.raises(RoutingInputError, match='where the agreement is recorded'):
        approvals.record(CLINIC, action='approved', alternate_number=TOLL_FREE, approved_by='Front desk',
                         approved_on=AGREED)
    with pytest.raises(RoutingInputError, match='nothing|no toll-free number on file'):
        approvals.record(CLINIC, action='withdrawn')
    assert approvals.history(CLINIC) == []


def test_only_the_newest_row_counts_and_no_row_is_ever_changed(approvals, database):  # noqa: F811
    noted = approvals.record(CLINIC, action='noted', alternate_number='1-800-555-0100')
    assert (noted['action'], noted['alternate_number'], noted['alternate_display']) == (
        'noted', TOLL_FREE, '+1 800-555-0100')
    assert approved_alternate(CLINIC, engine=database) is None and current_approval(CLINIC, engine=database) is None
    approved = approvals.record(CLINIC, action='approved', alternate_number=TOLL_FREE, approved_by='Dana, intake lead',
                                approved_on=AGREED, evidence='Email "Fax line" from Dana, 3 October 2026')
    assert approved['approved_on'] == '2026-10-03' and approved['approved_by'] == 'Dana, intake lead'
    assert approved_alternate(CLINIC, engine=database) == TOLL_FREE
    assert approvals.approved_alternate('+12025550199') is None
    held = current_approval(CLINIC, engine=database)
    assert held == {'id': approved['id'], 'number': CLINIC, 'alternate': TOLL_FREE, 'recipient_name': None,
                    'approved_by': 'Dana, intake lead', 'approved_at': AGREED, 'withdrawn_at': None,
                    'evidence': 'Email "Fax line" from Dana, 3 October 2026'}
    first_rows = snapshot(database)['toll_free_approvals']
    withdrawn = approvals.record(CLINIC, action='withdrawn', now=datetime.utcnow() + timedelta(seconds=5))
    assert withdrawn['action'] == 'withdrawn' and withdrawn['alternate_number'] == TOLL_FREE
    assert approved_alternate(CLINIC, engine=database) is None
    later = approval(approved['id'], engine=database)
    assert later['withdrawn_at'] is not None and later['alternate'] == TOLL_FREE
    assert approval(noted['id'], engine=database) is None  # a note is not an approval
    rows = snapshot(database)['toll_free_approvals']
    assert len(rows) == 3 and all(row in rows for row in first_rows)
    assert [row['action'] for row in approvals.history(CLINIC)] == ['withdrawn', 'approved', 'noted']


def test_the_reads_work_inside_the_callers_open_transaction(approvals, database):  # noqa: F811
    approvals.record(CLINIC, action='approved', alternate_number=OTHER_TOLL_FREE, approved_by='Lee',
                     approved_on=AGREED, evidence='Signed form on file')
    with database.begin() as connection:
        assert approved_alternate(CLINIC, connection=connection) == OTHER_TOLL_FREE
        assert current_approval(CLINIC, connection=connection)['alternate'] == OTHER_TOLL_FREE


def test_recommendations_say_who_pays_and_what_an_approval_does(approvals, database):  # noqa: F811
    routes = RouteStore(database, sip_preset=lambda: 'telnyx')
    empty = toll_free_recommendations(routes)
    assert empty['state'] == 'none' and empty['items'] == []
    assert 'the recipient pays for those calls' in empty['sentence']
    routes.update_destination(CLINIC, display_name='Synthetic Clinic')
    approvals.record(CLINIC, action='noted', alternate_number=TOLL_FREE)
    (item,) = toll_free_recommendations(routes)['items']
    assert item['approved'] is False and item['spend'] is None
    assert item['sentence'] == ('+1 800-555-0100 is on file but not approved. Calls to a toll-free number are paid by '
                                'the recipient, so record who at Synthetic Clinic agreed before Faxbot uses it. You '
                                'sent no faxes to it by a paid route in the last 30 days.')
    approvals.record(CLINIC, action='approved', alternate_number=TOLL_FREE, approved_by='Dana', approved_on=AGREED,
                     evidence='Email from Dana', now=datetime.utcnow() + timedelta(seconds=5))
    result = toll_free_recommendations(routes)
    assert result['sentence'] == ('1 recipient has a toll-free fax number on file, 1 approved. The recipient pays for '
                                  'each call to a toll-free number.')
    assert result['items'][0]['sentence'].startswith(
        'Dana agreed on 3 October 2026. With this approval, Faxbot sends faxes for Synthetic Clinic to '
        '+1 800-555-0100, and the recipient pays for those calls.')


# -- over HTTPS ------------------------------------------------------------------------------------------

def _audit(client):  # noqa: F811
    store = client.app.state.configuration_runtime.manager.store
    with store.engine.connect() as connection:
        rows = connection.execute(sa.text(
            "SELECT details, outcome FROM access_audit WHERE operation = 'routing.toll_free_approval' "
            'ORDER BY created_at'))
        return [json.loads(row.details) | {'outcome': row.outcome} for row in rows]


def test_recording_an_approval_needs_settings_write_and_is_audited_without_the_evidence(client):  # noqa: F811
    route = '/routing/destinations/%2B12025550123/toll-free'
    assert client.get(route, headers=ADMIN).json() == {'number': CLINIC, 'current': None, 'history': [],
                                                       'approved_alternate': None, 'sentence': None}
    sender = scoped_key(client, ['fax:send'])
    refused = client.post(route, headers=sender, json={'action': 'noted', 'alternate_number': TOLL_FREE})
    assert refused.status_code == 403 and client.get(route, headers=sender).status_code == 403
    bad = client.post(route, headers=ADMIN, json={'action': 'approved', 'alternate_number': TOLL_FREE})
    assert bad.status_code == 400 and bad.json()['detail'] == 'Enter who at the recipient agreed, in up to 200 characters.'
    saved = client.post(route, headers=ADMIN, json={
        'action': 'approved', 'alternate_number': '(800) 555-0100', 'approved_by': 'Dana', 'approved_on': '2026-10-03',
        'evidence': 'Email from Dana, private note'})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body['approved_alternate'] == TOLL_FREE and body['current']['approved_on'] == '2026-10-03'
    assert body['current']['recorded_by_name']  # the bootstrap owner who recorded it
    assert body['sentence'] == ('Dana agreed on 3 October 2026. With this approval, Faxbot sends faxes for the recipient '
                                'to +1 800-555-0100, and the recipient pays for those calls.')
    assert _audit(client) == [{'number': CLINIC, 'alternate_number': TOLL_FREE, 'action': 'approved',
                               'approved_by': 'Dana', 'approved_on': '2026-10-03', 'outcome': 'allowed'}]
    history = client.get(route, headers=ADMIN).json()['history']
    assert [row['action'] for row in history] == ['approved']
    recommendations = client.get('/routing/recommendations/toll-free', headers=ADMIN).json()
    assert recommendations['items'][0]['approved'] is True
    assert client.get('/routing/recommendations/toll-free', headers=sender).status_code == 403


def test_the_npi_registry_suggests_a_number_and_never_approves_it(client, monkeypatch):  # noqa: F811
    import sys
    import types
    route = '/routing/destinations/%2B12025550123/toll-free/suggestions'
    monkeypatch.delitem(sys.modules, 'app.routing.nppes', raising=False)
    missing = client.get(route, headers=ADMIN, params={'npi': '1234567893'})
    assert missing.status_code == 503 and 'not available in this version' in missing.json()['detail']
    asked = []

    def suggested_tollfree(npi=None, *, name=None, city=None, state=None, fetch=None, now=None):
        asked.append((npi, name, city, state))
        if npi == 'down':
            raise ConnectionError('synthetic outage')
        if not npi and not (name and (city or state)):
            raise ValueError('Enter an NPI, or a name with a city or state.')
        return [{'source': 'NPPES', 'npi': '1234567893', 'name': 'SYNTHETIC CLINIC', 'address_purpose': 'mailing address',
                 'address': '1 Example Way, DENVER, CO', 'fax_number': TOLL_FREE,
                 'evidence': 'NPPES record NPI 1234567893, mailing address, read October 7, 2026'}] if npi else []
    monkeypatch.setitem(sys.modules, 'app.routing.nppes', types.SimpleNamespace(suggested_tollfree=suggested_tollfree))
    found = client.get(route, headers=ADMIN, params={'npi': '1234567893'}).json()
    assert found['sentence'].startswith('NPPES lists a toll-free fax number for this provider.')
    assert found['items'][0]['fax_display'] == '+1 800-555-0100'
    assert client.get(route, headers=ADMIN, params={'name': 'Clinic', 'state': 'CO'}).json()['sentence'] == (
        'NPPES lists no toll-free fax number for this provider.')
    assert client.get(route, headers=ADMIN).json()['detail'] == 'Enter an NPI, or a name with a city or state.'
    assert client.get(route, headers=ADMIN, params={'npi': 'down'}).status_code == 502
    # A suggestion records nothing.
    assert client.get('/routing/destinations/%2B12025550123/toll-free', headers=ADMIN).json()['history'] == []
    assert asked[0] == ('1234567893', None, None, None)
