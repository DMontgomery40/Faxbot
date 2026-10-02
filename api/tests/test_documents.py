"""Document acceptance through the real HTTP and preparation boundaries."""

from io import BytesIO
from contextlib import contextmanager
import unicodedata

import pytest
from fastapi.testclient import TestClient
from fastapi import UploadFile
from PIL import Image
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from app.main import app
from app import main
from app.documents import prepare_upload, UploadPreparationError


@pytest.fixture
def document_client(monkeypatch, tmp_path, request):
    data_dir = tmp_path / "artifacts"
    backend = getattr(request, "param", "phaxio")
    for name, value in {
        "DATABASE_URL": f"sqlite:///{tmp_path / 'documents.db'}",
        "FAX_DATA_DIR": str(data_dir),
        "FAX_DISABLED": "true",
        "FAX_BACKEND": backend,
        "FAX_OUTBOUND_BACKEND": backend,
        "INBOUND_ENABLED": "false",
        "REQUIRE_API_KEY": "true",
        "API_KEY": "synthetic-document-test-key",
        "MAX_REQUESTS_PER_MINUTE": "0",
        "MAX_FILE_SIZE_MB": "1",
        "ENABLE_PERSISTED_SETTINGS": "false",
    }.items():
        monkeypatch.setenv(name, value)
    with TestClient(app, headers={"X-API-Key": "synthetic-document-test-key"}) as client:
        yield client, data_dir


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
    assert {p.name for p in data_dir.iterdir()} == {f"{job['id']}.pdf"}


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
    assert list(data_dir.iterdir()) == []


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
    assert {p.name for p in data_dir.iterdir()} == {f"{job_id}.pdf"}
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_oversized_upload_creates_no_job_or_artifacts(document_client):
    client, data_dir = document_client
    response = submit(client, b"x" * (1024 * 1024 + 1))
    assert response.status_code == 413
    assert client.get("/admin/fax-jobs").json()["total"] == 0
    assert list(data_dir.iterdir()) == []


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


@pytest.mark.parametrize("document_client", ["freeswitch"], indirect=True)
def test_missing_rasterizer_fails_without_accepting_job(document_client, monkeypatch):
    client, data_dir = document_client
    monkeypatch.setattr("app.conversion.shutil.which", lambda _: None)
    response = submit(client, b"Real document, unavailable conversion tool")
    assert response.status_code == 503
    assert response.json() == {"detail": "PDF rasterization is unavailable."}
    assert client.get("/admin/fax-jobs").json()["total"] == 0
    assert list(data_dir.iterdir()) == []


@pytest.mark.parametrize("committed", [False, True])
def test_commit_error_resolves_acceptance_before_cleaning_files(
    document_client, monkeypatch, committed,
):
    client, data_dir = document_client
    session_factory = main.SessionLocal

    @contextmanager
    def uncertain_session():
        with session_factory() as db:
            commit = db.commit

            def fail_commit():
                if committed:
                    commit()
                raise OSError("/private/database-connection-details")

            db.commit = fail_commit
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(main, "SessionLocal", uncertain_session)
        response = submit(client, b"Persisted document marker")
    assert "private" not in response.text
    jobs = client.get("/admin/fax-jobs").json()
    if committed:
        assert response.status_code == 202
        assert jobs["total"] == 1
        job_id = response.json()["id"]
        pdf = client.get(f"/admin/fax-jobs/{job_id}/pdf")
        assert "Persisted document marker" in PdfReader(BytesIO(pdf.content)).pages[0].extract_text()
    else:
        assert response.status_code == 503
        assert jobs["total"] == 0
        assert list(data_dir.iterdir()) == []


def test_unknown_database_outcome_retains_document_and_reports_uncertainty(
    document_client, monkeypatch,
):
    client, data_dir = document_client

    def unavailable_session():
        raise OSError("/private/database-host-details")

    with monkeypatch.context() as patch:
        patch.setattr(main, "SessionLocal", unavailable_session)
        response = submit(client, b"Retain until acceptance is known")
    assert response.status_code == 503
    assert "uncertain" in response.json()["detail"]
    assert "private" not in response.text
    artifacts = list(data_dir.iterdir())
    assert len(artifacts) == 1 and artifacts[0].suffix == ".pdf"
    assert artifacts[0].stem in response.json()["detail"]


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


def test_multibyte_text_across_upload_chunk_boundary_is_preserved(document_client):
    client, _ = document_client
    response = submit(client, b"x" * 65535 + "é\nFinal marker".encode())
    assert response.status_code == 202
    pdf = client.get(f"/admin/fax-jobs/{response.json()['id']}/pdf")
    contents = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
    assert "é" in contents and "Final marker" in contents
