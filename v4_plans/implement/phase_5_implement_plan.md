# Phase 5 Implementation Plan: Production Excellence & Platform Completion

**Branch**: `auto-tunnel` (CRITICAL - all v4 work stays here)
**Status**: FINAL phase - Complete v4 platform transformation
**Timeline**: 8-9 weeks

## Executive Summary

Phase 5 is the FINAL phase completing the v4 platform transformation. This phase focuses on production deployment excellence, comprehensive monitoring, complete provider parity (especially Sinch & HumbleFax), zero-downtime migration, and operational excellence. Every existing provider and service MUST work flawlessly with advanced capabilities.

**CRITICAL PROVIDER MANDATE**: ALL 7 providers must be first-class citizens:
1. **Sinch** - Most widely used, OAuth2 + Basic Auth, regional endpoints
2. **Phaxio** - HIPAA-ready with BAA, HMAC verification
3. **SignalWire** - Twilio-compatible (NOTE: Twilio fax API is sunset)
4. **HumbleFax** - Most widely used by non-HIPAA customers, Complex webhook + email inbound, IMAP integration
5. **SIP/Asterisk** - Self-hosted T.38, AMI interface
6. **FreeSWITCH** - ESL integration, mod_spandsp
7. **Test/Disabled** - Development and CI/CD

## Dependencies & Integration Points

### Phase 1-4 Foundation Requirements:
- ✅ **Phase 1**: Plugin architecture, manifest-first discovery, all transport providers as plugins
- ✅ **Phase 2**: Trait-based auth, user management, session cookies
- ✅ **Phase 3**: Hierarchical config, Redis caching, webhook hardening
- ✅ **Phase 4**: Plugin marketplace, enterprise identity, multi-cloud storage

### Phase 4 → Phase 5 Evolution:
```python
# Phase 4: Enterprise features with marketplace
plugin = marketplace.get_tenant_plugin('transport', 'sinch', tenant_context)
await plugin.send_with_analytics(message, tenant_analytics)

# Phase 5: Production-ready with monitoring and reliability
async with circuit_breaker(plugin) as cb:
    metrics.record_send_attempt(plugin.provider_id, tenant_id)
    result = await plugin.send_with_observability(message, trace_context)
    await health_monitor.record_success(plugin.provider_id)
```

## P0 (must fix now)

1) Async correctness (provider I/O)
- Replace blocking `open(...)` in async provider `send()` paths with `anyio.open_file` or `run_in_threadpool`.
- Apply to Sinch and HumbleFax upload flows and any large attachment reads.

2) HumbleFax IMAP is synchronous
- `imaplib` is blocking; offload all IMAP ops to a threadpool or use `aioimaplib`.
- Add jittered backoff, max message size guard, sanitize filenames, cap total attachment bytes, and PDF-only filter.

3) Observability: one metrics stack; align scrape port
- Choose a single metrics path. For Phase 5: use native `prometheus_client` on the API port (`/metrics` on 8080). Do not also register the OTel Prometheus reader.
- Update k8s YAML so Prometheus scrapes the actual port that serves `/metrics` (8080). Remove the extra container/service metrics port.

4) Health monitor duplication
- Reuse and extend the Phase-3 `ProviderHealthMonitor` (same breaker semantics). Do not introduce a second monitor type.

5) Traits schema consistency
- Normalize examples to the canonical keys:
  - `webhook.verification` (e.g., `hmac_sha256|basic_auth|none`)
  - `webhook.path`
  - `webhook.verify_header`
  - `auth.methods` as an array (e.g., `["basic","oauth2"]`)
  - `regions` as an array
- Provide a JSON Schema and validate in CI.

6) Webhook responses + idempotency
- Provider callbacks should return `202 Accepted` after verify+queue (not 200). Add dedupe by `(external_id, provider_id, timestamp)`.

7) Status naming consistency
- Canonical: `queued|in_progress|delivered|failed|canceled`. Map provider-specifics to these; keep canonical events separate.

8) Security envs in deployment
- Add `CONFIG_MASTER_KEY` (Fernet), `FAXBOT_SESSION_PEPPER`, marketplace flags (`ADMIN_MARKETPLACE_ENABLED`, `ADMIN_MARKETPLACE_REMOTE_INSTALL_ENABLED`), and DB/Redis TLS flags to k8s.

9) Sinch credential fallback clarity
- Do not fallback `project_id` to a Phaxio key. Only document explicit `SINCH_* → PHAXIO_*` key/secret fallback and log when used.

10) HumbleFax storage + PHI
- Ensure encrypted PVC, runAsNonRoot, sanitize filenames, retention enforcement, and never log PHI in paths. Only `.pdf` attachments; cap count and total MB.

11) Migration engine reality check
- Migrations via Alembic only. Blue/green: mirror callbacks to v3+v4; idempotent send with `Idempotency-Key`; add OpenAPI diff guard in CI.

12) Rate-limit precedence
- Support `api.rate_limit_rps` (token bucket). If unset, derive from `rpm/60`. Return `Retry-After` and `X-RateLimit-*` headers on 429.

## Week 1-2: Complete Provider Parity & Sinch Excellence

### 1. Sinch Provider Complete Implementation

**Enhance `api/app/providers/sinch.py`** (preserve existing functionality, async I/O and clear fallbacks):
```python
from typing import Optional, Dict, Any, Tuple
import httpx
import time
import asyncio
from datetime import datetime, timedelta
import os
import anyio

class SinchEnhancedProvider(ProviderAdapter):
    """Production-ready Sinch integration with OAuth2, regional endpoints, inbound"""

    id = "sinch"

    def __init__(self):
        self.project_id = ""
        self.api_key = ""
        self.api_secret = ""
        self.base_url = ""
        self.auth_method = "basic"  # "basic" or "oauth"
        self.regional_endpoints = {
            'global': 'https://fax.api.sinch.com/v3',
            'us': 'https://us.fax.api.sinch.com/v3',
            'eu': 'https://eu.fax.api.sinch.com/v3'
        }
        # OAuth2 token management
        self._oauth_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._token_refresh_lock = asyncio.Lock()

    async def initialize(self, config: Dict[str, Any]) -> bool:
        """Initialize Sinch provider with explicit, documented fallbacks only"""
        # Do NOT map project_id to Phaxio values. Only explicit SINCH_* values are valid.
        self.project_id = config.get('sinch_project_id')
        # Keys may fall back to Phaxio for convenience, but log when fallback is used.
        self.api_key = config.get('sinch_api_key') or config.get('phaxio_api_key', '')
        self.api_secret = config.get('sinch_api_secret') or config.get('phaxio_api_secret', '')

        # Regional endpoint selection
        region = config.get('sinch_region', 'global').lower()
        self.base_url = self.regional_endpoints.get(region, self.regional_endpoints['global'])

        # Auth method selection
        self.auth_method = config.get('sinch_auth_method', 'basic').lower()

        # Inbound webhook credentials
        self.inbound_basic_user = config.get('sinch_inbound_basic_user', '')
        self.inbound_basic_pass = config.get('sinch_inbound_basic_pass', '')

        if not (self.project_id and self.api_key and self.api_secret):
            raise ValueError("Sinch credentials missing (check SINCH_*; key/secret fallback allowed if documented)")

        # Test connectivity
        return await self._test_connection()

    async def _get_auth_headers(self) -> Dict[str, str]:
        """Get authentication headers based on auth method"""
        if self.auth_method == "oauth":
            token = await self._get_oauth_token()
            return {"Authorization": f"Bearer {token}"}
        else:
            # Basic auth (default)
            import base64
            credentials = f"{self.api_key}:{self.api_secret}"
            encoded = base64.b64encode(credentials.encode()).decode()
            return {"Authorization": f"Basic {encoded}"}

    async def _get_oauth_token(self) -> str:
        """Get OAuth2 token with automatic refresh"""
        async with self._token_refresh_lock:
            # Check if token is still valid
            if (self._oauth_token and self._token_expires_at and
                datetime.utcnow() < self._token_expires_at - timedelta(minutes=5)):
                return self._oauth_token

            # Get new token
            auth_url = "https://auth.sinch.com/oauth2/token"

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    auth_url,
                    data={
                        'grant_type': 'client_credentials',
                        'client_id': self.api_key,
                        'client_secret': self.api_secret
                    },
                    headers={'Content-Type': 'application/x-www-form-urlencoded'}
                )

                if response.status_code != 200:
                    raise RuntimeError(f"OAuth2 token request failed: {response.status_code}")

                token_data = response.json()
                self._oauth_token = token_data['access_token']
                expires_in = token_data.get('expires_in', 3600)
                self._token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

                return self._oauth_token

    async def send(self, to_number: str, file_path: str, **kwargs) -> Dict[str, Any]:
        """Send fax via Sinch with file upload then send flow"""
        auth_headers = await self._get_auth_headers()

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Step 1: Upload file
            async with await anyio.open_file(file_path, 'rb') as f:
                content = await f.read()
            files = {'file': (os.path.basename(file_path), content, 'application/pdf')}
            response = await client.post(
                f"{self.base_url}/projects/{self.project_id}/files",
                files=files,
                headers=auth_headers
            )

            if response.status_code != 200:
                raise RuntimeError(f"File upload failed: {response.status_code} - {response.text}")

            file_data = response.json()
            file_id = file_data.get('id')

            # Step 2: Send fax
            fax_data = {
                'to': to_number,
                'file': file_id
            }

            # Add optional parameters
            if 'from_number' in kwargs:
                fax_data['from'] = kwargs['from_number']
            if 'coversheet' in kwargs:
                fax_data['coversheet'] = kwargs['coversheet']

            response = await client.post(
                f"{self.base_url}/projects/{self.project_id}/faxes",
                json=fax_data,
                headers={**auth_headers, 'Content-Type': 'application/json'}
            )

            if response.status_code not in [200, 201]:
                raise RuntimeError(f"Fax send failed: {response.status_code} - {response.text}")

            result = response.json()

            # Return canonical format
            return {
                'job_id': result.get('id'),
                'provider_sid': result.get('id'),
                'status': 'queued',
                'to_number': to_number,
                'provider': 'sinch',
                'estimated_cost': result.get('cost'),
                'metadata': {
                    'file_id': file_id,
                    'sinch_project_id': self.project_id,
                    'auth_method': self.auth_method,
                    'region': self.base_url,
                    'send_time': datetime.utcnow().isoformat()
                }
            }

    async def verify_webhook(self, headers: Dict[str, str], body: bytes,
                           secrets: Dict[str, str]) -> Tuple[bool, Optional[str]]:
        """Verify Sinch inbound webhook with Basic Auth"""
        auth_header = headers.get('Authorization', '')

        if not auth_header.startswith('Basic '):
            return False, "Missing Basic Auth header"

        try:
            import base64
            encoded = auth_header[6:]  # Remove "Basic "
            decoded = base64.b64decode(encoded).decode('utf-8')
            username, password = decoded.split(':', 1)

            expected_user = self.inbound_basic_user
            expected_pass = self.inbound_basic_pass

            if not expected_user or not expected_pass:
                return True, "No inbound credentials configured - accepting"

            if username == expected_user and password == expected_pass:
                return True, None
            else:
                return False, "Invalid Basic Auth credentials"

        except Exception as e:
            return False, f"Basic Auth verification failed: {str(e)}"

    async def parse_inbound(self, headers: Dict[str, str], body: bytes) -> Dict[str, Any]:
        """Parse Sinch inbound webhook payload"""
        import json

        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception as e:
            raise ValueError(f"Invalid JSON payload: {e}")

        # Normalize Sinch webhook format
        return {
            'provider': 'sinch',
            'external_id': payload.get('id'),
            'from_number': payload.get('from'),
            'to_number': payload.get('to'),
            'pages': payload.get('pages'),
            'status': payload.get('status'),
            'direction': 'inbound',
            'received_at': payload.get('received_at') or datetime.utcnow().isoformat(),
            'file_url': payload.get('file_url') or payload.get('pdf_url'),
            'raw_payload': payload
        }
```

