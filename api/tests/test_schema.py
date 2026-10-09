"""Upgrade behavior against frozen SQL, never fixtures built from current models."""
import os
import sqlite3
import subprocess
import sys
import uuid
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from api.app.schema import HEAD, SchemaUpgradeError, create_database_engine, upgrade_schema
from api.app.config_values import ConfigurationValues

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures") / "schema"


@pytest.fixture(params=["sqlite", "postgresql"])
def database(tmp_path, request):
    if request.param == "sqlite":
        engine = create_database_engine("sqlite:///" + str(tmp_path / "schema.db"))
        yield engine
        engine.dispose()
        return
    url = os.environ.get("FAXBOT_SCHEMA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set FAXBOT_SCHEMA_TEST_POSTGRES_URL to run real PostgreSQL schema tests")
    admin = create_database_engine(url)
    namespace = "faxbot_schema_test_" + uuid.uuid4().hex
    with admin.begin() as conn:
        conn.exec_driver_sql(f"CREATE SCHEMA {namespace}")
    engine = create_database_engine(url, connect_args={"options": f"-csearch_path={namespace}"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.exec_driver_sql(f"DROP SCHEMA {namespace} CASCADE")
        admin.dispose()


def load_history(engine, revision, transform=lambda source: source):
    source = (FIXTURES / f"{revision}-{engine.dialect.name}.sql").read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("--"))
    source = transform(source)
    with engine.begin() as conn:
        for statement in source.split(";"):
            if statement.strip():
                conn.exec_driver_sql(statement)


def seed_history(engine):
    """Populate every historical column with nontrivial, distinguishable values."""
    with engine.begin() as conn:
        inspector = sa.inspect(conn)
        for table in inspector.get_table_names():
            columns = inspector.get_columns(table)
            values = {}
            for column in columns:
                name, kind = column["name"], column["type"]
                if isinstance(kind, sa.DateTime):
                    value = "2025-09-01 12:34:56.123456"
                elif isinstance(kind, sa.Integer):
                    value = 7
                else:
                    value = {"id": table + "-original", "backend": "twilio",
                             "outbound_backend": "telnyx", "inbound_backend": "sinch",
                             "status": "delivered"}.get(name, "saved-" + name)
                values[name] = value
            names = ", ".join(values)
            binds = ", ".join(":" + name for name in values)
            conn.execute(sa.text(f"INSERT INTO {table} ({names}) VALUES ({binds})"), values)


def snapshot(engine):
    with engine.connect() as conn:
        inspector = sa.inspect(conn)
        return {table: [dict(row) for row in conn.execute(sa.text(f"SELECT * FROM {table}")).mappings()]
                for table in inspector.get_table_names()}


@pytest.mark.parametrize("revision", ["c41a51bc", "15f81951", "3a480391", "dd8bd991"])
def test_historical_rows_and_auxiliary_children_survive(database, revision, tmp_path):
    load_history(database, revision)
    seed_history(database)
    artifact = tmp_path / "old-fax.tiff"
    artifact.write_bytes(b"original fax artifact, untouched by schema upgrade")
    with database.begin() as conn:
        conn.execute(sa.text("UPDATE fax_jobs SET tiff_path = :path"), {"path": str(artifact)})
        for action in ("CASCADE", "RESTRICT"):
            conn.exec_driver_sql(f"CREATE TABLE child_{action} (job_id VARCHAR(40) REFERENCES fax_jobs(id) ON DELETE {action}, payload TEXT)")
            conn.exec_driver_sql(f"INSERT INTO child_{action} VALUES ('fax_jobs-original', 'preserved')")
    before = snapshot(database)
    upgrade_schema(database)
    after = snapshot(database)
    for table, rows in before.items():
        assert [{name: row[name] for name in rows[0]} for row in after[table]] == rows
    job = after["fax_jobs"][0]
    assert job["backend"] == ("sip" if revision == "c41a51bc" else "twilio")
    assert job["outbound_backend"] == ("telnyx" if revision == "dd8bd991" else job["backend"])
    assert artifact.read_bytes() == b"original fax artifact, untouched by schema upgrade"
    upgrade_schema(database)
    assert snapshot(database) == after


def test_the_sqlite_database_and_its_journals_are_the_owners_alone(tmp_path):
    """Faxbot's SQLite database sits on the data volume Asterisk shares. SQLite makes it readable by
    everyone; each connection takes that away (it never adds access), so a database made before is fixed
    at the next start, and the WAL and shared-memory files, made with the database's mode, follow."""
    path = tmp_path / "faxbot.db"
    sqlite3.connect(path).close()
    os.chmod(path, 0o644)
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").write_bytes(b"")
        os.chmod(f"{path}{suffix}", 0o644)
    engine = create_database_engine("sqlite:///" + str(path))
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS probe (x INTEGER)")
            connection.exec_driver_sql("INSERT INTO probe VALUES (1)")
            connection.commit()
            modes = {name: oct(os.stat(name).st_mode & 0o777)
                     for name in (str(path), f"{path}-wal", f"{path}-shm") if os.path.exists(name)}
    finally:
        engine.dispose()
    assert modes[str(path)] == "0o600" and set(modes.values()) == {"0o600"}, modes
    # It never adds access: a database the owner made read-only stays so.
    os.chmod(path, 0o400)
    reader = create_database_engine("sqlite:///" + str(path))
    try:
        reader.connect().close()
    finally:
        reader.dispose()
    assert oct(os.stat(path).st_mode & 0o777) == "0o400"


def test_fresh_upgrade_is_versioned_and_repeatable(database):
    upgrade_schema(database)
    assert snapshot(database)["alembic_version"] == [{"version_num": HEAD}]
    before = snapshot(database)
    upgrade_schema(database)
    assert snapshot(database) == before


@pytest.mark.parametrize("location", ["repository", "api", "unrelated"])
def test_cli_independent_of_working_directory(tmp_path, location):
    target = tmp_path / "fresh%db.sqlite"
    cwd = {"repository": ROOT, "api": ROOT / "api", "unrelated": tmp_path}[location]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "api/alembic.ini"), "upgrade", "head"],
        cwd=cwd, env={"PATH": os.environ.get("PATH", ""), "DATABASE_URL": "sqlite:///" + str(target)},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [(HEAD,)]


