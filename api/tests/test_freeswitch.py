from fastapi.testclient import TestClient
from app.main import app
import pytest
import sqlalchemy as sa


@pytest.mark.parametrize("attempt_id,secret,status", [
    (None, "sekret", 409), ("a" * 32, "sekret", 409),
    ("a" * 32, None, 401), ("a" * 32, "wrong-secret", 401),
])
def test_held_freeswitch_job_refuses_unowned_result_without_mutation(
    isolated_installation, monkeypatch, tmp_path, attempt_id, secret, status,
):
    # Queue-only jobs have no issued attempt, even with an installation secret.
    monkeypatch.setenv("FAX_BACKEND", "freeswitch")
    monkeypatch.setenv("FAX_DISABLED", "true")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASTERISK_INBOUND_SECRET", "sekret")
    monkeypatch.setenv("API_KEY", "synthetic-freeswitch-test-key")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")

    with TestClient(app, base_url="https://testserver", headers={
        "X-API-Key": "synthetic-freeswitch-test-key", "Origin": "https://testserver",
    }) as c:
        # The actual upload conversion and durable held acceptance remain real.
        files = {
            "to": (None, "+15551230001"),
            "file": ("test.txt", b"hello world", "text/plain"),
        }
        r = c.post("/fax", files=files)
        assert r.status_code == 202
        job = r.json()
        job_id = job["id"]
        assert job_id
        assert job["status"] == "queued" and job["delivery_state"] == "held"
        from app.outbound_store import OutboundStore
        configuration = app.state.configuration_runtime.manager.store
        store = OutboundStore(configuration)
        before, history = store.get(job_id), store.history(job_id)
        before_job = c.get(f"/fax/{job_id}").json()
        payload = {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "fax_status": "SUCCESS",
            "fax_result_text": "completed",
            "fax_document_transferred_pages": 1,
            "uuid": "demo-uuid"
        }
        headers = {"X-Internal-Secret": secret} if secret is not None else {}
        r2 = c.post("/_internal/freeswitch/outbound_result", json=payload, headers=headers)
        assert r2.status_code == status
        assert "sekret" not in r2.text and "wrong-secret" not in r2.text and "demo-uuid" not in r2.text
        r3 = c.get(f"/fax/{job_id}")
        assert r3.status_code == 200
        assert r3.json() == before_job
        assert store.get(job_id) == before and store.history(job_id) == history
        assert before["attempt_id"] is None and before["dispatch_mode"] == "held"
        with configuration.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(store.attempts)) == 0