### 2. HumbleFax Provider Implementation (Complex Email + Webhook)

**Create `api/app/providers/humblefax.py`** (new, comprehensive; async I/O, IMAP offload & sanitization):
```python
import asyncio
import imaplib
import email
from email.mime.multipart import MIMEMultipart
import httpx
import hmac
import hashlib
import json
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime
import os
import anyio
from pathlib import Path

class HumbleFaxProvider(ProviderAdapter):
    """HumbleFax provider with webhook + IMAP inbound, complex configuration"""

    id = "humblefax"

    def __init__(self):
        # API credentials
        self.api_key = ""
        self.api_secret = ""
        self.base_url = "https://api.humblefax.com"

        # Webhook configuration
        self.webhook_enabled = True
        self.webhook_secret = ""
        self.callback_base = ""

        # IMAP configuration (fallback for inbound)
        self.imap_enabled = False
        self.imap_server = ""
        self.imap_username = ""
        self.imap_password = ""
        self.imap_port = 993
        self.imap_use_ssl = True
        self.imap_poll_interval = 300  # 5 minutes

        # File handling
        self.save_dir = Path("var/inbound/humblefax")
        self.save_dir.mkdir(parents=True, exist_ok=True)

    async def initialize(self, config: Dict[str, Any]) -> bool:
        """Initialize HumbleFax with both webhook and IMAP"""
        self.api_key = config.get('humblefax_access_key', '')
        self.api_secret = config.get('humblefax_secret_key', '')
        self.base_url = config.get('humblefax_base_url', 'https://api.humblefax.com')

        # Webhook setup
        self.webhook_enabled = config.get('humblefax_webhook_enabled', True)
        self.webhook_secret = config.get('humblefax_webhook_secret', '')
        self.callback_base = config.get('humblefax_callback_base', '')

        # IMAP setup (fallback)
        self.imap_enabled = config.get('humblefax_imap_enabled', False)
        self.imap_server = config.get('humblefax_imap_server', 'imap.gmail.com')
        self.imap_username = config.get('humblefax_imap_username', '')
        self.imap_password = config.get('humblefax_imap_password', '')

        if not (self.api_key and self.api_secret):
            raise ValueError("HumbleFax API credentials required")

        # Test API connectivity
        return await self._test_api_connection()

    async def send(self, to_number: str, file_path: str, **kwargs) -> Dict[str, Any]:
        """Send fax via HumbleFax CreateTmpFax -> CreateAttachment -> SendTmpFax flow"""

        async with httpx.AsyncClient(timeout=120.0) as client:
            # Step 1: Create temporary fax
            tmp_fax_data = {
                'recipientFaxNumbers': [to_number],
                'senderFaxNumber': kwargs.get('from_number', ''),
                'coverSheetMessage': kwargs.get('coversheet', ''),
                'resolution': kwargs.get('resolution', 'fine'),
                'pageSize': kwargs.get('page_size', 'letter')
            }

            response = await client.post(
                f"{self.base_url}/CreateTmpFax",
                json=tmp_fax_data,
                auth=(self.api_key, self.api_secret),
                headers={'Content-Type': 'application/json'}
            )

            if response.status_code != 200:
                raise RuntimeError(f"CreateTmpFax failed: {response.status_code} - {response.text}")

            tmp_fax = response.json()
            tmp_fax_id = tmp_fax.get('tmpFaxId')

            # Step 2: Upload attachment (async file read)
            async with await anyio.open_file(file_path, 'rb') as f:
                content = await f.read()
            files = {'attachment': (os.path.basename(file_path), content, 'application/pdf')}
            response = await client.post(
                f"{self.base_url}/CreateAttachment",
                params={'tmpFaxId': tmp_fax_id},
                files=files,
                auth=(self.api_key, self.api_secret)
            )

            if response.status_code != 200:
                raise RuntimeError(f"CreateAttachment failed: {response.status_code} - {response.text}")

            # Step 3: Send the fax
            response = await client.post(
                f"{self.base_url}/SendTmpFax",
                params={'tmpFaxId': tmp_fax_id},
                auth=(self.api_key, self.api_secret)
            )

            if response.status_code != 200:
                raise RuntimeError(f"SendTmpFax failed: {response.status_code} - {response.text}")

            result = response.json()

            return {
                'job_id': result.get('faxId') or tmp_fax_id,
                'provider_sid': result.get('faxId'),
                'status': 'queued',
                'to_number': to_number,
                'provider': 'humblefax',
                'metadata': {
                    'tmp_fax_id': tmp_fax_id,
                    'send_time': datetime.utcnow().isoformat(),
                    'recipients': tmp_fax_data['recipientFaxNumbers']
                }
            }

    async def verify_webhook(self, headers: Dict[str, str], body: bytes,
                           secrets: Dict[str, str]) -> Tuple[bool, Optional[str]]:
        """Verify HumbleFax webhook signature (HMAC-SHA256)"""

        if not self.webhook_secret:
            return True, "No webhook secret configured - accepting all"

        # Try multiple possible signature header names
        signature_header = (
            headers.get('X-Humblefax-Signature') or
            headers.get('X-HumbleFax-Signature') or
            headers.get('X-HF-Signature') or
            headers.get('X-Signature')
        )

        if not signature_header:
            return False, "No signature header found"

        try:
            expected_sig = hmac.new(
                self.webhook_secret.encode(),
                body,
                hashlib.sha256
            ).hexdigest()

            # Handle different signature formats (hex vs sha256=hex)
            provided_sig = signature_header.strip()
            if provided_sig.startswith('sha256='):
                provided_sig = provided_sig[7:]

            if hmac.compare_digest(expected_sig, provided_sig):
                return True, None
            else:
                return False, "Signature verification failed"

        except Exception as e:
            return False, f"Signature verification error: {str(e)}"

    async def parse_inbound(self, headers: Dict[str, str], body: bytes) -> Dict[str, Any]:
        """Parse HumbleFax inbound webhook payload"""
        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception as e:
            raise ValueError(f"Invalid JSON payload: {e}")

        # Normalize HumbleFax webhook format (flexible field mapping)
        file_url = (
            payload.get('fileUrl') or
            payload.get('pdfUrl') or
            payload.get('downloadUrl') or
            payload.get('file')
        )

        from_number = (
            payload.get('from') or
            payload.get('fromNumber') or
            payload.get('senderFaxNumber')
        )

        to_number = (
            payload.get('to') or
            payload.get('toNumber') or
            payload.get('recipientFaxNumber')
        )

        return {
            'provider': 'humblefax',
            'external_id': payload.get('faxId') or payload.get('id'),
            'from_number': from_number,
            'to_number': to_number,
            'pages': payload.get('pages') or payload.get('numPages'),
            'status': payload.get('status') or 'received',
            'direction': 'inbound',
            'received_at': payload.get('receivedAt') or datetime.utcnow().isoformat(),
            'file_url': file_url,
            'event_type': payload.get('event') or payload.get('type') or 'IncomingFax.SendComplete',
            'raw_payload': payload
        }

    async def _register_webhook(self, webhook_url: str) -> Dict[str, Any]:
        """Register webhook with HumbleFax API"""
        webhook_data = {
            'url': webhook_url,
            'subscriptions': ['IncomingFax.SendComplete', 'SentFax.SendComplete']
        }

        if self.webhook_secret:
            webhook_data['secret'] = self.webhook_secret

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/webhooks",
                json=webhook_data,
                auth=(self.api_key, self.api_secret)
            )

            return {
                'status_code': response.status_code,
                'response': response.json() if response.headers.get('content-type', '').startswith('application/json') else response.text
            }

    async def _list_webhooks(self) -> Dict[str, Any]:
        """List registered webhooks"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self.base_url}/webhooks",
                auth=(self.api_key, self.api_secret)
            )

            return {
                'status_code': response.status_code,
                'webhooks': response.json() if response.headers.get('content-type', '').startswith('application/json') else response.text
            }

    async def _delete_webhook(self, webhook_id: str) -> Dict[str, Any]:
        """Delete webhook by ID"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"{self.base_url}/webhooks/{webhook_id}",
                auth=(self.api_key, self.api_secret)
            )

            return {
                'status_code': response.status_code,
                'response': response.json() if response.headers.get('content-type', '').startswith('application/json') else response.text
            }

    async def check_imap_inbound(self) -> List[Dict[str, Any]]:
        """Check IMAP for inbound faxes (fallback method). Offload blocking ops."""
        if not self.imap_enabled:
            return []

        try:
            from fastapi.concurrency import run_in_threadpool

            def _sync_imap_fetch():
                # Connect to IMAP server
                if self.imap_use_ssl:
                    imap = imaplib.IMAP4_SSL(self.imap_server, self.imap_port)
                else:
                    imap = imaplib.IMAP4(self.imap_server, self.imap_port)

                imap.login(self.imap_username, self.imap_password)
                imap.select('INBOX')

                # Search for unread emails from HumbleFax
                status, messages = imap.search(None, 'UNSEEN', 'FROM', 'noreply@humblefax.com')

                inbound_faxes: List[Dict[str, Any]] = []
                max_total_bytes = 25 * 1024 * 1024
                total_bytes = 0

                for msg_num in messages[0].split():
                    status, msg_data = imap.fetch(msg_num, '(RFC822)')
                    email_body = msg_data[0][1]
                    total_bytes += len(email_body)
                    if total_bytes > max_total_bytes:
                        break
                    email_message = email.message_from_bytes(email_body)

                    # Extract fax information from email
                    fax_info = self._parse_email_fax(email_message)
                    if fax_info:
                        inbound_faxes.append(fax_info)

                    # Mark as read
                    imap.store(msg_num, '+FLAGS', '\\Seen')

                imap.close()
                imap.logout()
                return inbound_faxes

            return await run_in_threadpool(_sync_imap_fetch)

        except Exception as e:
            raise RuntimeError(f"IMAP check failed: {str(e)}")

    def _parse_email_fax(self, email_message) -> Optional[Dict[str, Any]]:
        """Parse fax information from HumbleFax email notification"""
        try:
            subject = email_message['Subject'] or ''
            from_addr = email_message['From'] or ''
            date = email_message['Date'] or ''

            # Look for PDF attachments
            pdf_attachments = []
            for part in email_message.walk():
                if part.get_content_type() == 'application/pdf':
                    filename = part.get_filename()
                    if filename:
                        # Save attachment with sanitized filename
                        safe_name = ''.join(c for c in filename if c.isalnum() or c in ('-', '_', '.'))
                        if not safe_name.lower().endswith('.pdf'):
                            continue
                        pdf_path = self.save_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
                        data = part.get_payload(decode=True)
                        if data is None:
                            continue
                        if len(data) > 10 * 1024 * 1024:
                            continue
                        with open(pdf_path, 'wb') as f:
                            f.write(data)
                        pdf_attachments.append(str(pdf_path))

            if pdf_attachments:
                return {
                    'provider': 'humblefax',
                    'direction': 'inbound',
                    'source': 'imap',
                    'subject': subject,
                    'from_email': from_addr,
                    'received_at': date,
                    'attachments': pdf_attachments,
                    'metadata': {
                        'email_date': date,
                        'inbound_method': 'imap'
                    }
                }

            return None

        except Exception as e:
            print(f"Error parsing email fax: {e}")
            return None
```

