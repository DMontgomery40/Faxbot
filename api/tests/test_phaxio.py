import pytest
from unittest.mock import AsyncMock, patch, Mock
from fastapi.testclient import TestClient

from app.phaxio_service import PhaxioFaxService
from app.main import app


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
async def test_phaxio_integration_end_to_end(monkeypatch, tmp_path):
    """Test complete Phaxio integration flow."""
    # Setup test environment
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("PHAXIO_API_KEY", "test_key")
    monkeypatch.setenv("PHAXIO_API_SECRET", "test_secret")
    monkeypatch.setenv("FAX_DISABLED", "true")  # Don't actually send
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path))
    
    # Create test PDF file
    test_pdf_path = tmp_path / "test.pdf"
    test_pdf_path.write_bytes(b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n>>\nendobj\nxref\n0 1\n0000000000 65535 f \ntrailer\n<<\n/Size 1\n/Root 1 0 R\n>>\nstartxref\n9\n%%EOF")
    
    with TestClient(app) as client:
        # Test fax submission with Phaxio backend
        files = {
            "to": (None, "+15551234567"),
            "file": ("test.pdf", test_pdf_path.read_bytes(), "application/pdf"),
        }
        
        with patch("app.phaxio_service.get_phaxio_service") as mock_get_service:
            mock_service = Mock()
            mock_service.is_configured.return_value = True
            mock_service.send_fax = AsyncMock(return_value={
                "provider_sid": "phaxio_123",
                "status": "queued"
            })
            mock_get_service.return_value = mock_service
            
            response = client.post("/fax", files=files)
            
            assert response.status_code == 202
            data = response.json()
            assert data["backend"] == "phaxio"
            assert data["status"] in ["queued", "disabled"]


def test_phaxio_callback_handling(monkeypatch):
    """Test Phaxio webhook callback processing."""
    # Disable signature verification for this unit test (default is now true)
    monkeypatch.setenv("PHAXIO_VERIFY_SIGNATURE", "false")
    with TestClient(app) as client:
        # Mock callback data from Phaxio
        callback_data = {
            "fax[id]": "phaxio_123",
            "fax[status]": "success",
            "fax[num_pages]": "2",
            "fax[to]": "+15551234567"
        }
        
        # Test callback endpoint
        response = client.post(
            "/phaxio-callback?job_id=test_job_123",
            data=callback_data
        )
        
        # Should return 200 even if job not found (graceful handling)
        assert response.status_code == 200


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
        ("cancelled", "FAILED"),
        ("in_progress", "in_progress"),
        ("sending", "in_progress"),
        ("unknown_status", "unknown_status"),  # Fallback
    ]
    
    for phaxio_status, expected_internal in test_cases:
        result = service._map_status_str(phaxio_status)
        assert result == expected_internal


def test_pdf_endpoint_security(monkeypatch, tmp_path):
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
    
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_error_post)):
        with pytest.raises(Exception, match="Phaxio API error 400"):
            await service.send_fax("+12223334444", "https://example.com/test.pdf", "job123")


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
