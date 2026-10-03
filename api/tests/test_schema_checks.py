"""Only the reviewed bounded CHECK grammar may match a frozen expression."""
import importlib

import pytest
import sqlalchemy as sa


def canonical(expression):
    try:
        module = importlib.import_module('api.app.schema_checks')
    except ModuleNotFoundError:
        pytest.fail('Frozen CHECK parser is missing')
    return module.canonical_check(expression, {'id': sa.String(40), 'kind': sa.String(16),
        'version': sa.Integer(), 'parent_id': sa.String(40)})


def test_postgres_reviewed_text_casts_and_redundant_grouping_match_sqlite():
    assert canonical("(id = 'state' AND version >= 1) OR parent_id IS NULL") == canonical(
        "((((id)::text = 'state'::text) AND (version >= 1)) OR (parent_id IS NULL))")


@pytest.mark.parametrize('changed', [
    "id = 'STATE'", "id <> 'state'", "id = 'state' OR version >= 0",
    "id = 'state' AND version >= 1", "parent_id IS NOT NULL",
])
def test_literals_operators_null_checks_and_boolean_structure_are_preserved(changed):
    assert canonical(changed) != canonical("id = 'state'")


def test_grouping_that_changes_boolean_precedence_is_not_erased():
    assert canonical("id = 'state' OR kind = 'legacy' AND version >= 1") != canonical(
        "(id = 'state' OR kind = 'legacy') AND version >= 1")


@pytest.mark.parametrize('expression', [
    "id = 'state' OR true", "id = 'state' -- ignored", "id = 'state' /* ignored */",
    "lower(id) = 'state'", "id::citext = 'state'", "version::text = '1'",
    "id = 'state'::varchar", "unknown = 'state'", "id IN ('state')",
    "id = 'state'; SELECT 1", "id = 'state' OR", "NOT id = 'state'",
    "id = 'state' + 1", "id = 'state' COLLATE nocase", "id = 'state'::text::text",
])
def test_unknown_or_nonfrozen_expression_forms_fail_closed(expression):
    with pytest.raises(ValueError):
        canonical(expression)
