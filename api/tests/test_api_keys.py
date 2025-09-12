import os
import shutil
from fastapi.testclient import TestClient  # type: ignore
from api.app.main import app
from api.app.config import settings


def test_admin_create_and_use_api_key(monkeypatch, tmp_path):
    # Isolate environment for this test
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("FAX_DISABLED", "true")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path / "faxdata_test_keys"))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./faxbot_test_api_keys.db")

    with TestClient(app) as client:
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
