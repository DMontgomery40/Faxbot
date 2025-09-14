from fastapi.testclient import TestClient  # type: ignore
from api.app.main import app


def test_rate_limit_enforced(monkeypatch, tmp_path):
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("FAX_DISABLED", "true")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path / "faxdata_test_rl"))
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("MAX_REQUESTS_PER_MINUTE", "2")

    with TestClient(app) as client:
        # Create a send+read key
        r = client.post(
            "/admin/api-keys",
            headers={"X-API-Key": "bootstrap_admin_only"},
            json={"name": "rl", "owner": "tester", "scopes": ["fax:send", "fax:read"]},
        )
        token = r.json()["token"]

        files = {"file": ("a.txt", b"1", "text/plain")}
        # First send
        assert client.post("/fax", headers={"X-API-Key": token}, data={"to": "+15551234567"}, files=files).status_code == 202
        # Second send
        assert client.post("/fax", headers={"X-API-Key": token}, data={"to": "+15551234567"}, files=files).status_code == 202
        # Third within same minute should hit 429
        r3 = client.post("/fax", headers={"X-API-Key": token}, data={"to": "+15551234567"}, files=files)
        assert r3.status_code == 429

