from fastapi.testclient import TestClient
from app.main import app


def test_inbound_route_gating_with_explicit_backend(monkeypatch, tmp_path):
    # Explicitly set inbound backend to phaxio; internal asterisk route should 404
    monkeypatch.setenv("INBOUND_ENABLED", "true")
    monkeypatch.setenv("FAX_INBOUND_BACKEND", "phaxio")
    monkeypatch.setenv("ASTERISK_INBOUND_SECRET", "sekret")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path / "faxdata_inb_gate"))
    # Admin API key for any admin endpoints if used
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")

    with TestClient(app) as client:
        r = client.post(
            "/_internal/asterisk/inbound",
            headers={"X-Internal-Secret": "sekret"},
            json={"tiff_path": str(tmp_path / "missing.tiff"), "to_number": "+15551234567", "uniqueid": "x"},
        )
        assert r.status_code == 404
