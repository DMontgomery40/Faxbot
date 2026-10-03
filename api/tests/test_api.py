from fastapi.testclient import TestClient
from app.main import app
import pytest


@pytest.fixture
def authenticated_client(isolated_installation, monkeypatch):
    """Positive fax flows use the installation's current bootstrap identity."""
    monkeypatch.setenv("API_KEY", "synthetic-api-test-key")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    with TestClient(app, base_url="https://testserver", headers={
        "X-API-Key": "synthetic-api-test-key", "Origin": "https://testserver",
    }) as client:
        yield client


def test_health(isolated_installation):
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_send_validation_bad_number(authenticated_client):
    """Test validation with bad phone number."""
    files = {
        "to": (None, "abc"),
        "file": ("test.txt", b"hello", "text/plain"),
    }
    r = authenticated_client.post("/fax", files=files)
    assert r.status_code == 400


def test_send_txt(authenticated_client):
    """Submit real text while the test environment disables fax transmission."""
    files = {
        "to": (None, "+15551230001"),
        "file": ("test.txt", b"hello world", "text/plain"),
    }
    r = authenticated_client.post("/fax", files=files)
    assert r.status_code == 202
    data = r.json()
    assert data["status"] in {"queued", "disabled"}
    assert data["id"]
