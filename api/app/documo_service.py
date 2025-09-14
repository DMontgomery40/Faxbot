import os
from typing import Optional, Dict, Any
import httpx

from .config import settings, reload_settings


class DocumoFaxService:
    """Documo (mFax) REST API integration.

    Uses direct file upload to create and send a fax via:
      POST {base}/v1/faxes (multipart/form-data)

    Required fields:
      - faxNumber: destination in E.164 or digits
      - attachments: PDF file

    Auth: API key created in Documo web app (Settings → API).
    Header typically 'Authorization: Bearer <API_KEY>'.
    """

    def __init__(self, api_key: str, base_url: Optional[str] = None, sandbox: bool = False):
        base = base_url or settings.documo_base_url
        if sandbox:
            base = "https://api.sandbox.documo.com"
        self.base_url = base.rstrip("/")
        self.api_key = api_key

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
        }

    async def send_fax_file(self, to_number: str, file_path: str) -> Dict[str, Any]:
        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)
        # Normalize number (Documo expects digits or E.164; keep simple)
        to = to_number
        if not to.startswith('+'):
            digits = ''.join(c for c in to if c.isdigit())
            to = digits if digits else to
        url = f"{self.base_url}/v1/faxes"
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {
                # field name 'attachments' per Documo docs; can accept multiple
                "attachments": (os.path.basename(file_path), open(file_path, "rb"), "application/pdf"),
            }
            data = {
                "faxNumber": to,
            }
            resp = await client.post(url, headers=self._headers(), data=data, files=files)
            if resp.status_code >= 400:
                raise RuntimeError(f"Documo send error {resp.status_code}: {resp.text}")
            return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text}


_documo_service: Optional[DocumoFaxService] = None


def get_documo_service() -> Optional[DocumoFaxService]:
    global _documo_service
    reload_settings()
    api_key = settings.documo_api_key
    if not api_key:
        _documo_service = None
        return None
    if _documo_service is None:
        _documo_service = DocumoFaxService(api_key=api_key, base_url=settings.documo_base_url, sandbox=settings.documo_use_sandbox)
    return _documo_service

