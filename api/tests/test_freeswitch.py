from fastapi.testclient import TestClient
from app.main import app


def test_freeswitch_send_and_result(monkeypatch, tmp_path):
    # Configure environment for FreeSWITCH backend in disabled mode
    monkeypatch.setenv("FAX_BACKEND", "freeswitch")
    monkeypatch.setenv("FAX_DISABLED", "true")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASTERISK_INBOUND_SECRET", "sekret")

    with TestClient(app) as c:
        # Submit a simple text fax (conversion paths are stubbed in disabled mode)
        files = {
            "to": (None, "+15551230001"),
            "file": ("test.txt", b"hello world", "text/plain"),
        }
        r = c.post("/fax", files=files)
        assert r.status_code == 202
        job = r.json()
        job_id = job["id"]
        assert job_id
        # Simulate FreeSWITCH outbound result callback
        payload = {
            "job_id": job_id,
            "fax_status": "SUCCESS",
            "fax_result_text": "completed",
            "fax_document_transferred_pages": 1,
            "uuid": "demo-uuid"
        }
        r2 = c.post("/_internal/freeswitch/outbound_result", json=payload, headers={"X-Internal-Secret": "sekret"})
        assert r2.status_code == 200
        # Verify job status updated
        r3 = c.get(f"/fax/{job_id}")
        assert r3.status_code == 200
        j = r3.json()
        assert j["status"] == "SUCCESS"

