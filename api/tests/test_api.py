from fastapi.testclient import TestClient


def test_health():
    from app.main import app
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_send_validation_bad_number(tmp_path, monkeypatch):
    # Ensure fax disabled to avoid AMI during tests
    monkeypatch.setenv("FAX_DISABLED", "true")

    from app.main import app
    with TestClient(app) as c:
        files = {
            "to": (None, "abc"),
            "file": ("test.txt", b"hello", "text/plain"),
        }
        r = c.post("/fax", files=files)
        assert r.status_code == 400


def test_send_txt(tmp_path, monkeypatch):
    monkeypatch.setenv("FAX_DISABLED", "true")
    from app.main import app
    with TestClient(app) as c:
        files = {
            "to": (None, "+15551230001"),
            "file": ("test.txt", b"hello world", "text/plain"),
        }
        r = c.post("/fax", files=files)
        assert r.status_code == 202
        data = r.json()
        assert data["status"] in {"queued", "disabled"}
        assert data["id"]
