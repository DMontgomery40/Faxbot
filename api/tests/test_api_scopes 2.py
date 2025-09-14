import os
from fastapi.testclient import TestClient  # type: ignore
from api.app.main import app


def test_scope_enforcement_send_requires_scope(monkeypatch, tmp_path):
    # Enforce auth
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("FAX_DISABLED", "true")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path / "faxdata_test_scopes"))
    # Use bootstrap for admin
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")

    with TestClient(app) as client:
        # Create a key with only fax:read
        r = client.post(
            "/admin/api-keys",
            headers={"X-API-Key": "bootstrap_admin_only"},
            json={"name": "read-only", "owner": "tester", "scopes": ["fax:read"]},
        )
        token = r.json()["token"]

        # Try to send a fax using read-only token → expect 403
        files = {"file": ("example.txt", b"hello", "text/plain")}
        r2 = client.post("/fax", headers={"X-API-Key": token}, data={"to": "+15551234567"}, files=files)
        assert r2.status_code == 403


def test_scope_enforcement_read_requires_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("FAX_DISABLED", "true")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path / "faxdata_test_scopes2"))
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")

    with TestClient(app) as client:
        # Create a send-capable key and queue a job
        r = client.post(
            "/admin/api-keys",
            headers={"X-API-Key": "bootstrap_admin_only"},
            json={"name": "send-only", "owner": "tester", "scopes": ["fax:send"]},
        )
        send_token = r.json()["token"]
        files = {"file": ("example.txt", b"hello", "text/plain")}
        r2 = client.post("/fax", headers={"X-API-Key": send_token}, data={"to": "+15551234567"}, files=files)
        assert r2.status_code == 202
        job_id = r2.json()["id"]

        # Create a read-only key
        r3 = client.post(
            "/admin/api-keys",
            headers={"X-API-Key": "bootstrap_admin_only"},
            json={"name": "read-only2", "owner": "tester", "scopes": ["fax:read"]},
        )
        read_token = r3.json()["token"]

        # Using send-only token to read should fail with 403
        r4 = client.get(f"/fax/{job_id}", headers={"X-API-Key": send_token})
        assert r4.status_code == 403

        # Using read-only token to read should succeed
        r5 = client.get(f"/fax/{job_id}", headers={"X-API-Key": read_token})
        assert r5.status_code == 200

