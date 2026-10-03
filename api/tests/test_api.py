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


# -- one canonical destination at acceptance ----------------------------------------------------------

BOOTSTRAP = "synthetic-api-test-key"
DOCUMENT = b"Synthetic referral letter"


def installation_client(monkeypatch, country):
    monkeypatch.setenv("API_KEY", BOOTSTRAP)
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    monkeypatch.setenv("MAX_REQUESTS_PER_MINUTE", "0")
    monkeypatch.setenv("FAX_DEFAULT_COUNTRY", country)
    return TestClient(app, base_url="https://testserver",
                      headers={"X-API-Key": BOOTSTRAP, "Origin": "https://testserver"})


def send(client, to, *, key=None, document=DOCUMENT, headers=None):
    extra = dict(headers or {})
    if key is not None:
        extra["Idempotency-Key"] = key
    return client.post("/fax", data={"to": to}, files={"file": ("letter.txt", document, "text/plain")},
                       headers=extra)


def stored_jobs():
    import sqlalchemy as sa
    configuration = app.state.configuration_runtime.manager.store
    with configuration.engine.connect() as connection:
        return connection.execute(sa.select(configuration.jobs.c.id, configuration.jobs.c.to_number)
                                  .order_by(configuration.jobs.c.created_at)).all()


@pytest.mark.parametrize("country, entered, expected", [
    ("US", "303 555 0123", "+13035550123"), ("US", "(303) 555-0123", "+13035550123"),
    ("US", "+44 1782 684953", "+441782684953"),
    ("GB", "01782 684953", "+441782684953"), ("GB", "+44 1782 684953", "+441782684953"),
])
def test_destination_is_resolved_once_and_stored_in_e164(isolated_installation, monkeypatch, country, entered, expected):
    with installation_client(monkeypatch, country) as client:
        response = send(client, entered)
        assert response.status_code == 202, response.text
        assert response.json()["to"] == expected
        assert client.get(f"/fax/{response.json()['id']}").json()["to"] == expected
        assert [row.to_number for row in stored_jobs()] == [expected]


@pytest.mark.parametrize("country, entered", [
    ("US", "555 0100"), ("US", "442079460000"), ("US", "01782 684953"), ("GB", "684953"), ("US", "abc"),
])
def test_incomplete_or_ambiguous_destination_is_refused_with_nothing_accepted(
        isolated_installation, monkeypatch, country, entered):
    with installation_client(monkeypatch, country) as client:
        response = send(client, entered, key="refused-intent")
        assert response.status_code == 400
        assert response.json()["detail"].endswith(".") and entered not in response.json()["detail"]
        assert stored_jobs() == []


def test_replays_match_one_canonical_request_and_conflicts_stay_conflicts(isolated_installation, monkeypatch):
    with installation_client(monkeypatch, "GB") as client:
        first = send(client, "01782 684953", key="intent-1")
        assert first.status_code == 202
        for form in ("01782 684953", "+44 1782 684953", "0044 1782 684953"):
            replay = send(client, form, key="intent-1")
            assert replay.status_code == 202 and replay.json()["id"] == first.json()["id"]
        assert send(client, "01782 684954", key="intent-1").status_code == 409
        assert send(client, "01782 684953", key="intent-1", document=b"changed").status_code == 409
        second = send(client, "01782 684953", key="intent-2")  # a deliberate second send
        assert second.status_code == 202 and second.json()["id"] != first.json()["id"]
        assert len(stored_jobs()) == 2


def make_pre_change(job_id, entered, document=DOCUMENT):
    """Rewrite an accepted row the way the previous release stored it."""
    import hashlib
    import sqlalchemy as sa
    from app.request_identity import intent_fingerprint
    configuration = app.state.configuration_runtime.manager.store
    deliveries = configuration.delivery_tables["outbound_deliveries"]
    with configuration.engine.begin() as connection:
        connection.execute(configuration.jobs.update().where(configuration.jobs.c.id == job_id)
                           .values(to_number=entered))
        connection.execute(deliveries.update().where(deliveries.c.id == job_id).values(
            request_fingerprint=intent_fingerprint(version=1, to=entered, queue_only=False,
                                                   document_sha256=hashlib.sha256(document).hexdigest())))


def test_pre_change_records_replay_on_their_exact_original_request(isolated_installation, monkeypatch):
    with installation_client(monkeypatch, "US") as client:
        old = send(client, "3035550123", key="old-intent").json()["id"]
        make_pre_change(old, "3035550123")
        replay = send(client, "3035550123", key="old-intent")
        assert replay.status_code == 202 and replay.json()["id"] == old
        # The old identity bound the text exactly; a reformatted retry is not that request.
        assert send(client, "+1 303 555 0123", key="old-intent").status_code == 409
        # A number the previous release accepted but this one refuses still replays.
        legacy = send(client, "303 555 0124", key="legacy-short").json()["id"]
        make_pre_change(legacy, "123456")
        replay = send(client, "123456", key="legacy-short")
        assert replay.status_code == 202 and replay.json()["id"] == legacy
        assert send(client, "123456", key="new-short").status_code == 400
        assert len(stored_jobs()) == 2


