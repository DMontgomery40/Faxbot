from __future__ import annotations

import json
import re
import httpx
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
                raise RuntimeError(f"Host {host} not in allowlist")

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
        # URL + path params
        url = act.url
        for pp in (act.path_params or []):
            name = str(pp.get("name") or "")
            src = str(pp.get("source") or name)
            val = _lookup(ctx, src)
            url = url.replace("{" + name + "}", str(val or ""))
        self._check_domain(url)

        headers = dict(act.headers or {})
        params: Dict[str, str] = {}
        self._apply_auth(headers, params)

        body_data: Any = None
        files: Any = None
        if act.body_kind == "json":
            rendered = _render(act.body_template, ctx)
            body_data = json.loads(rendered) if (rendered or "").strip().startswith("{") else {}
        elif act.body_kind == "form":
            # Expect template like: key1={{ var }}&key2={{ var2 }}
            rendered = _render(act.body_template, ctx)
            pairs = [kv for kv in (rendered.split("&") if rendered else []) if kv]
            for kv in pairs:
                k, _, v = kv.partition("=")
                params[k] = v
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
            # Attach file bytes by downloading file_url (preferred) or reading local path
            file_bytes: Optional[bytes] = None
            filename = "fax.pdf"
            if file_url:
                filename = (urlparse(file_url).path.rsplit('/', 1)[-1] or filename)
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.m.timeout_ms / 1000.0)) as client:
                    r = await client.get(str(file_url))
                    r.raise_for_status()
                    file_bytes = r.content
            elif file_path:
                try:
                    with open(file_path, 'rb') as f:
                        file_bytes = f.read()
                    filename = file_path.rsplit('/', 1)[-1] or filename
                except Exception:
                    file_bytes = None
            if attach_key and file_bytes is not None:
                files = {attach_key: (filename, file_bytes, 'application/pdf')}
            else:
                files = None
        elif act.body_kind == "none":
            pass
        else:
            raise RuntimeError(f"Unsupported body.kind: {act.body_kind}")

        timeout = httpx.Timeout(self.m.timeout_ms / 1000.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if act.body_kind == "multipart":
                resp = await client.request(act.method, url, headers=headers, params=params, data=body_data, files=files)
            elif act.body_kind == "form":
                resp = await client.request(act.method, url, headers=headers, params=params, data=params)
            else:
                resp = await client.request(act.method, url, headers=headers, params=params, json=body_data, files=files)
        try:
            data = resp.json()
        except Exception:
            data = {"status_code": resp.status_code, "text": resp.text}

        rm = act.response_map or {}
        job_id_expr = rm.get("job_id") or "id"
        status_expr = rm.get("status") or "status"
        error_expr = rm.get("error")
        job_id = _extract_path(data, job_id_expr) if isinstance(job_id_expr, str) else None
        status = _extract_path(data, status_expr) if isinstance(status_expr, str) else None
        if isinstance(rm.get("status_map"), dict) and status in rm["status_map"]:
            status = rm["status_map"][status]
        result = {
            "provider_id": self.m.id,
            "job_id": job_id or "",
            "status": status or ("FAILED" if resp.status_code >= 400 else "queued"),
        }
        if error_expr:
            err = _extract_path(data, error_expr)
            if err:
                result["error"] = err
        return result

    async def get_status(self, *, job_id: Optional[str] = None, provider_sid: Optional[str] = None, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Poll status via manifest get_status action (if defined)."""
        act = self.m.actions.get("get_status")
        if not act:
            raise RuntimeError("Manifest missing get_status action")
        ctx = {
            "job_id": job_id or provider_sid,
            "provider_sid": provider_sid or job_id,
            "settings": self.settings,
            "creds": self.creds,
        }
        # URL + path params
        url = act.url
        for pp in (act.path_params or []):
            name = str(pp.get("name") or "")
            src = str(pp.get("source") or name)
            val = _lookup(ctx, src)
            url = url.replace("{" + name + "}", str(val or ""))
        self._check_domain(url)

        headers = dict(act.headers or {})
        params: Dict[str, str] = {}
        self._apply_auth(headers, params)

        body_data: Any = None
        if act.body_kind == "json":
            rendered = _render(act.body_template, ctx)
            body_data = json.loads(rendered) if (rendered or "").strip().startswith("{") else {}
        elif act.body_kind == "form":
            rendered = _render(act.body_template, ctx)
            pairs = [kv for kv in (rendered.split("&") if rendered else []) if kv]
            for kv in pairs:
                k, _, v = kv.partition("=")
                params[k] = v
        elif act.body_kind == "none":
            pass
        else:
            raise RuntimeError(f"Unsupported body.kind: {act.body_kind}")

        timeout = httpx.Timeout(self.m.timeout_ms / 1000.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(act.method, url, headers=headers, params=params, json=body_data)
        try:
            data = resp.json()
        except Exception:
            data = {"status_code": resp.status_code, "text": resp.text}

        rm = act.response_map or {}
        job_id_expr = rm.get("job_id") or "id"
        status_expr = rm.get("status") or "status"
        error_expr = rm.get("error")
        jid = _extract_path(data, job_id_expr) if isinstance(job_id_expr, str) else None
        status = _extract_path(data, status_expr) if isinstance(status_expr, str) else None
        if isinstance(rm.get("status_map"), dict) and status in rm["status_map"]:
            status = rm["status_map"][status]
        result = {
            "provider_id": self.m.id,
            "job_id": jid or (job_id or provider_sid or ""),
            "status": status or ("FAILED" if resp.status_code >= 400 else "queued"),
        }
        if error_expr:
            err = _extract_path(data, error_expr)
            if err:
                result["error"] = err
        return result
