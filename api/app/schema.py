"""One locked, transactional installation/upgrade path for startup and Alembic."""
from contextlib import contextmanager
import os
from pathlib import Path
import re

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from alembic import command
from alembic.config import Config

from .schema_legacy import (
    CORE_TABLES, HYBRIDS, UNIQUE_IDENTITIES, frozen_metadata, has_unique_identity,
)

from . import schema_configuration, schema_outbound, schema_access, schema_authentication, schema_capabilities
from . import schema_delivery, schema_sip, schema_inbound, schema_work, schema_charges, schema_records
from . import schema_batching, schema_inbound_sources, schema_case_packets, schema_fax_engine, schema_terminal
from . import schema_retired_permissions, schema_local_delivery, schema_capacity, schema_history, schema_negotiation
from . import schema_shared_manifest
from . import schema_tollfree
from . import schema_dialed
from . import schema_routing_rules, schema_receiving_rules
from . import schema_dense_pages
from . import schema_fax_codec
from . import schema_peer_fax
from . import schema_screening, schema_engine_frames
from . import schema_case_ledger
from . import schema_forms
from . import schema_destination_schedule
from . import schema_intake_sources
from . import schema_friendly_pages
from . import schema_partner_relay
from . import schema_engine_learning
from . import schema_discovery
from . import schema_accounts
from . import schema_rules_delivery
from . import schema_notice_repair
from . import schema_certainty
from . import schema_send_once
from . import schema_trunks_sites
from . import schema_invoices
from . import schema_continuation
from . import schema_setup_plans
from . import schema_number_advice
from . import schema_digital_routes
from . import schema_engine_extras
from . import schema_measured_codec
from . import schema_shading_method
from . import schema_routing_learning
from . import schema_expected_faxes
from . import schema_fact_advice
from . import schema_encoder_tuning
from . import schema_polled_transmit, schema_analysis
from . import schema_countries
from . import schema_dialing
from . import schema_header_notice
from . import schema_route_selections
from . import schema_closures
from . import schema_station_check
from . import schema_route_families
from . import schema_codec_decoder
from . import schema_test_lines
from . import schema_after_answer
from . import schema_line_inventory
from . import schema_fax_server_renewal
from . import schema_pots_quote
from .schema_checks import canonical_check

FOUNDATION = "0002_schema_foundation"
CONFIGURATION = schema_configuration.REVISION
OUTBOUND = schema_outbound.REVISION
ACCESS = schema_access.REVISION
AUTHENTICATION = schema_authentication.REVISION
CAPABILITIES = schema_capabilities.REVISION
DELIVERY = schema_delivery.REVISION
SIP = schema_sip.REVISION
INBOUND = schema_inbound.REVISION
WORK = schema_work.REVISION
CHARGES = schema_charges.REVISION
RECORDS = schema_records.REVISION
BATCHING = schema_batching.REVISION
INBOUND_SOURCES = schema_inbound_sources.REVISION
CASE_PACKETS = schema_case_packets.REVISION
FAX_ENGINE = schema_fax_engine.REVISION
TERMINAL = schema_terminal.REVISION
RETIRED = schema_retired_permissions.REVISION
LOCAL_DELIVERY = schema_local_delivery.REVISION
CAPACITY = schema_capacity.REVISION
HISTORY = schema_history.REVISION
NEGOTIATION = schema_negotiation.REVISION
SHARED_MANIFEST = schema_shared_manifest.REVISION
TOLLFREE = schema_tollfree.REVISION
DIALED = schema_dialed.REVISION
ROUTING_RULES = schema_routing_rules.REVISION
RECEIVING_RULES = schema_receiving_rules.REVISION
DENSE_PAGES = schema_dense_pages.REVISION
FAX_CODEC = schema_fax_codec.REVISION
PEER_FAX = schema_peer_fax.REVISION
SCREENING = schema_screening.REVISION
ENGINE_FRAMES = schema_engine_frames.REVISION
CASE_LEDGER = schema_case_ledger.REVISION
FORMS = schema_forms.REVISION
DESTINATION_SCHEDULE = schema_destination_schedule.REVISION
INTAKE_SOURCES = schema_intake_sources.REVISION
FRIENDLY_PAGES = schema_friendly_pages.REVISION
PARTNER_RELAY = schema_partner_relay.REVISION
ENGINE_LEARNING = schema_engine_learning.REVISION
DISCOVERY = schema_discovery.REVISION
ACCOUNTS = schema_accounts.REVISION
RULES_DELIVERY = schema_rules_delivery.REVISION
NOTICE_REPAIR = schema_notice_repair.REVISION
CERTAINTY = schema_certainty.REVISION
SEND_ONCE = schema_send_once.REVISION
TRUNKS_SITES = schema_trunks_sites.REVISION
INVOICES = schema_invoices.REVISION
CONTINUATION = schema_continuation.REVISION
SETUP_PLANS = schema_setup_plans.REVISION
NUMBER_ADVICE = schema_number_advice.REVISION
DIGITAL_ROUTES = schema_digital_routes.REVISION
ENGINE_EXTRAS = schema_engine_extras.REVISION
MEASURED_CODEC = schema_measured_codec.REVISION
SHADING_METHOD = schema_shading_method.REVISION
ROUTING_LEARNING = schema_routing_learning.REVISION
EXPECTED_FAXES = schema_expected_faxes.REVISION
FACT_ADVICE = schema_fact_advice.REVISION
ENCODER_TUNING = schema_encoder_tuning.REVISION
POLLED_TRANSMIT = schema_polled_transmit.REVISION
ANALYSIS = schema_analysis.REVISION
COUNTRIES = schema_countries.REVISION
DIALING = schema_dialing.REVISION
HEADER_NOTICE = schema_header_notice.REVISION
ROUTE_SELECTIONS = schema_route_selections.REVISION
CLOSURES = schema_closures.REVISION
STATION_CHECK = schema_station_check.REVISION
ROUTE_FAMILIES = schema_route_families.REVISION
CODEC_DECODER = schema_codec_decoder.REVISION
TEST_LINES = schema_test_lines.REVISION
AFTER_ANSWER = schema_after_answer.REVISION
LINE_INVENTORY = schema_line_inventory.REVISION
FAX_SERVER_RENEWAL = schema_fax_server_renewal.REVISION
HEAD = schema_pots_quote.REVISION
STRICT_TABLES = (schema_access.TABLES | schema_authentication.TABLES | schema_capabilities.TABLES
                 | schema_delivery.TABLES | schema_sip.TABLES | schema_inbound.TABLES | schema_work.TABLES
                 | schema_charges.TABLES | schema_records.TABLES | schema_batching.TABLES
                 | schema_inbound_sources.TABLES | schema_case_packets.TABLES | schema_fax_engine.TABLES
                 | schema_history.TABLES | schema_tollfree.TABLES | schema_routing_rules.TABLES
                 | schema_receiving_rules.TABLES | schema_dense_pages.TABLES
                 | schema_fax_codec.TABLES | schema_screening.TABLES | schema_engine_frames.TABLES
                 | schema_case_ledger.TABLES | schema_forms.TABLES | schema_destination_schedule.TABLES
                 | schema_intake_sources.TABLES | schema_friendly_pages.TABLES | schema_partner_relay.TABLES
                 | schema_engine_learning.TABLES | schema_discovery.TABLES | schema_notice_repair.TABLES
                 | schema_certainty.TABLES | schema_send_once.TABLES
                 | schema_trunks_sites.TABLES
                 | schema_invoices.TABLES
                 | schema_continuation.TABLES
                 | schema_setup_plans.TABLES
                 | schema_number_advice.TABLES
                 | schema_digital_routes.TABLES
                 | schema_measured_codec.TABLES | schema_routing_learning.TABLES | schema_expected_faxes.TABLES | schema_fact_advice.TABLES | schema_encoder_tuning.TABLES | schema_polled_transmit.TABLES | schema_analysis.TABLES | schema_countries.TABLES | schema_dialing.TABLES | schema_header_notice.TABLES | schema_route_selections.TABLES | schema_closures.TABLES | schema_station_check.TABLES | schema_route_families.TABLES | schema_test_lines.TABLES | schema_after_answer.TABLES | schema_line_inventory.TABLES | schema_fax_server_renewal.TABLES | schema_pots_quote.TABLES)
