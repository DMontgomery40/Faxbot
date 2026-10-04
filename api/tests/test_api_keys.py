import os
import shutil
from fastapi.testclient import TestClient  # type: ignore
from api.app.main import app
from api.app.config import settings


def test_admin_create_and_use_api_key(isolated_installation, monkeypatch, tmp_path):
    # Isolate environment for this test
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    monkeypatch.setenv("FAX_DISABLED", "true")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path / "faxdata_test_keys"))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "api-keys.db"))

    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        # Sanity check: bootstrap key loaded after startup
        from api.app.config import settings as live_settings  # re-import reference
        assert live_settings.api_key == "bootstrap_admin_only"
        # Create a new DB-backed key via admin endpoint
        r = client.post(
            "/admin/api-keys",
            headers={"X-API-Key": "bootstrap_admin_only"},
            json={"name": "dev", "owner": "tester", "scopes": ["fax:send", "fax:read"]},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert "token" in data and data["token"].startswith("fbk_live_"), data
        token = data["token"]

        # Send a fax (test mode enabled)
        files = {"file": ("example.txt", b"hello world", "text/plain")}
        r2 = client.post(
            "/fax",
            headers={"X-API-Key": token},
            data={"to": "+15551234567"},
            files=files,
        )
        assert r2.status_code == 202, r2.text
        job = r2.json()
        assert "id" in job and job["status"] in ("queued", "in_progress", "SUCCESS", "disabled")
        job_id = job["id"]

        # Get status
        r3 = client.get(f"/fax/{job_id}", headers={"X-API-Key": token})
        assert r3.status_code == 200, r3.text
        status = r3.json()
        assert status["id"] == job_id

        # List keys (admin)
        r4 = client.get("/admin/api-keys", headers={"X-API-Key": "bootstrap_admin_only"})
        assert r4.status_code == 200
        keys = r4.json()
        assert any(k["key_id"] == data["key_id"] for k in keys)

        # Revoke key
        r5 = client.delete(f"/admin/api-keys/{data['key_id']}", headers={"X-API-Key": "bootstrap_admin_only"})
        assert r5.status_code == 200

        # Try to use revoked key
        r6 = client.get(f"/fax/{job_id}", headers={"X-API-Key": token})
        assert r6.status_code == 401


def _key_client(monkeypatch):
    for name, value in {"API_KEY": "bootstrap_admin_only", "REQUIRE_API_KEY": "true",
                        "PUBLIC_API_URL": "https://testserver", "FAX_DISABLED": "true",
                        "FAX_BACKEND": "phaxio"}.items():
        monkeypatch.setenv(name, value)
    return TestClient(app, base_url="https://testserver")


def _issue(client, admin, scopes, **fields):
    return client.post("/admin/api-keys", headers={"X-API-Key": admin}, json={"scopes": scopes, **fields})


def test_unknown_scopes_are_rejected_by_name_without_issuing(isolated_installation, monkeypatch):
    with _key_client(monkeypatch) as client:
        r = _issue(client, "bootstrap_admin_only", ["fax:send", "fax:admin", "*"], name="bad")
        assert r.status_code == 400
        assert "fax:admin" in r.json()["detail"] and "*" in r.json()["detail"]
        listed = client.get("/admin/api-keys", headers={"X-API-Key": "bootstrap_admin_only"})
        assert listed.status_code == 200 and listed.json() == []


def test_rotation_keeps_key_id_and_scopes_and_retires_the_old_secret(isolated_installation, monkeypatch):
    with _key_client(monkeypatch) as client:
        created = _issue(client, "bootstrap_admin_only", ["fax:send", "fax:read"], name="ios", owner="front desk")
        assert created.status_code == 200, created.text
        key = created.json()
        assert key["scopes"] == ["fax:read", "fax:send"] and key["owner"] == "front desk"
        sent = client.post("/fax", headers={"X-API-Key": key["token"]}, data={"to": "+15551234567"},
                           files={"file": ("a.txt", b"1", "text/plain")})
        assert sent.status_code == 202, sent.text
        rotated = client.post(f"/admin/api-keys/{key['key_id']}/rotate", headers={"X-API-Key": "bootstrap_admin_only"})
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["key_id"] == key["key_id"] and rotated.json()["token"] != key["token"]
        job = f"/fax/{sent.json()['id']}"
        assert client.get(job, headers={"X-API-Key": key["token"]}).status_code == 401
        assert client.get(job, headers={"X-API-Key": rotated.json()["token"]}).status_code == 200
        listed = client.get("/admin/api-keys", headers={"X-API-Key": "bootstrap_admin_only"}).json()
        assert [(k["key_id"], k["scopes"], k["name"], k["owner"]) for k in listed] == [
            (key["key_id"], ["fax:read", "fax:send"], "ios", "front desk")]
        missing = client.post("/admin/api-keys/000000000000/rotate", headers={"X-API-Key": "bootstrap_admin_only"})
        assert missing.status_code == 404


def test_key_manager_issues_only_within_its_own_scopes(isolated_installation, monkeypatch):
    with _key_client(monkeypatch) as client:
        manager = _issue(client, "bootstrap_admin_only", ["keys:manage", "fax:read"], name="manager").json()["token"]
        plain = _issue(client, "bootstrap_admin_only", ["fax:read"], name="plain").json()
        assert _issue(client, manager, ["fax:read"], name="delegated").status_code == 200
        # Delegation cannot exceed the manager's own authority.
        assert _issue(client, manager, ["fax:send"], name="escalated").status_code == 403
        assert client.get("/admin/api-keys", headers={"X-API-Key": manager}).status_code == 200
        # A key without keys:manage cannot list, revoke or probe for keys.
        assert client.get("/admin/api-keys", headers={"X-API-Key": plain["token"]}).status_code == 403
        assert client.delete(f"/admin/api-keys/{plain['key_id']}", headers={"X-API-Key": plain["token"]}).status_code == 403
        assert client.delete("/admin/api-keys/000000000000", headers={"X-API-Key": plain["token"]}).status_code == 403
        assert client.delete("/admin/api-keys/000000000000", headers={"X-API-Key": manager}).status_code == 404
        assert client.get("/admin/api-keys", headers={"X-API-Key": "fbk_live_000000000000_wrong"}).status_code == 401


def _last_used(client, admin, key_id):
    listed = client.get("/admin/api-keys", headers={"X-API-Key": admin})
    assert listed.status_code == 200, listed.text
    return next(k["last_used_at"] for k in listed.json() if k["key_id"] == key_id)


def test_header_key_use_records_last_used_at_most_once_a_minute(isolated_installation, monkeypatch):
    from datetime import datetime, timedelta
    with _key_client(monkeypatch) as client:
        key = _issue(client, "bootstrap_admin_only", ["fax:read"], name="scanner").json()
        assert _last_used(client, "bootstrap_admin_only", key["key_id"]) is None

        # A wrong secret for the same key is not a use.
        wrong = "fbk_live_" + key["key_id"] + "_not-the-secret"
        assert client.get("/auth/me", headers={"X-API-Key": wrong}).status_code == 401
        assert _last_used(client, "bootstrap_admin_only", key["key_id"]) is None

        assert client.get("/auth/me", headers={"X-API-Key": key["token"]}).status_code == 200
        first = _last_used(client, "bootstrap_admin_only", key["key_id"])
        assert first is not None

        # Within a minute, further requests leave the recorded time alone.
        assert client.get("/auth/me", headers={"X-API-Key": key["token"]}).status_code == 200
        assert _last_used(client, "bootstrap_admin_only", key["key_id"]) == first

        # Once the recorded use is more than a minute old, the next request records it again.
        service = app.state.access_runtime
        keys = service.store.tables["api_keys"]
        earlier = datetime.utcnow() - timedelta(minutes=5)
        with service.store.engine.begin() as connection:
            connection.execute(keys.update().where(keys.c.key_id == key["key_id"]).values(last_used_at=earlier))
        assert client.get("/auth/me", headers={"X-API-Key": key["token"]}).status_code == 200
        moved = datetime.fromisoformat(_last_used(client, "bootstrap_admin_only", key["key_id"]))
        assert moved > earlier + timedelta(minutes=4)


def test_last_used_is_recorded_after_authentication_and_a_failed_write_never_refuses_the_request(
        isolated_installation, monkeypatch):
    import sqlalchemy as sa
    with _key_client(monkeypatch) as client:
        key = _issue(client, "bootstrap_admin_only", ["fax:read"], name="scanner").json()
        engine = app.state.access_runtime.store.engine
        # A database that refuses the bookkeeping write (here a trigger) must not refuse the request.
        with engine.begin() as connection:
            connection.execute(sa.text(
                "CREATE TRIGGER refuse_key_use BEFORE UPDATE OF last_used_at ON api_keys "
                "BEGIN SELECT RAISE(ABORT, 'synthetic bookkeeping failure'); END"))
        assert client.get("/auth/me", headers={"X-API-Key": key["token"]}).status_code == 200
        assert _last_used(client, "bootstrap_admin_only", key["key_id"]) is None
        with engine.begin() as connection:
            connection.execute(sa.text("DROP TRIGGER refuse_key_use"))
        # The write happens once the request's authentication has committed.
        assert client.get("/auth/me", headers={"X-API-Key": key["token"]}).status_code == 200
        assert _last_used(client, "bootstrap_admin_only", key["key_id"]) is not None