@pytest.mark.parametrize("damage", ["partial", "unknown_column", "trigger", "unknown_revision", "head_partial"])
def test_unknown_schemas_fail_before_mutation(database, damage):
    load_history(database, "c41a51bc")
    seed_history(database)
    with database.begin() as conn:
        if damage == "partial":
            conn.exec_driver_sql("CREATE TABLE api_keys (id INTEGER PRIMARY KEY)")
        elif damage == "unknown_column":
            conn.exec_driver_sql("ALTER TABLE fax_jobs ADD COLUMN mystery TEXT")
        elif damage == "trigger":
            if database.dialect.name == "sqlite":
                conn.exec_driver_sql("CREATE TRIGGER mutate_job AFTER UPDATE ON fax_jobs BEGIN UPDATE fax_jobs SET error='changed' WHERE id=NEW.id; END")
            else:
                conn.exec_driver_sql("CREATE FUNCTION mutate_job_fn() RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN NEW.error = 'changed'; RETURN NEW; END $$")
                conn.exec_driver_sql("CREATE TRIGGER mutate_job BEFORE UPDATE ON fax_jobs FOR EACH ROW EXECUTE FUNCTION mutate_job_fn()")
        else:
            conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
            conn.execute(sa.text("INSERT INTO alembic_version VALUES (:revision)"),
                         {"revision": HEAD if damage == "head_partial" else "unknown"})
    before = snapshot(database)
    definitions = schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


def schema_description(engine):
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            return conn.exec_driver_sql("SELECT type,name,sql FROM sqlite_master ORDER BY name").all()
        return conn.exec_driver_sql("SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema=current_schema() ORDER BY table_name,ordinal_position").all()