INITIAL = "0001_initial"
LOCK_ID = 0x464158424F54  # FAXBOT, stable across processes and releases
LOCK_TIMEOUT_SECONDS = 10
API_DIRECTORY = Path(__file__).resolve().parents[1]


class SchemaUpgradeError(RuntimeError):
    """Safe operator-facing error; never includes SQL values or database URLs."""


def create_database_engine(url, *, lock_timeout=LOCK_TIMEOUT_SECONDS, **kwargs):
    parsed = sa.engine.make_url(url)
    if parsed.get_backend_name() not in {"sqlite", "postgresql"}:
        raise SchemaUpgradeError("Faxbot database must use SQLite or PostgreSQL.")
    connect_args = dict(kwargs.pop("connect_args", {}))
    if parsed.get_backend_name() == "sqlite":
        connect_args.setdefault("timeout", lock_timeout)
    engine = sa.create_engine(parsed, future=True, connect_args=connect_args, **kwargs)
    if engine.dialect.name == "sqlite":
        database = parsed.database if parsed.database and parsed.database != ":memory:" else None

        @sa.event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
            if database:
                keep_database_private(database)
    return engine


def keep_database_private(path):
    """The SQLite database and its journal files are the owner's alone (mode 0600 at most).

    It sits on the data volume Asterisk shares, and SQLite creates it readable
    by everyone. Run on each connection, so it holds from creation and fixes a
    database made before; it only ever removes access.
    """
    for name in (str(path), f"{path}-wal", f"{path}-shm", f"{path}-journal"):
        try:
            mode = os.stat(name).st_mode
            if mode & 0o077:
                os.chmod(name, mode & 0o700)
        except OSError:
            continue


def upgrade_schema(engine, *, lock_timeout=LOCK_TIMEOUT_SECONDS):
    config = Config(str(API_DIRECTORY / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIRECTORY / "alembic"))
    config.attributes["lock_timeout"] = lock_timeout
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    except SQLAlchemyError:
        raise SchemaUpgradeError("Database upgrade could not complete; check database access and migration locks, then retry.") from None


@contextmanager
def guarded_migration(connection, *, lock_timeout=LOCK_TIMEOUT_SECONDS):
    """The external transaction includes preflight, all DDL/data and version rows."""
    if connection.in_transaction():
        raise SchemaUpgradeError("Schema upgrades require an idle dedicated connection.")
    try:
        if connection.dialect.name == "sqlite":
            # sqlite3 legacy transaction mode does not begin for DDL on Python 3.11.
            connection.exec_driver_sql(f"PRAGMA busy_timeout={max(1, int(lock_timeout * 1000))}")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        elif connection.dialect.name == "postgresql":
            connection.begin()
            connection.execute(sa.text("SELECT set_config('lock_timeout', :timeout, true)"),
                               {"timeout": f"{max(1, int(lock_timeout * 1000))}ms"})
            connection.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})
        else:
            raise SchemaUpgradeError("Faxbot database must use SQLite or PostgreSQL.")
        validate_schema(connection)
        yield connection
        validate_schema(connection, require_version=True)
        connection.commit()
    except SQLAlchemyError:
        connection.rollback()
        raise SchemaUpgradeError("Database upgrade could not complete; check database access and migration locks, then retry.") from None
    except BaseException:
        connection.rollback()
        raise


def _reject(reason):
    raise SchemaUpgradeError("Database schema is unsupported: " + reason + ". Restore the expected schema or review the migration before retrying.")


def _validate_namespace(connection):
    names = tuple(sorted(CORE_TABLES | schema_configuration.TABLES | schema_outbound.TABLES | STRICT_TABLES | {"alembic_version"}))
    if connection.dialect.name == "postgresql":
        rows = connection.execute(sa.text("""
            SELECT c.relname, n.nspname, c.relkind, c.relrowsecurity,
                   EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid=c.oid OR i.inhparent=c.oid) AS inherited
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relname IN :names AND n.nspname = ANY(current_schemas(false))
        """).bindparams(sa.bindparam("names", expanding=True)), {"names": names}).mappings().all()
        current = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
        for row in rows:
            if row["nspname"] != current or row["relkind"] != "r" or row["relrowsecurity"] or row["inherited"]:
                _reject("core objects must be ordinary tables in a single current schema without row security or inheritance")
    else:
        rows = connection.exec_driver_sql("SELECT name,type FROM sqlite_master").all()
        if any(name in names and kind != "table" for name, kind in rows):
            _reject("reserved core object is not a table")


def _validate_no_write_hooks(connection, present):
    if not present:
        return
    if connection.dialect.name == "sqlite":
        hooks = connection.exec_driver_sql("SELECT tbl_name FROM sqlite_master WHERE type='trigger'").scalars()
        if set(hooks) & present:
            _reject("user triggers on core tables are not supported")
    else:
        hooks = connection.execute(sa.text("""
            SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE NOT t.tgisinternal AND n.nspname=current_schema()
            UNION
            SELECT c.relname FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema()
        """)).scalars()
        if set(hooks) & present:
            _reject("user triggers or rewrite rules on core tables are not supported")
        # SQLAlchemy's FK reflection omits convalidated. A matching NOT VALID
        # constraint can hide preexisting orphan bindings and is not equivalent
        # to the integrity contract of the migration-created schema.
        invalid = connection.execute(sa.text("""
            SELECT c.relname FROM pg_constraint k
            JOIN pg_class c ON c.oid=k.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND NOT k.convalidated
        """)).scalars()
        if set(invalid) & present:
            _reject("unvalidated constraints in core schema")


