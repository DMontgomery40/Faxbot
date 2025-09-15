import asyncio
from typing import Optional, Dict, Any
import httpx
import logging

from .config import settings, reload_settings

logger = logging.getLogger(__name__)


class SignalWireFaxService:
    """
    SignalWire Compatibility (Twilio-like) Fax API via Basic Auth.
    Uses MediaUrl to fetch the PDF; we supply our tokenized /fax/{id}/pdf URL with job correlation.
    """

    def __init__(
        self,
        space_url: str,
        project_id: str,
        api_token: str,
        from_number: Optional[str] = None,
        status_callback_url: Optional[str] = None,
    ):
        self.space_url = space_url.strip().rstrip('/')
        self.project_id = project_id.strip()
        self.api_token = api_token.strip()
        self.from_number = (from_number or '').strip() or None
        self.status_callback_url = status_callback_url

    def is_configured(self) -> bool:
        return bool(self.space_url and self.project_id and self.api_token)

    def _compat_base(self) -> str:
        return f"https://{self.space_url}/api/laml/2010-04-01"

    async def send_fax(self, to_number: str, media_url: str, job_id: str) -> Dict[str, Any]:
        if not self.is_configured():
            raise ValueError("SignalWire is not properly configured")

        # Normalize number to E.164 if possible
        if not to_number.startswith('+'):
            digits = ''.join(c for c in to_number if c.isdigit())
            if len(digits) >= 10:
                to_number = f"+{digits}"

        auth = (self.project_id, self.api_token)  # HTTP Basic

        data = {
            'To': to_number,
            'MediaUrl': media_url,
        }
        if self.from_number:
            data['From'] = self.from_number
        if self.status_callback_url:
            data['StatusCallback'] = f"{self.status_callback_url}?job_id={job_id}"

        url = f"{self._compat_base()}/Accounts/{self.project_id}/Faxes.json"

        # Simple retry with backoff
        attempts = 3
        delay = 1.0
        last_err: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for _ in range(attempts):
                try:
                    resp = await client.post(url, data=data, auth=auth)
                    if resp.status_code >= 400:
                        try:
                            j = resp.json()
                            msg = j.get('message') or str(j)
                        except Exception:
                            msg = resp.text
                        raise Exception(f"SignalWire API error {resp.status_code}: {msg}")
                    j = resp.json()
                    # Expected Twilio-like shape `{ sid, status, ... }`
                    sid = str(j.get('sid') or j.get('faxSid') or '')
                    status = str(j.get('status') or j.get('faxStatus') or 'queued').lower()
                    return {
                        'provider_sid': sid,
                        'status': self._map_status_str(status),
                    }
                except Exception as e:
                    last_err = e
                    await asyncio.sleep(delay)
                    delay = min(8.0, delay * 2)
        assert last_err is not None
        raise last_err

    async def get_fax_status(self, provider_sid: str) -> Dict[str, Any]:
        if not self.is_configured():
            raise ValueError("SignalWire is not properly configured")
        auth = (self.project_id, self.api_token)
        url = f"{self._compat_base()}/Accounts/{self.project_id}/Faxes/{provider_sid}.json"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, auth=auth)
            if resp.status_code >= 400:
                try:
                    msg = resp.json().get('message')
                except Exception:
                    msg = resp.text
                raise Exception(f"SignalWire API error {resp.status_code}: {msg}")
            j = resp.json()
            status = str(j.get('status') or j.get('faxStatus') or 'queued').lower()
            return {
                'provider_sid': str(j.get('sid') or provider_sid),
                'status': self._map_status_str(status),
                'provider_status': status,
            }

    async def handle_status_callback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        sid = payload.get('FaxSid') or payload.get('sid') or payload.get('MessageSid')
        status = (payload.get('FaxStatus') or payload.get('status') or '').lower()
        return {
            'provider_sid': sid,
            'status': self._map_status_str(status),
            'provider_status': status,
        }

    @staticmethod
    def _map_status_str(status: str) -> str:
        s = (status or '').lower()
        mapping = {
            'queued': 'queued',
            'sending': 'in_progress',
            'processing': 'in_progress',
            'in-progress': 'in_progress',
            'delivered': 'SUCCESS',
            'success': 'SUCCESS',
            'failed': 'FAILED',
            'error': 'FAILED',
            'canceled': 'FAILED',
        }
        return mapping.get(s, s or 'queued')


_svc: Optional[SignalWireFaxService] = None


def get_signalwire_service() -> Optional[SignalWireFaxService]:
    global _svc
    reload_settings()
    if not (settings.signalwire_space_url and settings.signalwire_project_id and settings.signalwire_api_token):
        _svc = None
        return None
    if _svc is None:
        _svc = SignalWireFaxService(
            space_url=settings.signalwire_space_url,
            project_id=settings.signalwire_project_id,
            api_token=settings.signalwire_api_token,
            from_number=settings.signalwire_fax_from_e164 or None,
            status_callback_url=settings.signalwire_status_callback_url or None,
        )
    return _svc

