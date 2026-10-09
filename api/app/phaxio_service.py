from typing import Optional, Dict, Any
import re

import httpx

from .config import settings
from .routing.numbers import canonical_number
from .callback_locator import callback_url_with_locators


def _before_fax_data(error_type):
    from .routing.predata import phaxio
    return phaxio(error_type)


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

    async def send_fax(self, to_number: str, pdf_url: str, job_id: str, *, attempt_id: Optional[str] = None) -> Dict[str, Any]:
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

        # Phaxio takes E.164, which is the accepted job's canonical form.
        to_number = canonical_number(to_number)
        
        # Captured locators are also used to reconstruct the signed public URL.
        callback_url = None
        if self.status_callback_url:
            callback_url = callback_url_with_locators(self.status_callback_url, job_id, attempt_id)

        data = {
            "to": to_number,
            "content_url[]": pdf_url,
        }
        if callback_url:
            data["callback_url"] = callback_url
            
        auth = (self.api_key, self.api_secret)
        # The durable owner marks submission before this one create request.
        # A timeout/parse failure can mean acceptance; no generic retry is safe.
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{self.BASE_URL}/faxes", data=data, auth=auth)
        except (httpx.HTTPError, httpx.InvalidURL):
            raise RuntimeError('Phaxio create request failed.') from None
        payload = self._response(resp)
        data = payload.get('data')
        if payload.get('success') is not True or not isinstance(data, dict) or not data.get('id'):
            raise RuntimeError('Unexpected Phaxio create response.') from None
        if (not isinstance(data['id'], (str, int)) or isinstance(data['id'], bool)
                or not isinstance(data.get('status', 'queued'), str)):
            raise RuntimeError('Unexpected Phaxio create response.') from None
        try:
            return {'provider_sid': str(data['id']),
                    'status': self._map_status_str(data.get('status', 'queued'))}
        except (TypeError, ValueError, AttributeError):
            raise RuntimeError('Unexpected Phaxio create response.') from None

    @staticmethod
    def _response(resp: httpx.Response) -> Dict[str, Any]:
        if not 200 <= resp.status_code < 300:
            raise RuntimeError(f'Phaxio request failed (HTTP {resp.status_code}).') from None
        try:
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (TypeError, ValueError):
            raise RuntimeError('Unexpected Phaxio response.') from None

    async def get_fax_status(self, provider_sid: str) -> Dict[str, Any]:
        if not self.is_configured():
            raise ValueError("Phaxio is not properly configured")
        auth = (self.api_key, self.api_secret)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self.BASE_URL}/faxes/{provider_sid}", auth=auth)
            payload = self._response(resp).get('data')
            if not isinstance(payload, dict):
                raise ValueError
            return self._map_status(payload)
        except (httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError, AttributeError):
            raise RuntimeError('Phaxio status request failed.') from None

    async def cancel_fax(self, provider_sid: str) -> bool:
        if not self.is_configured():
            raise ValueError("Phaxio is not properly configured")
        auth = (self.api_key, self.api_secret)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(f"{self.BASE_URL}/faxes/{provider_sid}/cancel", auth=auth)
                return resp.status_code == 200
        except (httpx.HTTPError, httpx.InvalidURL):
            raise RuntimeError('Phaxio cancellation request failed.') from None

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
            "error_type": 'provider_error' if error_type else None,
            "error_message": 'Provider reported an error.' if error_message else None,
        }

    # Received faxes (checked against phaxio.com/docs on 2026-10-03):
    # GET /v2.1/faxes/{id} returns the Fax Object (direction "received",
    # from_number, to_number, num_pages, status, completed_at in RFC 3339), and
    # GET /v2.1/faxes/{id}/file returns its PDF. Both use HTTP basic auth with
    # the API key and secret, and only ever go to api.phaxio.com.
    RECEIVED_BASE_URL = "https://api.phaxio.com/v2.1"

    @staticmethod
    def received_fax_id(value: Any) -> str:
        from .inbound.fetch import FetchError
        text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ''
        if re.fullmatch(r'[0-9]{1,20}', text) is None:
            raise FetchError('Phaxio fax IDs are numbers.')
        return text

    async def get_received_fax(self, fax_id: Any) -> Optional[Dict[str, Any]]:
        """Phaxio's record of one received fax in this account, or None when it has none."""
        from .inbound import fetch
        fax_id = self.received_fax_id(fax_id)
        if not self.is_configured():
            raise fetch.FetchError('Enter the Phaxio API key and secret so Faxbot can fetch received faxes.')
        status, payload = await fetch.get_json(f"{self.RECEIVED_BASE_URL}/faxes/{fax_id}",
                                               auth=(self.api_key, self.api_secret),
                                               hosts=fetch.PHAXIO_HOSTS, provider='Phaxio')
        if status == 404:
            return None
        if status in (401, 403):
            raise fetch.FetchError('Phaxio refused the configured API key.')
        data = payload.get('data') if payload and payload.get('success') is True else None
        if status != 200 or not isinstance(data, dict) or str(data.get('id')) != fax_id:
            raise fetch.FetchError('Phaxio did not answer the fax lookup.')
        if data.get('direction') != 'received':
            return None

        def text(name):
            value = data.get(name)
            return value.strip() if isinstance(value, str) and value.strip() else None
        pages = data.get('num_pages')
        return {'id': fax_id, 'status': (text('status') or '').lower(), 'from_number': text('from_number'),
                'to_number': text('to_number'), 'completed_at': text('completed_at'),
                'pages': pages if type(pages) is int and pages >= 0 else None,
                'is_test': data.get('is_test') is True}

    async def download_received_fax(self, fax_id: Any) -> bytes:
        """The PDF Phaxio holds for one received fax; at most 50 MB."""
        from .inbound import fetch
        fax_id = self.received_fax_id(fax_id)
        if not self.is_configured():
            raise fetch.FetchError('Enter the Phaxio API key and secret so Faxbot can fetch received faxes.')
        return await fetch.get_document(f"{self.RECEIVED_BASE_URL}/faxes/{fax_id}/file",
                                        auth=(self.api_key, self.api_secret),
                                        hosts=fetch.PHAXIO_HOSTS, provider='Phaxio')

    def _map_status(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        status = payload.get("status")
        sid = payload.get('id')
        if (not isinstance(sid, (str, int)) or isinstance(sid, bool)
                or (isinstance(sid, str) and not sid.strip())
                or not isinstance(status, str) or not status.strip()):
            raise ValueError('Unexpected Phaxio status response.') from None
        return {
            "provider_sid": str(sid),
            "status": self._map_status_str(status),
            "provider_status": status,
            "pages": payload.get("num_pages"),
            "error_type": 'provider_error' if payload.get('error_type') else None,
            "error_message": 'Provider reported an error.' if payload.get('error_message') else None,
            # Whether a failed call ended before any fax data, by Phaxio's error type (routing/predata.py).
            "before_fax_data": _before_fax_data(payload.get('error_type')),
        }

    @staticmethod
    def _map_status_str(status: str) -> str:
        status = (status or "").lower()
        mapping = {
            "queued": "queued",
            "success": "SUCCESS",
            "failure": "FAILED",
            "error": "FAILED",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "in_progress": "in_progress",
            "sending": "in_progress",
        }
        return mapping.get(status, status or "queued")