def _same_type(actual, expected):
    if isinstance(expected, sa.Text):
        return type(actual) in {sa.Text, sa.TEXT}
    if isinstance(expected, sa.String):
        return type(actual) in {sa.String, sa.VARCHAR} and actual.length == expected.length
    if isinstance(expected, sa.DateTime):
        return (isinstance(actual, sa.DateTime) and not actual.timezone
                and getattr(actual, "precision", None) in {None, 6})
    return type(actual) in {sa.Integer, sa.INTEGER}


def _validate_plain_indexes(connection, present):
    """Check catalogs before reflection, which skips some expression indexes."""
    if connection.dialect.name == "sqlite":
        # PRAGMA reflection omits conflict algorithms and non-indexed column
        # collations. Ignore comments/quoted identifiers, inspect SQL keywords.
        for name, definition in connection.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='table'"):
            if name not in present:
                continue
            tokens = _sqlite_keywords(definition or "")
            if "COLLATE" in tokens or any(pair == ("ON", "CONFLICT") for pair in zip(tokens, tokens[1:])):
                _reject("custom SQLite conflict policy or collation in core schema")
        for name in present:
            for index in connection.exec_driver_sql(f"PRAGMA index_list('{name}')").mappings():
                escaped = index["name"].replace("'", "''")
                fields = connection.exec_driver_sql(f"PRAGMA index_xinfo('{escaped}')").all()
                if index["partial"] or any(row[1] == -2 for row in fields):
                    _reject(f"partial or expression index in {name}")
                if any(row[5] and (row[3] or row[4] != "BINARY") for row in fields):
                    _reject(f"custom index ordering or collation in {name}")
    else:
        names = connection.execute(sa.text("""
            SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_class idx ON idx.oid=i.indexrelid JOIN pg_am am ON am.oid=idx.relam
            WHERE n.nspname=current_schema() AND (
                i.indexprs IS NOT NULL OR i.indpred IS NOT NULL
                OR NOT i.indisvalid OR NOT i.indisready OR NOT i.indislive
                OR i.indisexclusion OR NOT i.indimmediate OR i.indnullsnotdistinct
                OR am.amname <> 'btree' OR i.indnatts <> i.indnkeyatts
                OR EXISTS (
                    SELECT 1 FROM generate_series(0, i.indnkeyatts - 1) AS pos
                    JOIN pg_opclass op ON op.oid=i.indclass[pos]
                    JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=i.indkey[pos]
                    -- Frozen index keys use only VARCHAR, Integer and naive
                    -- DateTime. Require their qualified built-in default class;
                    -- a custom class may declare itself DEFAULT and change equality.
                    WHERE NOT op.opcdefault OR op.opcnamespace <> 'pg_catalog'::regnamespace
                        OR op.opcname <> CASE a.atttypid
                            WHEN 'pg_catalog.varchar'::regtype THEN 'text_ops'
                            WHEN 'pg_catalog.int4'::regtype THEN 'int4_ops'
                            WHEN 'pg_catalog.timestamp'::regtype THEN 'timestamp_ops'
                            ELSE '' END
                        OR i.indoption[pos] <> 0 OR i.indcollation[pos] <> a.attcollation
                )
            )
            UNION
            SELECT c.relname FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_type t ON t.oid=a.atttypid
            WHERE n.nspname=current_schema() AND a.attnum > 0 AND NOT a.attisdropped
                AND a.attcollation <> t.typcollation
        """)).scalars()
        if set(names) & present:
            _reject("invalid index, nonhistorical index semantics or collation in core schema")


def _sqlite_keywords(definition):
    ignored = r"--[^\n]*(?:\n|$)|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]"
    unquoted = re.sub(ignored, " ", definition, flags=re.DOTALL)
    return re.findall(r"[A-Za-z_]+", unquoted.upper())


def _allowed_default(table, column, default):
    if default is None:
        return True
    return table == "fax_jobs" and column == "backend" and str(default).strip() in {
        "'sip'", "'sip'::character varying", "('sip'::character varying)",
    }


def _validate_checks(connection, inspector, name, table):
    actual = inspector.get_check_constraints(name)
    if name not in STRICT_TABLES:
        if actual:
            _reject(f"unexpected check constraints in {name}")
        return
    expected = {constraint.name: constraint for constraint in table.constraints
                if isinstance(constraint, sa.CheckConstraint)}
    if len(actual) != len(expected) or {constraint.get('name') for constraint in actual} != set(expected):
        _reject(f"unexpected check constraints in {name}")
    columns = {column.name: column.type for column in table.columns}
    for constraint in actual:
        try:
            reflected = canonical_check(constraint['sqltext'], columns)
            frozen = canonical_check(str(expected[constraint['name']].sqltext), columns)
        except (ValueError, KeyError):
            _reject(f"unsupported check expression in {name}")
        if reflected != frozen:
            _reject(f"changed check expression in {name}")
    if connection.dialect.name == 'postgresql':
        abnormal = connection.execute(sa.text('''
            SELECT 1 FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relname=:name AND k.contype='c'
              AND (NOT k.convalidated OR k.connoinherit OR k.condeferrable OR k.condeferred
                   OR NOT k.conislocal OR k.coninhcount <> 0)
        '''), {'name': name}).first()
        if abnormal:
            _reject(f"unsupported check semantics in {name}")