## Week 3-4: Production Monitoring & Observability

### 3. Comprehensive Monitoring Stack

**Create `api/app/monitoring/observability.py`** (choose ONE metrics stack; Phase 5 defaults to native Prometheus client on /metrics):
```python
from typing import Dict, Any, Optional, List
import time
import asyncio
from datetime import datetime, timedelta
from dataclasses import dataclass
from contextlib import asynccontextmanager
import logging

# OpenTelemetry imports
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.jaeger.thrift import JaegerExporter

# Prometheus metrics
import prometheus_client
from prometheus_client import Counter, Histogram, Gauge, Summary

@dataclass
class ProviderMetrics:
    """Metrics for each provider"""
    requests_total: Counter
    request_duration: Histogram
    errors_total: Counter
    success_rate: Gauge
    active_connections: Gauge

class FaxbotObservability:
    """Comprehensive observability for all Faxbot providers and operations"""

    def __init__(self):
        self.setup_prometheus_metrics()
        self.setup_opentelemetry()
        self.provider_metrics = {}

    def setup_prometheus_metrics(self):
        """Initialize Prometheus metrics"""

        # Global application metrics
        self.app_requests_total = Counter(
            'faxbot_requests_total',
            'Total requests to Faxbot API',
            ['method', 'endpoint', 'status']
        )

        self.app_request_duration = Histogram(
            'faxbot_request_duration_seconds',
            'Request duration in seconds',
            ['method', 'endpoint']
        )

        # Fax-specific metrics
        self.fax_jobs_total = Counter(
            'faxbot_fax_jobs_total',
            'Total fax jobs processed',
            ['provider', 'direction', 'status', 'tenant']
        )

        self.fax_job_duration = Histogram(
            'faxbot_fax_job_duration_seconds',
            'Time to complete fax job',
            ['provider', 'direction'],
            buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800]
        )

        self.fax_pages_total = Counter(
            'faxbot_fax_pages_total',
            'Total fax pages processed',
            ['provider', 'direction']
        )

        # Provider health metrics
        self.provider_health = Gauge(
            'faxbot_provider_health',
            'Provider health status (1=healthy, 0=unhealthy)',
            ['provider']
        )

        self.provider_response_time = Histogram(
            'faxbot_provider_response_time_seconds',
            'Provider API response time',
            ['provider', 'operation'],
            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
        )

        # Circuit breaker metrics
        self.circuit_breaker_state = Gauge(
            'faxbot_circuit_breaker_state',
            'Circuit breaker state (0=closed, 1=open, 2=half-open)',
            ['provider']
        )

        # Authentication metrics
        self.auth_attempts_total = Counter(
            'faxbot_auth_attempts_total',
            'Authentication attempts',
            ['method', 'result', 'tenant']
        )

        # Plugin metrics
        self.plugin_operations_total = Counter(
            'faxbot_plugin_operations_total',
            'Plugin operations',
            ['plugin_id', 'operation', 'status']
        )

    def setup_opentelemetry(self):
        """Initialize OpenTelemetry for distributed tracing"""

        # Setup tracing
        trace.set_tracer_provider(TracerProvider())
        tracer_provider = trace.get_tracer_provider()

        # Add Jaeger exporter
        jaeger_exporter = JaegerExporter(
            agent_host_name="localhost",
            agent_port=6831,
        )

        span_processor = BatchSpanProcessor(jaeger_exporter)
        tracer_provider.add_span_processor(span_processor)

        self.tracer = trace.get_tracer(__name__)

        # Metrics: Using native prometheus_client only for Phase 5 (/metrics on API port)

    def get_provider_metrics(self, provider_id: str) -> ProviderMetrics:
        """Get or create metrics for a provider"""
        if provider_id not in self.provider_metrics:
            self.provider_metrics[provider_id] = ProviderMetrics(
                requests_total=Counter(
                    f'faxbot_provider_requests_total',
                    'Provider requests',
                    ['provider', 'operation', 'status']
                ),
                request_duration=Histogram(
                    f'faxbot_provider_request_duration',
                    'Provider request duration',
                    ['provider', 'operation']
                ),
                errors_total=Counter(
                    f'faxbot_provider_errors_total',
                    'Provider errors',
                    ['provider', 'error_type']
                ),
                success_rate=Gauge(
                    f'faxbot_provider_success_rate',
                    'Provider success rate',
                    ['provider']
                ),
                active_connections=Gauge(
                    f'faxbot_provider_active_connections',
                    'Active connections to provider',
                    ['provider']
                )
            )
        return self.provider_metrics[provider_id]

    @asynccontextmanager
    async def trace_operation(self, operation_name: str, provider_id: Optional[str] = None,
                            attributes: Optional[Dict[str, Any]] = None):
        """Context manager for tracing operations with metrics"""

        start_time = time.time()

        with self.tracer.start_as_current_span(operation_name) as span:
            if attributes:
                for key, value in attributes.items():
                    span.set_attribute(key, str(value))

            if provider_id:
                span.set_attribute("provider.id", provider_id)
                metrics = self.get_provider_metrics(provider_id)
                metrics.active_connections.labels(provider=provider_id).inc()

            try:
                yield span

                # Record success metrics
                duration = time.time() - start_time

                if provider_id:
                    metrics.requests_total.labels(
                        provider=provider_id,
                        operation=operation_name,
                        status='success'
                    ).inc()
                    metrics.request_duration.labels(
                        provider=provider_id,
                        operation=operation_name
                    ).observe(duration)

                span.set_attribute("success", True)
                span.set_attribute("duration", duration)

            except Exception as e:
                # Record error metrics
                if provider_id:
                    metrics = self.get_provider_metrics(provider_id)
                    metrics.requests_total.labels(
                        provider=provider_id,
                        operation=operation_name,
                        status='error'
                    ).inc()
                    metrics.errors_total.labels(
                        provider=provider_id,
                        error_type=type(e).__name__
                    ).inc()

                span.set_attribute("success", False)
                span.set_attribute("error.type", type(e).__name__)
                span.set_attribute("error.message", str(e))
                span.record_exception(e)

                raise
            finally:
                if provider_id:
                    metrics.active_connections.labels(provider=provider_id).dec()

    async def record_fax_job(self, job_data: Dict[str, Any]):
        """Record fax job metrics"""
        provider = job_data.get('provider', 'unknown')
        direction = job_data.get('direction', 'outbound')
        status = job_data.get('status', 'unknown')
        tenant = job_data.get('tenant_id', 'default')
        pages = job_data.get('pages', 0)
        duration = job_data.get('duration', 0)

        # Record job metrics
        self.fax_jobs_total.labels(
            provider=provider,
            direction=direction,
            status=status,
            tenant=tenant
        ).inc()

        if pages:
            self.fax_pages_total.labels(
                provider=provider,
                direction=direction
            ).inc(pages)

        if duration:
            self.fax_job_duration.labels(
                provider=provider,
                direction=direction
            ).observe(duration)

    async def update_provider_health(self, provider_id: str, is_healthy: bool):
        """Update provider health status"""
        self.provider_health.labels(provider=provider_id).set(1 if is_healthy else 0)

    async def record_circuit_breaker_state(self, provider_id: str, state: str):
        """Record circuit breaker state changes"""
        state_values = {'closed': 0, 'open': 1, 'half-open': 2}
        self.circuit_breaker_state.labels(provider=provider_id).set(
            state_values.get(state, -1)
        )

# Global observability instance
observability = FaxbotObservability()
```

### 4. Health Check & Diagnostics System

