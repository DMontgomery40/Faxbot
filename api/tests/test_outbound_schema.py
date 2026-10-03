"""Frozen outbound migration contract on real SQLite and private PostgreSQL."""
from datetime import datetime
import json

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app.schema import API_DIRECTORY, SchemaUpgradeError, upgrade_schema, validate_schema
from api.tests.test_schema import database, isolated_operator_database, snapshot, schema_description


PRIOR = "0003_configuration"
HEAD = "0004_outbound_delivery"
TABLES = {"outbound_attempts", "outbound_deliveries", "outbound_events"}
OLD = datetime(2025, 1, 2, 3, 4, 5, 123456)


def prior_schema(engine):
    config = Config(str(API_DIRECTORY / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIRECTORY / "alembic"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, PRIOR)


def add_job(connection, identity, status):
    connection.execute(sa.text("""
        INSERT INTO fax_jobs
            (id,to_number,file_name,tiff_path,status,backend,outbound_backend,created_at,updated_at)
        VALUES (:id,'+12025550123','legacy.txt','/unchanged/legacy.tiff',:status,'sip','sip',:created,:updated)
    """), {"id": identity, "status": status, "created": OLD, "updated": OLD})


def test_fresh_outbound_schema_has_exact_columns_indexes_and_foreign_keys(database):
    upgrade_schema(database)
    with database.connect() as connection:
        assert validate_schema(connection, require_version=True) == HEAD
        inspector = sa.inspect(connection)
        assert TABLES <= set(inspector.get_table_names())
        expected_columns = {
            "outbound_attempts": {
                "id": (sa.String, 40, False), "job_id": (sa.String, 40, False),
                "sequence": (sa.Integer, None, False), "profile_id": (sa.String, 40, True),
                "phase": (sa.String, 32, False), "provider_sid": (sa.String, 100, True),
                "error_category": (sa.String, 64, True), "created_at": (sa.DateTime, None, False),
                "submitted_at": (sa.DateTime, None, True), "completed_at": (sa.DateTime, None, True),
            },
            "outbound_deliveries": {
                "id": (sa.String, 40, False), "dispatch_mode": (sa.String, 16, False),
                "state": (sa.String, 32, False), "version": (sa.Integer, None, False),
                "attempt_id": (sa.String, 40, True), "claim_owner": (sa.String, 40, True),
                "claim_token": (sa.String, 64, True), "claim_expires_at": (sa.DateTime, None, True),
                "next_poll_at": (sa.DateTime, None, True), "request_fingerprint": (sa.String, 64, True),
                "principal_scope": (sa.String, 100, True), "idempotency_digest": (sa.String, 64, True),
                "legacy_status": (sa.String, 32, True), "created_at": (sa.DateTime, None, False),
                "updated_at": (sa.DateTime, None, False),
            },
            "outbound_events": {
                "id": (sa.String, 40, False), "job_id": (sa.String, 40, False),
                "attempt_id": (sa.String, 40, True), "kind": (sa.String, 40, False),
                "dedupe_key": (sa.String, 64, True), "details": (sa.Text, None, False),
                "created_at": (sa.DateTime, None, False),
            },
        }
        expected_indexes = {
            "outbound_attempts": {
                "uq_outbound_attempts_job_sequence": (("job_id", "sequence"), True),
                "ix_outbound_attempts_job_id": (("job_id",), False),
            },
            "outbound_deliveries": {
                "uq_outbound_deliveries_principal_idempotency": (("principal_scope", "idempotency_digest"), True),
                "ix_outbound_deliveries_state_claim_expires": (("state", "claim_expires_at"), False),
                "ix_outbound_deliveries_next_poll_at": (("next_poll_at",), False),
            },
            "outbound_events": {
                "uq_outbound_events_job_dedupe": (("job_id", "dedupe_key"), True),
                "ix_outbound_events_job_created_at": (("job_id", "created_at"), False),
            },
        }
        expected_foreign_keys = {
            "outbound_attempts": {(("job_id",), "fax_jobs", ("id",), "CASCADE"),
                                  (("profile_id",), "provider_profiles", ("id",), "RESTRICT")},
            "outbound_deliveries": {(("id",), "fax_jobs", ("id",), "CASCADE"),
                                    (("attempt_id",), "outbound_attempts", ("id",), "SET NULL")},
            "outbound_events": {(("job_id",), "fax_jobs", ("id",), "CASCADE"),
                                (("attempt_id",), "outbound_attempts", ("id",), "SET NULL")},
        }
        for table in TABLES:
            columns = inspector.get_columns(table)
            assert {column["name"] for column in columns} == set(expected_columns[table])
            for column in columns:
                kind, length, nullable = expected_columns[table][column["name"]]
                if kind is sa.Text:
                    assert isinstance(column["type"], sa.Text)
                else:
                    assert column["type"]._type_affinity is kind
                assert getattr(column["type"], "length", None) == length
                assert column["nullable"] is nullable
                assert column["default"] is None
                assert not getattr(column["type"], "timezone", False)
            assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"]
            assert not inspector.get_check_constraints(table)
            assert not inspector.get_unique_constraints(table)
            assert {index["name"]: (tuple(index["column_names"]), bool(index["unique"]))
                    for index in inspector.get_indexes(table)} == expected_indexes[table]
            assert {(tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]),
                     fk.get("options", {}).get("ondelete"))
                    for fk in inspector.get_foreign_keys(table)} == expected_foreign_keys[table]


