"""Migration 0072: route families, upstreams, receive owners and the UPS are additive, validated and kept."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_route_families
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database, snapshot  # noqa: F401

PRIOR = '0074_station_check'  # re-chained at merge (was 0067_route_selections)
NOW = datetime(2026, 10, 10, 12)


def _incident(**changes):
    return {'id': 'incident-1', 'account_key': 'sip', 'provider_id': 'sip', 'transport': 't38', 'phase': 'media',
            'generation': 'epoch-2', 'change_at': NOW, 'change_cause': 'settings', 'first_failure_at': NOW,
            'last_failure_at': NOW, 'destinations': 3, 'failures': 4, 'calls': 5, 'corroborated': 2,
            'opened_at': NOW, **changes}


def test_the_head_is_the_route_family_revision_and_validates(database):  # noqa: F811
    schema.upgrade_schema(database)
    assert schema.ROUTE_FAMILIES == '0072_route_families'
    assert schema_route_families.TABLES <= schema.STRICT_TABLES
    assert schema_route_families.TABLES <= _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


@pytest.mark.parametrize('changes', [{'transport': 'pigeon'}, {'phase': 'later'}, {'change_cause': 'luck'},
                                     {'destinations': 0}, {'failures': 2}, {'corroborated': 4}])
def test_impossible_incidents_are_refused(database, changes):  # noqa: F811
    schema.upgrade_schema(database)
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(_table(database, 'route_family_incidents').insert().values(**_incident(**changes)))


def test_one_incident_per_family_and_change_point_and_one_closure(database):  # noqa: F811
    schema.upgrade_schema(database)
    incidents, closures = _table(database, 'route_family_incidents'), _table(database, 'route_family_closures')
    with database.begin() as connection:
        connection.execute(incidents.insert().values(**_incident()))
        connection.execute(closures.insert().values(id='c1', incident_id='incident-1', closed_at=NOW,
                                                    reason='went_through', retired=0, retired_ids='[]'))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(incidents.insert().values(**_incident(id='incident-2')))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(closures.insert().values(id='c2', incident_id='incident-1', closed_at=NOW,
                                                        reason='person', retired=0, retired_ids='[]'))


def test_a_number_has_one_claim_per_generation_and_a_cell_one_send(database):  # noqa: F811
    schema.upgrade_schema(database)
    claims = _table(database, 'receive_owner_claims')
    row = {'number': '+13035550100', 'generation': 1, 'owner': 'sip', 'claimed_at': NOW}
    with database.begin() as connection:
        connection.execute(claims.insert().values(id='claim-1', **row))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(claims.insert().values(id='claim-2', **{**row, 'owner': 'sinch'}))
    tests, sends = _table(database, 'route_family_tests'), _table(database, 'route_family_test_sends')
    with database.begin() as connection:
        connection.execute(tests.insert().values(id='t1', route_a='sip', route_b='sinch', number_a='+13035550100',
                                                 number_b='+13035550101', created_at=NOW))
        connection.execute(sends.insert().values(id='s1', test_id='t1', cell='a1', job_id='j' * 32, sent_at=NOW))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(sends.insert().values(id='s2', test_id='t1', cell='a1', job_id='k' * 32, sent_at=NOW))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(tests.insert().values(id='t2', route_a='sip', route_b='sip', number_a='+13035550100',
                                                     number_b='+13035550101', created_at=NOW))


def test_an_upstream_needs_its_source(database):  # noqa: F811
    schema.upgrade_schema(database)
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(_table(database, 'route_upstreams').insert().values(
                id='u1', provider='sinch', upstream='Carrier', created_at=NOW))


def test_downgrade_keeps_rows_and_an_empty_schema_downgrades(database):  # noqa: F811
    schema.upgrade_schema(database)
    _downgrade(database, PRIOR)
    assert not (schema_route_families.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'power_sources').insert().values(
            id='p1', host='192.0.2.10', port=3493, ups_name='ups', reserve_seconds=120, created_at=NOW))
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='power settings'):
        _downgrade(database, PRIOR)
    assert snapshot(database) == before
