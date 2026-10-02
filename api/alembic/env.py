"""CLI and application migrations share the guarded connection path."""
import importlib
import os
from pathlib import Path
import sys

from alembic import context
from sqlalchemy.exc import SQLAlchemyError

API_DIRECTORY = Path(__file__).resolve().parents[1]
ROOT = API_DIRECTORY.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(API_DIRECTORY))
package = "api.app" if API_DIRECTORY.name == "api" else "app"
schema = importlib.import_module(package + ".schema")
legacy = importlib.import_module(package + ".schema_legacy")
config = context.config
config.attributes["schema_legacy"] = legacy


def migrate(connection):
    with schema.guarded_migration(connection, lock_timeout=config.attributes.get("lock_timeout", schema.LOCK_TIMEOUT_SECONDS)):
        context.configure(connection=connection, target_metadata=legacy.frozen_metadata(),
                          transactional_ddl=True, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise schema.SchemaUpgradeError("Faxbot upgrades require a live connection for locked schema validation; offline SQL is unsupported.")
elif config.attributes.get("connection") is not None:
    migrate(config.attributes["connection"])
else:
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    engine = schema.create_database_engine(url)
    try:
        with engine.connect() as connection:
            migrate(connection)
    except SQLAlchemyError:
        raise schema.SchemaUpgradeError("Database upgrade could not connect; check database access and retry.") from None
    finally:
        engine.dispose()