def stamp_initial(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('0001_initial')")


def test_stamped_initial_upgrades_without_recreating_tables(database):
    load_history(database, "3a480391")
    seed_history(database)
    stamp_initial(database)
    before = snapshot(database)
    upgrade_schema(database)
    after = snapshot(database)
    assert after.pop("alembic_version") == [{"version_num": HEAD}]
    before.pop("alembic_version")
    for table, rows in before.items():
        assert [{key: row[key] for key in rows[0]} for row in after[table]] == rows


@pytest.mark.parametrize("missing", [("pdf_token",), ("pdf_token_expires_at",), ("pdf_token", "pdf_token_expires_at")])
def test_optional_token_columns_adopt_independently(database, missing):
    load_history(database, "15f81951")
    with database.begin() as conn:
        for name in missing:
            conn.exec_driver_sql(f"ALTER TABLE fax_jobs DROP COLUMN {name}")
    seed_history(database)
    before = snapshot(database)["fax_jobs"][0]
    upgrade_schema(database)
    after = snapshot(database)["fax_jobs"][0]
    assert {key: after[key] for key in before} == before
    assert all(after[name] is None for name in missing)


@pytest.mark.parametrize("stamped", [False, True])
def test_mid_revision_failure_rolls_back_ddl_rows_and_version(database, monkeypatch, stamped):
    from api.app import schema_legacy
    load_history(database, "3a480391" if stamped else "c41a51bc")
    seed_history(database)
    if stamped:
        stamp_initial(database)
    before, definitions = snapshot(database), schema_description(database)
    original = schema_legacy.normalize_foundation

    def fail_after_backfill(connection, operations):
        original(connection, operations)
        raise RuntimeError("injected after DDL and backfill, before version write")

    with monkeypatch.context() as patch:
        patch.setattr(schema_legacy, "normalize_foundation", fail_after_backfill)
        with pytest.raises(RuntimeError, match="injected"):
            upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions
    upgrade_schema(database)
    assert snapshot(database)["alembic_version"] == [{"version_num": HEAD}]


@pytest.mark.parametrize("damage", ["duplicate_keys", "conflicting_index", "partial_index", "expression_index", "default", "multiple_versions", "version_type", "version_index", "missing_head_index"])
def test_invalid_identity_or_version_is_atomic(database, damage):
    load_history(database, "3a480391")
    seed_history(database)
    with database.begin() as conn:
        if damage == "duplicate_keys":
            conn.exec_driver_sql("DROP INDEX ix_api_keys_key_id")
            conn.exec_driver_sql("INSERT INTO api_keys (id,key_id,key_hash,created_at) SELECT 'duplicate',key_id,key_hash,created_at FROM api_keys")
        elif damage == "conflicting_index":
            conn.exec_driver_sql("DROP INDEX ix_fax_jobs_status")
            conn.exec_driver_sql("CREATE INDEX ix_fax_jobs_status ON fax_jobs (to_number)")
        elif damage == "partial_index":
            conn.exec_driver_sql("DROP INDEX ix_api_keys_key_id")
            conn.exec_driver_sql("CREATE UNIQUE INDEX ix_api_keys_key_id ON api_keys (key_id) WHERE revoked_at IS NULL")
        elif damage == "expression_index":
            conn.exec_driver_sql("CREATE INDEX lower_keys ON api_keys (lower(key_id))")
        elif damage == "default":
            conn.exec_driver_sql("ALTER TABLE fax_jobs ADD COLUMN outbound_backend VARCHAR(20) DEFAULT 'changed'")
        elif damage == "multiple_versions":
            conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
            conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('0001_initial'), ('0002_schema_foundation')")
        elif damage == "version_type":
            conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(100) NOT NULL PRIMARY KEY)")
        elif damage == "version_index":
            conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
            conn.exec_driver_sql("CREATE INDEX extra_version_index ON alembic_version (version_num)")
        # missing_head_index is applied after a legitimate upgrade below.
    if damage == "missing_head_index":
        upgrade_schema(database)
        with database.begin() as conn:
            conn.exec_driver_sql("DROP INDEX ix_fax_jobs_status")
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


def test_missing_identity_is_added_and_enforced(database):
    load_history(database, "3a480391")
    seed_history(database)
    with database.begin() as conn:
        conn.exec_driver_sql("DROP INDEX ix_api_keys_key_id")
    upgrade_schema(database)
    with pytest.raises(sa.exc.IntegrityError), database.begin() as conn:
        conn.exec_driver_sql("INSERT INTO api_keys (id,key_id,key_hash,created_at) SELECT 'duplicate',key_id,key_hash,created_at FROM api_keys")


