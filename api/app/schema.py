"""One locked, transactional installation/upgrade path for startup and Alembic."""
from contextlib import contextmanager
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
from . import schema_batching, schema_inbound_sources
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
HEAD = schema_inbound_sources.REVISION
STRICT_TABLES = (schema_access.TABLES | schema_authentication.TABLES | schema_capabilities.TABLES
                 | schema_delivery.TABLES | schema_sip.TABLES | schema_inbound.TABLES | schema_work.TABLES
                 | schema_charges.TABLES | schema_records.TABLES | schema_batching.TABLES
                 | schema_inbound_sources.TABLES)
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
        @sa.event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
    return engine


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
    protected = (present | extensions | outbound | access | authentication | capabilities | delivery | sip | inbound
                 | work | charges | records | batching | ({"alembic_version"} & tables))
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
        if len(revisions) > 1 or any(value not in {INITIAL, FOUNDATION, CONFIGURATION, OUTBOUND, ACCESS, AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, HEAD}
                                         for value in revisions):
            _reject("unknown or multiple migration revisions")
        revision = revisions[0] if revisions else None
    if require_version and revision is None:
        _reject("upgrade did not produce a version")
    if present not in (set(), {"fax_jobs"}, CORE_TABLES) or (revision and present != CORE_TABLES):
        _reject("incomplete core table set")
    if revision in {CONFIGURATION, OUTBOUND, ACCESS, AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, HEAD}:
        if extensions != schema_configuration.TABLES:
            _reject("incomplete configuration table set")
        metadata = schema_configuration.frozen_metadata(dialect=connection.dialect.name)
    else:
        if extensions:
            _reject("configuration tables exist before their migration revision")
        metadata = frozen_metadata()
    if revision in {OUTBOUND, ACCESS, AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, HEAD}:
        if outbound != schema_outbound.TABLES:
            _reject("incomplete outbound table set")
        metadata = schema_outbound.frozen_metadata(dialect=connection.dialect.name)
    elif outbound:
        _reject("outbound tables exist before their migration revision")
    if revision in {ACCESS, AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, HEAD}:
        if access != schema_access.TABLES:
            _reject('incomplete access table set')
        metadata = schema_access.frozen_metadata(dialect=connection.dialect.name)
    elif access:
        _reject('access tables exist before their migration revision')
    if revision in {AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, HEAD}:
        if authentication != schema_authentication.TABLES:
            _reject('incomplete authentication admission table set')
        metadata = schema_authentication.frozen_metadata(dialect=connection.dialect.name)
    elif authentication:
        _reject('authentication admission tables exist before their migration revision')
    if revision in {CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, HEAD}:
        if capabilities != schema_capabilities.TABLES:
            _reject('incomplete capability table set')
        metadata = schema_capabilities.frozen_metadata(dialect=connection.dialect.name)
    elif capabilities:
        _reject('capability tables exist before their migration revision')
    if revision in {DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, HEAD}:
        if delivery != schema_delivery.TABLES:
            _reject('incomplete delivery route table set')
        metadata = schema_delivery.frozen_metadata(dialect=connection.dialect.name)
    elif delivery:
        _reject('delivery route tables exist before their migration revision')
    if revision in {SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, HEAD}:
        if sip != schema_sip.TABLES:
            _reject('incomplete SIP call record table set')
        metadata = schema_sip.frozen_metadata(dialect=connection.dialect.name)
    elif sip:
        _reject('SIP call record tables exist before their migration revision')
    if revision in {INBOUND, WORK, CHARGES, RECORDS, BATCHING, HEAD}:
        if inbound != schema_inbound.TABLES:
            _reject('incomplete inbound import table set')
        metadata = schema_inbound.frozen_metadata(dialect=connection.dialect.name)
    elif inbound:
        _reject('inbound import tables exist before their migration revision')
    if revision in {WORK, CHARGES, RECORDS, BATCHING, HEAD}:
        if work != schema_work.TABLES:
            _reject('incomplete work item table set')
        metadata = schema_work.frozen_metadata(dialect=connection.dialect.name)
    elif work:
        _reject('work item tables exist before their migration revision')
    if revision in {CHARGES, RECORDS, BATCHING, HEAD}:
        if charges != schema_charges.TABLES:
            _reject('incomplete carrier charge table set')
        metadata = schema_charges.frozen_metadata(dialect=connection.dialect.name)
    elif charges:
        _reject('carrier charge tables exist before their migration revision')
    if revision in {RECORDS, BATCHING, HEAD}:
        if records != schema_records.TABLES:
            _reject('incomplete carrier record table set')
        metadata = schema_records.frozen_metadata(dialect=connection.dialect.name)
    elif records:
        _reject('carrier record tables exist before their migration revision')
    if revision in {BATCHING, HEAD}:
        if batching != schema_batching.TABLES:
            _reject('incomplete sending-together table set')
        metadata = schema_batching.frozen_metadata(dialect=connection.dialect.name)
    elif batching:
        _reject('sending-together tables exist before their migration revision')
    if revision == HEAD:
        # 0015 adds no table; it widens the inbound import source constraint.
        metadata = schema_inbound_sources.frozen_metadata(dialect=connection.dialect.name)
    complete = revision in {FOUNDATION, CONFIGURATION, OUTBOUND, ACCESS, AUTHENTICATION, CAPABILITIES, DELIVERY, SIP, INBOUND, WORK, CHARGES, RECORDS, BATCHING, HEAD}
    for name in sorted(present | extensions | outbound | access | authentication | capabilities | delivery | sip
                       | inbound | work | charges | records | batching):
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
    planned_metadata = schema_inbound_sources.frozen_metadata(dialect=connection.dialect.name)
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
