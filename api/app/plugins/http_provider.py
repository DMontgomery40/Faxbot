from __future__ import annotations

import json
import re
import httpx
import h11
from dataclasses import dataclass
from typing import Any, Dict, Optional, List
from urllib.parse import urlparse


def _extract_path(obj: Any, path: str) -> Any:
    """Very small JSONPath-like extractor supporting dot and [index]."""
    try:
        cur = obj
        for part in re.split(r"\.", path.strip()):
            if not part:
                continue
            m = re.match(r"^(\w+)(\[(\d+)\])?$", part)
            if not m:
                return None
            key = m.group(1)
            idx = m.group(3)
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
            if idx is not None:
                i = int(idx)
                if isinstance(cur, list) and 0 <= i < len(cur):
                    cur = cur[i]
                else:
                    return None
        return cur
    except Exception:
        return None


def _lookup(ctx: Dict[str, Any], dotted: str) -> Any:
    cur: Any = ctx
    for part in dotted.split('.'):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


_TPL_RE = re.compile(r"{{\s*([^}\s]+)\s*}}")


def _render(template: str, ctx: Dict[str, Any]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        val = _lookup(ctx, key)
        return "" if val is None else str(val)
    return _TPL_RE.sub(repl, template or "")


@dataclass
class HttpAction:
    method: str
    url: str
    headers: Dict[str, str]
    body_kind: str  # json|form|multipart|none
    body_template: str
    path_params: List[Dict[str, str]]
    response_map: Dict[str, Any]


@dataclass
class HttpManifest:
    id: str
    name: str
    auth: Dict[str, Any]
    actions: Dict[str, HttpAction]
    allowed_domains: List[str]
    timeout_ms: int = 15000

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "HttpManifest":
        actions: Dict[str, HttpAction] = {}
        a = data.get("actions") or {}
        for key in ["send_fax", "get_status", "cancel_fax"]:
            if key in a:
                ad = a[key] or {}
                actions[key] = HttpAction(
                    method=(ad.get("method") or "POST").upper(),
                    url=ad.get("url") or "",
                    headers=ad.get("headers") or {},
                    body_kind=(ad.get("body", {}).get("kind") or "none"),
                    body_template=(ad.get("body", {}).get("template") or ""),
                    path_params=ad.get("path_params") or [],
                    response_map=ad.get("response") or {},
                )
        return HttpManifest(
            id=data.get("id") or "",
            name=data.get("name") or data.get("id") or "provider",
            auth=data.get("auth") or {"scheme": "none"},
            actions=actions,
            allowed_domains=data.get("allowed_domains") or [],
            timeout_ms=int(data.get("timeout_ms") or 15000),
        )


class HttpProviderRuntime:
    def __init__(self, manifest: HttpManifest, credentials: Dict[str, Any], settings: Dict[str, Any] | None = None):
        self.m = manifest
        self.creds = credentials or {}
        self.settings = settings or {}

    def is_configured(self) -> bool:
        """Check captured send configuration without testing provider reachability.

        Job placeholders use representative values only for URL syntax. Missing
        optional body/header settings keep their normal empty rendering; captured
        credentials and endpoint settings must be usable before dispatch.
        """
        try:
            if not isinstance(self.creds, dict) or not isinstance(self.settings, dict):
                return False
            action = self.m.actions.get('send_fax')
            if not isinstance(action, HttpAction) or not isinstance(self.m.auth, dict):
                return False
            if (not isinstance(action.method, str) or action.method.upper() not in
                    {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'TRACE', 'CONNECT'}):
                return False
            if any(name in self.m.auth and (not isinstance(self.m.auth[name], str)
                   or not self.m.auth[name].strip()) for name in ('header_name', 'query_name')):
                return False
            scheme = self.m.auth.get('scheme', 'none')
            if not isinstance(scheme, str):
                return False
            scheme = scheme.lower()
            if scheme not in {'none', 'basic', 'bearer', 'api_key_header', 'api_key_query'}:
                return False

            def credential(value):
                return isinstance(value, str) and bool(value.strip())

            # Match _apply_auth's fallback semantics, including an explicitly
            # empty basic api_key taking precedence over a password.
            if scheme == 'basic':
                if not credential(self.creds.get('username')) or not credential(
                        self.creds.get('api_key', self.creds.get('password', ''))):
                    return False
            elif scheme == 'bearer':
                if not credential(self.creds.get('api_key') or self.creds.get('token')):
                    return False
            elif scheme in {'api_key_header', 'api_key_query'}:
                if not credential(self.creds.get('api_key')):
                    return False

            context = {
                'creds': self.creds, 'settings': self.settings,
                'to': '+15550000001', 'from': '+15550000002',
                'file_url': 'https://document.invalid/fax.pdf', 'file_path': '/document.pdf',
                'job_id': 'job', 'attempt_id': 'attempt',
            }
            if action.body_kind == 'multipart':
                # The attachment marker is consumed by multipart, not _lookup.
                context['file'] = 'document'

            def reference(key, *, endpoint):
                parts = key.split('.')
                if any(not part for part in parts):
                    return False
                root = parts[0]
                if root in {'creds', 'settings'}:
                    if len(parts) < 2:
                        return False
                    value = _lookup(context, key)
                    if root == 'creds':
                        return credential(value)
                    if value is None:
                        return not endpoint
                    if type(value) not in (str, int, float, bool):
                        return False
                    return not endpoint or not isinstance(value, str) or bool(value.strip())
                return len(parts) == 1 and root in context

            def template(value, *, endpoint=False, body=False):
                if not isinstance(value, str):
                    return False
                if any(not reference(match.group(1), endpoint=endpoint)
                       for match in _TPL_RE.finditer(value)):
                    return False
                remaining = _TPL_RE.sub('', value)
                # Nested JSON may legitimately end with two closing braces.
                return '{{' not in remaining and (body or '}}' not in remaining)

            if not template(action.url, endpoint=True) or not template(action.body_template, body=True):
                return False
            if action.body_kind not in {'none', 'json', 'form', 'multipart'}:
                return False
            if not isinstance(action.headers, dict) or not isinstance(action.path_params, list):
                return False
            if any(not isinstance(name, str) or not template(value)
                   for name, value in action.headers.items()):
                return False
            url = _render(action.url, context)
            for parameter in action.path_params:
                if not isinstance(parameter, dict):
                    return False
                name = parameter.get('name')
                source = parameter.get('source', name)
                if (not isinstance(name, str) or not name.strip()
                        or not isinstance(source, str) or not reference(source, endpoint=True)):
                    return False
                url = url.replace('{' + name + '}', str(_lookup(context, source) or ''))
            if any(ord(char) <= 32 or ord(char) == 127 or char in '{}' for char in url):
                return False
            parsed = urlparse(url)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
                return False
            parsed.port
            httpx.URL(url)
            if (not isinstance(self.m.allowed_domains, list)
                    or any(not isinstance(domain, str) or not domain.strip()
                           for domain in self.m.allowed_domains)):
                return False
            self._check_domain(url)

            headers = {name: _render(value, context) for name, value in action.headers.items()}
            parameters = {}
            self._apply_auth(headers, parameters)
            # Use the same literal header grammar as the manifest validator,
            # now with captured substitutions and auth applied.
            wire_headers = httpx.Headers(headers).raw
            h11.Response(status_code=200, headers=wire_headers)
            return True
        except (TypeError, ValueError, AttributeError, RuntimeError,
                UnicodeError, httpx.InvalidURL, h11.LocalProtocolError):
            return False

    def _apply_auth(self, headers: Dict[str, str], params: Dict[str, str]) -> None:
        scheme = (self.m.auth.get("scheme") or "none").lower()
        if scheme == "none":
            return
        if scheme == "basic":
            import base64
            user = self.creds.get("username", "")
            pwd = self.creds.get("api_key", self.creds.get("password", ""))
            token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
            return
        if scheme == "bearer":
            token = self.creds.get("api_key") or self.creds.get("token") or ""
            headers["Authorization"] = f"Bearer {token}"
            return
        if scheme == "api_key_header":
            name = self.m.auth.get("header_name") or "X-API-Key"
            headers[name] = self.creds.get("api_key") or ""
            return
        if scheme == "api_key_query":
            name = self.m.auth.get("query_name") or "api_key"
            params[name] = self.creds.get("api_key") or ""

    def _check_domain(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        if self.m.allowed_domains:
            if host not in self.m.allowed_domains:
                raise RuntimeError('Provider endpoint is outside its captured allowlist.') from None

    async def send_fax(self, *, to: str, file_url: Optional[str] = None, file_path: Optional[str] = None, from_number: Optional[str] = None, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
        act = self.m.actions.get("send_fax")
        if not act:
            raise RuntimeError("Manifest missing send_fax action")
        ctx = {
            "to": to,
            "from": from_number,
            "file_url": file_url,
            "file_path": file_path,
            "settings": self.settings,
            "creds": self.creds,
        }
        # Extra locators cannot replace captured credentials/settings/documents.
        ctx.update({key: (extra or {}).get(key) for key in ('job_id', 'attempt_id')})
        # URL + path params
        url = _render(act.url, ctx)
        for pp in (act.path_params or []):
            name = str(pp.get("name") or "")
            src = str(pp.get("source") or name)
            val = _lookup(ctx, src)
            url = url.replace("{" + name + "}", str(val or ""))
        self._check_domain(url)

        headers = {name: _render(value, ctx) for name, value in (act.headers or {}).items()}
        params: Dict[str, str] = {}
        self._apply_auth(headers, params)

        body_data: Any = None
        files: Any = None
        if act.body_kind == "json":
            rendered = _render(act.body_template, ctx)
            try:
                body_data = json.loads(rendered) if (rendered or "").strip().startswith("{") else {}
            except (TypeError, ValueError):
                raise RuntimeError('Provider request template could not be rendered.') from None
        elif act.body_kind == "form":
            # Expect template like: key1={{ var }}&key2={{ var2 }}
            rendered = _render(act.body_template, ctx)
            pairs = [kv for kv in (rendered.split("&") if rendered else []) if kv]
            body_data = {}
            for kv in pairs:
                k, _, v = kv.partition("=")
                body_data[k] = v
        elif act.body_kind == "multipart":
            # Support a simple query-like template where a special key 'attachment' or 'file'
            # indicates the binary PDF part. Example:
            #   request={"to":[{"phoneNumber":"{{to}}"}]}&attachment={{file}}
            rendered = _render(act.body_template, ctx)
            pairs = [kv for kv in (rendered.split("&") if rendered else []) if kv]
            form_fields: Dict[str, str] = {}
            attach_key: Optional[str] = None
            for kv in pairs:
                k, _, v = kv.partition("=")
                if k.lower() in {"attachment", "file", "document"}:
                    attach_key = k
                    # value handled below
                else:
                    form_fields[k] = v
            body_data = form_fields
            # The prepared local artifact wins; never switch to a token URL when
            # the supplied file cannot be read, or submit a missing attachment.
            file_bytes: Optional[bytes] = None
            filename = "fax.pdf"
            try:
                if attach_key and file_path:
                    with open(file_path, 'rb') as f:
                        file_bytes = f.read()
                    filename = file_path.rsplit('/', 1)[-1] or filename
                elif attach_key and file_url:
                    filename = (urlparse(file_url).path.rsplit('/', 1)[-1] or filename)
                    async with httpx.AsyncClient(timeout=httpx.Timeout(self.m.timeout_ms / 1000.0)) as client:
                        r = await client.get(str(file_url))
                        r.raise_for_status()
                        file_bytes = r.content
            except (OSError, httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError):
                raise RuntimeError('Provider attachment could not be read.') from None
            if attach_key and file_bytes:
                files = {attach_key: (filename, file_bytes, 'application/pdf')}
            elif attach_key:
                raise RuntimeError('Provider attachment is required.') from None
            else:
                files = None
        elif act.body_kind == "none":
            pass
        else:
            raise RuntimeError('Unsupported provider request body.') from None

        timeout = httpx.Timeout(self.m.timeout_ms / 1000.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if act.body_kind == "multipart":
                    resp = await client.request(act.method, url, headers=headers, params=params, data=body_data, files=files)
                elif act.body_kind == "form":
                    resp = await client.request(act.method, url, headers=headers, params=params, data=body_data)
                else:
                    resp = await client.request(act.method, url, headers=headers, params=params, json=body_data, files=files)
        except (httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError, OSError):
            raise RuntimeError('Provider create request failed.') from None
        return self._mapped_response(resp, act)

    def _mapped_response(self, resp: httpx.Response, act: HttpAction, provider_sid: Optional[str] = None,
                         *, require_status: bool = False) -> Dict[str, Any]:
        if not 200 <= resp.status_code < 300:
            raise RuntimeError(f'Provider request failed (HTTP {resp.status_code}).') from None
        try:
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError
            rm = act.response_map or {}
            job_id_expr = rm.get('job_id') or 'id'
            status_expr = rm.get('status') or 'status'
            jid = _extract_path(data, job_id_expr) if isinstance(job_id_expr, str) else None
            status = _extract_path(data, status_expr) if isinstance(status_expr, str) else None
            if jid is not None and (not isinstance(jid, (str, int)) or isinstance(jid, bool)):
                raise ValueError
            if status is not None and not isinstance(status, str):
                raise ValueError
            if require_status and (not isinstance(status, str) or not status.strip()):
                raise ValueError
            if isinstance(rm.get('status_map'), dict) and status in rm['status_map']:
                status = rm['status_map'][status]
            if status is not None and not isinstance(status, str):
                raise ValueError
            if require_status and (not isinstance(status, str) or not status.strip()):
                raise ValueError
            result = {'provider_id': self.m.id, 'job_id': jid or provider_sid or '', 'status': status or 'queued'}
            if rm.get('error') and _extract_path(data, rm['error']):
                result['error'] = 'Provider reported an error.'
            return result
        except (TypeError, ValueError, AttributeError):
            raise RuntimeError('Unexpected provider response.') from None

    async def get_status(self, *, job_id: Optional[str] = None, provider_sid: Optional[str] = None, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Poll status via manifest get_status action (if defined)."""
        act = self.m.actions.get("get_status")
        if not act:
            raise RuntimeError("Manifest missing get_status action")
        ctx = {
            "job_id": job_id or provider_sid,
            "provider_sid": provider_sid,
            "settings": self.settings,
            "creds": self.creds,
        }
        # URL + path params
        url = _render(act.url, ctx)
        for pp in (act.path_params or []):
            name = str(pp.get("name") or "")
            src = str(pp.get("source") or name)
            val = _lookup(ctx, src)
            url = url.replace("{" + name + "}", str(val or ""))
        self._check_domain(url)

        headers = {name: _render(value, ctx) for name, value in (act.headers or {}).items()}
        params: Dict[str, str] = {}
        self._apply_auth(headers, params)

        body_data: Any = None
        if act.body_kind == "json":
            rendered = _render(act.body_template, ctx)
            try:
                body_data = json.loads(rendered) if (rendered or "").strip().startswith("{") else {}
            except (TypeError, ValueError):
                raise RuntimeError('Provider request template could not be rendered.') from None
        elif act.body_kind == "form":
            rendered = _render(act.body_template, ctx)
            pairs = [kv for kv in (rendered.split("&") if rendered else []) if kv]
            body_data = {}
            for kv in pairs:
                k, _, v = kv.partition("=")
                body_data[k] = v
        elif act.body_kind == "none":
            pass
        else:
            raise RuntimeError('Unsupported provider request body.') from None

        timeout = httpx.Timeout(self.m.timeout_ms / 1000.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if act.body_kind == 'form':
                    resp = await client.request(act.method, url, headers=headers, params=params, data=body_data)
                else:
                    resp = await client.request(act.method, url, headers=headers, params=params, json=body_data)
        except (httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError, OSError):
            raise RuntimeError('Provider status request failed.') from None
        return self._mapped_response(resp, act, provider_sid, require_status=True)