def test_0003_history_is_enrolled_conservatively_without_rewriting_or_inventing_bindings(database, tmp_path):
    prior_schema(database)
    statuses = ["success", "completed", "completed_ok", "SUCCESS", "Completed_OK", "failed",
                "queued", "sending", "cancelled", "disabled", "delivered", " success", "odd\nstatus"]
    artifact = tmp_path / "legacy.tiff"
    artifact.write_bytes(b"original artifact")
    with database.begin() as connection:
        for index, status in enumerate(statuses):
            add_job(connection, f"legacy-{index}", status)
        connection.execute(sa.text("UPDATE fax_jobs SET tiff_path=:path"), {"path": str(artifact)})
    before = snapshot(database)
    earliest = datetime.utcnow()
    upgrade_schema(database)
    latest = datetime.utcnow()
    after = snapshot(database)
    for table, rows in before.items():
        if table != "alembic_version":
            assert after[table] == rows
    assert after["alembic_version"] == [{"version_num": HEAD}]
    assert not after["outbound_attempts"]
    assert not after["provider_profiles"]
    assert not after["fax_job_bindings"]
    deliveries = {row["id"]: row for row in after["outbound_deliveries"]}
    events = {row["job_id"]: row for row in after["outbound_events"]}
    assert len(deliveries) == len(events) == len(statuses)
    times = set()
    for index, status in enumerate(statuses):
        delivery, event = deliveries[f"legacy-{index}"], events[f"legacy-{index}"]
        assert delivery["legacy_status"] == status
        assert delivery["dispatch_mode"] == "legacy"
        assert delivery["version"] == 1
        assert delivery["state"] == ("success" if status.lower() in {"success", "completed", "completed_ok"}
                                      else "reconciliation_required")
        assert all(delivery[key] is None for key in ["attempt_id", "claim_owner", "claim_token",
                   "claim_expires_at", "next_poll_at", "request_fingerprint", "principal_scope", "idempotency_digest"])
        assert event["kind"] == "legacy_migrated"
        assert event["dedupe_key"] == "legacy_migration"
        assert event["attempt_id"] is None
        assert json.loads(event["details"]) == {"legacy_status": "".join(c for c in status if c.isprintable())[:128]}
        for value in [delivery["created_at"], delivery["updated_at"], event["created_at"]]:
            value = datetime.fromisoformat(value) if isinstance(value, str) else value
            assert earliest <= value <= latest
            assert value.tzinfo is None
            times.add(value)
    assert len(times) == 1
    assert artifact.read_bytes() == b"original artifact"
    upgrade_schema(database)
    assert snapshot(database) == after


def test_sqlite_overlength_legacy_status_is_preserved_but_event_is_bounded(database):
    if database.dialect.name != "sqlite":
        pytest.skip("SQLite historically does not enforce VARCHAR length")
    prior_schema(database)
    status = "x" * 5000 + "\n"
    with database.begin() as connection:
        add_job(connection, "long-status", status)
    upgrade_schema(database)
    rows = snapshot(database)
    assert rows["fax_jobs"][0]["status"] == rows["outbound_deliveries"][0]["legacy_status"] == status
    assert json.loads(rows["outbound_events"][0]["details"]) == {"legacy_status": "x" * 128}
    assert len(rows["outbound_events"][0]["details"]) < 1024


