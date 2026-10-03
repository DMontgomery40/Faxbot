"""Internal grant projection uses the policy's resource tree and scope kinds."""
import pytest

from api.tests.test_access_policy import world, database
from api.app.access.catalog import BUILTIN_ROLE_PERMISSIONS
from api.app.access.types import ResourceRef, InvalidTransactionError


def test_mixed_operator_role_projects_only_scope_applicable_members(world):
    world.user('alice')
    permissions = BUILTIN_ROLE_PERMISSIONS['role_fax_operator']
    with world.store.transaction() as connection:
        projected = world.control.scope_projection_on(connection, permissions, ResourceRef('personal-alice'))
    applicable = {item.permission for item in projected.applicable}
    assert 'fax:send' in applicable
    assert 'fax:read' in applicable
    assert all(item.resource.id == 'personal-alice' for item in projected.applicable)
    assert applicable.isdisjoint(projected.inactive)
    assert applicable | projected.inactive == permissions
    assert tuple(item.permission for item in projected.applicable) == tuple(sorted(applicable))


def test_root_fax_projection_is_coverage_not_operation_authorization(world):
    from api.tests.test_access_policy import NOW
    actor = world.user('alice')
    world.role('reader', ['fax:read'])
    world.assignment('alice', 'reader')
    root = ResourceRef('installation')
    with world.store.transaction() as connection:
        projected = world.control.scope_projection_on(connection, frozenset({'fax:read'}), root)
        assert [item.permission for item in projected.applicable] == ['fax:read']
        assert not projected.inactive
        assert world.control.can_grant_on(connection, actor, projected.applicable, now=NOW).allowed
        assert not world.control.authorize_on(connection, actor, 'fax:read', root, now=NOW).allowed


def test_inactive_known_members_are_not_unknown_or_invalid_resources(world):
    from api.app.access.types import InvalidScopeError
    fax = world.outbound('fax')
    with world.store.transaction() as connection:
        projected = world.control.scope_projection_on(connection, frozenset({'users:manage'}), fax)
        assert not projected.applicable
        assert projected.inactive == frozenset({'users:manage'})
        for permissions, resource in ((frozenset({'unknown'}), fax),
                                      ({'fax:read'}, fax),
                                      (frozenset({'fax:read'}), ResourceRef('missing'))):
            with pytest.raises(InvalidScopeError, match='^invalid_scope$'):
                world.control.scope_projection_on(connection, permissions, resource)


def test_disabled_ancestor_rejects_even_empty_projection(world):
    from api.app.access.types import InvalidScopeError
    fax = world.outbound('fax')
    world.update('access_resources', 'legacy', enabled=0)
    with world.store.transaction() as connection:
        with pytest.raises(InvalidScopeError):
            world.control.scope_projection_on(connection, frozenset(), fax)


def test_projection_requires_exact_store_lock(world):
    with world.engine.begin() as connection:
        with pytest.raises(InvalidTransactionError):
            world.control.scope_projection_on(connection, frozenset({'fax:read'}), ResourceRef('installation'))
