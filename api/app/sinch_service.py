from typing import Optional, Dict, Any, Tuple
import httpx
import os
import re

from .config import settings
from .routing.numbers import canonical_number


class SinchFaxService:
    """
    Sinch Fax API v3 integration ("Phaxio by Sinch").

    Flow:
      1) POST /v3/projects/{projectId}/files (multipart/form-data) → returns file id
      2) POST /v3/projects/{projectId}/faxes { to, file } → returns fax object (id/status)
      3) GET /v3/projects/{projectId}/faxes/{id} → poll status (optional)
    """

    DEFAULT_BASES = (
        "https://fax.api.sinch.com/v3",
        "https://us.fax.api.sinch.com/v3",
        "https://eu.fax.api.sinch.com/v3",
    )

    def __init__(self, project_id: str, api_key: str, api_secret: str, base_url: Optional[str] = None):
        self.project_id = project_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url or self.DEFAULT_BASES[0]

    def is_configured(self) -> bool:
        return bool(self.project_id and self.api_key and self.api_secret)

    def _auth(self) -> Tuple[str, str]:
        return (self.api_key, self.api_secret)

    async def upload_file(self, file_path: str) -> int:
        if not os.path.exists(file_path):
            raise FileNotFoundError('Sinch attachment is unavailable.')
        url = f"{self.base_url}/projects/{self.project_id}/files"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(file_path, "rb") as handle:
                    files = {"file": (os.path.basename(file_path), handle, "application/pdf")}
                    resp = await client.post(url, files=files, auth=self._auth())
        except (httpx.HTTPError, httpx.InvalidURL, OSError):
            raise RuntimeError("Sinch file upload request failed.") from None
        if not 200 <= resp.status_code < 300:
            raise RuntimeError(f"Sinch file upload failed (HTTP {resp.status_code}).")
        try:
            data = resp.json()
            file_id = data.get("id") or data.get("data", {}).get("id")
            if file_id is None:
                raise ValueError
            return int(file_id)
        except (TypeError, ValueError, AttributeError):
            raise RuntimeError("Unexpected Sinch upload response.") from None

    async def send_fax(self, to_number: str, file_id: int) -> Dict[str, Any]:
        if not self.is_configured():
            raise ValueError('Sinch is not properly configured')
        # Sinch takes E.164, which is the accepted job's canonical form.
        to = canonical_number(to_number)
        url = f"{self.base_url}/projects/{self.project_id}/faxes"
        payload = {"to": to, "file": file_id}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, auth=self._auth())
        except (httpx.HTTPError, httpx.InvalidURL):
            raise RuntimeError('Sinch create request failed.') from None
        return self._fax_response(resp)

    @staticmethod
    def _fax_response(resp: httpx.Response) -> Dict[str, Any]:
        if not 200 <= resp.status_code < 300:
            raise RuntimeError(f'Sinch request failed (HTTP {resp.status_code}).') from None
        try:
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (TypeError, ValueError):
            raise RuntimeError('Unexpected Sinch response.') from None

    async def get_fax_status(self, fax_id: str) -> Dict[str, Any]:
        if not self.is_configured():
            raise ValueError('Sinch is not properly configured')
        url = f"{self.base_url}/projects/{self.project_id}/faxes/{fax_id}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, auth=self._auth())
        except (httpx.HTTPError, httpx.InvalidURL):
            raise RuntimeError('Sinch status request failed.') from None
        return self._fax_response(resp)

    # Received faxes (checked against developers.sinch.com on 2026-10-03):
    # GET /v3/projects/{projectId}/faxes/{id} returns the fax (direction
    # "INBOUND", from, to, numberOfPages, status, completedTime) and
    # GET /v3/projects/{projectId}/faxes/{id}/file returns its document. Both
    # use HTTP basic auth with the key ID and secret, and only ever go to a
    # documented Sinch Fax API host.
    @staticmethod
    def received_fax_id(value: Any) -> str:
        from .inbound.fetch import FetchError
        text = value.strip() if isinstance(value, str) else ''
        if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', text) is None:
            raise FetchError('This is not a Sinch fax ID.')
        return text

    def _received_url(self, fax_id: str, suffix: str = '') -> str:
        from .inbound import fetch
        if not self.is_configured():
            raise fetch.FetchError('Enter the Sinch project ID, key ID and secret so Faxbot can fetch received faxes.')
        if re.fullmatch(r'[A-Za-z0-9-]{1,64}', self.project_id or '') is None:
            raise fetch.FetchError('The Sinch project ID in settings is not valid.')
        url = f"{self.base_url.rstrip('/')}/projects/{self.project_id}/faxes/{self.received_fax_id(fax_id)}{suffix}"
        return fetch.require_host(url, fetch.SINCH_HOSTS, 'Sinch')

    async def get_received_fax(self, fax_id: Any) -> Optional[Dict[str, Any]]:
        """Sinch's record of one received fax in this project, or None when it has none."""
        from .inbound import fetch
        fax_id = self.received_fax_id(fax_id)
        status, data = await fetch.get_json(self._received_url(fax_id), auth=self._auth(),
                                            hosts=fetch.SINCH_HOSTS, provider='Sinch')
        if status == 404:
            return None
        if status in (401, 403):
            raise fetch.FetchError('Sinch refused the configured key.')
        if status != 200 or not isinstance(data, dict) or data.get('id') != fax_id:
            raise fetch.FetchError('Sinch did not answer the fax lookup.')
        if str(data.get('direction') or '').upper() != 'INBOUND':
            return None

        def text(name):
            value = data.get(name)
            return value.strip() if isinstance(value, str) and value.strip() else None
        pages = data.get('numberOfPages')
        return {'id': fax_id, 'status': (text('status') or '').lower(), 'from_number': text('from'),
                'to_number': text('to'), 'completed_at': text('completedTime'),
                'pages': pages if type(pages) is int and pages >= 0 else None, 'is_test': False}

    async def download_received_fax(self, fax_id: Any) -> bytes:
        """The document Sinch holds for one received fax; at most 50 MB."""
        from .inbound import fetch
        return await fetch.get_document(self._received_url(fax_id, '/file'), auth=self._auth(),
                                        hosts=fetch.SINCH_HOSTS, provider='Sinch')

    async def send_fax_file(self, to_number: str, file_path: str) -> Dict[str, Any]:
        """Create a fax by posting the file directly as multipart/form-data.

        This mirrors what the Sinch console does and avoids a separate /files upload.
        """
        if not self.is_configured():
            raise ValueError('Sinch is not properly configured')
        to = canonical_number(to_number)
        url = f"{self.base_url}/projects/{self.project_id}/faxes"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(file_path, 'rb') as fh:
                    files = {'file': (os.path.basename(file_path), fh, 'application/pdf')}
                    resp = await client.post(url, files=files, data={'to': to}, auth=self._auth())
        except (httpx.HTTPError, httpx.InvalidURL, OSError):
            raise RuntimeError('Sinch multipart create request failed.') from None
        return self._fax_response(resp)