def test_existing_configuration_and_job_binding_survive_without_dispatch_authority(database):
    prior_schema(database)
    with database.begin() as connection:
        add_job(connection, "captured-job", "queued")
        connection.execute(sa.text("""
            INSERT INTO configuration_revisions (id,format_version,key_id,envelope,actor,created_at)
            VALUES ('revision',1,'synthetic-key','opaque-synthetic-envelope','operator',:created)
        """), {"created": OLD})
        connection.execute(sa.text("""
            INSERT INTO provider_profiles (id,account_id,provider_id,format_version,key_id,envelope,created_at)
            VALUES ('profile','account','phaxio',1,'synthetic-key','opaque-synthetic-profile',:created)
        """), {"created": OLD})
        connection.execute(sa.text("""
            INSERT INTO configuration_state (id,installation_id,generation,active_revision_id,updated_at)
            VALUES ('state','installation',7,'revision',:created)
        """), {"created": OLD})
        connection.exec_driver_sql("INSERT INTO fax_job_bindings (id,revision_id,profile_id) VALUES ('captured-job','revision','profile')")
    before = snapshot(database)
    upgrade_schema(database)
    after = snapshot(database)
    for table, rows in before.items():
        if table != "alembic_version":
            assert after[table] == rows
    assert after["outbound_deliveries"][0]["dispatch_mode"] == "legacy"
    assert after["outbound_deliveries"][0]["state"] == "reconciliation_required"
    assert after["outbound_deliveries"][0]["attempt_id"] is None
    assert not after["outbound_attempts"]


def test_failed_outbound_upgrade_rolls_back_tables_rows_and_version(database, monkeypatch):
    from api.app import schema_outbound
    prior_schema(database)
    with database.begin() as connection:
        add_job(connection, "rollback", "failed")
    before, definitions = snapshot(database), schema_description(database)
    original = schema_outbound.upgrade_outbound

    def fail_after_migration(connection, operations):
        original(connection, operations)
        raise RuntimeError("injected post-backfill failure")

    monkeypatch.setattr(schema_outbound, "upgrade_outbound", fail_after_migration)
    with pytest.raises(RuntimeError, match="injected post-backfill failure"):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


@pytest.mark.parametrize("damage", ["extra_column", "missing_index", "wrong_index", "missing_table", "trigger"])
def test_malformed_outbound_head_is_refused_without_mutation(database, damage):
    upgrade_schema(database)
    with database.begin() as connection:
        if damage == "extra_column":
            connection.exec_driver_sql("ALTER TABLE outbound_events ADD COLUMN raw_payload TEXT")
        elif damage == "missing_index":
            connection.exec_driver_sql("DROP INDEX ix_outbound_deliveries_next_poll_at")
        elif damage == "wrong_index":
            connection.exec_driver_sql("DROP INDEX uq_outbound_attempts_job_sequence")
            connection.exec_driver_sql("CREATE UNIQUE INDEX uq_outbound_attempts_job_sequence ON outbound_attempts (sequence,job_id)")
        elif damage == "missing_table":
            connection.exec_driver_sql("DROP TABLE outbound_events")
        elif database.dialect.name == "sqlite":
            connection.exec_driver_sql("CREATE TRIGGER alter_event AFTER INSERT ON outbound_events BEGIN DELETE FROM outbound_events WHERE id=NEW.id; END")
        else:
            connection.exec_driver_sql("CREATE FUNCTION alter_event_fn() RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$")
            connection.exec_driver_sql("CREATE TRIGGER alter_event BEFORE INSERT ON outbound_events FOR EACH ROW EXECUTE FUNCTION alter_event_fn()")
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


