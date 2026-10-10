"""Schema validation must not rebuild every superseded historical description."""
from functools import wraps

from api.app import schema
from api.tests.test_schema import database  # noqa: F401 - isolated SQLite/PostgreSQL fixture


def test_current_schema_validation_builds_only_the_selected_description(database, monkeypatch):  # noqa: F811
    """Repeated historical construction caused the full CI suite to exceed six hours."""
    schema.upgrade_schema(database)
    built = []

    def observed(name, factory):
        @wraps(factory)
        def build(*args, **kwargs):
            built.append(name)
            return factory(*args, **kwargs)
        return build

    # Keep every real factory and the real database checks. Historical factories
    # retain their frozen predecessor references; count only top-level requests.
    for name, module in vars(schema).items():
        if name.startswith('schema_') and hasattr(module, 'frozen_metadata'):
            monkeypatch.setattr(module, 'frozen_metadata', observed(name, module.frozen_metadata))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    # One selected description and, if needed, one head description for checking
    # reserved names. Intermediate descriptions would immediately be discarded.
    assert built and set(built) == {'schema_test_lines'}
    assert len(built) <= 2
