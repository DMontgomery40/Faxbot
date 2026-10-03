"""Internal shared configuration/access acceptance and replay transactions."""

import importlib

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_policy import World, NOW
from api.app.access.policy import AccessControl
from api.app.access.fax_resources import FaxResources, FaxAccessError
from api.app.access.types import StaleCredentialError
from api.app.config_store import ConfigurationStore, ConfigurationConflict
from api.app.config_values import ConfigurationValues
from api.app.config_profiles import ProviderConfiguration
from api.app.request_identity import RequestIdentity, IdempotentReplay, IdempotencyConflict

try:
    O = importlib.import_module("api.app.access.outbound")
except ModuleNotFoundError:
    O = None


@pytest.fixture
def aw(database, tmp_path):
    assert O is not None, "Authorized outbound adapter missing"
    w = World(database)
    w.actor = w.user("alice")
    w.user("bob")
    w.assignment("alice", "role_fax_operator", "personal-alice")
    w.configuration = ConfigurationStore(database, tmp_path / "key")
    w.snapshot = w.configuration.initialize(
        ConfigurationValues.from_environment({"FAX_DISABLED": "false"}),
        actor="test",
        providers={
            "outbound": ProviderConfiguration("phaxio", credentials={"api_key": "synthetic"})
        },
    )
    w.control = AccessControl(w.configuration.access_store)
    w.faxes = FaxResources(w.control)
    w.outbound = O.AuthorizedOutbound(w.configuration, w.faxes, clock=lambda: NOW)
    w.identity = RequestIdentity.from_key(
        "test-intent", principal_scope=w.actor.replay_scope, fingerprint="a" * 64
    )
    w.job = dict(
        id="new",
        to_number="+12025550123",
        file_name="private.pdf",
        tiff_path="",
        status="queued",
        created_at=NOW,
        updated_at=NOW,
    )
    return w


def test_module_exists():
    assert O is not None, "Authorized outbound adapter missing"


def test_acceptance_commits_job_capture_resource_and_audit_once(aw):
    w = aw
    profile = w.outbound.accept(w.actor, w.snapshot.active, w.job, request_identity=w.identity)
    with w.engine.connect() as c:
        r = w.tables["access_resources"]
        assert (
            c.execute(sa.select(r.c.parent_id).where(r.c.fax_job_id == "new")).scalar_one()
            == "personal-alice"
        )
        assert c.scalar(sa.text("SELECT count(*) FROM outbound_deliveries")) == 1
        assert (
            c.scalar(sa.text("SELECT count(*) FROM access_audit WHERE operation='fax.accept'")) == 1
        )
    assert w.configuration.outbound_profile("new") == profile
    with pytest.raises(IdempotentReplay) as replay:
        w.outbound.accept(
            w.actor, w.snapshot.active, {**w.job, "id": "unused"}, request_identity=w.identity
        )
    assert replay.value.job_id == "new"
    assert w.outbound.find_replay(w.actor, w.identity) == "new"
    assert w.outbound.replay_max_bytes(w.actor, w.identity) == 10 * 1024 * 1024
    assert w.outbound.replay_values(w.actor, w.identity) == w.snapshot.active.values


def test_replay_matches_an_earlier_fingerprint_version_but_never_stores_it(aw):
    w = aw
    w.outbound.accept(w.actor, w.snapshot.active, w.job, request_identity=w.identity)
    newer = RequestIdentity(w.identity.principal_scope, w.identity.idempotency_digest, "c" * 64,
                            (w.identity.request_fingerprint,))
    assert w.outbound.find_replay(w.actor, newer) == "new"
    with pytest.raises(IdempotentReplay):
        w.outbound.accept(w.actor, w.snapshot.active, {**w.job, "id": "unused"}, request_identity=newer)
    unrelated = RequestIdentity(w.identity.principal_scope, w.identity.idempotency_digest, "c" * 64,
                                ("d" * 64,))
    with pytest.raises(IdempotencyConflict):
        w.outbound.find_replay(w.actor, unrelated)
    with w.engine.connect() as c:
        assert c.scalar(sa.text("SELECT request_fingerprint FROM outbound_deliveries")) == \
            w.identity.request_fingerprint


@pytest.mark.parametrize("operation", ["find_replay", "replay_max_bytes", "replay_values", "accept"])
def test_replay_and_acceptance_refuse_rotated_source(aw, operation):
    w = aw
    w.outbound.accept(w.actor, w.snapshot.active, w.job, request_identity=w.identity)
    w.update("access_principals", "alice", security_version=2)
    with pytest.raises(StaleCredentialError):
        if operation == "accept":
            w.outbound.accept(
                w.actor, w.snapshot.active, {**w.job, "id": "unused"}, request_identity=w.identity
            )
        else:
            getattr(w.outbound, operation)(w.actor, w.identity)
    with w.engine.connect() as c:
        assert c.scalar(sa.text("SELECT count(*) FROM fax_jobs")) == 1