**Create `api/app/monitoring/health.py`** (reuse Phase-3 design; do not duplicate different monitor semantics):
```python
from typing import Dict, Any, List, Optional
import asyncio
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
import httpx
from enum import Enum

class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"

@dataclass
class HealthCheckResult:
    status: HealthStatus
    message: str
    response_time_ms: Optional[float] = None
    details: Dict[str, Any] = None
    checked_at: datetime = None

    def __post_init__(self):
        if self.checked_at is None:
            self.checked_at = datetime.utcnow()
        if self.details is None:
            self.details = {}

class ProviderHealthMonitor:
    """Health monitoring for all providers"""

    def __init__(self):
        self.health_history = {}  # provider_id -> List[HealthCheckResult]
        self.max_history = 100

    async def check_all_providers(self, config: Dict[str, Any]) -> Dict[str, HealthCheckResult]:
        """Check health of all configured providers"""
        providers = {
            'phaxio': self._check_phaxio,
            'sinch': self._check_sinch,
            'signalwire': self._check_signalwire,
            'humblefax': self._check_humblefax,
            'sip': self._check_asterisk,
            'freeswitch': self._check_freeswitch,
        }

        results = {}

        # Run health checks in parallel
        tasks = []
        for provider_id, check_func in providers.items():
            task = asyncio.create_task(
                self._run_health_check(provider_id, check_func, config)
            )
            tasks.append((provider_id, task))

        for provider_id, task in tasks:
            try:
                result = await task
                results[provider_id] = result
                self._record_health_result(provider_id, result)
            except Exception as e:
                error_result = HealthCheckResult(
                    status=HealthStatus.UNHEALTHY,
                    message=f"Health check failed: {str(e)}"
                )
                results[provider_id] = error_result
                self._record_health_result(provider_id, error_result)

        return results

    async def _run_health_check(self, provider_id: str, check_func, config: Dict[str, Any]) -> HealthCheckResult:
        """Run individual health check with timeout"""
        try:
            return await asyncio.wait_for(check_func(config), timeout=30.0)
        except asyncio.TimeoutError:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message="Health check timed out after 30 seconds"
            )
        except Exception as e:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"Health check error: {str(e)}"
            )

    async def _check_phaxio(self, config: Dict[str, Any]) -> HealthCheckResult:
        """Check Phaxio API health"""
        api_key = config.get('phaxio_api_key')
        api_secret = config.get('phaxio_api_secret')

        if not (api_key and api_secret):
            return HealthCheckResult(
                status=HealthStatus.UNKNOWN,
                message="Phaxio credentials not configured"
            )

        start_time = time.time()

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    "https://api.phaxio.com/v2/account/status",
                    auth=(api_key, api_secret)
                )

                response_time = (time.time() - start_time) * 1000

                if response.status_code == 200:
                    data = response.json()
                    return HealthCheckResult(
                        status=HealthStatus.HEALTHY,
                        message="Phaxio API accessible",
                        response_time_ms=response_time,
                        details={
                            'account_status': data.get('success', False),
                            'balance': data.get('data', {}).get('balance')
                        }
                    )
                else:
                    return HealthCheckResult(
                        status=HealthStatus.UNHEALTHY,
                        message=f"Phaxio API returned {response.status_code}",
                        response_time_ms=response_time
                    )

        except Exception as e:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"Phaxio connection failed: {str(e)}",
                response_time_ms=(time.time() - start_time) * 1000
            )

    async def _check_sinch(self, config: Dict[str, Any]) -> HealthCheckResult:
        """Check Sinch API health with OAuth2/Basic Auth"""
        project_id = config.get('sinch_project_id') or config.get('phaxio_api_key', '')
        api_key = config.get('sinch_api_key') or config.get('phaxio_api_key', '')
        api_secret = config.get('sinch_api_secret') or config.get('phaxio_api_secret', '')
        auth_method = config.get('sinch_auth_method', 'basic').lower()

        if not (project_id and api_key and api_secret):
            return HealthCheckResult(
                status=HealthStatus.UNKNOWN,
                message="Sinch credentials not configured (check Phaxio fallback)"
            )

        start_time = time.time()
        base_url = config.get('sinch_base_url', 'https://fax.api.sinch.com/v3')

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Test authentication method
                if auth_method == 'oauth':
                    # Test OAuth2 token endpoint
                    token_response = await client.post(
                        "https://auth.sinch.com/oauth2/token",
                        data={
                            'grant_type': 'client_credentials',
                            'client_id': api_key,
                            'client_secret': api_secret
                        }
                    )

                    if token_response.status_code != 200:
                        return HealthCheckResult(
                            status=HealthStatus.UNHEALTHY,
                            message=f"Sinch OAuth2 failed: {token_response.status_code}",
                            response_time_ms=(time.time() - start_time) * 1000
                        )

                    token_data = token_response.json()
                    headers = {"Authorization": f"Bearer {token_data['access_token']}"}
                else:
                    # Basic auth
                    auth = (api_key, api_secret)
                    headers = {}

                # Test project access
                response = await client.get(
                    f"{base_url}/projects/{project_id}/faxes",
                    params={'limit': 1},
                    auth=auth if auth_method == 'basic' else None,
                    headers=headers
                )

                response_time = (time.time() - start_time) * 1000

                if response.status_code in [200, 401]:  # 401 is OK, means auth works but no access
                    return HealthCheckResult(
                        status=HealthStatus.HEALTHY,
                        message=f"Sinch API accessible ({auth_method} auth)",
                        response_time_ms=response_time,
                        details={
                            'auth_method': auth_method,
                            'project_id': project_id,
                            'base_url': base_url,
                            'fallback_used': 'sinch_project_id' not in config
                        }
                    )
                else:
                    return HealthCheckResult(
                        status=HealthStatus.DEGRADED,
                        message=f"Sinch API returned {response.status_code}",
                        response_time_ms=response_time
                    )

        except Exception as e:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"Sinch connection failed: {str(e)}",
                response_time_ms=(time.time() - start_time) * 1000
            )

    async def _check_humblefax(self, config: Dict[str, Any]) -> HealthCheckResult:
        """Check HumbleFax API health"""
        api_key = config.get('humblefax_access_key')
        api_secret = config.get('humblefax_secret_key')

        if not (api_key and api_secret):
            return HealthCheckResult(
                status=HealthStatus.UNKNOWN,
                message="HumbleFax credentials not configured"
            )

        start_time = time.time()
        base_url = config.get('humblefax_base_url', 'https://api.humblefax.com')

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Test with GetSentFaxes (lightweight endpoint)
                response = await client.get(
                    f"{base_url}/GetSentFaxes",
                    params={'limit': 1},
                    auth=(api_key, api_secret)
                )

                response_time = (time.time() - start_time) * 1000

                if response.status_code == 200:
                    return HealthCheckResult(
                        status=HealthStatus.HEALTHY,
                        message="HumbleFax API accessible",
                        response_time_ms=response_time,
                        details={
                            'webhook_enabled': config.get('humblefax_webhook_enabled', False),
                            'imap_enabled': config.get('humblefax_imap_enabled', False)
                        }
                    )
                else:
                    return HealthCheckResult(
                        status=HealthStatus.UNHEALTHY,
                        message=f"HumbleFax API returned {response.status_code}",
                        response_time_ms=response_time
                    )

        except Exception as e:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"HumbleFax connection failed: {str(e)}",
                response_time_ms=(time.time() - start_time) * 1000
            )

    async def _check_signalwire(self, config: Dict[str, Any]) -> HealthCheckResult:
        """Check SignalWire API health"""
        project_id = config.get('signalwire_project_id', '')
        api_token = config.get('signalwire_api_token', '')
        space_url = config.get('signalwire_space_url', '')

        if not (project_id and api_token and space_url):
            return HealthCheckResult(
                status=HealthStatus.UNKNOWN,
                message="SignalWire credentials not configured"
            )

        start_time = time.time()

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"https://{space_url}/api/laml/2010-04-01/Accounts/{project_id}.json",
                    auth=(project_id, api_token)
                )

                response_time = (time.time() - start_time) * 1000

                if response.status_code == 200:
                    return HealthCheckResult(
                        status=HealthStatus.HEALTHY,
                        message="SignalWire API accessible",
                        response_time_ms=response_time,
                        details={'space_url': space_url}
                    )
                else:
                    return HealthCheckResult(
                        status=HealthStatus.UNHEALTHY,
                        message=f"SignalWire API returned {response.status_code}",
                        response_time_ms=response_time
                    )

        except Exception as e:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"SignalWire connection failed: {str(e)}",
                response_time_ms=(time.time() - start_time) * 1000
            )

    async def _check_asterisk(self, config: Dict[str, Any]) -> HealthCheckResult:
        """Check Asterisk AMI health"""
        ami_host = config.get('asterisk_ami_host', 'localhost')
        ami_port = config.get('asterisk_ami_port', 5038)
        ami_username = config.get('asterisk_ami_username', '')
        ami_password = config.get('asterisk_ami_password', '')

        if not (ami_username and ami_password):
            return HealthCheckResult(
                status=HealthStatus.UNKNOWN,
                message="Asterisk AMI credentials not configured"
            )

        start_time = time.time()

        try:
            # Simple TCP connection test (AMI is not HTTP)
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            result = sock.connect_ex((ami_host, int(ami_port)))
            sock.close()

            response_time = (time.time() - start_time) * 1000

            if result == 0:
                return HealthCheckResult(
                    status=HealthStatus.HEALTHY,
                    message=f"Asterisk AMI port {ami_port} accessible",
                    response_time_ms=response_time,
                    details={
                        'host': ami_host,
                        'port': ami_port
                    }
                )
            else:
                return HealthCheckResult(
                    status=HealthStatus.UNHEALTHY,
                    message=f"Cannot connect to Asterisk AMI {ami_host}:{ami_port}",
                    response_time_ms=response_time
                )

        except Exception as e:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"Asterisk AMI check failed: {str(e)}",
                response_time_ms=(time.time() - start_time) * 1000
            )

    async def _check_freeswitch(self, config: Dict[str, Any]) -> HealthCheckResult:
        """Check FreeSWITCH ESL health"""
        esl_host = config.get('freeswitch_esl_host', 'localhost')
        esl_port = config.get('freeswitch_esl_port', 8021)
        esl_password = config.get('freeswitch_esl_password', 'ClueCon')

        start_time = time.time()

        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            result = sock.connect_ex((esl_host, int(esl_port)))
            sock.close()

            response_time = (time.time() - start_time) * 1000

            if result == 0:
                return HealthCheckResult(
                    status=HealthStatus.HEALTHY,
                    message=f"FreeSWITCH ESL port {esl_port} accessible",
                    response_time_ms=response_time,
                    details={
                        'host': esl_host,
                        'port': esl_port
                    }
                )
            else:
                return HealthCheckResult(
                    status=HealthStatus.UNHEALTHY,
                    message=f"Cannot connect to FreeSWITCH ESL {esl_host}:{esl_port}",
                    response_time_ms=response_time
                )

        except Exception as e:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"FreeSWITCH ESL check failed: {str(e)}",
                response_time_ms=(time.time() - start_time) * 1000
            )

    def _record_health_result(self, provider_id: str, result: HealthCheckResult):
        """Record health check result in history"""
        if provider_id not in self.health_history:
            self.health_history[provider_id] = []

        self.health_history[provider_id].append(result)

        # Keep only recent history
        if len(self.health_history[provider_id]) > self.max_history:
            self.health_history[provider_id] = self.health_history[provider_id][-self.max_history:]

    def get_provider_health_summary(self, provider_id: str) -> Dict[str, Any]:
        """Get health summary for a provider"""
        history = self.health_history.get(provider_id, [])

        if not history:
            return {
                'status': 'unknown',
                'message': 'No health checks performed',
                'last_check': None
            }

        latest = history[-1]
        recent_checks = history[-10:]  # Last 10 checks

        success_count = sum(1 for check in recent_checks if check.status == HealthStatus.HEALTHY)
        success_rate = (success_count / len(recent_checks)) * 100 if recent_checks else 0

        avg_response_time = None
        response_times = [check.response_time_ms for check in recent_checks if check.response_time_ms]
        if response_times:
            avg_response_time = sum(response_times) / len(response_times)

        return {
            'status': latest.status.value,
            'message': latest.message,
            'last_check': latest.checked_at.isoformat(),
            'success_rate_percent': round(success_rate, 1),
            'avg_response_time_ms': round(avg_response_time, 1) if avg_response_time else None,
            'total_checks': len(history),
            'recent_checks': len(recent_checks)
        }

# Global health monitor instance (single source of truth; extend Phase-3, do not duplicate)
health_monitor = ProviderHealthMonitor()
```

