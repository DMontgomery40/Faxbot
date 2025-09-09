import asyncio
from typing import Optional, Dict, Any
import httpx
import logging

from .config import settings, reload_settings

logger = logging.getLogger(__name__)


class PhaxioFaxService:
    """
    Phaxio Fax API integration for sending faxes via cloud service.
    This replaces the deprecated Twilio Fax product.
    """

    BASE_URL = "https://api.phaxio.com/v2"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        status_callback_url: Optional[str] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.status_callback_url = status_callback_url

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    async def send_fax(self, to_number: str, pdf_url: str, job_id: str) -> Dict[str, Any]:
        """
        Send a fax using Phaxio. Uses content_url to avoid uploading files.
        
        Args:
            to_number: Destination fax number (E.164 format preferred)
            pdf_url: Public URL where Phaxio can fetch the PDF
            job_id: Internal job ID for tracking
            
        Returns:
            Dict with provider_sid and status
        """
        if not self.is_configured():
            raise ValueError("Phaxio is not properly configured")

        # Normalize phone number to E.164 format
        if not to_number.startswith('+'):
            # Remove all non-digit characters
            clean_number = ''.join(c for c in to_number if c.isdigit())
            # Add + if it looks like it has a country code (10+ digits)
            if len(clean_number) >= 10:
                to_number = f"+{clean_number}"
        
        # Compose callback URL with our job_id to correlate
        callback_url = None
        if self.status_callback_url:
            callback_url = f"{self.status_callback_url}?job_id={job_id}"

        data = {
            "to": to_number,
            "content_url[]": pdf_url,
        }
        if callback_url:
            data["callback_url"] = callback_url
            
        logger.info(f"Sending fax via Phaxio: job_id={job_id}, to={to_number}, pdf_url=redacted")

        auth = (self.api_key, self.api_secret)

        # Basic retry with exponential backoff
        attempts = 3
        delay = 1.0
        from typing import Optional
        last_err: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for _ in range(attempts):
                try:
                    resp = await client.post(f"{self.BASE_URL}/faxes", data=data, auth=auth)
                    if resp.status_code >= 400:
                        try:
                            j = resp.json()
                            msg = j.get("message") or str(j)
                        except Exception:
                            msg = resp.text
                        error_msg = f"Phaxio API error {resp.status_code}: {msg}"
                        logger.error(error_msg)
                        raise Exception(error_msg)
                    
                    payload = resp.json()
                    if not payload.get("success", False):
                        error_msg = f"Phaxio API returned success=false: {payload.get('message', 'Unknown error')}"
                        logger.error(error_msg)
                        raise Exception(error_msg)
                        
                    data = payload.get("data", {})
                    fax_id = data.get("id")
                    if not fax_id:
                        raise Exception("Phaxio API did not return a fax ID")
                        
                    result = {
                        "provider_sid": str(fax_id),
                        "status": self._map_status_str(data.get("status", "queued")),
                    }
                    logger.info(f"Phaxio fax sent successfully: {result}")
                    return result
                except Exception as e:
                    last_err = e
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 8.0)
        # Exhausted retries
        assert last_err is not None
        raise last_err

    async def get_fax_status(self, provider_sid: str) -> Dict[str, Any]:
        if not self.is_configured():
            raise ValueError("Phaxio is not properly configured")
        auth = (self.api_key, self.api_secret)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self.BASE_URL}/faxes/{provider_sid}", auth=auth)
            resp.raise_for_status()
            payload = resp.json().get("data", {})
            return self._map_status(payload)

    async def cancel_fax(self, provider_sid: str) -> bool:
        if not self.is_configured():
            raise ValueError("Phaxio is not properly configured")
        auth = (self.api_key, self.api_secret)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self.BASE_URL}/faxes/{provider_sid}/cancel", auth=auth)
            return resp.status_code == 200

    async def handle_status_callback(self, callback_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process Phaxio webhook payload (form-encoded keys like fax[id]).
        """
        # Flatten keys like fax[id]
        def g(key: str) -> Optional[str]:
            return callback_data.get(key)

        sid = g("fax[id]") or g("id")
        status = g("fax[status]") or g("status") or ""
        pages = g("fax[num_pages]") or g("num_pages")
        error_type = g("fax[error_type]") or g("error_type")
        error_message = g("fax[error_message]") or g("error_message")

        internal = self._map_status_str(status)
        return {
            "provider_sid": sid,
            "status": internal,
            "provider_status": status,
            "pages": int(pages) if pages else None,
            "error_type": error_type,
            "error_message": error_message,
        }

    def _map_status(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        status = payload.get("status") or ""
        return {
            "provider_sid": str(payload.get("id")),
            "status": self._map_status_str(status),
            "provider_status": status,
            "pages": payload.get("num_pages"),
            "error_type": payload.get("error_type"),
            "error_message": payload.get("error_message"),
        }

    @staticmethod
    def _map_status_str(status: str) -> str:
        status = (status or "").lower()
        mapping = {
            "queued": "queued",
            "success": "SUCCESS",
            "failure": "FAILED",
            "error": "FAILED",
            "cancelled": "FAILED",
            "in_progress": "in_progress",
            "sending": "in_progress",
        }
        return mapping.get(status, status or "queued")


_phaxio_service: Optional[PhaxioFaxService] = None


def get_phaxio_service() -> Optional[PhaxioFaxService]:
    """Get singleton Phaxio service instance."""
    global _phaxio_service
    # Ensure settings reflect current environment (tests monkeypatch env at runtime)
    reload_settings()
    # If not configured, ensure we don't keep a stale instance
    if not (settings.phaxio_api_key and settings.phaxio_api_secret):
        _phaxio_service = None
        return None
    if _phaxio_service is None:
        _phaxio_service = PhaxioFaxService(
            api_key=settings.phaxio_api_key,
            api_secret=settings.phaxio_api_secret,
            status_callback_url=settings.phaxio_status_callback_url or None,
        )
    return _phaxio_service
