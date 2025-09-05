"""
Pytest configuration and shared fixtures for Faxbot tests.
"""
import pytest
import tempfile
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
test_dir = Path(__file__).parent
api_dir = test_dir.parent
sys.path.insert(0, str(api_dir))


@pytest.fixture
def temp_fax_dir():
    """Create temporary directory for fax data during tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def test_pdf_content():
    """Minimal valid PDF content for testing."""
    return b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n>>\nendobj\nxref\n0 1\n0000000000 65535 f \ntrailer\n<<\n/Size 1\n/Root 1 0 R\n>>\nstartxref\n9\n%%EOF"


@pytest.fixture
def phaxio_env_vars():
    """Standard Phaxio environment variables for testing."""
    return {
        "FAX_BACKEND": "phaxio",
        "PHAXIO_API_KEY": "test_key_12345",
        "PHAXIO_API_SECRET": "test_secret_67890",
        "PHAXIO_STATUS_CALLBACK_URL": "https://example.com/phaxio-callback",
        "PUBLIC_API_URL": "https://example.com",
        "FAX_DISABLED": "true",  # Default to disabled for safety
        "DATABASE_URL": "sqlite:///./test_faxbot.db"
    }


@pytest.fixture
def sip_env_vars():
    """Standard SIP environment variables for testing."""
    return {
        "FAX_BACKEND": "sip",
        "ASTERISK_AMI_HOST": "localhost",
        "ASTERISK_AMI_PORT": "5038",
        "ASTERISK_AMI_USERNAME": "test",
        "ASTERISK_AMI_PASSWORD": "test",
        "FAX_DISABLED": "true",  # Default to disabled for safety
        "DATABASE_URL": "sqlite:///./test_faxbot.db"
    }


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch, temp_fax_dir):
    """Automatically set up safe test environment for all tests."""
    monkeypatch.setenv("FAX_DATA_DIR", temp_fax_dir)
    monkeypatch.setenv("FAX_DISABLED", "true")  # Safety first
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./test_faxbot.db")


@pytest.fixture(autouse=True)
def mock_slow_conversions():
    """Mock all slow file conversion operations for fast tests."""
    from unittest.mock import patch
    
    with patch("app.conversion.txt_to_pdf") as mock_txt_to_pdf, \
         patch("app.conversion.pdf_to_tiff", return_value=(1, "mock.tiff")) as mock_pdf_to_tiff:
        yield {
            "txt_to_pdf": mock_txt_to_pdf,
            "pdf_to_tiff": mock_pdf_to_tiff
        }