@pytest.mark.parametrize("reserved", ["table", "view", "index_name"])
def test_reserved_outbound_objects_before_revision_are_refused(database, reserved):
    prior_schema(database)
    with database.begin() as connection:
        if reserved == "table":
            connection.exec_driver_sql("CREATE TABLE outbound_events (id VARCHAR(40) PRIMARY KEY)")
        elif reserved == "view":
            connection.exec_driver_sql("CREATE VIEW outbound_events AS SELECT id FROM fax_jobs")
        else:
            connection.exec_driver_sql("CREATE TABLE auxiliary (id VARCHAR(40) PRIMARY KEY)")
            connection.exec_driver_sql("CREATE INDEX ix_outbound_events_job_created_at ON auxiliary (id)")
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


def test_attempt_removal_nulls_references_and_job_removal_cascades_without_cycles(database):
    upgrade_schema(database)
    with database.begin() as connection:
        add_job(connection, "delete-job", "queued")
        connection.execute(sa.text("INSERT INTO outbound_attempts (id,job_id,sequence,phase,created_at) VALUES ('attempt','delete-job',1,'prepared',:created)"), {"created": OLD})
        connection.execute(sa.text("INSERT INTO outbound_deliveries (id,dispatch_mode,state,version,attempt_id,created_at,updated_at) VALUES ('delete-job','normal','preparing',1,'attempt',:created,:created)"), {"created": OLD})
        connection.execute(sa.text("INSERT INTO outbound_events (id,job_id,attempt_id,kind,details,created_at) VALUES ('event','delete-job','attempt','claimed','{}',:created)"), {"created": OLD})
        connection.exec_driver_sql("DELETE FROM outbound_attempts WHERE id='attempt'")
        assert connection.exec_driver_sql("SELECT attempt_id FROM outbound_deliveries").scalar_one() is None
        assert connection.exec_driver_sql("SELECT attempt_id FROM outbound_events").scalar_one() is None
        connection.execute(sa.text("INSERT INTO outbound_attempts (id,job_id,sequence,phase,created_at) VALUES ('second','delete-job',2,'prepared',:created)"), {"created": OLD})
        connection.exec_driver_sql("UPDATE outbound_deliveries SET attempt_id='second'")
        connection.exec_driver_sql("UPDATE outbound_events SET attempt_id='second'")
        connection.exec_driver_sql("DELETE FROM fax_jobs WHERE id='delete-job'")
        for table in TABLES:
            assert connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one() == 0


def test_unvalidated_postgres_outbound_foreign_key_is_refused(database):
    if database.dialect.name != "postgresql":
        pytest.skip("PostgreSQL NOT VALID foreign key semantics")
    upgrade_schema(database)
    with database.begin() as connection:
        fk = next(fk for fk in sa.inspect(connection).get_foreign_keys("outbound_attempts") if fk["constrained_columns"] == ["job_id"])
        connection.exec_driver_sql('ALTER TABLE outbound_attempts DROP CONSTRAINT "' + fk["name"] + '"')
        connection.exec_driver_sql("ALTER TABLE outbound_attempts ADD CONSTRAINT unvalidated_job FOREIGN KEY(job_id) REFERENCES fax_jobs(id) ON DELETE CASCADE NOT VALID")
    with pytest.raises(SchemaUpgradeError, match="unvalidated constraints"):
        upgrade_schema(database)


def test_postgres_custom_integer_operator_class_is_refused(database, isolated_operator_database):
    database = isolated_operator_database
    if database.dialect.name != "postgresql":
        pytest.skip("PostgreSQL operator classes")
    upgrade_schema(database)
    with database.begin() as connection:
        connection.exec_driver_sql("""
            CREATE OPERATOR CLASS custom_integer_ops FOR TYPE integer USING btree AS
              OPERATOR 1 pg_catalog.< (integer,integer), OPERATOR 2 pg_catalog.<= (integer,integer),
              OPERATOR 3 pg_catalog.= (integer,integer), OPERATOR 4 pg_catalog.>= (integer,integer),
              OPERATOR 5 pg_catalog.> (integer,integer), FUNCTION 1 pg_catalog.btint4cmp(integer,integer)
        """)
        connection.exec_driver_sql("DROP INDEX uq_outbound_attempts_job_sequence")
        connection.exec_driver_sql("CREATE UNIQUE INDEX uq_outbound_attempts_job_sequence ON outbound_attempts (job_id,sequence custom_integer_ops)")
    with pytest.raises(SchemaUpgradeError, match="index semantics"):
        upgrade_schema(database)
