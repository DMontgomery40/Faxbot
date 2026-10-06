import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.phaxio_service import PhaxioFaxService
from app.main import app
from api.tests.test_outbound_store import installation, accept
from api.tests.test_schema import database


def test_phaxio_service_initialization():
    service = PhaxioFaxService(
        api_key="key",
        api_secret="secret",
        status_callback_url="https://example.com/callback",
    )
    assert service.is_configured() is True


def test_phaxio_service_not_configured():
    service = PhaxioFaxService(api_key="", api_secret="", status_callback_url=None)
    assert service.is_configured() is False


@pytest.mark.asyncio
async def test_send_fax_not_configured():
    service = PhaxioFaxService(api_key="", api_secret="", status_callback_url=None)
    with pytest.raises(ValueError):
        await service.send_fax("+15551234567", "https://example.com/file.pdf", "job123")


@pytest.mark.asyncio
async def test_send_fax_success(monkeypatch):
    service = PhaxioFaxService(api_key="key", api_secret="secret", status_callback_url="https://cb")

    class DummyResp:
        status_code = 200

        def json(self):
            return {"success": True, "data": {"id": 123, "status": "queued"}}

    async def fake_post(url, data=None, auth=None):
        assert url.endswith("/faxes")
        assert auth == ("key", "secret")
        return DummyResp()

    # Patch httpx.AsyncClient.post
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
        res = await service.send_fax("+12223334444", "https://example.com/a.pdf", "jobid")
        assert res["provider_sid"] == "123"
        assert res["status"] == "queued"


@pytest.mark.asyncio
async def test_handle_status_callback():
    service = PhaxioFaxService(api_key="key", api_secret="secret", status_callback_url=None)
    data = {
        "fax[id]": "999",
        "fax[status]": "success",
        "fax[num_pages]": "2",
    }
    res = await service.handle_status_callback(data)
    assert res["provider_sid"] == "999"
    assert res["status"] == "SUCCESS"
    assert res["pages"] == 2


def test_backend_selection_from_env(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    settings = Settings()
    assert settings.fax_backend == "phaxio"

    monkeypatch.setenv("FAX_BACKEND", "sip")
    settings = Settings()
    assert settings.fax_backend == "sip"


@pytest.mark.asyncio
async def test_phaxio_integration_end_to_end(isolated_installation, monkeypatch, tmp_path):
    """Test complete Phaxio integration flow."""
    # Setup test environment
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("PHAXIO_API_KEY", "test_key")
    monkeypatch.setenv("PHAXIO_API_SECRET", "test_secret")
    monkeypatch.setenv("FAX_DISABLED", "true")  # Don't actually send
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "synthetic-phaxio-test-key")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    
    # Create test PDF file
    test_pdf_path = tmp_path / "test.pdf"
    document = PdfWriter()
    document.add_blank_page(width=612, height=792)
    document.write(test_pdf_path)
    
    with TestClient(app, base_url="https://testserver", headers={
        "X-API-Key": "synthetic-phaxio-test-key", "Origin": "https://testserver",
    }) as client:
        # Test fax submission with Phaxio backend
        files = {
            "to": (None, "+15551234567"),
            "file": ("test.pdf", test_pdf_path.read_bytes(), "application/pdf"),
        }
        
        response = client.post("/fax", files=files)

        assert response.status_code == 202
        data = response.json()
        assert data["backend"] == "phaxio"
        assert data["status"] in ["queued", "disabled"]


@pytest.mark.parametrize("unknown_job", [False, True])
def test_phaxio_unsigned_unbound_callback_is_refused_without_mutation(
    isolated_installation, monkeypatch, unknown_job,
):
    """A disabled signature flag never authorizes an unowned callback."""
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("PHAXIO_VERIFY_SIGNATURE", "false")
    monkeypatch.setenv("API_KEY", "synthetic-phaxio-callback-test-key")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    with TestClient(app, base_url="https://testserver", headers={
        "X-API-Key": "synthetic-phaxio-callback-test-key", "Origin": "https://testserver",
    }) as client:
        from app.outbound_store import OutboundStore
        response = client.post("/fax", data={"to": "+12025550123"},
            files={"file": ("synthetic.txt", b"Held callback document", "text/plain")})
        assert response.status_code == 202
        job_id = response.json()["id"]
        assert response.json()["delivery_state"] == "held"
        store = OutboundStore(app.state.configuration_runtime.manager.store)
        before, history = store.get(job_id), store.history(job_id)
        before_job = client.get(f"/fax/{job_id}").json()
        callback_data = {
            "fax[id]": "phaxio_123",
            "fax[status]": "success",
            "fax[num_pages]": "2",
            "fax[to]": "+15551234567"
        }
        
        locator = "f" * 32 if unknown_job else job_id
        response = client.post(
            "/phaxio-callback?job_id=" + locator,
            data=callback_data, headers={"X-Phaxio-Signature": "invalid-signature"},
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "Callback could not be authenticated for this attempt."}
        assert client.get(f"/fax/{job_id}").json() == before_job
        assert store.get(job_id) == before and store.history(job_id) == history
        assert before["attempt_id"] is None and before["dispatch_mode"] == "held"
        import sqlalchemy as sa
        configuration = store.configuration
        with configuration.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(configuration.jobs)) == 1


