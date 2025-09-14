from fastapi.testclient import TestClient  # type: ignore
from api.app.main import app


def test_internal_asterisk_inbound_flow(monkeypatch, tmp_path):
    # Enable inbound and set secret
    monkeypatch.setenv("INBOUND_ENABLED", "true")
    monkeypatch.setenv("ASTERISK_INBOUND_SECRET", "sekret")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path / "faxdata_inb"))
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")

    # Create a dummy tiff file
    tiff = tmp_path / "in.tiff"
    tiff.write_bytes(b"TIFF_PLACEHOLDER")

    with TestClient(app) as client:
        # Post internal event
        r = client.post(
            "/_internal/asterisk/inbound",
            headers={"X-Internal-Secret": "sekret"},
            json={
                "tiff_path": str(tiff),
                "to_number": "+15551234567",
                "from_number": "+15550001111",
                "faxstatus": "received",
                "faxpages": 1,
                "uniqueid": "abc123"
            },
        )
        assert r.status_code == 200, r.text
        inbound_id = r.json()["id"]

        # Create a read key for listing and fetching
        r2 = client.post(
            "/admin/api-keys",
            headers={"X-API-Key": "bootstrap_admin_only"},
            json={"name": "in-read", "owner": "tester", "scopes": ["inbound:list", "inbound:read"]},
        )
        token = r2.json()["token"]

        # List
        r3 = client.get("/inbound", headers={"X-API-Key": token})
        assert r3.status_code == 200
        assert any(item["id"] == inbound_id for item in r3.json())

        # Get metadata
        r4 = client.get(f"/inbound/{inbound_id}", headers={"X-API-Key": token})
        assert r4.status_code == 200

        # Download PDF via API key auth
        r5 = client.get(f"/inbound/{inbound_id}/pdf", headers={"X-API-Key": token})
        assert r5.status_code == 200
        assert r5.headers.get("content-type", "").startswith("application/pdf")

