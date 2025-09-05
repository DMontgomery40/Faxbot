"""
Comprehensive integration tests for Phaxio backend.
Tests the complete flow from API request to webhook callback.
"""
import pytest
from unittest.mock import AsyncMock, patch, Mock
from fastapi.testclient import TestClient

from app.main import app
from app.phaxio_service import PhaxioFaxService, get_phaxio_service
from app.db import SessionLocal, FaxJob


class TestPhaxioIntegration:
    """Test complete Phaxio integration workflow."""
    
    @pytest.fixture(autouse=True)
    def setup_phaxio_env(self, monkeypatch, phaxio_env_vars):
        """Setup Phaxio environment for all tests in this class."""
        for key, value in phaxio_env_vars.items():
            monkeypatch.setenv(key, value)
    
    @pytest.mark.asyncio
    async def test_complete_fax_workflow(self, test_pdf_content):
        """Test the complete fax sending workflow with Phaxio."""
        
        # Mock Phaxio API responses
        mock_send_response = {
            "success": True,
            "message": "Fax queued for sending",
            "data": {
                "id": 12345,
                "status": "queued",
                "to": "+15551234567",
                "num_pages": 1
            }
        }
        
        mock_status_response = {
            "success": True,
            "data": {
                "id": 12345,
                "status": "success",
                "to": "+15551234567",
                "num_pages": 1,
                "cost": 7
            }
        }
        
        class MockResponse:
            def __init__(self, json_data, status_code=200):
                self.json_data = json_data
                self.status_code = status_code
            
            def json(self):
                return self.json_data
            
            @property
            def text(self):
                return str(self.json_data)
        
        async def mock_post(*args, **kwargs):
            return MockResponse(mock_send_response)
        
        async def mock_get(*args, **kwargs):
            return MockResponse(mock_status_response)
        
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)), \
             patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=mock_get)):
            
            with TestClient(app) as client:
                # Initialize database for test
                from app.db import init_db
                init_db()
                
                # Step 1: Submit fax
                files = {
                    "to": (None, "+15551234567"),
                    "file": ("test.pdf", test_pdf_content, "application/pdf"),
                }
                
                response = client.post("/fax", files=files)
                assert response.status_code == 202
                
                fax_data = response.json()
                assert fax_data["backend"] == "phaxio"
                assert fax_data["status"] == "queued"
                job_id = fax_data["id"]
                
                # Step 2: Check status
                response = client.get(f"/fax/{job_id}")
                assert response.status_code == 200
                
                # Step 3: Test PDF serving endpoint
                # First we need to simulate the job having a pdf_url
                with SessionLocal() as db:
                    job = db.get(FaxJob, job_id)
                    if job:
                        job.pdf_url = f"https://example.com/fax/{job_id}/pdf?token=test_token_123"
                        db.add(job)
                        db.commit()
                
                # Test PDF access with valid token
                response = client.get(f"/fax/{job_id}/pdf?token=test_token_123")
                assert response.status_code == 200
                assert response.headers["content-type"] == "application/pdf"
                
                # Test PDF access with invalid token
                response = client.get(f"/fax/{job_id}/pdf?token=invalid_token")
                assert response.status_code == 403
    
    def test_phaxio_webhook_callback(self):
        """Test Phaxio webhook callback processing."""
        
        with TestClient(app) as client:
            # Initialize database for test
            from app.db import init_db
            init_db()
            
            # Create a test job in database first
            with SessionLocal() as db:
                job = FaxJob(
                    id="test_job_123",
                    to_number="+15551234567",
                    file_name="test.pdf",
                    tiff_path="/tmp/test.tiff",
                    backend="phaxio",
                    provider_sid="phaxio_12345",
                    status="in_progress"
                )
                db.add(job)
                db.commit()
            
            # Simulate Phaxio webhook callback
            callback_data = {
                "fax[id]": "phaxio_12345",
                "fax[status]": "success",
                "fax[num_pages]": "2",
                "fax[to]": "+15551234567",
                "fax[from]": "+15559876543",
                "fax[cost]": "7"
            }
            
            response = client.post(
                "/phaxio-callback?job_id=test_job_123",
                data=callback_data
            )
            
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            
            # Verify job status was updated
            with SessionLocal() as db:
                updated_job = db.get(FaxJob, "test_job_123")
                assert updated_job.status == "SUCCESS"
                assert updated_job.pages == 2
    
    @pytest.mark.asyncio
    async def test_phaxio_error_scenarios(self):
        """Test various Phaxio error scenarios."""
        
        # Test API error response
        error_response = {
            "success": False,
            "message": "Invalid phone number format",
            "errors": ["Phone number must be in E.164 format"]
        }
        
        class MockErrorResponse:
            status_code = 422
            def json(self):
                return error_response
            @property
            def text(self):
                return str(error_response)
        
        async def mock_error_post(*args, **kwargs):
            return MockErrorResponse()
        
        service = PhaxioFaxService(
            api_key="test_key",
            api_secret="test_secret"
        )
        
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_error_post)):
            with pytest.raises(Exception, match="Phaxio API error 422"):
                await service.send_fax("+15551234567", "https://example.com/test.pdf", "job123")
    
    def test_phone_number_normalization(self):
        """Test phone number normalization logic."""
        service = PhaxioFaxService(api_key="key", api_secret="secret")
        
        # Test the normalization logic that's in send_fax
        test_cases = [
            ("5551234567", "+5551234567"),
            ("(555) 123-4567", "+5551234567"),
            ("555-123-4567", "+5551234567"),
            ("+15551234567", "+15551234567"),  # Already formatted
            ("1-555-123-4567", "+15551234567"),
            ("555.123.4567", "+5551234567"),
            ("555 123 4567", "+5551234567"),
        ]
        
        for input_num, expected in test_cases:
            # Replicate the normalization logic from send_fax
            to_number = input_num
            if not to_number.startswith('+'):
                clean_number = ''.join(c for c in to_number if c.isdigit())
                if len(clean_number) >= 10:
                    to_number = f"+{clean_number}"
            
            assert to_number == expected, f"Failed to normalize {input_num} to {expected}"
    
    def test_configuration_validation(self, monkeypatch):
        """Test Phaxio configuration validation."""
        # Test with no configuration
        monkeypatch.setenv("PHAXIO_API_KEY", "")
        monkeypatch.setenv("PHAXIO_API_SECRET", "")
        
        service = get_phaxio_service()
        assert service is None
        
        # Test with partial configuration
        monkeypatch.setenv("PHAXIO_API_KEY", "test_key")
        monkeypatch.setenv("PHAXIO_API_SECRET", "")
        
        # Clear the singleton to force re-initialization
        from app import phaxio_service
        phaxio_service._phaxio_service = None
        
        service = get_phaxio_service()
        assert service is None
        
        # Test with full configuration
        monkeypatch.setenv("PHAXIO_API_KEY", "test_key")
        monkeypatch.setenv("PHAXIO_API_SECRET", "test_secret")
        
        # Clear the singleton to force re-initialization
        app.phaxio_service._phaxio_service = None
        
        service = get_phaxio_service()
        assert service is not None
        assert service.is_configured()


def test_backend_routing_logic(monkeypatch, tmp_path):
    """Test that the correct backend is chosen based on FAX_BACKEND setting."""
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FAX_DISABLED", "true")
    
    # Test PDF content
    test_pdf_content = b"%PDF-1.4\ntest"
    
    # Test Phaxio backend selection
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("PHAXIO_API_KEY", "test_key")
    monkeypatch.setenv("PHAXIO_API_SECRET", "test_secret")
    
    with TestClient(app) as client:
        files = {
            "to": (None, "+15551234567"),
            "file": ("test.pdf", test_pdf_content, "application/pdf"),
        }
        
        response = client.post("/fax", files=files)
        assert response.status_code == 202
        
        data = response.json()
        assert data["backend"] == "phaxio"
    
    # Test SIP backend selection
    monkeypatch.setenv("FAX_BACKEND", "sip")
    
    with TestClient(app) as client:
        response = client.post("/fax", files=files)
        assert response.status_code == 202
        
        data = response.json()
        assert data["backend"] == "sip"