def test_equivalent_unique_index_name_is_preserved(database):
    load_history(database, "3a480391")
    with database.begin() as conn:
        conn.exec_driver_sql("DROP INDEX ix_api_keys_key_id")
        conn.exec_driver_sql("CREATE UNIQUE INDEX historical_key_identity ON api_keys (key_id)")
    upgrade_schema(database)
    with database.connect() as conn:
        indexes = sa.inspect(conn).get_indexes("api_keys")
        assert [index["name"] for index in indexes if index["unique"]] == ["historical_key_identity"]


def test_equivalent_composite_identity_order_is_preserved(database):
    load_history(database, "3a480391", lambda source: source.replace("UNIQUE (provider_sid, event_type)", "UNIQUE (event_type, provider_sid)"))
    seed_history(database)
    upgrade_schema(database)
    with database.connect() as conn:
        identities = sa.inspect(conn).get_unique_constraints("inbound_events")
        assert [item["column_names"] for item in identities] == [["event_type", "provider_sid"]]


def test_null_hybrids_backfill_from_each_stored_provider(database):
    load_history(database, "dd8bd991")
    seed_history(database)
    with database.begin() as conn:
        conn.exec_driver_sql("UPDATE fax_jobs SET outbound_backend=NULL, backend='telnyx'")
        conn.exec_driver_sql("UPDATE inbound_faxes SET inbound_backend=NULL, backend='sinch'")
    upgrade_schema(database)
    rows = snapshot(database)
    assert rows["fax_jobs"][0]["outbound_backend"] == "telnyx"
    assert rows["inbound_faxes"][0]["inbound_backend"] == "sinch"


def test_unrelated_auxiliary_tables_can_precede_installation(database):
    with database.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE auxiliary_only (value TEXT)")
        conn.exec_driver_sql("INSERT INTO auxiliary_only VALUES ('saved')")
    upgrade_schema(database)
    assert snapshot(database)["auxiliary_only"] == [{"value": "saved"}]


def test_auxiliary_index_collision_rejected_before_ddl(database):
    with database.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE auxiliary_only (value TEXT)")
        conn.exec_driver_sql("CREATE INDEX ix_fax_jobs_id ON auxiliary_only (value)")
    before = schema_description(database)
    with pytest.raises(SchemaUpgradeError, match="another table"):
        upgrade_schema(database)
    assert schema_description(database) == before


def test_foreign_keys_enforced_on_every_owned_sqlite_connection(tmp_path):
    engine = create_database_engine("sqlite:///" + str(tmp_path / "foreign-keys.db"))
    try:
        upgrade_schema(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE test_child (job_id VARCHAR(40) REFERENCES fax_jobs(id))")
        with engine.connect() as first, engine.connect() as second:
            assert first.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert second.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            with pytest.raises(sa.exc.IntegrityError):
                second.exec_driver_sql("INSERT INTO test_child VALUES ('does-not-exist')")
    finally:
        engine.dispose()


def test_held_lock_is_bounded_then_retry_succeeds(database):
    from api.app.schema import LOCK_ID
    with database.connect() as holder:
        if database.dialect.name == "sqlite":
            holder.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            holder.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})
        started = time.monotonic()
        with pytest.raises(SchemaUpgradeError, match="locks"):
            upgrade_schema(database, lock_timeout=0.15)
        assert time.monotonic() - started < 3
        holder.rollback()
    assert snapshot(database) == {}
    upgrade_schema(database)


