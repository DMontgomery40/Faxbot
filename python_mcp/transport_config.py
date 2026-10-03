"""Explicit MCP configuration and per-invocation REST credentials."""
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
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
_api_configuration: ContextVar[APIConfiguration | None] = ContextVar('faxbot_mcp_api_configuration', default=None)


def current_api_configuration(default_url: str, default_key: str) -> APIConfiguration:
    configured = _api_configuration.get()
    return configured if configured is not None else APIConfiguration(default_url, default_key)


def bind_tool(function, provider: APIConfigurationProvider):
    """Hold one immutable credential frame for the complete tool invocation."""
    @wraps(function)
    async def invoke(*args, **kwargs):
        configuration = provider()
        if not isinstance(configuration, APIConfiguration):
            raise TypeError('MCP configuration provider must return APIConfiguration.')
        token = _api_configuration.set(configuration)
        try:
            return await function(*args, **kwargs)
        finally:
            _api_configuration.reset(token)

    return invoke


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


class OAuthMiddleware:
    def __init__(self, app, *, verifier):
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        root = scope.get('root_path', '')
        relative_path = path[len(root):] if root and path.startswith(root + '/') else path
        if scope['type'] != 'http' or relative_path == '/health':
            await self.app(scope, receive, send)
            return
        authorization = next((value.decode('latin-1') for key, value in scope['headers']
                              if key.lower() == b'authorization'), '')
        try:
            claims = await self.verifier(authorization)
        except Exception:
            await JSONResponse({'error': 'Unauthorized'}, status_code=401)(scope, receive, send)
            return
        scope.setdefault('state', {})['user'] = claims
        await self.app(scope, receive, send)