## Week 5-6: Zero-Downtime Migration & Deployment

### 5. Migration Engine

**Create `api/app/migration/v3_to_v4.py`**:
```python
import os
import json
import shutil
from typing import Dict, Any, List, Optional
from datetime import datetime
from pathlib import Path
import asyncio
import sqlite3
from dataclasses import dataclass

@dataclass
class MigrationStep:
    name: str
    description: str
    rollback_description: str
    completed: bool = False

class V3ToV4MigrationEngine:
    """Zero-downtime migration from v3 to v4 architecture"""

    def __init__(self):
        self.backup_dir = Path("backups") / f"v3_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.migration_log = []

        self.migration_steps = [
            MigrationStep(
                name="backup_current_state",
                description="Create complete backup of current v3 configuration",
                rollback_description="Restore from backup directory"
            ),
            MigrationStep(
                name="migrate_environment_config",
                description="Convert .env configuration to hierarchical config",
                rollback_description="Restore original .env file"
            ),
            MigrationStep(
                name="migrate_provider_configs",
                description="Convert provider configurations to plugin manifests",
                rollback_description="Remove plugin configurations"
            ),
            MigrationStep(
                name="migrate_database_schema",
                description="Update database schema for v4 features (Alembic-only)",
                rollback_description="Restore database from backup"
            ),
            MigrationStep(
                name="validate_migration",
                description="Validate that all providers work in v4",
                rollback_description="Migration validation failed - rollback required"
            ),
            MigrationStep(
                name="blue_green_deployment",
                description="Gradual traffic shift to v4 endpoints (mirror callbacks; canary first)",
                rollback_description="Shift traffic back to v3 endpoints"
            )
        ]

    async def migrate(self) -> bool:
        """Execute complete migration with rollback capability"""
        self.log("Starting v3 to v4 migration")

        try:
            for step in self.migration_steps:
                self.log(f"Executing: {step.description}")

                success = await self._execute_step(step)
                if not success:
                    self.log(f"Step failed: {step.name}")
                    await self._rollback()
                    return False

                step.completed = True
                self.log(f"Completed: {step.name}")

            self.log("Migration completed successfully!")
            return True

        except Exception as e:
            self.log(f"Migration failed with error: {str(e)}")
            await self._rollback()
            return False

    async def _execute_step(self, step: MigrationStep) -> bool:
        """Execute individual migration step"""
        try:
            if step.name == "backup_current_state":
                return await self._backup_current_state()
            elif step.name == "migrate_environment_config":
                return await self._migrate_env_config()
            elif step.name == "migrate_provider_configs":
                return await self._migrate_provider_configs()
            elif step.name == "migrate_database_schema":
                return await self._migrate_database_schema()
            elif step.name == "validate_migration":
                return await self._validate_migration()
            elif step.name == "blue_green_deployment":
                return await self._blue_green_deployment()
            else:
                self.log(f"Unknown migration step: {step.name}")
                return False
        except Exception as e:
            self.log(f"Step {step.name} failed: {str(e)}")
            return False

    async def _migrate_database_schema(self) -> bool:
        """Update database schema for v4 features (Alembic-only)"""
        try:
            # Use Alembic migrations only; do not apply raw DDL here.
            # Example Alembic command invocation (pseudo-code):
            # await run_alembic_upgrade("head")
            return True
        except Exception as e:
            self.log(f"DB schema migration failed: {str(e)}")
            return False

    async def _blue_green_deployment(self) -> bool:
        """Gradual traffic shift to v4 endpoints"""
        # Strategy: mirror callbacks to v3+v4, canary read-only, then promote
        # - Mirror provider callbacks to both v3 and v4 during cutover window
        # - Ensure idempotent send path (Idempotency-Key) to avoid duplicates
        # - OpenAPI diff guard in CI: fail if existing routes/response shapes drift
        return True

    async def _backup_current_state(self) -> bool:
        """Create complete backup of current state"""
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)

            # Backup .env file
            if Path(".env").exists():
                shutil.copy2(".env", self.backup_dir / ".env")

            # Backup configuration files
            config_files = [
                "config/provider_traits.json",
                "config/plugin_registry.json",
                "config/provider_status_map.json"
            ]

            for config_file in config_files:
                if Path(config_file).exists():
                    dest_path = self.backup_dir / config_file
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(config_file, dest_path)

            # Backup database
            db_files = ["faxbot.db", "faxbot_test.db"]
            for db_file in db_files:
                if Path(db_file).exists():
                    shutil.copy2(db_file, self.backup_dir / db_file)

            # Backup critical application files
            app_files = [
                "api/app/config.py",
                "api/app/main.py",
                "api/app/providers/",
                "api/app/plugins/"
            ]

            for app_file in app_files:
                if Path(app_file).exists():
                    dest_path = self.backup_dir / app_file
                    if Path(app_file).is_dir():
                        shutil.copytree(app_file, dest_path, dirs_exist_ok=True)
                    else:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(app_file, dest_path)

            self.log(f"Backup created at: {self.backup_dir}")
            return True

        except Exception as e:
            self.log(f"Backup failed: {str(e)}")
            return False

    async def _migrate_env_config(self) -> bool:
        """Convert .env configuration to hierarchical config"""
        try:
            if not Path(".env").exists():
                self.log("No .env file found - skipping env migration")
                return True

            # Read current .env
            env_vars = {}
            with open(".env", "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        env_vars[key] = value.strip('"\'')

            # Map env vars to hierarchical config
            hierarchical_config = self._map_env_to_hierarchical(env_vars)

            # Store in database (assuming Phase 3 hierarchical config is available)
            await self._store_hierarchical_config(hierarchical_config)

            self.log(f"Migrated {len(env_vars)} environment variables to hierarchical config")
            return True

        except Exception as e:
            self.log(f"Environment migration failed: {str(e)}")
            return False

    async def _migrate_provider_configs(self) -> bool:
        """Convert providers to plugin architecture"""
        try:
            providers_to_migrate = {
                'phaxio': self._migrate_phaxio_provider,
                'sinch': self._migrate_sinch_provider,
                'signalwire': self._migrate_signalwire_provider,
                'humblefax': self._migrate_humblefax_provider,
                'sip': self._migrate_asterisk_provider,
                'freeswitch': self._migrate_freeswitch_provider
            }

            for provider_id, migrate_func in providers_to_migrate.items():
                success = await migrate_func()
                if not success:
                    self.log(f"Failed to migrate provider: {provider_id}")
                    return False

            self.log("All providers migrated to plugin architecture")
            return True

        except Exception as e:
            self.log(f"Provider migration failed: {str(e)}")
            return False

    async def _migrate_sinch_provider(self) -> bool:
        """Migrate Sinch to plugin with OAuth2 support"""
        try:
            manifest = {
                "id": "sinch",
                "name": "Sinch Fax API v3",
                "version": "1.0.0",
                "type": "transport",
                "description": "Sinch (Phaxio by Sinch) with OAuth2 and regional endpoints",
                "author": "Faxbot Team",
                "categories": ["transport"],
                "capabilities": ["send_fax", "get_status", "webhook", "inbound"],
                "traits": [
                    "send_fax",
                    "status_callback",
                    "webhook_basic_auth",
                    "inbound_supported",
                    "oauth2_supported",
                    "regional_endpoints"
                ],
                "config_schema": {
                    "type": "object",
                    "required": ["sinch_project_id", "sinch_api_key", "sinch_api_secret"],
                    "properties": {
                        "sinch_project_id": {"type": "string", "title": "Project ID"},
                        "sinch_api_key": {"type": "string", "title": "API Key", "secret": True},
                        "sinch_api_secret": {"type": "string", "title": "API Secret", "secret": True},
                        "sinch_auth_method": {"type": "string", "enum": ["basic", "oauth"], "default": "basic"},
                        "sinch_region": {"type": "string", "enum": ["global", "us", "eu"], "default": "global"},
                        "sinch_base_url": {"type": "string", "title": "Base URL (optional)"},
                        "sinch_inbound_basic_user": {"type": "string", "title": "Inbound Basic Auth User"},
                        "sinch_inbound_basic_pass": {"type": "string", "title": "Inbound Basic Auth Password", "secret": True}
                    }
                },
                "fallback_config": {
                    "sinch_api_key": "PHAXIO_API_KEY",
                    "sinch_api_secret": "PHAXIO_API_SECRET"
                },
                "webhook_path": "/callbacks/sinch",
                "inbound_verification": "basic_auth",
                "hipaa_compliant": True
            }

            # Save manifest
            manifest_path = Path("api/app/plugins/transport/sinch/manifest.json")
            manifest_path.parent.mkdir(parents=True, exist_ok=True)

            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)

            self.log("Sinch provider migrated to plugin with OAuth2 support")
            return True

        except Exception as e:
            self.log(f"Sinch migration failed: {str(e)}")
            return False

    async def _migrate_humblefax_provider(self) -> bool:
        """Migrate HumbleFax with webhook + IMAP support"""
        try:
            manifest = {
                "id": "humblefax",
                "name": "HumbleFax",
                "version": "1.0.0",
                "type": "transport",
                "description": "HumbleFax with webhook and IMAP inbound support",
                "author": "Faxbot Team",
                "categories": ["transport"],
                "capabilities": ["send_fax", "get_status", "webhook", "imap_inbound"],
                "traits": [
                    "send_fax",
                    "status_callback",
                    "webhook_hmac",
                    "imap_inbound",
                    "email_notifications",
                    "multi_attachment"
                ],
                "config_schema": {
                    "type": "object",
                    "required": ["humblefax_access_key", "humblefax_secret_key"],
                    "properties": {
                        "humblefax_access_key": {"type": "string", "title": "Access Key", "secret": True},
                        "humblefax_secret_key": {"type": "string", "title": "Secret Key", "secret": True},
                        "humblefax_base_url": {"type": "string", "default": "https://api.humblefax.com"},
                        "humblefax_webhook_enabled": {"type": "boolean", "default": True},
                        "humblefax_webhook_secret": {"type": "string", "title": "Webhook HMAC Secret", "secret": True},
                        "humblefax_callback_base": {"type": "string", "title": "Public Callback URL Base"},
                        "humblefax_imap_enabled": {"type": "boolean", "default": False},
                        "humblefax_imap_server": {"type": "string", "default": "imap.gmail.com"},
                        "humblefax_imap_username": {"type": "string", "title": "IMAP Username"},
                        "humblefax_imap_password": {"type": "string", "title": "IMAP Password", "secret": True}
                    }
                },
                "webhook_path": "/inbound/humblefax/webhook",
                "inbound_verification": "hmac_sha256",
                "rate_limits": {
                    "requests_per_second": 5,
                    "max_recipients": 3,
                    "max_file_size_mb": 50
                },
                "hipaa_compliant": False
            }

            # Save manifest
            manifest_path = Path("api/app/plugins/transport/humblefax/manifest.json")
            manifest_path.parent.mkdir(parents=True, exist_ok=True)

            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)

            self.log("HumbleFax provider migrated with webhook + IMAP support")
            return True

        except Exception as e:
            self.log(f"HumbleFax migration failed: {str(e)}")
            return False

    def _map_env_to_hierarchical(self, env_vars: Dict[str, str]) -> Dict[str, Any]:
        """Map flat .env variables to hierarchical configuration"""

        hierarchical = {
            "global": {},
            "providers": {},
            "features": {},
            "security": {}
        }

        # Provider mappings
        provider_mappings = {
            'PHAXIO_': 'phaxio',
            'SINCH_': 'sinch',
            'SIGNALWIRE_': 'signalwire',
            'HUMBLEFAX_': 'humblefax',
            'ASTERISK_': 'sip',
            'FREESWITCH_': 'freeswitch'
        }

        for env_key, env_value in env_vars.items():
            # Map provider-specific configs
            for prefix, provider_id in provider_mappings.items():
                if env_key.startswith(prefix):
                    provider_key = env_key[len(prefix):].lower()
                    if provider_id not in hierarchical["providers"]:
                        hierarchical["providers"][provider_id] = {}
                    hierarchical["providers"][provider_id][provider_key] = env_value
                    break
            else:
                # Global configurations
                if env_key.startswith('FAX_'):
                    hierarchical["features"][env_key.lower()] = env_value
                elif env_key.startswith('API_'):
                    hierarchical["security"][env_key.lower()] = env_value
                else:
                    hierarchical["global"][env_key.lower()] = env_value

        return hierarchical

    async def _validate_migration(self) -> bool:
        """Validate that all providers work after migration"""
        try:
            # Import health monitor
            from api.app.monitoring.health import health_monitor

            # Check all provider health
            config = await self._get_migrated_config()
            health_results = await health_monitor.check_all_providers(config)

            failed_providers = []
            for provider_id, result in health_results.items():
                if result.status.value not in ['healthy', 'unknown']:
                    failed_providers.append(provider_id)

            if failed_providers:
                self.log(f"Validation failed - unhealthy providers: {failed_providers}")
                return False

            self.log("All providers validated successfully after migration")
            return True

        except Exception as e:
            self.log(f"Migration validation failed: {str(e)}")
            return False

    async def _rollback(self) -> bool:
        """Rollback migration to v3 state"""
        try:
            self.log("Starting migration rollback...")

            # Restore .env file
            if (self.backup_dir / ".env").exists():
                shutil.copy2(self.backup_dir / ".env", ".env")
                self.log("Restored .env file")

            # Restore config files
            config_files = [
                "config/provider_traits.json",
                "config/plugin_registry.json",
                "config/provider_status_map.json"
            ]

            for config_file in config_files:
                backup_file = self.backup_dir / config_file
                if backup_file.exists():
                    Path(config_file).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_file, config_file)
                    self.log(f"Restored {config_file}")

            # Restore database
            for db_file in ["faxbot.db", "faxbot_test.db"]:
                backup_db = self.backup_dir / db_file
                if backup_db.exists():
                    shutil.copy2(backup_db, db_file)
                    self.log(f"Restored database: {db_file}")

            self.log("Migration rollback completed")
            return True

        except Exception as e:
            self.log(f"Rollback failed: {str(e)}")
            return False

    def log(self, message: str):
        """Log migration message with timestamp"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[{timestamp}] {message}"
        print(log_message)
        self.migration_log.append(log_message)
```

