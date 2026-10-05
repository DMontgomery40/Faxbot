"""Single-use capability service and its frozen 0007 table on SQLite and PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading

import pytest
import sqlalchemy as sa

from api.app import schema, schema_capabilities
from api.app.access.capabilities import CapabilityError, CapabilityService
from api.app.access.mutation_types import MutationDeniedError, MutationReason
from api.app.access.types import ResourceRef, StaleCredentialError
from api.tests.test_schema import database, snapshot
from api.tests.test_access_policy import World, NOW, ceiling, key_context
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes, without_work_catalogue


class CapabilityWorld(World):
    def __init__(self, engine):
        super().__init__(engine)
        self.clock = NOW
        self.service = CapabilityService(self.store, self.control, clock=lambda: self.clock)
        self.alice = self.user('alice')
        self.role('terminal-role', ['host:terminal'])
        self.assignment('alice', 'terminal-role')
        self.bob = self.user('bob')

    def rows(self, name):
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(sa.text(f'SELECT * FROM {name}')).mappings()]


@pytest.fixture
def cw(database):
    return CapabilityWorld(database)


def test_mint_binds_principal_and_session_and_stores_only_a_digest(cw):
    issued = cw.service.mint('terminal', cw.alice, timedelta(seconds=30), {'cols': 80}, permission='host:terminal')
    assert issued.secret not in repr(issued) and len(issued.secret) >= 40
    assert issued.expires_at == NOW + timedelta(seconds=30)
    (row,) = cw.rows('access_capabilities')
    assert (row['id'], row['kind'], row['principal_id'], row['session_id']) == (issued.id, 'terminal', 'alice', 'session-alice')
    assert row['consumed_at'] is None and issued.secret not in repr(row)
    audit = [r for r in cw.rows('access_audit') if r['operation'] == 'capability.issue']
    assert len(audit) == 1 and audit[0]['target_id'] == issued.id and audit[0]['actor_session_id'] == 'session-alice'
    assert issued.secret not in repr(audit)


def test_mint_requires_the_named_permission_and_a_current_credential(cw):
    with pytest.raises(MutationDeniedError) as denied:
        cw.service.mint('terminal', cw.bob, timedelta(seconds=30), permission='host:terminal')
    assert denied.value.reason == MutationReason.FORBIDDEN
    with pytest.raises(MutationDeniedError) as invalid:
        cw.service.mint('terminal', cw.alice, timedelta(seconds=30), permission='not:a-permission')
    assert invalid.value.reason == MutationReason.INVALID_INPUT
    for kind, ttl, metadata in (('shell', timedelta(seconds=30), None), ('terminal', timedelta(0), None),
                                ('terminal', timedelta(hours=2), None), ('terminal', timedelta(seconds=5), {'x': object()}),
                                ('terminal', timedelta(seconds=5), {'x': 'y' * 3000})):
        with pytest.raises(MutationDeniedError):
            cw.service.mint(kind, cw.alice, ttl, metadata, permission='host:terminal')
    cw.update('access_principals', 'alice', enabled=0, security_version=2)
    with pytest.raises(StaleCredentialError):
        cw.service.mint('terminal', cw.alice, timedelta(seconds=30), permission='host:terminal')
    assert cw.rows('access_capabilities') == []


def test_consume_is_single_use_kind_bound_and_expires(cw):
    issued = cw.service.mint('terminal', cw.alice, timedelta(seconds=30), {'cols': 80}, permission='host:terminal')
    for kind, secret in (('pairing', issued.secret), ('terminal', issued.secret + 'x'), ('terminal', ''), ('nope', issued.secret)):
        with pytest.raises(CapabilityError):
            cw.service.consume(kind, secret)
    record = cw.service.consume('terminal', issued.secret)
    assert (record.id, record.principal_id, record.session_id, record.metadata) == (issued.id, 'alice', 'session-alice', {'cols': 80})
    assert record.consumed_at == NOW and record.permission == 'host:terminal'
    # The consumer rechecks with the minting credential itself, never a copied authority.
    assert record.actor == cw.alice
    assert cw.control.authorize(record.actor, 'host:terminal', ResourceRef('installation'), now=NOW).allowed
    with pytest.raises(CapabilityError):
        cw.service.consume('terminal', issued.secret)
    late = cw.service.mint('terminal', cw.alice, timedelta(seconds=30), permission='host:terminal')
    with pytest.raises(CapabilityError):
        cw.service.consume('terminal', late.secret, now=NOW + timedelta(seconds=30))
    assert [r['operation'] for r in cw.rows('access_audit') if r['operation'].startswith('capability.')].count('capability.consume') == 1


def test_consume_refuses_a_disabled_principal_or_revoked_session(cw):
    first = cw.service.mint('terminal', cw.alice, timedelta(seconds=30), permission='host:terminal')
    second = cw.service.mint('terminal', cw.alice, timedelta(seconds=30), permission='host:terminal')
    cw.update('access_sessions', 'session-alice', revoked_at=NOW)
    with pytest.raises(CapabilityError):
        cw.service.consume('terminal', first.secret)
    cw.update('access_sessions', 'session-alice', revoked_at=None)
    cw.update('access_principals', 'alice', enabled=0)
    with pytest.raises(CapabilityError):
        cw.service.consume('terminal', second.secret)


def test_a_capability_dies_with_its_minting_key_or_session(cw):
    key = key_context(cw, 'alice')
    ceiling(cw, 'host:terminal')
    revoked = cw.service.mint('pairing', key, timedelta(minutes=5), permission='host:terminal')
    rotated = cw.service.mint('pairing', key, timedelta(minutes=5), permission='host:terminal')
    (stored,) = [r for r in cw.rows('access_capabilities') if r['id'] == revoked.id]
    assert stored['session_id'] is None and revoked.secret not in stored['credential']
    cw.update('access_key_bindings', 'binding', security_version=2)
    with pytest.raises(CapabilityError):
        cw.service.consume('pairing', rotated.secret)
    cw.update('access_key_bindings', 'binding', security_version=1, state='revoked', revoked_at=NOW)
    with pytest.raises(CapabilityError):
        cw.service.consume('pairing', revoked.secret)
    # A refused redemption stays unconsumed; it simply expires.
    assert all(r['consumed_at'] is None for r in cw.rows('access_capabilities'))
    record = cw.service.consume('terminal', cw.service.mint('terminal', cw.alice, timedelta(seconds=30),
                                                            permission='host:terminal').secret)
    cw.update('access_sessions', 'session-alice', revoked_at=NOW)
    with pytest.raises(StaleCredentialError):
        cw.control.authorize(record.actor, 'host:terminal', ResourceRef('installation'), now=NOW)


def test_every_credential_kind_round_trips_without_secrets():
    from api.app.access import capabilities
    from api.app.access.types import (BootstrapEvidence, KeyEvidence, KeySessionEvidence,
                                      PasswordSessionEvidence, PrincipalContext)
    for actor in (PrincipalContext('p', 3, KeyEvidence('b', 2), 'key:abc'),
                  PrincipalContext('p', 3, KeySessionEvidence('s', 'b', 2), 'key:abc'),
                  PrincipalContext('p', 3, PasswordSessionEvidence('s', 4), 'principal:p'),
                  PrincipalContext('bootstrap', 1, BootstrapEvidence('f' * 64, 's'), 'key:env'),
                  PrincipalContext('bootstrap', 1, BootstrapEvidence('f' * 64), 'key:env')):
        stored = capabilities._credential(actor, 'host:terminal')
        assert capabilities._actor(actor.principal_id, stored) == ('host:terminal', actor)


def test_consume_requires_the_minted_permission_to_still_be_held(cw):
    issued = cw.service.mint('terminal', cw.alice, timedelta(seconds=30), permission='host:terminal')
    assignments = cw.tables['access_assignments']
    with cw.engine.begin() as c:
        c.execute(assignments.delete().where(assignments.c.principal_id == 'alice'))
    with pytest.raises(CapabilityError):
        cw.service.consume('terminal', issued.secret)


def test_pairing_codes_are_six_digits_and_unique_while_active(cw, monkeypatch):
    from api.app.access import capabilities
    first = cw.service.mint('pairing', cw.alice, timedelta(minutes=5), permission='host:terminal')
    assert len(first.secret) == 6 and first.secret.isdigit()
    draws = iter([int(first.secret), int(first.secret), 123456])
    monkeypatch.setattr(capabilities.secrets, 'randbelow', lambda bound: next(draws))
    second = cw.service.mint('pairing', cw.alice, timedelta(minutes=5), permission='host:terminal')
    assert second.secret == '123456'
    assert cw.service.consume('pairing', first.secret).id == first.id
    assert cw.service.consume('pairing', '123456').id == second.id


def test_concurrent_redemption_has_exactly_one_winner(cw):
    issued = cw.service.mint('pairing', cw.alice, timedelta(minutes=5), {'device': 'phone'}, permission='host:terminal')
    barrier = threading.Barrier(4)

    def redeem():
        barrier.wait()
        try:
            return cw.service.consume('pairing', issued.secret).id
        except CapabilityError:
            return None

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: redeem(), range(4)))
    assert results.count(issued.id) == 1 and results.count(None) == 3


def test_purge_removes_only_expired_capabilities(cw):
    old = cw.service.mint('terminal', cw.alice, timedelta(seconds=10), permission='host:terminal')
    cw.clock = NOW + timedelta(seconds=5)
    fresh = cw.service.mint('terminal', cw.alice, timedelta(seconds=60), permission='host:terminal')
    assert cw.service.purge(now=NOW + timedelta(seconds=10)) == 1
    assert [r['id'] for r in cw.rows('access_capabilities')] == [fresh.id]
    assert old.id != fresh.id


def test_0007_upgrade_preserves_0006_state_and_validates_frozen_shape(database):
    at_revision(database, '0006_auth_admission')
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert schema_capabilities.REVISION == schema.CAPABILITIES
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    assert after['access_capabilities'] == []
    for name, rows in before.items():
        if name != 'alembic_version':
            assert (without_later_access_changes(name, without_work_catalogue(name, after[name]))
                    == without_later_access_changes(name, rows)), name
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        assert inspector.get_pk_constraint('access_capabilities')['name'] == 'pk_access_capabilities'
        assert {c['name'] for c in inspector.get_check_constraints('access_capabilities')} == {
            'ck_access_capabilities_kind', 'ck_access_capabilities_times'}
        assert {f['name'] for f in inspector.get_foreign_keys('access_capabilities')} == {
            'fk_access_capabilities_principal', 'fk_access_capabilities_session'}
        assert {(i['name'], tuple(i['column_names']), bool(i['unique'])) for i in inspector.get_indexes('access_capabilities')} == {
            (name, columns, unique) for name, columns, unique in schema_capabilities.INDEXES}
    schema.upgrade_schema(database)
    assert snapshot(database) == after


@pytest.mark.parametrize('conflict', ['table', 'view'])
def test_reserved_capability_namespace_refuses_without_changes(database, conflict):
    at_revision(database, '0006_auth_admission')
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql('CREATE TABLE access_capabilities (private_data VARCHAR(40))')
        else:
            connection.exec_driver_sql('CREATE VIEW access_capabilities AS SELECT id FROM access_state')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
