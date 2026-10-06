from typing import Optional, Dict, Any
import httpx

from .config import settings
from .routing.numbers import canonical_number
from .callback_locator import callback_url_with_locators


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

    async def send_fax(self, to_number: str, media_url: str, job_id: str, *, attempt_id: Optional[str] = None) -> Dict[str, Any]:
        if not self.is_configured():
            raise ValueError("SignalWire is not properly configured")

        # SignalWire takes E.164, which is the accepted job's canonical form.
        to_number = canonical_number(to_number)

        auth = (self.project_id, self.api_token)  # HTTP Basic

        data = {
            'To': to_number,
            'MediaUrl': media_url,
        }
        if self.from_number:
            data['From'] = self.from_number
        if self.status_callback_url:
            data['StatusCallback'] = callback_url_with_locators(self.status_callback_url, job_id, attempt_id)

        url = f"{self._compat_base()}/Accounts/{self.project_id}/Faxes.json"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, data=data, auth=auth)
        except (httpx.HTTPError, httpx.InvalidURL):
            raise RuntimeError('SignalWire create request failed.') from None
        j = self._response(resp)
        sid = j.get('sid') or j.get('faxSid') or ''
        status = j.get('status') or j.get('faxStatus') or 'queued'
        if not isinstance(sid, str) or not isinstance(status, str):
            raise RuntimeError('Unexpected SignalWire create response.') from None
        return {'provider_sid': sid, 'status': self._map_status_str(status)}

    @staticmethod
    def _response(resp: httpx.Response) -> Dict[str, Any]:
        if not 200 <= resp.status_code < 300:
            raise RuntimeError(f'SignalWire request failed (HTTP {resp.status_code}).') from None
        try:
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (TypeError, ValueError):
            raise RuntimeError('Unexpected SignalWire response.') from None

    async def get_fax_status(self, provider_sid: str) -> Dict[str, Any]:
        if not self.is_configured():
            raise ValueError("SignalWire is not properly configured")
        auth = (self.project_id, self.api_token)
        url = f"{self._compat_base()}/Accounts/{self.project_id}/Faxes/{provider_sid}.json"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, auth=auth)
        except (httpx.HTTPError, httpx.InvalidURL):
            raise RuntimeError('SignalWire status request failed.') from None
        j = self._response(resp)
        sid = j.get('sid') or provider_sid
        status = j.get('status', j.get('faxStatus'))
        if not isinstance(sid, str) or not isinstance(status, str) or not status.strip():
            raise RuntimeError('Unexpected SignalWire status response.') from None
        status = status.lower()
        return {'provider_sid': sid,
                'status': self._map_status_str(status), 'provider_status': status}

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
            'canceled': 'cancelled',
            'cancelled': 'cancelled',
        }
        return mapping.get(s, s or 'queued')