def test_two_process_initializers_converge(database):
    with database.connect() as conn:
        namespace = conn.execute(sa.text("SELECT current_schema()")).scalar_one() if database.dialect.name == "postgresql" else ""
    script = """
import os
from api.app.schema import create_database_engine, upgrade_schema
args = {'options': '-csearch_path='+os.environ['SCHEMA']} if os.environ['SCHEMA'] else {}
engine = create_database_engine(os.environ['TARGET'], connect_args=args)
try:
    # This tests convergence, not the production lock-wait budget. A fresh
    # migration and head validation serialize and can outlast that budget.
    upgrade_schema(engine, lock_timeout=60)
finally:
    engine.dispose()
"""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT),
           "TARGET": database.url.render_as_string(hide_password=False), "SCHEMA": namespace}
    processes = [subprocess.Popen([sys.executable, "-c", script], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=90)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert snapshot(database)["alembic_version"] == [{"version_num": HEAD}]


def test_same_url_preserves_engine_and_imported_session_factory(database, monkeypatch):
    from api.app import db
    previous_bind = db.SessionLocal.kw["bind"]
    factory = db.SessionLocal
    try:
        monkeypatch.setattr(db, "engine", database)
        monkeypatch.setattr(db, "settings", ConfigurationValues.from_environment(
            {"DATABASE_URL": database.url.render_as_string(hide_password=False)}))
        db.SessionLocal.configure(bind=database)
        db.init_db()
        db.init_db()
        assert db.engine is database
        assert db.SessionLocal is factory
        assert factory.kw["bind"] is database
        with factory() as session:
            assert session.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
    finally:
        factory.configure(bind=previous_bind)


def test_failed_candidate_preserves_working_binding_and_disposes_candidate(tmp_path, monkeypatch):
    from api.app import db
    previous_bind = db.SessionLocal.kw["bind"]
    working = create_database_engine("sqlite:///" + str(tmp_path / "working.db"))
    broken_url = "sqlite:///" + str(tmp_path / "broken.db")
    broken = create_database_engine(broken_url)
    with broken.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE fax_jobs (id INTEGER PRIMARY KEY)")
    broken.dispose()
    disposed = []

    def candidate_factory(url):
        candidate = create_database_engine(url)
        sa.event.listen(candidate, "engine_disposed", lambda engine: disposed.append(engine))
        return candidate

    try:
        upgrade_schema(working)
        monkeypatch.setattr(db, "engine", working)
        monkeypatch.setattr(db, "create_database_engine", candidate_factory)
        monkeypatch.setattr(db, "settings", ConfigurationValues.from_environment({"DATABASE_URL": broken_url}))
        db.SessionLocal.configure(bind=working)
        with pytest.raises(SchemaUpgradeError):
            db.init_db()
        assert db.engine is working
        assert db.SessionLocal.kw["bind"] is working
        assert len(disposed) == 1 and disposed[0] is not working
        with db.SessionLocal() as session:
            assert session.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
    finally:
        db.SessionLocal.configure(bind=previous_bind)
        working.dispose()


def test_successful_rebinding_is_serialized_and_disposes_old_engine(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from api.app import db
    previous_bind = db.SessionLocal.kw["bind"]
    old = create_database_engine("sqlite:///" + str(tmp_path / "old.db"))
    target = "sqlite:///" + str(tmp_path / "new.db")
    created, disposed = [], []
    sa.event.listen(old, "engine_disposed", lambda engine: disposed.append(engine))

    def candidate_factory(url):
        candidate = create_database_engine(url)
        created.append(candidate)
        return candidate

    try:
        monkeypatch.setattr(db, "engine", old)
        monkeypatch.setattr(db, "create_database_engine", candidate_factory)
        monkeypatch.setattr(db, "settings", ConfigurationValues.from_environment({"DATABASE_URL": target}))
        db.SessionLocal.configure(bind=old)
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: db.init_db(), range(3)))
        assert len(created) == 1
        assert db.engine is created[0]
        assert db.SessionLocal.kw["bind"] is created[0]
        assert disposed == [old]
    finally:
        db.SessionLocal.configure(bind=previous_bind)
        for engine in created:
            engine.dispose()
        old.dispose()


def test_postgres_mixed_search_path_rejected_without_second_core(database):
    if database.dialect.name != "postgresql":
        pytest.skip("PostgreSQL search_path behavior")
    load_history(database, "c41a51bc")
    seed_history(database)
    before = snapshot(database)
    other = "faxbot_schema_test_" + uuid.uuid4().hex
    with database.begin() as conn:
        existing = conn.execute(sa.text("SELECT current_schema()")).scalar_one()
        conn.exec_driver_sql(f"CREATE SCHEMA {other}")
    mixed = create_database_engine(database.url, connect_args={"options": f"-csearch_path={other},{existing}"})
    try:
        with pytest.raises(SchemaUpgradeError, match="single current schema"):
            upgrade_schema(mixed)
        with mixed.connect() as conn:
            assert sa.inspect(conn).get_table_names(schema=other) == []
        assert snapshot(database) == before
    finally:
        mixed.dispose()
        with database.begin() as conn:
            conn.exec_driver_sql(f"DROP SCHEMA {other} CASCADE")