def _validate_columns(connection, inspector, name, expected, *, complete):
    columns = {column["name"]: column for column in inspector.get_columns(name)}
    expected_names = set(expected.columns.keys())
    if set(columns) - expected_names:
        _reject(f"unexpected columns in {name}")
    missing = expected_names - set(columns)
    optional = {HYBRIDS[name]} if name in HYBRIDS else set()
    if name == "fax_jobs":
        optional |= {"pdf_token", "pdf_token_expires_at"}
        cloud = {"backend", "provider_sid", "pdf_url"}
        if not (cloud & set(columns)):
            optional |= cloud
        elif not cloud <= set(columns):
            _reject("incomplete historical provider columns in fax_jobs")
    if missing and (complete or not missing <= optional):
        _reject(f"missing required columns in {name}")
    if inspector.get_pk_constraint(name).get("constrained_columns") != ["id"]:
        _reject(f"unexpected primary key in {name}")
    if name in STRICT_TABLES and inspector.get_pk_constraint(name).get('name') != expected.primary_key.name:
        _reject(f"unexpected primary key name in {name}")
    _validate_checks(connection, inspector, name, expected)
    actual_foreign_keys = inspector.get_foreign_keys(name)
    expected_foreign_keys = {
        (tuple(element.parent.name for element in constraint.elements),
         constraint.referred_table.name,
         tuple(element.column.name for element in constraint.elements), constraint.ondelete,
         constraint.name if name in STRICT_TABLES else None)
        for constraint in expected.foreign_key_constraints
    }
    actual_shapes = set()
    for constraint in actual_foreign_keys:
        options = constraint.get("options", {})
        if constraint.get("referred_schema") is not None or any(
            key != "ondelete" and value not in (None, False) for key, value in options.items()
        ):
            _reject(f"unsupported foreign key semantics in {name}")
        actual_shapes.add((tuple(constraint["constrained_columns"]), constraint["referred_table"],
                           tuple(constraint["referred_columns"]), options.get("ondelete"),
                           constraint.get('name') if name in STRICT_TABLES else None))
    if actual_shapes != expected_foreign_keys or len(actual_foreign_keys) != len(expected_foreign_keys):
        _reject(f"unexpected foreign keys in {name}")
    for key, column in columns.items():
        field = expected.c[key]
        if not _same_type(column["type"], field.type) or bool(column["nullable"]) != field.nullable:
            _reject(f"incompatible column definition in {name}")
        if column.get("computed") or column.get("identity") or not _allowed_default(name, key, column.get("default")):
            _reject(f"unsupported column default or expression in {name}")


def _validate_indexes(connection, inspector, name, table, *, complete):
    expected = {index.name: (tuple(col.name for col in index.columns), bool(index.unique)) for index in table.indexes}
    if name in STRICT_TABLES:
        actual = {index['name']: (tuple(index['column_names']), bool(index['unique']))
                  for index in inspector.get_indexes(name)}
        if actual != expected or inspector.get_unique_constraints(name):
            _reject(f"unexpected or missing index in {name}")
    identity = UNIQUE_IDENTITIES.get(name)
    admissible = set(expected.values())
    if identity:
        admissible.add((identity, True))
    for index in inspector.get_indexes(name):
        shape = (tuple(index["column_names"]), bool(index["unique"]))
        equivalent_identity = identity and shape[1] and set(shape[0]) == set(identity) and len(shape[0]) == len(identity)
        if (shape not in admissible and not equivalent_identity) or (index["name"] in expected and shape != expected[index["name"]]):
            _reject(f"unsupported or conflicting index in {name}")
        options = index.get("dialect_options", {})
        if index.get("expressions") or any(value is not None and value is not False and value != [] for value in options.values()):
            _reject(f"partial or expression index in {name}")
    for constraint in inspector.get_unique_constraints(name):
        if not identity or set(constraint["column_names"]) != set(identity) or len(constraint["column_names"]) != len(identity):
            _reject(f"unexpected uniqueness in {name}")
    if identity and not has_unique_identity(inspector, name, identity):
        fields = ",".join(identity)
        if connection.execute(sa.text(f"SELECT 1 FROM {name} GROUP BY {fields} HAVING COUNT(*) > 1 LIMIT 1")).first():
            _reject(f"duplicate identity rows in {name}")
        if complete:
            _reject(f"missing identity uniqueness in {name}")
    if complete:
        actual = {(tuple(index["column_names"]), bool(index["unique"])) for index in inspector.get_indexes(name)}
        for shape in expected.values():
            if shape not in actual and not (shape[1] and has_unique_identity(inspector, name, shape[0])):
                _reject(f"missing required index in {name}")