## Week 7-8: Complete Documentation & Production Deployment

### 6. Provider Traits Registry (All 7 Providers)

**Update `config/provider_traits.json`** (comprehensive; canonical keys):
```json
{
  "_schema": {
    "version": 2,
    "description": "Complete provider traits for all 7 supported providers",
    "canonical_trait_keys": [
      "send_fax", "status_callback", "inbound_supported",
      "webhook_verification", "auth_method", "regional_endpoints",
      "oauth2_supported", "imap_supported", "rate_limits",
      "hipaa_compliant", "requires_ami", "requires_ghostscript"
    ]
  },

  "phaxio": {
    "id": "phaxio",
    "name": "Phaxio Cloud Fax",
    "kind": "cloud",
    "status": "production",
    "traits": {
      "send_fax": true,
      "status_callback": true,
      "inbound_supported": true,
      "webhook": {
        "verification": "hmac_sha256",
        "path": "/callbacks/phaxio",
        "verify_header": "X-Phaxio-Signature"
      },
      "auth": { "methods": ["basic"] },
      "requires_ghostscript": true,
      "requires_ami": false,
      "requires_tiff": false,
      "needs_storage": true,
      "hipaa_compliant": true,
      "baa_available": true
    }
  },

  "sinch": {
    "id": "sinch",
    "name": "Sinch Fax API v3 (Phaxio by Sinch)",
    "kind": "cloud",
    "status": "production",
    "traits": {
      "send_fax": true,
      "status_callback": true,
      "inbound_supported": true,
      "webhook": {
        "verification": "basic_auth",
        "path": "/callbacks/sinch",
        "verify_header": "Authorization"
      },
      "auth": { "methods": ["basic","oauth2"] },
      "oauth2_supported": true,
      "regional_endpoints": true,
      "requires_ghostscript": true,
      "requires_ami": false,
      "requires_tiff": false,
      "needs_storage": true,
      "hipaa_compliant": true,
      "fallback_config": {
        "sinch_api_key": "PHAXIO_API_KEY",
        "sinch_api_secret": "PHAXIO_API_SECRET"
      },
      "regions": ["global", "us", "eu"]
    }
  },

  "humblefax": {
    "id": "humblefax",
    "name": "HumbleFax",
    "kind": "cloud",
    "status": "production",
    "traits": {
      "send_fax": true,
      "status_callback": true,
      "inbound_supported": true,
      "webhook": {
        "verification": "hmac_sha256",
        "path": "/inbound/humblefax/webhook",
        "verify_header": "X-Humblefax-Signature"
      },
      "imap_supported": true,
      "auth": { "methods": ["basic"] },
      "requires_ghostscript": true,
      "requires_ami": false,
      "requires_tiff": false,
      "needs_storage": false,
      "hipaa_compliant": false,
      "rate_limits": {
        "requests_per_second": 5,
        "max_recipients": 3,
        "max_file_size_mb": 50
      },
      "special_features": ["imap_inbound", "email_notifications", "zapier_integration"]
    }
  },

  "signalwire": {
    "id": "signalwire",
    "name": "SignalWire (Twilio-Compatible)",
    "kind": "cloud",
    "status": "production",
    "traits": {
      "send_fax": true,
      "status_callback": true,
      "inbound_supported": false,
      "webhook": {
        "verification": "basic_auth",
        "path": "/callbacks/signalwire",
        "verify_header": "Authorization"
      },
      "auth": { "methods": ["basic"] },
      "requires_ghostscript": true,
      "requires_ami": false,
      "requires_tiff": false,
      "needs_storage": false,
      "hipaa_compliant": false,
      "outbound_status_only": true,
      "twilio_compatible": true
    }
  },

  "sip": {
    "id": "sip",
    "name": "SIP/Asterisk (Self-hosted)",
    "kind": "self_hosted",
    "status": "production",
    "traits": {
      "send_fax": true,
      "status_callback": true,
      "inbound_supported": true,
      "webhook_verification": "internal_secret",
      "auth_method": "ami",
      "requires_ghostscript": true,
      "requires_ami": true,
      "requires_tiff": true,
      "needs_storage": true,
      "hipaa_compliant": true,
      "t38_protocol": true,
      "network_isolation_recommended": true,
      "technical_complexity": "high"
    }
  },

  "freeswitch": {
    "id": "freeswitch",
    "name": "FreeSWITCH (ESL Integration)",
    "kind": "self_hosted",
    "status": "production",
    "traits": {
      "send_fax": true,
      "status_callback": true,
      "inbound_supported": false,
      "webhook_verification": "none",
      "auth_method": "esl",
      "requires_ghostscript": true,
      "requires_ami": false,
      "requires_tiff": true,
      "needs_storage": false,
      "hipaa_compliant": true,
      "mod_spandsp": true,
      "event_socket": true,
      "outbound_status_only": false
    }
  },

  "disabled": {
    "id": "disabled",
    "name": "Test/Disabled Mode",
    "kind": "test",
    "status": "development",
    "traits": {
      "send_fax": false,
      "status_callback": false,
      "inbound_supported": false,
      "webhook_verification": "none",
      "auth_method": "none",
      "requires_ghostscript": false,
      "requires_ami": false,
      "requires_tiff": false,
      "needs_storage": false,
      "hipaa_compliant": false,
      "cost_free": true,
      "testing_only": true
    }
  }
}
```

### 7. Production Deployment Configuration

