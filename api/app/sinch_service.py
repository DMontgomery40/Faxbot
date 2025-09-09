from typing import Optional, Dict, Any, Tuple
import httpx
import logging
import os

from .config import settings, reload_settings

logger = logging.getLogger(__name__)


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
        self.base_url = base_url or os.getenv("SINCH_BASE_URL") or self.DEFAULT_BASES[0]

    def is_configured(self) -> bool:
        return bool(self.project_id and self.api_key and self.api_secret)

    def _auth(self) -> Tuple[str, str]:
        return (self.api_key, self.api_secret)

    async def upload_file(self, file_path: str) -> int:
        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)
        urls = [self.base_url] + [b for b in self.DEFAULT_BASES if b != self.base_url]
        from typing import Optional, Tuple as _Tuple
        last: Optional[_Tuple[str, object, str]] = None
        for base in urls:
            url = f"{base}/projects/{self.project_id}/files"
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    files = {"file": (os.path.basename(file_path), open(file_path, "rb"), "application/pdf")}
                    resp = await client.post(url, files=files, auth=self._auth())
                if resp.status_code < 400:
                    data = resp.json()
                    file_id = data.get("id") or data.get("data", {}).get("id")
                    if file_id is None:
                        raise RuntimeError(f"Unexpected Sinch upload response: {data}")
                    return int(file_id)
                last = (url, resp.status_code, resp.text)
            except Exception as e:  # pragma: no cover
                last = (url, "exception", str(e))
                continue
        raise RuntimeError(f"Sinch file upload failed: {last}")

    async def send_fax(self, to_number: str, file_id: int) -> Dict[str, Any]:
        # Normalize number to E.164 if possible
        to = to_number
        if not to.startswith('+'):
            digits = ''.join(c for c in to if c.isdigit())
            if len(digits) >= 10:
                to = f"+{digits}"
        url = f"{self.base_url}/projects/{self.project_id}/faxes"
        payload = {"to": to, "file": file_id}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, auth=self._auth())
            if resp.status_code >= 400:
                raise RuntimeError(f"Sinch create fax error {resp.status_code}: {resp.text}")
            return resp.json()

    async def get_fax_status(self, fax_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/projects/{self.project_id}/faxes/{fax_id}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, auth=self._auth())
            resp.raise_for_status()
            return resp.json()

    async def send_fax_file(self, to_number: str, file_path: str) -> Dict[str, Any]:
        """Create a fax by posting the file directly as multipart/form-data.

        This mirrors what the Sinch console does and avoids a separate /files upload.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)
        to = to_number
        if not to.startswith('+'):
            digits = ''.join(c for c in to if c.isdigit())
            if len(digits) >= 10:
                to = f"+{digits}"
        url = f"{self.base_url}/projects/{self.project_id}/faxes"
        async with httpx.AsyncClient(timeout=60.0) as client:
            # httpx expects a mapping of field name → (filename, fileobj, content_type)
            # For the additional text field, pass as data not files
            with open(file_path, "rb") as fh:
                files = {"file": (os.path.basename(file_path), fh, "application/pdf")}
                data = {"to": to}
                resp = await client.post(url, files=files, data=data, auth=self._auth())
            if resp.status_code >= 400:
                raise RuntimeError(f"Sinch create fax error {resp.status_code}: {resp.text}")
            return resp.json()


_sinch_service: Optional[SinchFaxService] = None


def get_sinch_service() -> Optional[SinchFaxService]:
    global _sinch_service
    reload_settings()
    if not (settings.sinch_project_id and settings.sinch_api_key and settings.sinch_api_secret):
        _sinch_service = None
        return None
    if _sinch_service is None:
        _sinch_service = SinchFaxService(
            project_id=settings.sinch_project_id,
            api_key=settings.sinch_api_key,
            api_secret=settings.sinch_api_secret,
            base_url=os.getenv("SINCH_BASE_URL") or None,
        )
    return _sinch_service