def validate_schema(connection, *, require_version=False):
    if connection.dialect.name == 'sqlite':
        if connection.exec_driver_sql('PRAGMA foreign_keys').scalar_one() != 1:
            _reject('SQLite foreign key enforcement is disabled')
        if connection.exec_driver_sql('PRAGMA ignore_check_constraints').scalar_one() != 0:
            _reject('SQLite CHECK enforcement is disabled')
    _validate_namespace(connection)
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    present = tables & CORE_TABLES
    extensions = tables & schema_configuration.TABLES
    outbound = tables & schema_outbound.TABLES
    access = tables & schema_access.TABLES
    authentication = tables & schema_authentication.TABLES
    capabilities = tables & schema_capabilities.TABLES
    delivery = tables & schema_delivery.TABLES
    sip = tables & schema_sip.TABLES
    inbound = tables & schema_inbound.TABLES
    work = tables & schema_work.TABLES
    charges = tables & schema_charges.TABLES
    records = tables & schema_records.TABLES
    batching = tables & schema_batching.TABLES
    case_packets = tables & schema_case_packets.TABLES
    fax_engine = tables & schema_fax_engine.TABLES
    history = tables & schema_history.TABLES
    tollfree = tables & schema_tollfree.TABLES
    routing_rules = tables & schema_routing_rules.TABLES
    receiving_rules = tables & schema_receiving_rules.TABLES
    dense_pages = tables & schema_dense_pages.TABLES
    fax_codec = tables & schema_fax_codec.TABLES
    screening = tables & schema_screening.TABLES
    frames = tables & schema_engine_frames.TABLES
    case_ledger = tables & schema_case_ledger.TABLES
    forms = tables & schema_forms.TABLES
    schedule = tables & schema_destination_schedule.TABLES
    intake_sources = tables & schema_intake_sources.TABLES
    friendly_pages = tables & schema_friendly_pages.TABLES
    relay = tables & schema_partner_relay.TABLES
    learning = tables & schema_engine_learning.TABLES
    discovery = tables & schema_discovery.TABLES
    notice_repair = tables & schema_notice_repair.TABLES
    certainty = tables & schema_certainty.TABLES
    send_once = tables & schema_send_once.TABLES
    trunks_sites = tables & schema_trunks_sites.TABLES
    invoices = tables & schema_invoices.TABLES
    continuation = tables & schema_continuation.TABLES
    setup_plans = tables & schema_setup_plans.TABLES
    number_advice = tables & schema_number_advice.TABLES
    digital = tables & schema_digital_routes.TABLES
    measured_codec = tables & schema_measured_codec.TABLES
    routing_learning = tables & schema_routing_learning.TABLES
    expected = tables & schema_expected_faxes.TABLES
    fact_advice = tables & schema_fact_advice.TABLES
    encoder_tuning = tables & schema_encoder_tuning.TABLES
    polled_transmit = tables & schema_polled_transmit.TABLES
    analysis = tables & schema_analysis.TABLES
    countries = tables & schema_countries.TABLES
    dialing = tables & schema_dialing.TABLES
    header_notices = tables & schema_header_notice.TABLES
    route_selections = tables & schema_route_selections.TABLES
    closures = tables & schema_closures.TABLES
    station_checks = tables & schema_station_check.TABLES
    route_families = tables & schema_route_families.TABLES
    test_lines = tables & schema_test_lines.TABLES
    after_answer = tables & schema_after_answer.TABLES
    line_inventory = tables & schema_line_inventory.TABLES
    fax_server_renewal = tables & schema_fax_server_renewal.TABLES
    pots_quotes = tables & schema_pots_quote.TABLES
    protected = (present | extensions | outbound | access | authentication | capabilities | delivery | sip | inbound
                 | work | charges | records | batching | case_packets | fax_engine | history | tollfree
                 | routing_rules | receiving_rules | dense_pages | fax_codec | screening | frames | case_ledger
                 | forms | schedule | intake_sources | friendly_pages | relay | learning | discovery | notice_repair
                 | certainty | send_once | trunks_sites | invoices | continuation | setup_plans | number_advice
                 | digital | measured_codec | routing_learning | expected | fact_advice | encoder_tuning | polled_transmit | analysis | countries | dialing | header_notices | route_selections | closures | station_checks | route_families | test_lines | after_answer | line_inventory | fax_server_renewal | pots_quotes
                 | ({"alembic_version"} & tables))
    _validate_no_write_hooks(connection, protected)
    _validate_plain_indexes(connection, protected)
    revision = None
    if "alembic_version" in tables:
        columns = inspector.get_columns("alembic_version")
        if len(columns) != 1 or columns[0]["name"] != "version_num" or not _same_type(columns[0]["type"], sa.String(32)) or columns[0]["nullable"] or columns[0].get("default") is not None:
            _reject("invalid version table columns")
        if columns[0].get("computed") or columns[0].get("identity"):
            _reject("invalid version table generated value")
        if inspector.get_pk_constraint("alembic_version").get("constrained_columns") != ["version_num"]:
            _reject("invalid version table primary key")
        if (inspector.get_foreign_keys("alembic_version") or inspector.get_check_constraints("alembic_version")
                or inspector.get_indexes("alembic_version") or inspector.get_unique_constraints("alembic_version")):
            _reject("invalid version table constraints")
        revisions = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
        if len(revisions) > 1 or any(value not in {INITIAL, FOUNDATION, CONFIGURATION, OUTBOUND, ACCESS, AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}
                                         for value in revisions):
            _reject("unknown or multiple migration revisions")
        revision = revisions[0] if revisions else None
    if require_version and revision is None:
        _reject("upgrade did not produce a version")
    if present not in (set(), {"fax_jobs"}, CORE_TABLES) or (revision and present != CORE_TABLES):
        _reject("incomplete core table set")
    if revision in {CONFIGURATION, OUTBOUND, ACCESS, AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if extensions != schema_configuration.TABLES:
            _reject("incomplete configuration table set")
        metadata_factory = lambda: schema_configuration.frozen_metadata(dialect=connection.dialect.name)
    else:
        if extensions:
            _reject("configuration tables exist before their migration revision")
        metadata_factory = lambda: frozen_metadata()
    if revision in {OUTBOUND, ACCESS, AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if outbound != schema_outbound.TABLES:
            _reject("incomplete outbound table set")
        metadata_factory = lambda: schema_outbound.frozen_metadata(dialect=connection.dialect.name)
    elif outbound:
        _reject("outbound tables exist before their migration revision")
    if revision in {ACCESS, AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if access != schema_access.TABLES:
            _reject('incomplete access table set')
        metadata_factory = lambda: schema_access.frozen_metadata(dialect=connection.dialect.name)
    elif access:
        _reject('access tables exist before their migration revision')
    if revision in {AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if authentication != schema_authentication.TABLES:
            _reject('incomplete authentication admission table set')
        metadata_factory = lambda: schema_authentication.frozen_metadata(dialect=connection.dialect.name)
    elif authentication:
        _reject('authentication admission tables exist before their migration revision')
    if revision in {CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if capabilities != schema_capabilities.TABLES:
            _reject('incomplete capability table set')
        metadata_factory = lambda: schema_capabilities.frozen_metadata(dialect=connection.dialect.name)
    elif capabilities:
        _reject('capability tables exist before their migration revision')
    if revision in {DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if delivery != schema_delivery.TABLES:
            _reject('incomplete delivery route table set')
        metadata_factory = lambda: schema_delivery.frozen_metadata(dialect=connection.dialect.name)
    elif delivery:
        _reject('delivery route tables exist before their migration revision')
    if revision in {SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if sip != schema_sip.TABLES:
            _reject('incomplete SIP call record table set')
        metadata_factory = lambda: schema_sip.frozen_metadata(dialect=connection.dialect.name)
    elif sip:
        _reject('SIP call record tables exist before their migration revision')
    if revision in {INBOUND, WORK, CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if inbound != schema_inbound.TABLES:
            _reject('incomplete inbound import table set')
        metadata_factory = lambda: schema_inbound.frozen_metadata(dialect=connection.dialect.name)
    elif inbound:
        _reject('inbound import tables exist before their migration revision')
    if revision in {WORK, CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if work != schema_work.TABLES:
            _reject('incomplete work item table set')
        metadata_factory = lambda: schema_work.frozen_metadata(dialect=connection.dialect.name)
    elif work:
        _reject('work item tables exist before their migration revision')
    if revision in {CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if charges != schema_charges.TABLES:
            _reject('incomplete carrier charge table set')
        metadata_factory = lambda: schema_charges.frozen_metadata(dialect=connection.dialect.name)
    elif charges:
        _reject('carrier charge tables exist before their migration revision')
    if revision in {RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if records != schema_records.TABLES:
            _reject('incomplete carrier record table set')
        metadata_factory = lambda: schema_records.frozen_metadata(dialect=connection.dialect.name)
    elif records:
        _reject('carrier record tables exist before their migration revision')
    if revision in {BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if batching != schema_batching.TABLES:
            _reject('incomplete sending-together table set')
        metadata_factory = lambda: schema_batching.frozen_metadata(dialect=connection.dialect.name)
    elif batching:
        _reject('sending-together tables exist before their migration revision')
    if revision in {INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0015 adds no table; it widens the inbound import source constraint.
        metadata_factory = lambda: schema_inbound_sources.frozen_metadata(dialect=connection.dialect.name)
    if revision in {CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if case_packets != schema_case_packets.TABLES:
            _reject('incomplete case packet send table set')
        metadata_factory = lambda: schema_case_packets.frozen_metadata(dialect=connection.dialect.name)
    elif case_packets:
        _reject('case packet send tables exist before their migration revision')
    if revision in {FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if fax_engine != schema_fax_engine.TABLES:
            _reject('incomplete fax engine record table set')
        metadata_factory = lambda: schema_fax_engine.frozen_metadata(dialect=connection.dialect.name)
    elif fax_engine:
        _reject('fax engine record tables exist before their migration revision')
    if revision in {LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0020 adds no table: a widened inbound source constraint and one nullable fax_jobs column.
        metadata_factory = lambda: schema_local_delivery.frozen_metadata(dialect=connection.dialect.name)
    if revision in {CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0021 adds no table: nullable fax_jobs.urgent and delivery_destinations.max_calls.
        metadata_factory = lambda: schema_capacity.frozen_metadata(dialect=connection.dialect.name)
    if revision in {HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if history != schema_history.TABLES:
            _reject('incomplete history table set')
        metadata_factory = lambda: schema_history.frozen_metadata(dialect=connection.dialect.name)
    elif history:
        _reject('history tables exist before their migration revision')
    if revision in {NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0023 adds no table: nullable negotiation columns on fax_engine_calls.
        metadata_factory = lambda: schema_negotiation.frozen_metadata(dialect=connection.dialect.name)
    if revision in {SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0025 adds no table: nullable index-page columns on the sending-together tables.
        metadata_factory = lambda: schema_shared_manifest.frozen_metadata(dialect=connection.dialect.name)
    if revision in {TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if tollfree != schema_tollfree.TABLES:
            _reject('incomplete toll-free approval table set')
        metadata_factory = lambda: schema_tollfree.frozen_metadata(dialect=connection.dialect.name)
    elif tollfree:
        _reject('toll-free approval tables exist before their migration revision')
    if revision in {DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0027 adds no table: nullable dialed-number columns on outbound_deliveries and outbound_attempts.
        metadata_factory = lambda: schema_dialed.frozen_metadata(dialect=connection.dialect.name)
    if revision in {ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if routing_rules != schema_routing_rules.TABLES:
            _reject('incomplete sending rule table set')
        metadata_factory = lambda: schema_routing_rules.frozen_metadata(dialect=connection.dialect.name)
    elif routing_rules:
        _reject('sending rule tables exist before their migration revision')
    if revision in {RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if receiving_rules != schema_receiving_rules.TABLES:
            _reject('incomplete receiving rule table set')
        metadata_factory = lambda: schema_receiving_rules.frozen_metadata(dialect=connection.dialect.name)
    elif receiving_rules:
        _reject('receiving rule tables exist before their migration revision')
    if revision in {DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if dense_pages != schema_dense_pages.TABLES:
            _reject('incomplete dense pages table set')
        metadata_factory = lambda: schema_dense_pages.frozen_metadata(dialect=connection.dialect.name)
    elif dense_pages:
        _reject('dense pages tables exist before their migration revision')
    if revision in {FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if fax_codec != schema_fax_codec.TABLES:
            _reject('incomplete fax payload codec table set')
        metadata_factory = lambda: schema_fax_codec.frozen_metadata(dialect=connection.dialect.name)
    elif fax_codec:
        _reject('fax payload codec tables exist before their migration revision')
    if revision in {PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0034 adds no table: nullable peer fax columns on direct_peers and direct_deliveries.
        metadata_factory = lambda: schema_peer_fax.frozen_metadata(dialect=connection.dialect.name)
    if revision in {SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if screening != schema_screening.TABLES:
            _reject('incomplete junk screening table set')
        metadata_factory = lambda: schema_screening.frozen_metadata(dialect=connection.dialect.name)
    elif screening:
        _reject('junk screening tables exist before their migration revision')
    if revision in {ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if frames != schema_engine_frames.TABLES:
            _reject('incomplete engine frame table set')
        metadata_factory = lambda: schema_engine_frames.frozen_metadata(dialect=connection.dialect.name)
    elif frames:
        _reject('engine frame tables exist before their migration revision')
    if revision in {CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if case_ledger != schema_case_ledger.TABLES:
            _reject('incomplete case ledger table set')
        metadata_factory = lambda: schema_case_ledger.frozen_metadata(dialect=connection.dialect.name)
    elif case_ledger:
        _reject('case ledger tables exist before their migration revision')
    if revision in {FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if forms != schema_forms.TABLES:
            _reject('incomplete registered form table set')
        metadata_factory = lambda: schema_forms.frozen_metadata(dialect=connection.dialect.name)
    elif forms:
        _reject('registered form tables exist before their migration revision')
    if revision in {DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if schedule != schema_destination_schedule.TABLES:
            _reject('incomplete recipient schedule table set')
        metadata_factory = lambda: schema_destination_schedule.frozen_metadata(dialect=connection.dialect.name)
    elif schedule:
        _reject('recipient schedule tables exist before their migration revision')
    if revision in {INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if intake_sources != schema_intake_sources.TABLES:
            _reject('incomplete intake connector table set')
        metadata_factory = lambda: schema_intake_sources.frozen_metadata(dialect=connection.dialect.name)
    elif intake_sources:
        _reject('intake connector tables exist before their migration revision')
    if revision in {FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if friendly_pages != schema_friendly_pages.TABLES:
            _reject('incomplete fax-friendly pages table set')
        metadata_factory = lambda: schema_friendly_pages.frozen_metadata(dialect=connection.dialect.name)
    elif friendly_pages:
        _reject('fax-friendly pages tables exist before their migration revision')
    if revision in {PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if relay != schema_partner_relay.TABLES:
            _reject('incomplete partner relay table set')
        metadata_factory = lambda: schema_partner_relay.frozen_metadata(dialect=connection.dialect.name)
    elif relay:
        _reject('partner relay tables exist before their migration revision')
    if revision in {ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if learning != schema_engine_learning.TABLES:
            _reject('incomplete engine learning table set')
        metadata_factory = lambda: schema_engine_learning.frozen_metadata(dialect=connection.dialect.name)
    elif learning:
        _reject('engine learning tables exist before their migration revision')
    if revision in {DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if discovery != schema_discovery.TABLES:
            _reject('incomplete partner discovery table set')
        metadata_factory = lambda: schema_discovery.frozen_metadata(dialect=connection.dialect.name)
    elif discovery:
        _reject('partner discovery tables exist before their migration revision')
    if revision in {ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0043 adds no table: nullable subaddress columns on inbound_rule_options and inbound_fax_routing.
        metadata_factory = lambda: schema_accounts.frozen_metadata(dialect=connection.dialect.name)
    if revision in {RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0032 adds no table: the Approve faxes permission rows and outbound_attempts.ended_before_data.
        metadata_factory = lambda: schema_rules_delivery.frozen_metadata(dialect=connection.dialect.name)
    if revision in {NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if notice_repair != schema_notice_repair.TABLES:
            _reject('incomplete notice fax and transfer table set')
        metadata_factory = lambda: schema_notice_repair.frozen_metadata(dialect=connection.dialect.name)
    elif notice_repair:
        _reject('notice fax and transfer tables exist before their migration revision')
    if revision in {CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if certainty != schema_certainty.TABLES:
            _reject('incomplete uncertain sent fax table set')
        metadata_factory = lambda: schema_certainty.frozen_metadata(dialect=connection.dialect.name)
    elif certainty:
        _reject('uncertain sent fax tables exist before their migration revision')
    if revision in {SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if send_once != schema_send_once.TABLES:
            _reject('incomplete send-once and reuse table set')
        metadata_factory = lambda: schema_send_once.frozen_metadata(dialect=connection.dialect.name)
    elif send_once:
        _reject('send-once and reuse tables exist before their migration revision')
    if revision in {TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if trunks_sites != schema_trunks_sites.TABLES:
            _reject('incomplete rate row table set')
        metadata_factory = lambda: schema_trunks_sites.frozen_metadata(dialect=connection.dialect.name)
    elif trunks_sites:
        _reject('rate row tables exist before their migration revision')
    if revision in {INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if invoices != schema_invoices.TABLES:
            _reject('incomplete invoice table set')
        metadata_factory = lambda: schema_invoices.frozen_metadata(dialect=connection.dialect.name)
    elif invoices:
        _reject('invoice tables exist before their migration revision')
    if revision in {CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if continuation != schema_continuation.TABLES:
            _reject('incomplete fax continuation table set')
        metadata_factory = lambda: schema_continuation.frozen_metadata(dialect=connection.dialect.name)
    elif continuation:
        _reject('fax continuation tables exist before their migration revision')
    if revision in {SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if setup_plans != schema_setup_plans.TABLES:
            _reject('incomplete setup plan table set')
        metadata_factory = lambda: schema_setup_plans.frozen_metadata(dialect=connection.dialect.name)
    elif setup_plans:
        _reject('setup plan tables exist before their migration revision')
    if revision in {NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if number_advice != schema_number_advice.TABLES:
            _reject('incomplete number advice table set')
        metadata_factory = lambda: schema_number_advice.frozen_metadata(dialect=connection.dialect.name)
    elif number_advice:
        _reject('number advice tables exist before their migration revision')
    if revision in {DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if digital != schema_digital_routes.TABLES:
            _reject('incomplete digital route table set')
        metadata_factory = lambda: schema_digital_routes.frozen_metadata(dialect=connection.dialect.name)
    elif digital:
        _reject('digital route tables exist before their migration revision')
    if revision in {ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0054 adds no table: nullable columns on sip_call_records, inbound_rule_options and inbound_fax_routing.
        metadata_factory = lambda: schema_engine_extras.frozen_metadata(dialect=connection.dialect.name)
    if revision in {MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if measured_codec != schema_measured_codec.TABLES:
            _reject('incomplete measured coding table set')
        metadata_factory = lambda: schema_measured_codec.frozen_metadata(dialect=connection.dialect.name)
    elif measured_codec:
        _reject('measured coding tables exist before their migration revision')
    if revision in {SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0058 adds no table: the nullable fax_friendly_pages.method.
        metadata_factory = lambda: schema_shading_method.frozen_metadata(dialect=connection.dialect.name)
    if revision in {ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if routing_learning != schema_routing_learning.TABLES:
            _reject('incomplete polling table set')
        metadata_factory = lambda: schema_routing_learning.frozen_metadata(dialect=connection.dialect.name)
    elif routing_learning:
        _reject('polling tables exist before their migration revision')
    if revision in {EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if expected != schema_expected_faxes.TABLES:
            _reject('incomplete expected fax table set')
        metadata_factory = lambda: schema_expected_faxes.frozen_metadata(dialect=connection.dialect.name)
    elif expected:
        _reject('expected fax tables exist before their migration revision')
    if revision in {FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if fact_advice != schema_fact_advice.TABLES:
            _reject('incomplete fact advice table set')
        metadata_factory = lambda: schema_fact_advice.frozen_metadata(dialect=connection.dialect.name)
    elif fact_advice:
        _reject('fact advice tables exist before their migration revision')
    if revision in {ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if encoder_tuning != schema_encoder_tuning.TABLES:
            _reject('incomplete encoder tuning table set')
        metadata_factory = lambda: schema_encoder_tuning.frozen_metadata(dialect=connection.dialect.name)
    elif encoder_tuning:
        _reject('encoder tuning tables exist before their migration revision')
    if revision in {POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if polled_transmit != schema_polled_transmit.TABLES:
            _reject('incomplete polled transmission table set')
        metadata_factory = lambda: schema_polled_transmit.frozen_metadata(dialect=connection.dialect.name)
    elif polled_transmit:
        _reject('polled transmission tables exist before their migration revision')
    if revision in {ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if analysis != schema_analysis.TABLES:
            _reject('incomplete analysis table set')
        metadata_factory = lambda: schema_analysis.frozen_metadata(dialect=connection.dialect.name)
    elif analysis:
        _reject('analysis tables exist before their migration revision')
    if revision in {COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if countries != schema_countries.TABLES:
            _reject('incomplete caller-ID price and registered-sender table set')
        metadata_factory = lambda: schema_countries.frozen_metadata(dialect=connection.dialect.name)
    elif countries:
        _reject('caller-ID price and registered-sender tables exist before their migration revision')
    if revision in {DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if dialing != schema_dialing.TABLES:
            _reject('incomplete dialing guard table set')
        metadata_factory = lambda: schema_dialing.frozen_metadata(dialect=connection.dialect.name)
    elif dialing:
        _reject('dialing guard tables exist before their migration revision')
    if revision in {HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if header_notices != schema_header_notice.TABLES:
            _reject('incomplete header notice table set')
        metadata_factory = lambda: schema_header_notice.frozen_metadata(dialect=connection.dialect.name)
    elif header_notices:
        _reject('header notice tables exist before their migration revision')
    if revision in {ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if route_selections != schema_route_selections.TABLES:
            _reject('incomplete route selection table set')
        metadata_factory = lambda: schema_route_selections.frozen_metadata(dialect=connection.dialect.name)
    elif route_selections:
        _reject('route selection tables exist before their migration revision')
    if revision in {CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if closures != schema_closures.TABLES:
            _reject('incomplete closure, notice and eligibility table set')
        metadata_factory = lambda: schema_closures.frozen_metadata(dialect=connection.dialect.name)
    elif closures:
        _reject('closure, notice and eligibility tables exist before their migration revision')
    if revision in {STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if station_checks != schema_station_check.TABLES:
            _reject('incomplete station check table set')
        metadata_factory = lambda: schema_station_check.frozen_metadata(dialect=connection.dialect.name)
    elif station_checks:
        _reject('station check tables exist before their migration revision')
    if revision in {ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if route_families != schema_route_families.TABLES:
            _reject('incomplete route family table set')
        metadata_factory = lambda: schema_route_families.frozen_metadata(dialect=connection.dialect.name)
    elif route_families:
        _reject('route family tables exist before their migration revision')
    if revision in {CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        # 0071 adds only columns: the recipient's decoder on the encoded-page settings and their history.
        metadata_factory = lambda: schema_codec_decoder.frozen_metadata(dialect=connection.dialect.name)
    if revision in {TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if test_lines != schema_test_lines.TABLES:
            _reject('incomplete test line table set')
        metadata_factory = lambda: schema_test_lines.frozen_metadata(dialect=connection.dialect.name)
    elif test_lines:
        _reject('test line tables exist before their migration revision')
    if revision in {AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if after_answer != schema_after_answer.TABLES:
            _reject('incomplete digits-after-answer table set')
        metadata_factory = lambda: schema_after_answer.frozen_metadata(dialect=connection.dialect.name)
    elif after_answer:
        _reject('digits-after-answer tables exist before their migration revision')
    if revision in {LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}:
        if line_inventory != schema_line_inventory.TABLES:
            _reject('incomplete line inventory table set')
        metadata_factory = lambda: schema_line_inventory.frozen_metadata(dialect=connection.dialect.name)
    elif line_inventory:
        _reject('line inventory tables exist before their migration revision')
    if revision in {FAX_SERVER_RENEWAL, HEAD}:
        if fax_server_renewal != schema_fax_server_renewal.TABLES:
            _reject('incomplete fax server renewal table set')
        metadata_factory = lambda: schema_fax_server_renewal.frozen_metadata(dialect=connection.dialect.name)
    elif fax_server_renewal:
        _reject('fax server renewal tables exist before their migration revision')
    if revision == HEAD:
        if pots_quotes != schema_pots_quote.TABLES:
            _reject('incomplete POTS-replacement quote table set')
        metadata_factory = lambda: schema_pots_quote.frozen_metadata(dialect=connection.dialect.name)
    elif pots_quotes:
        _reject('POTS-replacement quote tables exist before their migration revision')
    # Frozen descriptions include all their predecessors. Build only the final
    # selected description, after every revision and table-set check has passed.
    metadata = metadata_factory()
    complete = revision in {FOUNDATION, CONFIGURATION, OUTBOUND, ACCESS, AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, INBOUND_SOURCES, CASE_PACKETS, FAX_ENGINE, TERMINAL, RETIRED, LOCAL_DELIVERY, CAPACITY, HISTORY, NEGOTIATION, SHARED_MANIFEST, TOLLFREE, DIALED, ROUTING_RULES, RECEIVING_RULES, DENSE_PAGES, FAX_CODEC, PEER_FAX, SCREENING, ENGINE_FRAMES, CASE_LEDGER, FORMS, DESTINATION_SCHEDULE, INTAKE_SOURCES, FRIENDLY_PAGES, PARTNER_RELAY, ENGINE_LEARNING, DISCOVERY, ACCOUNTS, RULES_DELIVERY, NOTICE_REPAIR, CERTAINTY, SEND_ONCE, TRUNKS_SITES, INVOICES, CONTINUATION, SETUP_PLANS, NUMBER_ADVICE, DIGITAL_ROUTES, ENGINE_EXTRAS, MEASURED_CODEC, SHADING_METHOD, ROUTING_LEARNING, EXPECTED_FAXES, FACT_ADVICE, ENCODER_TUNING, POLLED_TRANSMIT, ANALYSIS, COUNTRIES, DIALING, HEADER_NOTICE, ROUTE_SELECTIONS, CLOSURES, STATION_CHECK, ROUTE_FAMILIES, CODEC_DECODER, TEST_LINES, AFTER_ANSWER, LINE_INVENTORY, FAX_SERVER_RENEWAL, HEAD}
    for name in sorted(present | extensions | outbound | access | authentication | capabilities | delivery | sip
                       | inbound | work | charges | records | batching | case_packets | fax_engine | history
                       | tollfree | routing_rules | receiving_rules | dense_pages | fax_codec | screening | frames
                       | case_ledger | forms | schedule | intake_sources | friendly_pages | relay | learning
                       | discovery | notice_repair | certainty | send_once | trunks_sites | invoices | continuation
                       | setup_plans | number_advice | digital | measured_codec | routing_learning | expected | fact_advice | encoder_tuning | polled_transmit | analysis | countries | dialing | header_notices | route_selections | closures | station_checks | route_families | test_lines | after_answer | line_inventory | fax_server_renewal | pots_quotes):
        _validate_columns(connection, inspector, name, metadata.tables[name], complete=complete)
        if name == "fax_jobs" and present == CORE_TABLES and "backend" not in {column["name"] for column in inspector.get_columns(name)}:
            _reject("six-table historical schema is missing provider columns")
        if revision == INITIAL:
            required = set(metadata.tables[name].columns.keys()) - ({HYBRIDS[name]} if name in HYBRIDS else set())
            if not required <= {column["name"] for column in inspector.get_columns(name)}:
                _reject(f"stamped initial schema is incomplete in {name}")
        _validate_indexes(connection, inspector, name, metadata.tables[name], complete=complete)
    # Index names share a schema namespace with unrelated tables. Detect conflicts
    # before any DDL so auxiliary objects can never be replaced or repurposed.
    planned_metadata = schema_pots_quote.frozen_metadata(dialect=connection.dialect.name)
    planned = {index.name: name for name, table in planned_metadata.tables.items() for index in table.indexes}
    planned.update({planned_metadata.tables[name].primary_key.name: name for name in STRICT_TABLES})
    planned.update({f"uq_{name}_identity": name for name in UNIQUE_IDENTITIES})
    if connection.dialect.name == 'postgresql':
        current = connection.exec_driver_sql('SELECT current_schema()').scalar_one()
        objects = connection.execute(sa.text('''
            SELECT c.relname, n.nspname, c.relkind, parent.relname AS owner
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_index i ON i.indexrelid=c.oid
            LEFT JOIN pg_class parent ON parent.oid=i.indrelid
            WHERE c.relname IN :names AND n.nspname=ANY(current_schemas(false))
        ''').bindparams(sa.bindparam('names', expanding=True)), {'names': tuple(planned)}).mappings()
        if any(row['nspname'] != current or row['relkind'] != 'i' or row['owner'] != planned[row['relname']]
               for row in objects):
            _reject('required index name belongs to another table or object')
    else:
        objects = connection.exec_driver_sql('SELECT name,type,tbl_name FROM sqlite_master').all()
        if any(name in planned and (kind != 'index' or owner != planned[name]) for name, kind, owner in objects):
            _reject('required index name belongs to another table or object')
    for name in tables:
        indexes = inspector.get_indexes(name) + [inspector.get_pk_constraint(name)]
        for index in indexes:
            if index["name"] in planned and planned[index["name"]] != name:
                _reject("required index name belongs to another table")
    return revision