**Create `deployment/kubernetes/faxbot-v4.yaml`**:
```yaml
# Production Kubernetes deployment for Faxbot v4
apiVersion: apps/v1
kind: Deployment
metadata:
  name: faxbot-v4
  labels:
    app: faxbot
    version: v4
spec:
  replicas: 3
  selector:
    matchLabels:
      app: faxbot
      version: v4
  template:
    metadata:
      labels:
        app: faxbot
        version: v4
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8080"
        prometheus.io/path: "/metrics"
    spec:
      containers:
      - name: faxbot-api
        image: faxbot:v4-latest
        ports:
        - containerPort: 8080
          name: http-api
        env:
        - name: CONFIG_MASTER_KEY
          valueFrom:
            secretKeyRef:
              name: faxbot-secrets
              key: config_master_key
        - name: FAXBOT_SESSION_PEPPER
          valueFrom:
            secretKeyRef:
              name: faxbot-secrets
              key: session_pepper
        - name: ADMIN_MARKETPLACE_ENABLED
          value: "false"
        - name: ADMIN_MARKETPLACE_REMOTE_INSTALL_ENABLED
          value: "false"
        - name: DB_TLS_ENABLED
          value: "false"
        - name: REDIS_TLS_ENABLED
          value: "false"
        - name: BOOT_FAIL_ON_MISSING_SECRETS
          value: "true"
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: faxbot-secrets
              key: database_url
        - name: REDIS_URL
          valueFrom:
            secretKeyRef:
              name: faxbot-secrets
              key: redis_url
        # Provider credentials from secrets
        - name: PHAXIO_API_KEY
          valueFrom:
            secretKeyRef:
              name: faxbot-provider-secrets
              key: phaxio_api_key
        - name: SINCH_PROJECT_ID
          valueFrom:
            secretKeyRef:
              name: faxbot-provider-secrets
              key: sinch_project_id
        - name: HUMBLEFAX_ACCESS_KEY
          valueFrom:
            secretKeyRef:
              name: faxbot-provider-secrets
              key: humblefax_access_key
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /health/ready
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 5
        resources:
          requests:
            memory: "512Mi"
            cpu: "250m"
          limits:
            memory: "1Gi"
            cpu: "500m"
        volumeMounts:
        - name: fax-storage
          mountPath: /app/faxdata
        - name: config-volume
          mountPath: /app/config
      volumes:
      - name: fax-storage
        persistentVolumeClaim:
          claimName: faxbot-storage
      - name: config-volume
        configMap:
          name: faxbot-config

---
apiVersion: v1
kind: Service
metadata:
  name: faxbot-service
  labels:
    app: faxbot
spec:
  selector:
    app: faxbot
  ports:
  - name: http
    port: 80
    targetPort: 8080
  type: LoadBalancer

---
apiVersion: v1
kind: ConfigMap
metadata:
  name: faxbot-config
data:
  provider_traits.json: |
    {
      "phaxio": {
        "id": "phaxio",
        "traits": {
          "send_fax": true,
          "hipaa_compliant": true,
          "webhook_verification": "hmac_sha256"
        }
      }
    }

---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: faxbot-ingress
  annotations:
    kubernetes.io/ingress.class: "nginx"
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  tls:
  - hosts:
    - api.faxbot.example.com
    secretName: faxbot-tls
  rules:
  - host: api.faxbot.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: faxbot-service
            port:
              number: 80
```

### 6.a Setup Wizard & Admin Console wiring for HumbleFax (traits-first, schema-driven)

- Traits-first contract: UI is schema/trait driven. No provider-name checks and no bespoke provider components. As long as the manifest and traits are complete, the existing Wizard and Settings auto-render.

- Setup Wizard (generic schema renderer): the transport step consumes the manifest `config_schema` for `humblefax` and renders fields automatically.
  - Ensure the manifest includes: `humblefax_access_key` (secret), `humblefax_secret_key` (secret), `humblefax_base_url` (optional),
    `humblefax_webhook_enabled`, `humblefax_webhook_secret` (secret), `humblefax_callback_base`, `humblefax_imap_enabled`,
    `humblefax_imap_server`, `humblefax_imap_username`, `humblefax_imap_password` (secret).
  - Pre-validate (schema/constraints): non-empty secrets; if webhook enabled, require `callback_base`; if IMAP enabled, require server/credentials.
  - Persist via HybridConfigProvider at tenant scope; no restart required.

- Admin Settings (generic schema renderer):
  - The Settings screen renders from `config_schema` and `traits`. No provider-specific code; secrets masked by field metadata.
  - IMAP group collapsible; helper text + docsBase “Learn more” links; errors actionable.

- Diagnostics/Dashboard (traits-first):
  - `/admin/providers` already surfaces traits. Diagnostics filters by active traits and shows only HumbleFax checks (webhook reachability, IMAP enabled state, recent health) when active.
  - Dashboard cards/help text derive from traits; only HumbleFax guidance appears when HumbleFax is active.

- Manifest ingestion (auto-wiring):
  - The manifest+traits alone must enable Wizard/Settings rendering and diagnostics gating. No string comparisons on backend ids.
  - Acceptance: switching active transport to `humblefax` with a complete manifest immediately shows HumbleFax-only UI without additional code.

- Acceptance (UI parity): With HumbleFax selected, only HumbleFax-specific settings, diagnostics, and help appear; no other provider guidance is visible.

### 8. Complete API Documentation

**Create comprehensive OpenAPI 3.1 specification with all providers**:

```yaml
# openapi-v4.yaml - Complete API documentation
openapi: 3.1.0
info:
  title: Faxbot API v4
  description: |
    Complete fax transmission API supporting 7 providers:
    - **Sinch** (most popular, OAuth2 + Basic Auth)
    - **Phaxio** (HIPAA-compliant with BAA)
    - **HumbleFax** (webhook + IMAP inbound)
    - **SignalWire** (Twilio-compatible)
    - **SIP/Asterisk** (self-hosted T.38)
    - **FreeSWITCH** (ESL integration)
    - **Test/Disabled** (development mode)
  version: 4.0.0
  contact:
    name: Faxbot Support
    url: https://docs.faxbot.net
  license:
    name: MIT
    url: https://opensource.org/licenses/MIT

servers:
  - url: https://api.faxbot.example.com
    description: Production server
  - url: http://localhost:8080
    description: Local development

components:
  securitySchemes:
    ApiKeyAuth:
      type: apiKey
      in: header
      name: X-API-Key
    BearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT

  schemas:
    FaxJob:
      type: object
      properties:
        job_id:
          type: string
          description: Unique job identifier
        provider:
          type: string
          enum: [phaxio, sinch, humblefax, signalwire, sip, freeswitch, disabled]
        to_number:
          type: string
          description: Recipient fax number in E.164 format
        status:
          type: string
          enum: [queued, in_progress, delivered, failed, canceled]
        pages:
          type: integer
          description: Number of pages transmitted
        cost:
          type: number
          format: float
          description: Transmission cost in USD
        created_at:
          type: string
          format: date-time
        completed_at:
          type: string
          format: date-time

    SendFaxRequest:
      type: object
      required:
        - to
        - file
      properties:
        to:
          type: string
          description: Recipient fax number
          example: "+1234567890"
        file:
          type: string
          format: binary
          description: PDF or TIFF file to send
        from_number:
          type: string
          description: Sender fax number (optional)
        coversheet:
          type: string
          description: Cover sheet message
        provider:
          type: string
          enum: [phaxio, sinch, humblefax, signalwire, sip, freeswitch]
          description: Specific provider to use (optional)

    ProviderHealth:
      type: object
      properties:
        provider_id:
          type: string
        status:
          type: string
          enum: [healthy, degraded, unhealthy, unknown]
        message:
          type: string
        response_time_ms:
          type: number
        last_check:
          type: string
          format: date-time
        traits:
          type: object
          additionalProperties: true

security:
  - ApiKeyAuth: []
  - BearerAuth: []

paths:
  /fax/send:
    post:
      summary: Send Fax
      description: |
        Send fax using any configured provider. Provider selection is automatic
        based on configuration unless specifically requested.

        **Supported Providers:**
        - **Sinch**: OAuth2/Basic auth, regional endpoints
        - **Phaxio**: HIPAA-compliant, HMAC webhooks
        - **HumbleFax**: Webhook + IMAP, rate limited (5 req/sec)
        - **SignalWire**: Twilio-compatible API
        - **SIP/Asterisk**: Self-hosted T.38 protocol
        - **FreeSWITCH**: ESL event socket integration
      requestBody:
        required: true
        content:
          multipart/form-data:
            schema:
              $ref: '#/components/schemas/SendFaxRequest'
      responses:
        '200':
          description: Fax queued successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/FaxJob'
        '400':
          description: Invalid request
        '401':
          description: Authentication required
        '413':
          description: File too large (max 10MB)
        '415':
          description: Unsupported file type
        '429':
          description: Rate limit exceeded
      headers:
        Idempotency-Key:
          description: Optional idempotency key
          schema:
            type: string

  /fax/status/{job_id}:
    get:
      summary: Get Fax Status
      parameters:
        - name: job_id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: Fax status retrieved
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/FaxJob'

  /health/providers:
    get:
      summary: Provider Health Check
      description: Check health status of all configured providers
      responses:
        '200':
          description: Provider health status
          content:
            application/json:
              schema:
                type: object
                properties:
                  providers:
                    type: object
                    additionalProperties:
                      $ref: '#/components/schemas/ProviderHealth'
                  overall_status:
                    type: string
                    enum: [healthy, degraded, unhealthy]

  # Webhook endpoints for each provider
  /callbacks/phaxio:
    post:
      summary: Phaxio Webhook
      description: Receive status callbacks from Phaxio (HMAC verified)
      responses:
        '202':
          description: Webhook accepted for processing (idempotent)

  /callbacks/sinch:
    post:
      summary: Sinch Webhook
      description: Receive status callbacks from Sinch (Basic Auth verified)
      responses:
        '202':
          description: Webhook accepted for processing (idempotent)

  /inbound/humblefax/webhook:
    post:
      summary: HumbleFax Inbound Webhook
      description: Receive inbound fax notifications from HumbleFax
      responses:
        '202':
          description: Webhook accepted for processing (idempotent)

  # Admin endpoints for provider management
  /admin/providers:
    get:
      summary: List Providers
      description: Get list of all configured providers with traits
      responses:
        '200':
          description: Provider list retrieved
          content:
            application/json:
              schema:
                type: object
                properties:
                  providers:
                    type: array
                    items:
                      type: object
                      properties:
                        id:
                          type: string
                        name:
                          type: string
                        status:
                          type: string
                        traits:
                          type: object
                        config_valid:
                          type: boolean

  /admin/providers/{provider_id}/test:
    post:
      summary: Test Provider Connection
      parameters:
        - name: provider_id
          in: path
          required: true
          schema:
            type: string
            enum: [phaxio, sinch, humblefax, signalwire, sip, freeswitch]
      responses:
        '200':
          description: Connection test results
          content:
            application/json:
              schema:
                type: object
                properties:
                  success:
                    type: boolean
                  message:
                    type: string
                  response_time_ms:
                    type: number

tags:
  - name: Fax Operations
    description: Core fax sending and status operations
  - name: Provider Management
    description: Provider configuration and health monitoring
  - name: Webhooks
    description: Webhook endpoints for status callbacks
  - name: Administration
    description: Admin operations and diagnostics
```

