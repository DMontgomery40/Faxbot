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
config.attributes["schema_configuration"] = importlib.import_module(package + ".schema_configuration")
config.attributes["schema_outbound"] = importlib.import_module(package + ".schema_outbound")
config.attributes["schema_access"] = importlib.import_module(package + ".schema_access")
config.attributes["schema_authentication"] = importlib.import_module(package + ".schema_authentication")
config.attributes["schema_capabilities"] = importlib.import_module(package + ".schema_capabilities")
config.attributes["schema_delivery"] = importlib.import_module(package + ".schema_delivery")
config.attributes["schema_sip"] = importlib.import_module(package + ".schema_sip")
config.attributes["schema_inbound"] = importlib.import_module(package + ".schema_inbound")
config.attributes["schema_work"] = importlib.import_module(package + ".schema_work")
config.attributes["schema_charges"] = importlib.import_module(package + ".schema_charges")
config.attributes["schema_records"] = importlib.import_module(package + ".schema_records")
config.attributes["schema_batching"] = importlib.import_module(package + ".schema_batching")
config.attributes["schema_inbound_sources"] = importlib.import_module(package + ".schema_inbound_sources")
config.attributes["schema_case_packets"] = importlib.import_module(package + ".schema_case_packets")
config.attributes["schema_fax_engine"] = importlib.import_module(package + ".schema_fax_engine")
config.attributes["schema_terminal"] = importlib.import_module(package + ".schema_terminal")
config.attributes["schema_retired_permissions"] = importlib.import_module(package + ".schema_retired_permissions")
config.attributes["schema_local_delivery"] = importlib.import_module(package + ".schema_local_delivery")
config.attributes["schema_capacity"] = importlib.import_module(package + ".schema_capacity")
config.attributes["schema_history"] = importlib.import_module(package + ".schema_history")
config.attributes["schema_negotiation"] = importlib.import_module(package + ".schema_negotiation")
config.attributes["schema_shared_manifest"] = importlib.import_module(package + ".schema_shared_manifest")
config.attributes["schema_tollfree"] = importlib.import_module(package + ".schema_tollfree")
config.attributes["schema_dialed"] = importlib.import_module(package + ".schema_dialed")
config.attributes["schema_routing_rules"] = importlib.import_module(package + ".schema_routing_rules")
config.attributes["schema_receiving_rules"] = importlib.import_module(package + ".schema_receiving_rules")
config.attributes["schema_dense_pages"] = importlib.import_module(package + ".schema_dense_pages")
config.attributes["schema_fax_codec"] = importlib.import_module(package + ".schema_fax_codec")
config.attributes["schema_peer_fax"] = importlib.import_module(package + ".schema_peer_fax")
config.attributes["schema_screening"] = importlib.import_module(package + ".schema_screening")
config.attributes["schema_engine_frames"] = importlib.import_module(package + ".schema_engine_frames")
config.attributes["schema_case_ledger"] = importlib.import_module(package + ".schema_case_ledger")
config.attributes["schema_forms"] = importlib.import_module(package + ".schema_forms")
config.attributes["schema_destination_schedule"] = importlib.import_module(package + ".schema_destination_schedule")
config.attributes["schema_intake_sources"] = importlib.import_module(package + ".schema_intake_sources")
config.attributes["schema_friendly_pages"] = importlib.import_module(package + ".schema_friendly_pages")
config.attributes["schema_partner_relay"] = importlib.import_module(package + ".schema_partner_relay")
config.attributes["schema_engine_learning"] = importlib.import_module(package + ".schema_engine_learning")
config.attributes["schema_discovery"] = importlib.import_module(package + ".schema_discovery")
config.attributes["schema_accounts"] = importlib.import_module(package + ".schema_accounts")


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
