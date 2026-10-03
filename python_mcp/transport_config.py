"""Faxbot REST location and per-request caller credentials for the MCP servers.

Network transports (Streamable HTTP, SSE) never use a shared Faxbot key. Each
MCP HTTP request must carry the caller's own Faxbot API key, either as
``Authorization: Bearer <key>`` or ``X-API-Key: <key>``. When OAuth is
configured, the Bearer token is a JWT instead, and its subject is mapped to a
Faxbot API key from an operator-maintained JSON file. Only the stdio server
uses the ``API_KEY`` environment variable, as a single integration identity.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from jose import jwt
from starlette.responses import JSONResponse

API_KEY_SCOPE = 'faxbot.api_key'
"""ASGI scope key holding the resolved caller key for one MCP HTTP request."""

MAX_REQUEST_BODY_SIZE = 16 * 1024 * 1024
"""A 10 MB fax is about 13.4 MB as base64 JSON."""


@dataclass(frozen=True)
class APIConfiguration:
    api_base_url: str
    api_key: str = field(default='', repr=False)

    def __post_init__(self):
        object.__setattr__(self, 'api_base_url', self.api_base_url.rstrip('/'))


APIConfigurationProvider = Callable[[], APIConfiguration]


@dataclass(frozen=True)
class OAuthConfiguration:
    issuer: str = ''
    audience: str = ''
    jwks_url: str = ''

    def __post_init__(self):
        issuer = self.issuer.rstrip('/')
        object.__setattr__(self, 'issuer', issuer)
        object.__setattr__(self, 'jwks_url', self.jwks_url or (f'{issuer}/.well-known/jwks.json' if issuer else ''))


class BearerTokenVerifier:
    """Each immutable OAuth identity owns its own public signing-key cache."""
    def __init__(self, configuration: OAuthConfiguration):
        self.configuration = configuration
        self.cache = {'ts': 0, 'jwks': None}
        self.cache_ttl = 300

    async def fetch_jwks(self, client: httpx.AsyncClient):
        now = time.time()
        if self.cache['jwks'] and now - self.cache['ts'] < self.cache_ttl:
            return self.cache['jwks']
        if not self.configuration.jwks_url:
            raise RuntimeError('OAUTH_JWKS_URL is not configured')
        response = await client.get(self.configuration.jwks_url, timeout=10.0)
        response.raise_for_status()
        keys = response.json()
        self.cache.update(ts=now, jwks=keys)
        return keys

    async def verify(self, auth_header: str):
        if not auth_header or not auth_header.startswith('Bearer '):
            raise ValueError('Missing bearer token')
        configuration = self.configuration
        if not configuration.issuer or not configuration.audience or not configuration.jwks_url:
            raise ValueError('OAuth settings are not configured')
        token = auth_header.split(' ', 1)[1].strip()
        header = jwt.get_unverified_header(token)
        kid = header.get('kid')
        if not kid:
            raise ValueError("Token missing 'kid' header")
        async with httpx.AsyncClient() as client:
            keys = await self.fetch_jwks(client)
        key = next((key for key in keys.get('keys', []) if key.get('kid') == kid), None)
        if key is None:
            raise ValueError('No matching JWK for token')
        return jwt.decode(
            token, key, algorithms=['RS256', 'RS384', 'RS512', 'ES256', 'ES384', 'ES512'],
            audience=configuration.audience, issuer=configuration.issuer,
            options={'verify_aud': True, 'verify_exp': True, 'verify_nbf': True},
        )


def load_subject_keys(path: str) -> dict[str, str]:
    """Read ``{"<oauth subject>": "<faxbot api key>"}`` from an operator-owned file."""
    if not path:
        return {}
    with open(path, encoding='utf-8') as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or not all(
            isinstance(subject, str) and isinstance(key, str) and key for subject, key in document.items()):
        raise ValueError('MCP_OAUTH_SUBJECT_KEYS_FILE must map OAuth subjects to Faxbot API keys.')
    return document


def env_list(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, '').split(',') if item.strip()]


def _header(scope, name: bytes) -> str:
    return next((value.decode('latin-1') for key, value in scope['headers'] if key.lower() == name), '').strip()


class CallerCredentialMiddleware:
    """Resolve the caller's Faxbot API key before any MCP message is handled.

    Requests without a usable credential are refused here, so no MCP message
    reaches a tool and nothing is forwarded to Faxbot. Browser requests are
    accepted only from the listed origins (none by default).
    """
    def __init__(self, app, *, verifier=None, subject_keys: Callable[[], Mapping[str, str]] | None = None,
                 allowed_origins: list[str] | None = None, resource_url: str = '',
                 authorization_server: str = ''):
        self.app = app
        self.verifier = verifier
        self.subject_keys = subject_keys or (lambda: {})
        self.allowed_origins = set(allowed_origins or ())
        self.resource_url = resource_url.rstrip('/')
        self.authorization_server = authorization_server
        self.metadata_url = f'{self.resource_url}/.well-known/oauth-protected-resource' if self.resource_url else ''

    def _challenge(self, status: int, error: str):
        header = 'Bearer'
        if self.metadata_url:
            header += f' resource_metadata="{self.metadata_url}"'
        return JSONResponse({'error': error}, status_code=status, headers={'WWW-Authenticate': header})

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        root = scope.get('root_path', '')
        relative = path[len(root):] if root and path.startswith(root) else path
        if relative == '/health':
            await self.app(scope, receive, send)
            return
        if relative == '/.well-known/oauth-protected-resource' and self.metadata_url:
            await JSONResponse({'resource': self.resource_url, 'authorization_servers': [self.authorization_server],
                                'bearer_methods_supported': ['header']})(scope, receive, send)
            return
        origin = _header(scope, b'origin')
        if origin and origin not in self.allowed_origins:
            await JSONResponse({'error': 'Origin not allowed'}, status_code=403)(scope, receive, send)
            return
        authorization = _header(scope, b'authorization')
        if self.verifier is not None:
            try:
                claims = await self.verifier(authorization)
            except Exception:
                await self._challenge(401, 'Unauthorized')(scope, receive, send)
                return
            api_key = self.subject_keys().get(str(claims.get('sub') or ''), '')
            if not api_key:
                await JSONResponse({'error': 'No Faxbot API key is assigned to this account.'},
                                   status_code=403)(scope, receive, send)
                return
            scope.setdefault('state', {})['user'] = claims
        else:
            api_key = authorization[7:].strip() if authorization[:7].lower() == 'bearer ' else ''
            api_key = api_key or _header(scope, b'x-api-key')
            if not api_key:
                await self._challenge(401, 'Unauthorized')(scope, receive, send)
                return
        scope[API_KEY_SCOPE] = api_key
        await self.app(scope, receive, send)


def caller_api_key(request: Any) -> str:
    """The key resolved by CallerCredentialMiddleware for the request carrying this message."""
    scope = getattr(request, 'scope', None) or {}
    return scope.get(API_KEY_SCOPE, '')