def test_postgres_rewrite_rule_rejected(database):
    if database.dialect.name != "postgresql":
        pytest.skip("PostgreSQL rewrite rules")
    load_history(database, "c41a51bc")
    seed_history(database)
    with database.begin() as conn:
        conn.exec_driver_sql("CREATE RULE skip_updates AS ON UPDATE TO fax_jobs DO INSTEAD NOTHING")
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError, match="rewrite"):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


def test_postgres_cli_accepts_percent_encoded_url_options(database, tmp_path):
    if database.dialect.name != "postgresql":
        pytest.skip("PostgreSQL URL options")
    with database.connect() as conn:
        namespace = conn.execute(sa.text("SELECT current_schema()")).scalar_one()
    url = database.url.update_query_dict({"options": "-csearch_path=" + namespace}).render_as_string(hide_password=False)
    assert "%3D" in url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "api/alembic.ini"), "upgrade", "head"],
        cwd=tmp_path, env={"PATH": os.environ.get("PATH", ""), "DATABASE_URL": url},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot(database)["alembic_version"] == [{"version_num": HEAD}]


def test_migrated_columns_match_current_orm_contract(database):
    from api.app.db import Base
    upgrade_schema(database)
    with database.connect() as conn:
        inspector = sa.inspect(conn)
        for table in Base.metadata.tables.values():
            actual = {column["name"]: column for column in inspector.get_columns(table.name)}
            assert set(actual) == set(table.columns.keys())
            for column in table.columns:
                assert actual[column.name]["nullable"] == column.nullable
                assert actual[column.name]["type"]._type_affinity == column.type._type_affinity
                assert getattr(actual[column.name]["type"], "length", None) == getattr(column.type.dialect_impl(database.dialect), "length", None)


def test_fixed_width_character_type_is_not_historical_varchar(database):
    load_history(database, "15f81951", lambda source: source.replace("backend VARCHAR(20)", "backend CHAR(20)"))
    seed_history(database)
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