def test_replay_checks_visibility_before_disclosing_fingerprint_conflict(aw):
    w = aw
    w.outbound.accept(w.actor, w.snapshot.active, w.job, request_identity=w.identity)
    with w.engine.begin() as c:
        c.execute(
            w.tables["access_assignments"]
            .delete()
            .where(w.tables["access_assignments"].c.principal_id == "alice")
        )
    w.role("send-only", ["fax:send"])
    w.assignment("alice", "send-only", "personal-alice")
    wrong = RequestIdentity(w.identity.principal_scope, w.identity.idempotency_digest, "b" * 64)
    for operation in ["find_replay", "replay_max_bytes", "replay_values"]:
        with pytest.raises(FaxAccessError) as error:
            getattr(w.outbound, operation)(w.actor, wrong)
        assert error.value.code == "not_found"


def test_request_scope_cannot_be_supplied_for_another_principal(aw):
    forged = RequestIdentity.from_key(
        "test-intent", principal_scope="principal:bob", fingerprint="a" * 64
    )
    with pytest.raises(FaxAccessError) as error:
        aw.outbound.accept(aw.actor, aw.snapshot.active, aw.job, request_identity=forged)
    assert error.value.code == "invalid_input"
    with aw.engine.connect() as c:
        assert c.scalar(sa.text("SELECT count(*) FROM fax_jobs")) == 0


def test_resource_audit_failure_rolls_back_every_acceptance_row(aw):
    w = aw

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO ACCESS_AUDIT"):
            raise RuntimeError("synthetic audit refusal")

    sa.event.listen(w.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="synthetic audit refusal"):
            w.outbound.accept(w.actor, w.snapshot.active, w.job, request_identity=w.identity)
    finally:
        sa.event.remove(w.engine, "before_cursor_execute", fail)
    with w.engine.connect() as c:
        for table in ["fax_jobs", "fax_job_bindings", "outbound_deliveries", "outbound_events"]:
            assert c.scalar(sa.text("SELECT count(*) FROM " + table)) == 0
        assert (
            c.execute(
                sa.select(w.tables["access_resources"].c.id).where(
                    w.tables["access_resources"].c.fax_job_id == "new"
                )
            ).first()
            is None
        )


def test_stale_configuration_cannot_accept_but_authorized_replay_survives(aw):
    w = aw
    w.outbound.accept(w.actor, w.snapshot.active, w.job, request_identity=w.identity)
    w.configuration.apply(
        w.snapshot,
        w.snapshot.desired.values.with_patch({"fax_header": "new"}),
        restart_required=False,
        actor="test",
    )
    with pytest.raises(ConfigurationConflict):
        w.outbound.accept(w.actor, w.snapshot.active, {**w.job, "id": "new-other"})
    assert w.outbound.find_replay(w.actor, w.identity) == "new"


def test_revocation_preserves_already_accepted_delivery_lifecycle(aw):
    from api.app.outbound_store import OutboundStore

    w = aw
    profile = w.outbound.accept(w.actor, w.snapshot.active, w.job, request_identity=w.identity)
    w.update("access_principals", "alice", enabled=0, security_version=2)
    delivery = OutboundStore(w.configuration)
    claim = delivery.claim("synthetic-worker", now=NOW)
    assert claim.job_id == "new" and claim.profile_id == profile.id
    with pytest.raises(StaleCredentialError):
        w.outbound.find_replay(w.actor, w.identity)
    assert delivery.get("new")["state"] == "preparing"


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_acceptance_does_not_repeat_or_return_success(aw, monkeypatch, committed):
    from api.app.config_store import ConfigurationCommitUncertain

    w = aw
    real = sa.engine.Connection.commit

    def lose_ack(connection):
        if committed:
            real(connection)
        raise sa.exc.OperationalError(
            None, None, RuntimeError("synthetic lost commit acknowledgement")
        )

    with monkeypatch.context() as patch:
        patch.setattr(sa.engine.Connection, "commit", lose_ack)
        with pytest.raises(ConfigurationCommitUncertain):
            w.outbound.accept(w.actor, w.snapshot.active, w.job, request_identity=w.identity)
    with w.engine.connect() as c:
        assert c.scalar(sa.text("SELECT count(*) FROM fax_jobs")) == int(committed)
        assert c.scalar(
            sa.text("SELECT count(*) FROM access_audit WHERE operation='fax.accept'")
        ) == int(committed)
        assert c.scalar(
            sa.select(sa.func.count())
            .select_from(w.tables["access_resources"])
            .where(w.tables["access_resources"].c.fax_job_id == "new")
        ) == int(committed)
    assert w.outbound.find_replay(w.actor, w.identity) == ("new" if committed else None)