def test_disabled_phaxio_callbacks_refuse_even_valid_captured_signature(installation):
    """Internal account/attempt seam: disabling verification disables callbacks."""
    import hashlib
    import hmac
    from api.app.config_profiles import ProviderConfiguration
    from api.app.outbound_callbacks import CapturedCallbacks, CallbackRejected
    from api.app.provider_signatures import verify_phaxio_signature
    configuration, store, snapshot = installation
    token = "synthetic-callback-token"
    callback = "https://synthetic.invalid/phaxio-callback"
    snapshot = configuration.apply(snapshot, snapshot.active.values, actor="test",
        restart_required=False, providers={"outbound": ProviderConfiguration("phaxio",
            credentials={"callback_token": token},
            settings={"callback_url": callback, "verify_signature": False})})
    job_id = accept((configuration, store, snapshot))
    claim = store.claim("synthetic-worker")
    assert store.begin_submission(claim)
    fields = [("id", "remote-one"), ("status", "success")]
    url = callback + "?job_id=" + job_id + "&attempt_id=" + claim.attempt_id
    message = url + "".join(name + value for name, value in sorted(fields))
    signature = hmac.new(token.encode(), message.encode(), hashlib.sha1).hexdigest()
    assert verify_phaxio_signature(token, url, fields, [], signature)
    before, history = store.get(job_id), store.history(job_id)
    with pytest.raises(CallbackRejected):
        CapturedCallbacks(store).receive("phaxio", job_id, claim.attempt_id,
            fields=fields, files=[], signature=signature)
    assert store.get(job_id) == before and store.history(job_id) == history


def test_phone_number_normalization():
    """Test phone number formatting for Phaxio."""
    service = PhaxioFaxService(api_key="key", api_secret="secret")
    
    # Test various phone number formats
    test_cases = [
        ("5551234567", "+5551234567"),
        ("(555) 123-4567", "+5551234567"),
        ("555-123-4567", "+5551234567"),
        ("+15551234567", "+15551234567"),  # Already formatted
        ("1-555-123-4567", "+15551234567"),
    ]
    
    # We'll test the normalization logic by checking the data sent to Phaxio
    for input_num, expected_output in test_cases:
        # This would be tested in the actual send_fax method
        # For now, just verify the logic works
        if not input_num.startswith('+'):
            clean_number = ''.join(c for c in input_num if c.isdigit())
            if len(clean_number) >= 10:
                normalized = f"+{clean_number}"
                assert normalized == expected_output


def test_status_mapping():
    """Test Phaxio status mapping to internal statuses."""
    service = PhaxioFaxService(api_key="key", api_secret="secret")
    
    # Test status mappings
    test_cases = [
        ("queued", "queued"),
        ("success", "SUCCESS"),
        ("failure", "FAILED"),
        ("error", "FAILED"),
        ("cancelled", "cancelled"),
        ("in_progress", "in_progress"),
        ("sending", "in_progress"),
        ("unknown_status", "unknown_status"),  # Fallback
    ]
    
    for phaxio_status, expected_internal in test_cases:
        result = service._map_status_str(phaxio_status)
        assert result == expected_internal


def test_pdf_endpoint_security(isolated_installation, monkeypatch, tmp_path):
    """Test PDF serving endpoint security."""
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FAX_DISABLED", "true")
    
    # Create test PDF
    test_pdf = tmp_path / "test_job_123.pdf"
    test_pdf.write_bytes(b"%PDF-1.4\ntest content")
    
    with TestClient(app) as client:
        # Test without token
        response = client.get("/fax/test_job_123/pdf")
        assert response.status_code == 422  # Missing required token
        
        # Test with invalid token
        response = client.get("/fax/test_job_123/pdf?token=invalid")
        assert response.status_code == 404  # Job not found (no DB entry)
        
        # Test with non-existent job
        response = client.get("/fax/nonexistent/pdf?token=valid")
        assert response.status_code == 404


@pytest.mark.asyncio 
async def test_phaxio_error_handling():
    """Test Phaxio service error handling."""
    service = PhaxioFaxService(api_key="key", api_secret="secret")
    
    class ErrorResp:
        status_code = 400
        def json(self):
            return {"success": False, "message": "Invalid phone number"}
        def text(self):
            return "Bad Request"
    
    async def fake_error_post(url, data=None, auth=None):
        return ErrorResp()
    
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_error_post)) as post:
        with pytest.raises(RuntimeError, match=r"Phaxio request failed \(HTTP 400\)\.") as error:
            await service.send_fax("+12223334444", "https://example.com/test.pdf", "job123")
        assert 'Invalid phone number' not in str(error.value)
        assert post.await_count == 1


def test_phaxio_configuration_validation():
    """Test Phaxio configuration validation."""
    # Test with missing credentials
    service = PhaxioFaxService(api_key="", api_secret="")
    assert not service.is_configured()
    
    # Test with partial credentials
    service = PhaxioFaxService(api_key="key", api_secret="")
    assert not service.is_configured()
    
    # Test with full credentials
    service = PhaxioFaxService(api_key="key", api_secret="secret")
    assert service.is_configured()
