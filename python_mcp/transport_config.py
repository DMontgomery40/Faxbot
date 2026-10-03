"""MCP transport configuration and per-request Faxbot credentials.

HTTP and SSE callers present their own Faxbot API key (X-API-Key or
Authorization: Bearer). When OAuth is configured the bearer token is a JWT and
the Faxbot key comes from X-API-Key or from a stored per-subject key file.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import os
import time

import httpx
from jose import jwt
from starlette.responses import JSONResponse


@dataclass(frozen=True)
class APIConfiguration:
    api_base_url: str
    api_key: str = field(default='', repr=False)

    def __post_init__(self):
        object.__setattr__(self, 'api_base_url', self.api_base_url.rstrip('/'))


APIConfigurationProvider = Callable[[], APIConfiguration]
CALLER_KEY_SCOPE = 'faxbot.api_key'
PROTECTED_RESOURCE_PATH = '/.well-known/oauth-protected-resource'


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


class SubjectKeyMap:
    """JSON file mapping OAuth subjects to their Faxbot API keys: {"<sub>": "<key>"}.

    The file is re-read when it changes so keys can be rotated without a restart.
    """
    def __init__(self, path: str = ''):
        self.path = path
        self._loaded = (None, {})

    def lookup(self, subject) -> str:
        if not self.path or not isinstance(subject, str) or not subject:
            return ''
        try:
            stamp = os.stat(self.path).st_mtime_ns
        except OSError:
            return ''
        if self._loaded[0] != stamp:
            try:
                with open(self.path, encoding='utf-8') as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                data = {}
            self._loaded = (stamp, data if isinstance(data, dict) else {})
        key = self._loaded[1].get(subject)
        return key.strip() if isinstance(key, str) else ''


def _header(scope, name: bytes) -> str:
    return next((value.decode('latin-1') for key, value in scope['headers'] if key.lower() == name), '').strip()


class CallerCredentialMiddleware:
    """Require a caller credential on every MCP request and record the Faxbot key to forward."""
    def __init__(self, app, *, verifier=None, subject_keys: SubjectKeyMap | None = None, issuer: str = ''):
        self.app = app
        self.verifier = verifier
        self.subject_keys = subject_keys or SubjectKeyMap()
        self.issuer = issuer

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        root = scope.get('root_path', '')
        relative_path = path[len(root):] if root and path.startswith(root + '/') else path
        if relative_path == '/health':
            await self.app(scope, receive, send)
            return
        if self.verifier is not None and relative_path == PROTECTED_RESOURCE_PATH:
            await JSONResponse(self._resource_metadata(scope))(scope, receive, send)
            return
        authorization = _header(scope, b'authorization')
        bearer = authorization[7:].strip() if authorization[:7].lower() == 'bearer ' else ''
        api_key = _header(scope, b'x-api-key')
        if self.verifier is None:
            api_key = api_key or bearer
            if not api_key:
                await self._unauthorized(scope, receive, send)
                return
        else:
            try:
                claims = await self.verifier(authorization)
            except Exception:
                await self._unauthorized(scope, receive, send)
                return
            scope.setdefault('state', {})['user'] = claims
            api_key = api_key or self.subject_keys.lookup(claims.get('sub'))
            if not api_key:
                await JSONResponse({'error': 'No Faxbot API key is linked to this account.'},
                                   status_code=403)(scope, receive, send)
                return
        scope[CALLER_KEY_SCOPE] = api_key
        await self.app(scope, receive, send)

    def _base_url(self, scope) -> str:
        host = _header(scope, b'host') or 'localhost'
        return f"{scope.get('scheme', 'http')}://{host}{scope.get('root_path', '')}"

    def _resource_metadata(self, scope) -> dict:
        return {'resource': self._base_url(scope), 'authorization_servers': [self.issuer],
                'bearer_methods_supported': ['header']}

    async def _unauthorized(self, scope, receive, send):
        challenge = 'Bearer'
        if self.verifier is not None:
            challenge += f' resource_metadata="{self._base_url(scope)}{PROTECTED_RESOURCE_PATH}"'
        await JSONResponse({'error': 'Unauthorized'}, status_code=401,
                           headers={'WWW-Authenticate': challenge})(scope, receive, send)