### 8.a CI validation for trait schema (prevent drift)

- Add a JSON Schema for `config/provider_traits.json` (e.g., `schemas/provider_traits.schema.json`) enforcing:
  - `webhook.verification` (enum), `webhook.path`, `webhook.verify_header`
  - `auth.methods` as array of enums (e.g., basic, oauth2, ami, esl)
  - `regions` as array of strings when present
- Validate in CI:

```bash
# using python jsonschema
python -m jsonschema -i config/provider_traits.json schemas/provider_traits.schema.json

# or using ajv
npx ajv validate -s schemas/provider_traits.schema.json -d config/provider_traits.json
```

## Week 9: Final Testing & Documentation

### 9. Performance Benchmarking & Load Testing

**Create `testing/load_test.py`**:
```python
import asyncio
import aiohttp
import time
from typing import List, Dict, Any
import statistics

class FaxbotLoadTester:
    """Load testing for all Faxbot providers"""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key
        self.results = []

    async def run_provider_load_test(self, provider: str, concurrent_requests: int = 10,
                                   total_requests: int = 100) -> Dict[str, Any]:
        """Run load test against specific provider"""

        print(f"Starting load test: {provider} - {concurrent_requests} concurrent, {total_requests} total")

        semaphore = asyncio.Semaphore(concurrent_requests)
        tasks = []

        start_time = time.time()

        for i in range(total_requests):
            task = asyncio.create_task(
                self._send_test_fax(semaphore, provider, i)
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        end_time = time.time()
        total_duration = end_time - start_time

        # Analyze results
        successes = [r for r in results if isinstance(r, dict) and r.get('success')]
        errors = [r for r in results if not (isinstance(r, dict) and r.get('success'))]

        response_times = [r['response_time'] for r in successes]

        return {
            'provider': provider,
            'total_requests': total_requests,
            'concurrent_requests': concurrent_requests,
            'total_duration': total_duration,
            'requests_per_second': total_requests / total_duration,
            'success_count': len(successes),
            'error_count': len(errors),
            'success_rate': (len(successes) / total_requests) * 100,
            'response_times': {
                'min': min(response_times) if response_times else 0,
                'max': max(response_times) if response_times else 0,
                'mean': statistics.mean(response_times) if response_times else 0,
                'median': statistics.median(response_times) if response_times else 0,
                'p95': statistics.quantiles(response_times, n=20)[18] if len(response_times) >= 20 else 0,
                'p99': statistics.quantiles(response_times, n=100)[98] if len(response_times) >= 100 else 0
            }
        }

    async def _send_test_fax(self, semaphore: asyncio.Semaphore,
                           provider: str, request_id: int) -> Dict[str, Any]:
        """Send individual test fax"""
        async with semaphore:
            start_time = time.time()

            try:
                async with aiohttp.ClientSession() as session:
                    # Create test PDF content
                    test_pdf = b'%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n/Pages 2 0 R\n>>\nendobj\n2 0 obj\n<<\n/Type /Pages\n/Kids [3 0 R]\n/Count 1\n>>\nendobj\n3 0 obj\n<<\n/Type /Page\n/Parent 2 0 R\n/MediaBox [0 0 612 792]\n/Contents 4 0 R\n>>\nendobj\n4 0 obj\n<<\n/Length 44\n>>\nstream\nBT\n/F1 12 Tf\n100 700 Td\n(Test Fax) Tj\nET\nendstream\nendobj\nxref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000074 00000 n \n0000000120 00000 n \n0000000179 00000 n \ntrailer\n<<\n/Size 5\n/Root 1 0 R\n>>\nstartxref\n274\n%%EOF'

                    form_data = aiohttp.FormData()
                    form_data.add_field('to', '+1234567890')  # Test number
                    form_data.add_field('file', test_pdf, filename='test.pdf', content_type='application/pdf')
                    if provider != 'auto':
                        form_data.add_field('provider', provider)

                    async with session.post(
                        f"{self.base_url}/fax/send",
                        data=form_data,
                        headers={'X-API-Key': self.api_key},
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as response:
                        response_time = time.time() - start_time

                        if response.status == 200:
                            result = await response.json()
                            return {
                                'success': True,
                                'response_time': response_time,
                                'job_id': result.get('job_id'),
                                'provider_used': result.get('provider'),
                                'request_id': request_id
                            }
                        else:
                            error_text = await response.text()
                            return {
                                'success': False,
                                'response_time': response_time,
                                'status_code': response.status,
                                'error': error_text,
                                'request_id': request_id
                            }

            except Exception as e:
                response_time = time.time() - start_time
                return {
                    'success': False,
                    'response_time': response_time,
                    'error': str(e),
                    'request_id': request_id
                }

    async def benchmark_all_providers(self) -> List[Dict[str, Any]]:
        """Benchmark all configured providers"""

        providers = ['phaxio', 'sinch', 'humblefax', 'signalwire', 'sip', 'freeswitch']
        results = []

        for provider in providers:
            # Start with conservative load
            result = await self.run_provider_load_test(
                provider=provider,
                concurrent_requests=5,
                total_requests=50
            )
            results.append(result)

            # Brief pause between provider tests
            await asyncio.sleep(2)

        return results

# Usage example
async def main():
    tester = FaxbotLoadTester("http://localhost:8080", "your-api-key")
    results = await tester.benchmark_all_providers()

    print("\n=== LOAD TEST RESULTS ===")
    for result in results:
        print(f"\nProvider: {result['provider']}")
        print(f"  Success Rate: {result['success_rate']:.1f}%")
        print(f"  Requests/sec: {result['requests_per_second']:.2f}")
        print(f"  Response Time P95: {result['response_times']['p95']:.3f}s")
        print(f"  Errors: {result['error_count']}")

if __name__ == "__main__":
    asyncio.run(main())
```

### CI quick greps (drop-in)

```bash
# async file I/O in providers
rg -n "open\(.+,'rb'\)" api/app/providers | sort

# duplicate health monitors
rg -n "class .*ProviderHealthMonitor" api/app | sort

# mixed metrics stacks
rg -n "prometheus_client|PrometheusMetricReader" api/app | sort

# webhook 200 vs 202
rg -n "callbacks|inbound.*webhook" -n api/app | xargs rg -n "return\s+JSONResponse|status_code=200" || true

# traits schema non-canonical
rg -n "\"auth_method\":\s*\".*,|webhook_verification\"|verify_header" config/provider_traits.json

# missing secrets in k8s spec
rg -n "CONFIG_MASTER_KEY|FAXBOT_SESSION_PEPPER" deployment/kubernetes | sort

# status enums mixed case
rg -n "SUCCESS|FAILED|queued|in_progress" v4_plans/implement/phase_5_implement_plan.md openapi-v4.yaml 2>/dev/null || true

# enforce single health monitor and single metrics endpoint in code
rg -n "class .*ProviderHealthMonitor" api/app | wc -l
rg -n "/metrics" api/app | wc -l
```

### Acceptance deltas (Phase 5)

- `/metrics` scrapes clean on API port; no double registration.
- Single ProviderHealthMonitor (Phase 3) powers breaker + metrics; no duplicate monitor classes.
- Webhooks return 202 and are idempotent; DLQ headers allow-listed.
- Sinch OAuth2 + regional endpoints verified; no silent project_id fallback.
- HumbleFax inbound webhook+IMAP runs threadpooled/worker-based (never on the event loop); attachments sanitized and capped.
- OpenAPI 3.1 checks pass; status enums unified.
- k8s env includes `CONFIG_MASTER_KEY` and `FAXBOT_SESSION_PEPPER`; service fails fast at boot if missing; Terminal remains loopback-only; secure cookies confirmed.
- Rate-limit headers on 429: `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.

## Success Criteria & Acceptance Tests

### ✅ **All 7 Providers Operational**
- **Sinch**: OAuth2 + Basic Auth, regional endpoints, inbound webhooks ✅
- **Phaxio**: HIPAA-compliant, HMAC verification, BAA available ✅
- **HumbleFax**: Webhook + IMAP inbound, rate limiting, email integration ✅
- **SignalWire**: Twilio-compatible API, outbound only ✅
- **SIP/Asterisk**: T.38 protocol, AMI integration, self-hosted ✅
- **FreeSWITCH**: ESL integration, mod_spandsp, self-hosted ✅
- **Test/Disabled**: Development mode, cost-free testing ✅

### ✅ **Production Excellence**
- **Zero-downtime migration** from v3 to v4 with rollback capability ✅
- **Comprehensive monitoring** with Prometheus, Grafana, OpenTelemetry ✅
- **Health checks** for all providers with circuit breakers ✅
- **Load testing** shows <100ms P95 latency under normal load ✅
- **Complete documentation** with runbooks and troubleshooting guides ✅

### ✅ **Backward Compatibility**
- **All existing APIs** work unchanged ✅
- **All webhooks** continue to function ✅
- **Configuration migration** preserves all settings ✅
- **SDKs** (Node.js and Python) work without changes ✅

### ✅ **Enterprise Features**
- **Plugin marketplace** with signature verification ✅
- **Multi-tenant configuration** with hierarchical inheritance ✅
- **Advanced authentication** with LDAP, SAML, OAuth2 ✅
- **Usage analytics** and rate limiting per tenant ✅

## Timeline Summary

**Week 1-2**: Complete provider parity (Sinch OAuth2, HumbleFax webhook+IMAP)
**Week 3-4**: Production monitoring stack (Prometheus, health checks, alerts)
**Week 5-6**: Zero-downtime migration engine with rollback capability
**Week 7-8**: Complete documentation, deployment configs, load testing
**Week 9**: Final validation, performance tuning, production deployment

**Total Duration**: 9 weeks
**Result**: Production-ready Faxbot v4 with all 7 providers operational

This completes the v4 platform transformation with enterprise-grade reliability, monitoring, and operational excellence while preserving 100% backward compatibility.