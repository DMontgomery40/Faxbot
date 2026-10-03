"""Versioned operator identity attachment; internal database/protocol seams only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from api.tests.test_outbound_store import installation, accept
from api.tests.test_schema import database
from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_polling import OutboundPoller
from api.app.outbound_store import DeliveryConflict, OutboundStore


def uncertain(installation, *, profile=None):
    configuration, store, snapshot = installation
    if profile is not None:
        snapshot = configuration.apply(snapshot, snapshot.active.values, actor="test",
            restart_required=False, providers={"outbound": profile})
    job_id = accept((configuration, store, snapshot))
    claim = store.claim("synthetic-original-worker")
    assert store.begin_submission(claim)
    store.record_uncertain(claim, category="response_unusable")
    return job_id, claim, snapshot


def records(configuration, store, job_id):
    with configuration.engine.connect() as connection:
        job = dict(connection.execute(sa.select(configuration.jobs).where(
            configuration.jobs.c.id == job_id)).mappings().one())
        attempts = [dict(row) for row in connection.execute(sa.select(store.attempts).where(
            store.attempts.c.job_id == job_id)).mappings()]
    return job, attempts


def test_binding_preserves_submission_authority_and_only_attaches_identity(installation):
    configuration, store, _ = installation
    job_id, claim, snapshot = uncertain(installation)
    with configuration.engine.begin() as connection:
        connection.execute(store.deliveries.update().where(store.deliveries.c.id == job_id).values(
            next_poll_at=datetime.utcnow() + timedelta(hours=1)))
    before, history = store.get(job_id), store.history(job_id)
    job, attempts = records(configuration, store, job_id)
    view = store.operator_view(job_id)
    assert view["can_bind_provider_identity"] is True and view["bind_refusal_reason"] is None
    assert (view["provider_id"], view["profile_id"], view["revision_id"]) == (
        "phaxio", claim.profile_id, snapshot.active.id)
    assert view["attempt"]["id"] == claim.attempt_id and view["attempt"]["phase"] == "uncertain"
    version = store.bind_provider_identity(job_id, expected_version=before["version"],
        provider_sid="confirmed-remote_123", actor="key:operator-1")
    after = store.get(job_id)
    assert version == after["version"] == before["version"] + 1
    assert after["state"] == "reconciliation_required" and after["next_poll_at"] is None
    for key in ("dispatch_mode", "attempt_id", "claim_owner", "claim_token", "claim_expires_at"):
        assert after[key] == before[key]
    current_job, current_attempts = records(configuration, store, job_id)
    assert len(current_attempts) == len(attempts) == 1
    assert current_attempts[0] == {**attempts[0], "provider_sid": "confirmed-remote_123"}
    assert current_job["provider_sid"] == "confirmed-remote_123"
    assert current_job["status"] == job["status"] == "queued"
    with configuration.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(configuration.jobs)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(store.attempts)) == 1
    assert len(store.history(job_id)) == len(history) + 1
    event = store.history(job_id)[-1]
    assert event["kind"] == "operator_identity_bound" and event["attempt_id"] == claim.attempt_id
    assert json.loads(event["details"]) == {"actor": "key:operator-1", "provider_sid": "confirmed-remote_123"}
    assert store.claim("another-worker") is None and store.begin_submission(claim) is False
    after_view = store.operator_view(job_id)
    assert after_view["can_bind_provider_identity"] is False
    assert after_view["attempt"]["provider_sid"] == "confirmed-remote_123"
    assert after_view["events"][-1]["details"] == {
        "actor": "key:operator-1", "provider_sid": "confirmed-remote_123"}


def test_concurrent_loaded_versions_cannot_attach_two_different_identities(installation):
    configuration, store, _ = installation
    job_id, _, _ = uncertain(installation)
    before = store.get(job_id)
    start = Barrier(2)
    def bind(sid):
        start.wait(timeout=10)
        try:
            return OutboundStore(configuration).bind_provider_identity(job_id,
                expected_version=before["version"], provider_sid=sid, actor="admin")
        except DeliveryConflict:
            return "refused"
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(bind, ["remote-one", "remote-two"]))
    assert sorted(results, key=str) == sorted([before["version"] + 1, "refused"], key=str)
    assert store.get(job_id)["version"] == before["version"] + 1
    assert len([event for event in store.history(job_id) if event["kind"] == "operator_identity_bound"]) == 1


@pytest.mark.parametrize("changes", [
    {"expected_version": True}, {"expected_version": 0}, {"expected_version": "4"},
    {"provider_sid": ""}, {"provider_sid": "a" * 101}, {"provider_sid": "remote\nsecret"},
    {"provider_sid": "remote/one"}, {"provider_sid": "é"},
    {"actor": "raw-secret-token"}, {"actor": "key:"}, {"actor": "key:a\nprivate"},
    {"actor": "key:" + "a" * 97},
])
def test_invalid_operator_input_is_refused_without_mutation_or_echo(installation, changes):
    configuration, store, _ = installation
    job_id, _, _ = uncertain(installation)
    before, history = store.get(job_id), store.history(job_id)
    prior = records(configuration, store, job_id)
    arguments = {"expected_version": before["version"], "provider_sid": "remote-one", "actor": "development"}
    arguments.update(changes)
    with pytest.raises(ValueError) as failure:
        store.bind_provider_identity(job_id, **arguments)
    assert "secret" not in str(failure.value) and "private" not in str(failure.value)
    assert store.get(job_id) == before and store.history(job_id) == history
    assert records(configuration, store, job_id) == prior


@pytest.mark.parametrize("state", ["ready", "held", "preparing", "submitting", "success", "failed", "cancelled"])
def test_only_uncertain_submitted_work_is_bindable(installation, state):
    configuration, store, snapshot = installation
    if state == "held":
        snapshot = configuration.apply(snapshot, snapshot.active.values.with_patch({"fax_disabled": True}),
            actor="test", restart_required=False)
    job_id = accept((configuration, store, snapshot))
    if state not in {"ready", "held"}:
        claim = store.claim("synthetic-worker")
        if state != "preparing":
            assert store.begin_submission(claim)
        if state in {"success", "failed", "cancelled"}:
            store.record_receipt(claim, provider_sid="issued-one", status=state)
    before, history = store.get(job_id), store.history(job_id)
    view = store.operator_view(job_id)
    assert view["state"] == state and view["can_bind_provider_identity"] is False
    assert view["bind_refusal_reason"]
    with pytest.raises(DeliveryConflict):
        store.bind_provider_identity(job_id, expected_version=before["version"],
            provider_sid="remote-one", actor="admin")
    assert store.get(job_id) == before and store.history(job_id) == history


@pytest.mark.parametrize("damage", ["unbound", "noattempt", "mismatched_profile", "not_submitted"])
def test_unverified_attempt_cannot_borrow_a_current_account(installation, damage):
    configuration, store, _ = installation
    job_id, claim, snapshot = uncertain(installation)
    with configuration.engine.begin() as connection:
        if damage == "unbound":
            connection.execute(configuration.job_bindings.delete().where(configuration.job_bindings.c.id == job_id))
        elif damage == "noattempt":
            connection.execute(store.deliveries.update().where(store.deliveries.c.id == job_id).values(attempt_id=None))
        elif damage == "mismatched_profile":
            connection.execute(store.attempts.update().where(store.attempts.c.id == claim.attempt_id).values(profile_id=None))
        else:
            connection.execute(store.attempts.update().where(store.attempts.c.id == claim.attempt_id).values(submitted_at=None))
    before, history = store.get(job_id), store.history(job_id)
    view = store.operator_view(job_id)
    assert view["can_bind_provider_identity"] is False and view["bind_refusal_reason"]
    if damage == "unbound":
        assert view["profile_id"] is None and view["revision_id"] is None and view["provider_id"] is None
    with pytest.raises(DeliveryConflict):
        store.bind_provider_identity(job_id, expected_version=before["version"],
            provider_sid="remote-one", actor="admin")
    assert store.get(job_id) == before and store.history(job_id) == history


@pytest.mark.parametrize("dispatch_mode,phase,completed", [
    ("held", "uncertain", False), ("unexpected", "uncertain", False),
    ("normal", "preparing", False), ("normal", "abandoned", False),
    ("normal", "success", False), ("normal", "failed", False),
    ("normal", "cancelled", False), ("normal", "uncertain", True),
])
def test_reconciliation_state_cannot_bind_held_or_resolved_attempt_combinations(
    installation, dispatch_mode, phase, completed,
):
    configuration, store, _ = installation
    job_id, claim, _ = uncertain(installation)
    with configuration.engine.begin() as connection:
        connection.execute(store.deliveries.update().where(store.deliveries.c.id == job_id).values(dispatch_mode=dispatch_mode))
        connection.execute(store.attempts.update().where(store.attempts.c.id == claim.attempt_id).values(
            phase=phase, completed_at=datetime.utcnow() if completed else None))
    before, history = store.get(job_id), store.history(job_id)
    prior = records(configuration, store, job_id)
    view = store.operator_view(job_id)
    assert view["state"] == "reconciliation_required"
    assert view["can_bind_provider_identity"] is False and view["bind_refusal_reason"]
    with pytest.raises(DeliveryConflict):
        store.bind_provider_identity(job_id, expected_version=before["version"], provider_sid="remote-one", actor="admin")
    assert store.get(job_id) == before and store.history(job_id) == history
    assert records(configuration, store, job_id) == prior


@pytest.mark.parametrize("profile,bindable", [
    (ProviderConfiguration("sip"), False), (ProviderConfiguration("freeswitch"), False),
    (ProviderConfiguration("custom", manifest={"id": "custom", "actions": {"send_fax": {}}}), False),
    (ProviderConfiguration("freeswitch", manifest={"id": "freeswitch", "actions": {"get_status": {}}}), True),
    (ProviderConfiguration("signalwire", settings={"status_poll_seconds": 0}), True),
    (ProviderConfiguration("sinch"), True),
])
def test_bindability_uses_captured_status_capability_not_provider_name(installation, profile, bindable):
    _, store, _ = installation
    job_id, _, _ = uncertain(installation, profile=profile)
    view = store.operator_view(job_id)
    assert view["can_bind_provider_identity"] is bindable
    if bindable:
        store.bind_provider_identity(job_id, expected_version=view["version"], provider_sid="remote-one", actor="admin")
    else:
        assert view["bind_refusal_reason"]
        with pytest.raises(DeliveryConflict):
            store.bind_provider_identity(job_id, expected_version=view["version"], provider_sid="remote-one", actor="admin")


def test_stale_version_and_existing_sid_never_change_identity(installation):
    _, store, _ = installation
    job_id, _, _ = uncertain(installation)
    version = store.get(job_id)["version"]
    with pytest.raises(DeliveryConflict):
        store.bind_provider_identity(job_id, expected_version=version - 1, provider_sid="remote-one", actor="admin")
    store.bind_provider_identity(job_id, expected_version=version, provider_sid="remote-one", actor="admin")
    before, history = store.get(job_id), store.history(job_id)
    for loaded_version in (version, before["version"]):
        with pytest.raises(DeliveryConflict):
            store.bind_provider_identity(job_id, expected_version=loaded_version, provider_sid="different", actor="admin")
    assert store.get(job_id) == before and store.history(job_id) == history


def test_remote_identity_already_owned_by_same_profile_cannot_attach_to_another_job(installation):
    configuration, store, snapshot = installation
    first, _, snapshot = uncertain(installation)
    store.bind_provider_identity(first, expected_version=store.get(first)["version"], provider_sid="same-remote", actor="admin")
    second, _, _ = uncertain((configuration, store, snapshot))
    before, history = store.get(second), store.history(second)
    with pytest.raises(DeliveryConflict):
        store.bind_provider_identity(second, expected_version=before["version"], provider_sid="same-remote", actor="admin")
    assert store.get(second) == before and store.history(second) == history


def test_bound_historical_job_without_attempt_still_owns_its_provider_identity(installation):
    configuration, store, snapshot = installation
    historical = accept(installation)
    with configuration.engine.begin() as connection:
        connection.execute(configuration.jobs.update().where(configuration.jobs.c.id == historical).values(provider_sid="known-old-sid"))
        connection.execute(store.deliveries.update().where(store.deliveries.c.id == historical).values(
            dispatch_mode="legacy", state="reconciliation_required"))
    job_id, _, _ = uncertain((configuration, store, snapshot))
    before, history = store.get(job_id), store.history(job_id)
    with pytest.raises(DeliveryConflict):
        store.bind_provider_identity(job_id, expected_version=before["version"], provider_sid="known-old-sid", actor="admin")
    assert store.get(job_id) == before and store.history(job_id) == history


def test_same_remote_identity_from_a_different_captured_profile_is_independent(installation):
    configuration, store, _ = installation
    first, _, snapshot = uncertain(installation)
    store.bind_provider_identity(first, expected_version=store.get(first)["version"], provider_sid="same-remote", actor="admin")
    second, _, _ = uncertain((configuration, store, snapshot),
        profile=ProviderConfiguration("phaxio", credentials={"api_key": "different-account"}))
    store.bind_provider_identity(second, expected_version=store.get(second)["version"], provider_sid="same-remote", actor="admin")
    assert configuration.outbound_profile(first).id != configuration.outbound_profile(second).id
    assert records(configuration, store, first)[0]["provider_sid"] == "same-remote"
    assert records(configuration, store, second)[0]["provider_sid"] == "same-remote"


def test_unbound_legacy_view_refuses_binding_without_inventing_an_account(installation):
    configuration, store, _ = installation
    job_id, now = uuid4().hex, datetime.utcnow()
    with configuration.engine.begin() as connection:
        connection.execute(configuration.jobs.insert().values(id=job_id, to_number="+12025550123",
            file_name="legacy.txt", tiff_path="", status="failed", backend="phaxio", created_at=now, updated_at=now))
        connection.execute(store.deliveries.insert().values(id=job_id, dispatch_mode="legacy", state="reconciliation_required",
            legacy_status="failed", version=1, created_at=now, updated_at=now))
    before, history = store.get(job_id), store.history(job_id)
    view = store.operator_view(job_id)
    assert view["attempt"] is None and view["provider_id"] is None
    assert view["profile_id"] is None and view["revision_id"] is None
    assert view["can_bind_provider_identity"] is False and "maintenance" in view["bind_refusal_reason"]
    with pytest.raises(DeliveryConflict):
        store.bind_provider_identity(job_id, expected_version=1, provider_sid="remote-one", actor="admin")
    assert store.get(job_id) == before and store.history(job_id) == history


def test_actor_total_length_boundary_is_supported_and_audited(installation):
    _, store, _ = installation
    job_id, _, _ = uncertain(installation)
    actor = "key:" + "a" * 96
    store.bind_provider_identity(job_id, expected_version=store.get(job_id)["version"], provider_sid="remote-one", actor=actor)
    assert store.operator_view(job_id)["events"][-1]["details"]["actor"] == actor


def test_late_original_receipt_cannot_replace_operator_bound_identity(installation):
    configuration, store, _ = installation
    job_id, claim, _ = uncertain(installation)
    store.bind_provider_identity(job_id, expected_version=store.get(job_id)["version"], provider_sid="confirmed-one", actor="admin")
    before = store.get(job_id)
    with pytest.raises(DeliveryConflict):
        store.record_receipt(claim, provider_sid="different-one", status="success")
    assert store.get(job_id) == before
    assert records(configuration, store, job_id)[0]["provider_sid"] == "confirmed-one"
    assert store.history(job_id)[-1]["kind"] == "provider_observation_refused"
    assert store.record_receipt(claim, provider_sid="confirmed-one", status="success")
    assert store.get(job_id)["state"] == "success"


@pytest.mark.asyncio
async def test_attached_identity_reconciles_through_original_account_after_rotation(installation, monkeypatch):
    configuration, store, _ = installation
    profile = ProviderConfiguration("phaxio", credentials={"api_key": "old-key", "api_secret": "old-secret"})
    job_id, claim, snapshot = uncertain(installation, profile=profile)
    configuration.apply(snapshot, snapshot.active.values, actor="test", restart_required=False,
        providers={"outbound": ProviderConfiguration("phaxio", credentials={"api_key": "new-key", "api_secret": "new-secret"})})
    store.bind_provider_identity(job_id, expected_version=store.get(job_id)["version"], provider_sid="confirmed-one", actor="admin")
    requests = []
    def response(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path.endswith("/faxes/confirmed-one")
        assert request.headers["Authorization"] == "Basic b2xkLWtleTpvbGQtc2VjcmV0"
        return httpx.Response(200, json={"success": True, "data": {"id": "confirmed-one", "status": "success"}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(response), **kwargs))
    assert await OutboundPoller(store).refresh(job_id)
    assert len(requests) == 1 and store.get(job_id)["state"] == "success"
    assert configuration.outbound_profile(job_id).id == claim.profile_id
    assert len(records(configuration, store, job_id)[1]) == 1


def test_operator_view_bounds_orders_and_sanitizes_history_without_secret_echo(installation):
    configuration, store, _ = installation
    job_id, claim, _ = uncertain(installation)
    now = datetime.utcnow() + timedelta(seconds=1)
    with configuration.engine.begin() as connection:
        connection.execute(configuration.jobs.update().where(configuration.jobs.c.id == job_id).values(
            pdf_token="private-media-token", pdf_url="https://private.invalid/?secret=private-query-key"))
        for index in range(105):
            connection.execute(store.events.insert().values(id=f"event-{index:03}", job_id=job_id,
                attempt_id=claim.attempt_id, kind="provider_observed", created_at=now + timedelta(seconds=index),
                details=json.dumps({"status": "in_progress", "private": "private-history-secret",
                    "actor": "raw-private-token", "category": "private-category", "provider_sid": "https://private.invalid"})))
    view = store.operator_view(job_id)
    assert view["events_truncated"] is True and len(view["events"]) == 100
    assert [event["id"] for event in view["events"]] == [f"event-{index:03}" for index in range(5, 105)]
    assert all(event["details"] == {"status": "in_progress"} for event in view["events"])
    assert "private" not in json.dumps(view, default=str) and "synthetic-key" not in json.dumps(view, default=str)
    assert "claim_token" not in view and "claim_owner" not in view


def test_operator_history_keeps_known_safe_evidence_and_hides_unknown_body(installation):
    configuration, store, _ = installation
    job_id, claim, _ = uncertain(installation)
    expected = {"status": "in_progress", "category": "response_unusable", "dispatch_mode": "legacy",
        "actor": "key:operator-1", "provider_sid": "known-sid", "legacy_status": "Previously Failed"}
    with configuration.engine.begin() as connection:
        connection.execute(store.events.insert().values(id=uuid4().hex, job_id=job_id,
            attempt_id=claim.attempt_id, kind="legacy_migrated", created_at=datetime.utcnow() + timedelta(seconds=1),
            details=json.dumps({**expected, "note": "private-provider-body", "credentials": {"token": "private-token"}})))
    event = store.operator_view(job_id)["events"][-1]
    assert event["details"] == expected and "private" not in json.dumps(event, default=str)


def test_missing_job_operator_operations_are_actionable_conflicts(installation):
    _, store, _ = installation
    with pytest.raises(DeliveryConflict):
        store.operator_view(uuid4().hex)
    with pytest.raises(DeliveryConflict):
        store.bind_provider_identity(uuid4().hex, expected_version=1, provider_sid="remote-one", actor="admin")