@pytest.mark.parametrize("alteration", ["replace", "ignore", "commented_replace", "index_collation", "column_collation"])
@pytest.mark.parametrize("stamped", [False, True])
def test_sqlite_conflict_and_collation_changes_rejected(tmp_path, alteration, stamped):
    engine = create_database_engine("sqlite:///" + str(tmp_path / "conflict.db"))
    changes = {
        "replace": ("PRIMARY KEY (id)", "PRIMARY KEY (id) ON CONFLICT REPLACE"),
        "ignore": ("PRIMARY KEY (id)", "PRIMARY KEY (id) ON CONFLICT IGNORE"),
        "commented_replace": ("PRIMARY KEY (id)", "PRIMARY KEY (id) ON /* misleading comment */ CONFLICT REPLACE"),
        "index_collation": ("ON api_keys (key_id)", "ON api_keys (key_id COLLATE NOCASE)"),
        "column_collation": ("file_name VARCHAR(255)", "file_name VARCHAR(255) COLLATE NOCASE"),
    }
    try:
        load_history(engine, "dd8bd991" if stamped else "3a480391", lambda source: source.replace(*changes[alteration]))
        seed_history(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE cascade_child (job_id VARCHAR(40) REFERENCES fax_jobs(id) ON DELETE CASCADE)")
            conn.exec_driver_sql("INSERT INTO cascade_child VALUES ('fax_jobs-original')")
            if stamped:
                conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
                conn.execute(sa.text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": HEAD})
        before, definitions = snapshot(engine), schema_description(engine)
        with pytest.raises(SchemaUpgradeError):
            upgrade_schema(engine)
        assert snapshot(engine) == before
        assert schema_description(engine) == definitions
    finally:
        engine.dispose()


@pytest.mark.parametrize("stamped", [False, True])
def test_postgres_failed_concurrent_unique_index_is_not_enforcement(database, stamped):
    if database.dialect.name != "postgresql":
        pytest.skip("PostgreSQL invalid concurrent indexes")
    load_history(database, "3a480391")
    seed_history(database)
    if stamped:
        upgrade_schema(database)
    with database.begin() as conn:
        conn.exec_driver_sql("DROP INDEX ix_api_keys_key_id")
        conn.exec_driver_sql("INSERT INTO api_keys (id,key_id,key_hash,created_at) SELECT 'duplicate',key_id,key_hash,created_at FROM api_keys")
    with database.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        with pytest.raises(sa.exc.IntegrityError):
            conn.exec_driver_sql("CREATE UNIQUE INDEX CONCURRENTLY ix_api_keys_key_id ON api_keys (key_id)")
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


@pytest.mark.parametrize("alteration", ["index_collation", "column_collation", "deferred_unique", "operator_class", "casefold_index", "casefold_column", "timestamp_precision"])
def test_postgres_nonhistorical_comparison_and_constraint_semantics(database, alteration):
    if database.dialect.name != "postgresql":
        pytest.skip("PostgreSQL index semantics")
    changes = {
        "index_collation": ("ON api_keys (key_id)", 'ON api_keys (key_id COLLATE "C")'),
        "column_collation": ("file_name VARCHAR(255)", 'file_name VARCHAR(255) COLLATE "C"'),
        "deferred_unique": ("UNIQUE (provider_sid, event_type)", "UNIQUE (provider_sid, event_type) DEFERRABLE INITIALLY DEFERRED"),
        "operator_class": ("ON api_keys (key_id)", "ON api_keys (key_id text_pattern_ops)"),
        "casefold_index": ("ON api_keys (key_id)", "ON api_keys (key_id COLLATE casefold_identity)"),
        "casefold_column": ("key_id VARCHAR(32)", "key_id VARCHAR(32) COLLATE casefold_identity"),
        "timestamp_precision": ("TIMESTAMP WITHOUT TIME ZONE", "TIMESTAMP(3) WITHOUT TIME ZONE"),
    }
    if alteration.startswith("casefold"):
        with database.begin() as conn:
            conn.exec_driver_sql("CREATE COLLATION casefold_identity (provider=icu, locale='und-u-ks-level2', deterministic=false)")
    load_history(database, "3a480391", lambda source: source.replace(*changes[alteration]))
    seed_history(database)
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


@pytest.fixture
def isolated_operator_database(database):
    """Default operator-class selection is database-wide, not schema-scoped."""
    if database.dialect.name != 'postgresql':
        yield database
        return
    identity = 'faxbot_operator_test_' + uuid.uuid4().hex
    with database.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        connection.exec_driver_sql(f'CREATE DATABASE {identity}')
    isolated = create_database_engine(database.url.set(database=identity))
    try:
        yield isolated
    finally:
        isolated.dispose()
        with database.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            connection.exec_driver_sql(f'DROP DATABASE {identity}')


def test_postgres_custom_default_operator_class_is_not_historical(database, isolated_operator_database):
    shared_database = database
    database = isolated_operator_database
    if database.dialect.name != "postgresql":
        pytest.skip("PostgreSQL operator classes")
    load_history(database, "3a480391")
    seed_history(database)
    with database.begin() as conn:
        conn.exec_driver_sql("CREATE FUNCTION fold_cmp(varchar,varchar) RETURNS integer LANGUAGE SQL IMMUTABLE STRICT AS $$ SELECT bttextcmp(lower($1),lower($2)) $$")
        for operator, function in [("<", "fold_lt"), ("<=", "fold_le"), ("=", "fold_eq"), (">=", "fold_ge"), (">", "fold_gt")]:
            conn.exec_driver_sql(f"CREATE FUNCTION {function}(varchar,varchar) RETURNS boolean LANGUAGE SQL IMMUTABLE STRICT AS $$ SELECT lower($1) {operator} lower($2) $$")
            conn.exec_driver_sql(f"CREATE OPERATOR {operator} (LEFTARG=varchar, RIGHTARG=varchar, FUNCTION={function})")
        conn.exec_driver_sql("CREATE OPERATOR CLASS casefold_ops DEFAULT FOR TYPE varchar USING btree AS OPERATOR 1 < (varchar,varchar), OPERATOR 2 <= (varchar,varchar), OPERATOR 3 = (varchar,varchar), OPERATOR 4 >= (varchar,varchar), OPERATOR 5 > (varchar,varchar), FUNCTION 1 fold_cmp(varchar,varchar)")
        conn.exec_driver_sql("DROP INDEX ix_api_keys_key_id")
        conn.exec_driver_sql("CREATE UNIQUE INDEX ix_api_keys_key_id ON api_keys (key_id casefold_ops)")
    # Another normal migration may run concurrently in the shared test database.
    # Its indexes must retain pg_catalog.text_ops despite this custom default.
    upgrade_schema(shared_database)
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions
