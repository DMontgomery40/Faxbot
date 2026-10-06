"""Document acceptance through the real HTTP and preparation boundaries."""

from io import BytesIO
from contextlib import contextmanager
import unicodedata

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from fastapi import UploadFile
from PIL import Image
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from app.main import app
from app.config_values import ConfigurationValues
from app.documents import prepare_upload, UploadPreparationError


LIFECYCLE_LOCKS = frozenset({".faxbot-startup.lock", ".faxbot-serving.lock"})


def assert_artifacts(data_dir, *names):
    """Account for exact runtime control files without hiding unexpected artifacts."""
    assert {path.name for path in data_dir.iterdir()} == LIFECYCLE_LOCKS | set(names)
    for name in LIFECYCLE_LOCKS:
        path = data_dir / name
        assert path.is_file() and not path.is_symlink()
        assert path.read_bytes() == b""


@pytest.fixture
def document_client(monkeypatch, tmp_path, request):
    data_dir = tmp_path / "artifacts"
    backend = getattr(request, "param", "phaxio")
    for name in ConfigurationValues.environment_keys():
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "DATABASE_URL": f"sqlite:///{tmp_path / 'documents.db'}",
        "FAX_DATA_DIR": str(data_dir),
        "FAXBOT_INSTALLATION_KEY_PATH": str(tmp_path / ".configuration.key"),
        "FAX_DISABLED": "true",
        "FAX_BACKEND": backend,
        "FAX_OUTBOUND_BACKEND": backend,
        "INBOUND_ENABLED": "false",
        "REQUIRE_API_KEY": "true",
        "API_KEY": "synthetic-document-test-key",
        "PUBLIC_API_URL": "https://testserver",
        "MAX_REQUESTS_PER_MINUTE": "0",
        "MAX_FILE_SIZE_MB": "1",
        "ENABLE_PERSISTED_SETTINGS": "false",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    with TestClient(app, base_url="https://testserver", headers={
        "X-API-Key": "synthetic-document-test-key", "Origin": "https://testserver",
    }) as client:
        assert_artifacts(data_dir)
        lock_bytes = {name: (data_dir / name).read_bytes() for name in LIFECYCLE_LOCKS}
        try:
            yield client, data_dir
        finally:
            assert {
                name: (data_dir / name).read_bytes() for name in LIFECYCLE_LOCKS
            } == lock_bytes


def submit(client, content, filename="contest.txt", content_type="text/plain"):
    return client.post(
        "/fax", data={"to": "+15551230001"},
        files={"file": (filename, content, content_type)},
    )


def test_disabled_send_upload_returns_the_original_text(document_client):
    client, _ = document_client
    response = submit(client, b"First line\nFinal clinical billing marker")
    assert response.status_code == 202
    job = response.json()
    download = client.get(f"/admin/fax-jobs/{job['id']}/pdf")
    assert download.status_code == 200
    pdf = PdfReader(BytesIO(download.content))
    text = "\n".join(page.extract_text() for page in pdf.pages)
    assert "First line" in text
    assert "Final clinical billing marker" in text
    assert job["pages"] == len(pdf.pages) == 1


def pdf_bytes():
    output = BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(40, 700, "Original PDF first page")
    pdf.showPage()
    pdf.drawString(40, 700, "Original PDF final marker")
    pdf.save()
    return output.getvalue()


@pytest.mark.parametrize("filename,content_type", [
    ("contest.txt", "text/plain"), ("scan.dat", "application/octet-stream"),
])
def test_pdf_bytes_survive_misleading_metadata(document_client, filename, content_type):
    client, data_dir = document_client
    source = pdf_bytes()
    response = submit(client, source, filename, content_type)
    assert response.status_code == 202
    job = response.json()
    download = client.get(f"/admin/fax-jobs/{job['id']}/pdf")
    assert download.content == source
    assert job["pages"] == 2
    assert_artifacts(data_dir, f"{job['id']}.pdf")


@pytest.mark.parametrize("content,status", [
    (b"%PDF-1.4\ntest\n%%EOF", 400),
    (b"\xff\xfebinary", 415), (b"binary\x00payload", 415),
    (b"", 400), (b" \n\t", 415),
])
def test_invalid_upload_creates_no_job_or_artifacts(document_client, content, status):
    client, data_dir = document_client
    response = submit(client, content, "private-source.txt")
    assert response.status_code == status
    assert set(response.json()) == {"detail"}
    assert "private-source" not in response.json()["detail"]
    assert client.get("/admin/fax-jobs").json()["total"] == 0
    assert_artifacts(data_dir)


@pytest.mark.parametrize("filename", [
    "../../escaped.txt", r"C:\outside\escaped.txt", "x" * 500 + ".txt",
    "nested/subfolder/normal.txt",
])
def test_uploaded_name_is_display_only(document_client, filename, tmp_path):
    client, data_dir = document_client
    response = submit(client, b"Path-safe original contents", filename)
    assert response.status_code == 202
    job_id = response.json()["id"]
    display_name = client.get(f"/admin/fax-jobs/{job_id}").json()["file_name"]
    assert 0 < len(display_name) <= 200
    assert "/" not in display_name and "\\" not in display_name
    assert_artifacts(data_dir, f"{job_id}.pdf")
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_oversized_upload_creates_no_job_or_artifacts(document_client):
    client, data_dir = document_client
    response = submit(client, b"x" * (1024 * 1024 + 1))
    assert response.status_code == 413
    assert client.get("/admin/fax-jobs").json()["total"] == 0
    assert_artifacts(data_dir)


@pytest.mark.parametrize("document_client", ["freeswitch"], indirect=True)
def test_disabled_telephony_upload_produces_real_two_page_tiff(document_client):
    client, data_dir = document_client
    response = submit(client, pdf_bytes())
    assert response.status_code == 202
    job = response.json()
    assert job["backend"] == "freeswitch"
    assert job["pages"] == 2
    with Image.open(data_dir / f"{job['id']}.tiff") as fax:
        assert fax.format == "TIFF"
        assert fax.n_frames == 2
        fax.seek(1)
        fax.load()
        # GS tiffg4 AdjustWidth=1 normalizes near-A4 widths to 1728 fax columns.
        assert fax.size == (1728, 2292)
    # Asterisk runs as its own user in the data folder's group: it may read the pages it sends (0640);
    # the document itself stays the API's alone.
    assert oct((data_dir / f"{job['id']}.tiff").stat().st_mode & 0o777) == "0o640"
    assert oct((data_dir / f"{job['id']}.pdf").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("document_client", ["freeswitch"], indirect=True)
def test_missing_rasterizer_fails_without_accepting_job(document_client, monkeypatch):
    client, data_dir = document_client
    monkeypatch.setattr("app.conversion.shutil.which", lambda _: None)
    response = submit(client, b"Real document, unavailable conversion tool")
    assert response.status_code == 503
    assert response.json() == {"detail": "PDF rasterization is unavailable."}
    assert client.get("/admin/fax-jobs").json()["total"] == 0
    assert_artifacts(data_dir)


@contextmanager
def acceptance_fault(monkeypatch, data_dir, *, committed=False,
                     fail_before_commit=False, forbid_post_commit_reads=False):
    """Fail the real acceptance transaction, leaving startup and request reads intact."""
    store = app.state.configuration_runtime.manager.store
    outbound = app.state.access_runtime.outbound
    accept = outbound.accept
    commit = sa.engine.Connection.commit
    execute = sa.engine.Connection.execute
    connect = store.engine.connect
    state = {
        "accept": 0, "insert": 0, "commit": 0, "post_commit_reads": 0,
        "accepting": False, "commit_attempted": False,
    }

    def accepting(actor, revision, job, **kwargs):
        state["accept"] += 1
        state["revision"] = revision
        state["job_id"] = job["id"]
        assert (data_dir / f"{job['id']}.pdf").is_file(), "fault preceded preparation"
        state["accepting"] = True
        try:
            return accept(actor, revision, job, **kwargs)
        finally:
            state["accepting"] = False

    def faulting_execute(connection, statement, *args, **kwargs):
        if (state["accepting"] and getattr(statement, "is_insert", False)
                and statement.table is store.jobs):
            state["insert"] += 1
            if fail_before_commit:
                raise sa.exc.OperationalError(
                    "INSERT", {}, OSError("/private/database-host-details"),
                )
        return execute(connection, statement, *args, **kwargs)

    def faulting_commit(connection):
        if not state["accepting"]:
            return commit(connection)
        state["commit"] += 1
        state["commit_attempted"] = True
        if committed:
            commit(connection)
        raise sa.exc.OperationalError(
            "COMMIT", {}, OSError("/private/database-connection-details"),
        )

    def guarded_connect(*args, **kwargs):
        if forbid_post_commit_reads and state["commit_attempted"]:
            state["post_commit_reads"] += 1
            raise sa.exc.OperationalError(
                "SELECT", {}, OSError("/private/database-host-details"),
            )
        return connect(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(outbound, "accept", accepting)
        patch.setattr(sa.engine.Connection, "execute", faulting_execute)
        patch.setattr(sa.engine.Connection, "commit", faulting_commit)
        patch.setattr(store.engine, "connect", guarded_connect)
        yield state


def assert_durable_acceptance(state, *, committed):
    """Reconcile through a fresh connection after the fault guards are removed."""
    from app.outbound_store import OutboundStore
    store = app.state.configuration_runtime.manager.store
    deliveries = OutboundStore(store)
    with store.engine.connect() as connection:
        jobs = connection.execute(sa.select(store.jobs)).mappings().all()
        bindings = connection.execute(sa.select(store.job_bindings)).mappings().all()
        records = connection.execute(sa.select(deliveries.deliveries)).mappings().all()
        events = connection.execute(sa.select(deliveries.events)).mappings().all()
        assert connection.scalar(sa.select(sa.func.count()).select_from(deliveries.attempts)) == 0
    assert len(jobs) == len(bindings) == len(records) == len(events) == int(committed)
    # Queue-only acceptance stays held even when the COMMIT acknowledgement
    # is lost. Neither request failure nor recovery manufactures an attempt.
    assert deliveries.claim("synthetic-recovery-worker") is None
    if committed:
        job_id = state["job_id"]
        revision = state["revision"]
        assert jobs[0]["id"] == job_id
        assert jobs[0]["status"] == "queued"
        assert bindings[0]["id"] == job_id
        assert bindings[0]["revision_id"] == revision.id
        assert bindings[0]["profile_id"] == revision.profile_id("outbound")
        profile = store.outbound_profile(job_id)
        assert profile.id == bindings[0]["profile_id"]
        assert jobs[0]["backend"] == profile.configuration.provider_id
        assert records[0]["id"] == job_id
        assert records[0]["state"] == records[0]["dispatch_mode"] == "held"
        assert records[0]["version"] == 1 and records[0]["attempt_id"] is None
        assert events[0]["job_id"] == job_id and events[0]["kind"] == "accepted"


@pytest.mark.parametrize("committed", [False, True])
def test_commit_error_resolves_acceptance_before_cleaning_files(
    document_client, monkeypatch, committed,
):
    client, data_dir = document_client
    with acceptance_fault(monkeypatch, data_dir, committed=committed) as state:
        response = submit(client, b"Persisted document marker")
    assert state["accept"] == state["insert"] == state["commit"] == 1
    assert response.status_code == 503
    assert "private" not in response.text
    # Neither a missing row nor a successful recovery read changes lost acknowledgment.
    assert "uncertain" in response.json()["detail"]
    job_id = state["job_id"]
    assert job_id in response.json()["detail"]
    assert_artifacts(data_dir, f"{job_id}.pdf")
    text = PdfReader(data_dir / f"{job_id}.pdf").pages[0].extract_text()
    assert "Persisted document marker" in text
    assert_durable_acceptance(state, committed=committed)


def test_unknown_database_outcome_retains_document_and_reports_uncertainty(
    document_client, monkeypatch,
):
    client, data_dir = document_client
    with acceptance_fault(monkeypatch, data_dir, committed=True,
                          forbid_post_commit_reads=True) as state:
        response = submit(client, b"Retain until acceptance is known")
    assert state["accept"] == state["insert"] == state["commit"] == 1
    assert state["post_commit_reads"] == 0
    assert response.status_code == 503
    assert "uncertain" in response.json()["detail"]
    assert "private" not in response.text
    job_id = state["job_id"]
    assert_artifacts(data_dir, f"{job_id}.pdf")
    assert job_id in response.json()["detail"]
    text = PdfReader(data_dir / f"{job_id}.pdf").pages[0].extract_text()
    assert "Retain until acceptance is known" in text
    assert_durable_acceptance(state, committed=True)


def test_failure_before_any_commit_attempt_cleans_prepared_files(document_client, monkeypatch):
    client, data_dir = document_client

    with acceptance_fault(monkeypatch, data_dir, fail_before_commit=True) as state:
        response = submit(client, b"Acceptance failed before commit")
    assert state["accept"] == state["insert"] == 1
    assert state["commit"] == 0
    assert response.status_code == 503
    assert "private" not in response.text
    assert_artifacts(data_dir)
    assert_durable_acceptance(state, committed=False)


@pytest.mark.asyncio
async def test_display_controls_and_explicit_unaccepted_cleanup(tmp_path):
    upload = UploadFile(file=BytesIO(b"Original text"), filename="../a\x00b\u202ename.txt")
    prepared = await prepare_upload(
        upload, job_id="a" * 32, data_dir=str(tmp_path), max_bytes=100,
        requires_tiff=False,
    )
    assert prepared.original_name == "abname.txt"
    assert all(not unicodedata.category(c).startswith("C") for c in prepared.original_name)
    prepared.cleanup()
    prepared.cleanup()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("content,status", [(b"abcd", None), (b"abcde", 413)])
async def test_streaming_limit_accepts_exactly_the_configured_bytes(tmp_path, content, status):
    upload = UploadFile(file=BytesIO(content), filename="text.txt")
    if status:
        with pytest.raises(UploadPreparationError) as error:
            await prepare_upload(upload, job_id="b" * 32, data_dir=str(tmp_path), max_bytes=4, requires_tiff=False)
        assert error.value.status_code == status
        assert list(tmp_path.iterdir()) == []
    else:
        prepared = await prepare_upload(upload, job_id="b" * 32, data_dir=str(tmp_path), max_bytes=4, requires_tiff=False)
        assert "abcd" in PdfReader(prepared.pdf_path).pages[0].extract_text()


@pytest.mark.asyncio
async def test_identity_collision_never_overwrites_or_cleans_existing_artifact(tmp_path):
    existing = tmp_path / f"{'c' * 32}.pdf"
    existing.write_bytes(b"Previously accepted artifact")
    upload = UploadFile(file=BytesIO(b"New text"), filename="text.txt")
    with pytest.raises(UploadPreparationError) as error:
        await prepare_upload(upload, job_id="c" * 32, data_dir=str(tmp_path), max_bytes=100, requires_tiff=False)
    assert error.value.status_code == 503
    assert existing.read_bytes() == b"Previously accepted artifact"
    assert list(tmp_path.iterdir()) == [existing]


@pytest.mark.asyncio
async def test_cleanup_failure_is_sanitized_even_after_partial_publication(tmp_path, monkeypatch):
    from pathlib import Path

    job_id = "d" * 32
    existing_tiff = tmp_path / f"{job_id}.tiff"
    existing_tiff.write_bytes(b"Previous artifact")
    published_pdf = tmp_path / f"{job_id}.pdf"
    unlink = Path.unlink

    def fail_owned_unlink(path, *args, **kwargs):
        if path == published_pdf:
            raise OSError("/private/storage-mount-details")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_owned_unlink)
    upload = UploadFile(file=BytesIO(b"New document"), filename="document.txt")
    with pytest.raises(UploadPreparationError) as error:
        await prepare_upload(upload, job_id=job_id, data_dir=str(tmp_path), max_bytes=100, requires_tiff=True)
    assert error.value.status_code == 503
    assert "private" not in str(error.value)
    assert existing_tiff.read_bytes() == b"Previous artifact"


def test_multibyte_text_across_upload_chunk_boundary_is_preserved(document_client):
    client, _ = document_client
    response = submit(client, b"x" * 65535 + "é\nFinal marker".encode())
    assert response.status_code == 202
    pdf = client.get(f"/admin/fax-jobs/{response.json()['id']}/pdf")
    contents = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
    assert "é" in contents and "Final marker" in contents