def test_replay_resolves_under_the_country_its_original_was_accepted_with(isolated_installation, monkeypatch):
    with installation_client(monkeypatch, "GB") as client:
        first = send(client, "01782 684953", key="uk-intent").json()["id"]
        changed = client.put("/admin/settings", json={"fax_default_country": "US"})
        assert changed.status_code == 200, changed.text
        replay = send(client, "01782 684953", key="uk-intent")
        assert replay.status_code == 202 and replay.json()["id"] == first
        assert replay.json()["to"] == "+441782684953"
        assert send(client, "01782 684953", key="new-intent").status_code == 400  # not a US number
        assert [row.to_number for row in stored_jobs()] == ["+441782684953"]


def test_replay_requires_the_original_principal_and_current_credentials(isolated_installation, monkeypatch):
    with installation_client(monkeypatch, "US") as client:
        created = client.post("/admin/api-keys", json={"name": "sender", "scopes": ["fax:send", "fax:read"]})
        assert created.status_code == 200, created.text
        sender = {"X-API-Key": created.json()["token"]}
        first = send(client, "303 555 0123", key="shared-key", headers=sender)
        assert first.status_code == 202
        # The same key value from another principal is that principal's own request.
        other = send(client, "303 555 0123", key="shared-key")
        assert other.status_code == 202 and other.json()["id"] != first.json()["id"]
        assert client.delete(f"/admin/api-keys/{created.json()['key_id']}").status_code == 200
        assert send(client, "303 555 0123", key="shared-key", headers=sender).status_code == 401
        assert len(stored_jobs()) == 2


# -- client recovery against the real server ----------------------------------------------------------

class LoseFirstAnswer:
    """A requests transport into the app that loses the first fax answer after the server has it."""

    def __init__(self, client):
        self.client, self.posts, self.lost = client, 0, 0

    def send(self, request, **kwargs):
        import requests
        response = self.client.request(request.method, request.path_url, headers=dict(request.headers),
                                       content=request.body)
        if request.method == "POST" and request.path_url == "/fax":
            self.posts += 1
            if self.lost == 0:
                self.lost += 1
                raise requests.exceptions.ConnectionError("answer lost after the server accepted the fax")
        reply = requests.Response()
        reply.status_code, reply._content, reply.url, reply.request = (
            response.status_code, response.content, request.url, request)
        reply.headers = requests.structures.CaseInsensitiveDict(response.headers)
        reply.reason = response.reason_phrase
        return reply

    def close(self):
        pass


def test_sdk_recovers_a_lost_answer_as_one_job_and_one_provider_submission(isolated_installation, monkeypatch, tmp_path):
    import sys
    import time
    from pathlib import Path
    import requests
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdks" / "python"))
    from faxbot import FaxbotClient, FaxOperationConflict
    for name, value in {"FAX_DISABLED": "false", "FAX_BACKEND": "phaxio", "PHAXIO_API_KEY": "synthetic-key",
                        "PHAXIO_API_SECRET": "synthetic-secret"}.items():
        monkeypatch.setenv(name, value)
    submitted = []

    class Provider:
        status_callback_url = None
        def is_configured(self):
            return True
        async def send_fax(self, to, url, job_id, *, attempt_id):
            submitted.append((job_id, to))
            return {"provider_sid": f"PX-{len(submitted)}", "status": "queued"}

    monkeypatch.setattr("app.outbound_transport.service_from_profile", lambda profile: Provider())
    document = tmp_path / "letter.txt"
    document.write_bytes(DOCUMENT)
    with installation_client(monkeypatch, "GB") as client:
        transport = LoseFirstAnswer(client)
        session = requests.Session()
        session.mount("https://testserver", transport)
        sdk = FaxbotClient("https://testserver", BOOTSTRAP, session=session, retries=2, retry_backoff=0)
        operation = FaxbotClient.new_operation_id()
        job = sdk.send_fax("01782 684953", str(document), operation_id=operation)
        assert (transport.posts, transport.lost) == (2, 1)  # accepted, answer lost, recovered
        assert job["to"] == "+441782684953"
        deadline = time.monotonic() + 15
        while not submitted and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(2.5)  # further worker passes submit nothing more
        assert submitted == [(job["id"], "+441782684953")]
        assert [row.id for row in stored_jobs()] == [job["id"]]
        # The same operation with a different number is a conflict, never a second fax.
        with pytest.raises(FaxOperationConflict):
            sdk.send_fax("01782 684954", str(document), operation_id=operation)
        # Resuming the same operation returns the same job.
        assert sdk.resume_fax(operation, "+44 1782 684953", str(document))["id"] == job["id"]
        # A deliberate second send of the same document is a separate fax.
        second = sdk.send_fax("01782 684953", str(document))
        assert second["id"] != job["id"] and len(stored_jobs()) == 2
