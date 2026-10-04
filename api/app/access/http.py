"""HTTP authentication adapter; services retain transaction and policy ownership."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..config import settings
from ..config_runtime import run_lifecycle_step
from .auth_work import AuthenticationBusyError
from .authentication import AuthenticationThrottledError
from .credentials import InvalidCredentialInputError
from .fax_resources import FaxAccessError
from .mutation_types import MutationDeniedError, OwnerEnrollment, StaleVersionError
from .mutations import _Graph
from .sessions import SessionCursor, SessionDeniedError
from .transport import TransportError, credential_source, single_header
from .types import AccessError, AccessUnavailableError, AuthenticationError, ResourceRef


PRIVATE_HEADERS = {'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff', 'X-Frame-Options':'DENY'}


def private_response_path(path):
    return path in {'/fax', '/inbound', '/plugins', '/plugin-registry', '/work', '/imports'} or path.startswith(
        ('/auth/', '/access/', '/admin/', '/fax/', '/inbound/', '/plugins/', '/work/', '/imports/'))


class BrowserRequestVerificationError(AccessError):
    code = 'browser_request_verification_failed'


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def private_operation(operation):
    """Never propagate SQL parameters or a backend exception to the transport."""
    @wraps(operation)
    def wrapped(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except AccessError:
            raise
        except Exception:
            pass
        raise AccessUnavailableError()
    return wrapped


KEY_OWNER_RESET = 'The owner of this key must set a new password before it can be used.'
SESSION_RESET = 'Choose a new password before continuing.'


def _reset_message(request):
    """A temporary password blocks its owner's keys and sessions; say which one was used."""
    return KEY_OWNER_RESET if request.headers.get('x-api-key') is not None else SESSION_RESET


async def access_error_response(request, error):
    headers = dict(PRIVATE_HEADERS)
    if isinstance(error, AuthenticationThrottledError):
        status, message = 429, 'Too many authentication attempts. Try again later.'
        headers['Retry-After'] = str(error.retry_after_seconds)
    elif isinstance(error, AuthenticationBusyError):
        status, message = 503, 'Authentication is temporarily unavailable.'
        headers['Retry-After'] = '1'
    elif isinstance(error, AuthenticationError):
        status, message = 401, 'Authentication required or credentials no longer valid.'
    elif isinstance(error, StaleVersionError):
        status, message = 409, 'Access policy changed. Reload and try again.'
    elif isinstance(error, BrowserRequestVerificationError):
        status, message = 403, 'Browser request verification failed. Refresh your session and try again.'
    elif isinstance(error, TransportError):
        status, message = 403, 'Credential transport or browser origin is not allowed.'
    elif isinstance(error, FaxAccessError):
        status = {'not_found':404, 'invalid_target':404, 'invalid_input':400}.get(error.code, 403)
        message = {400:'Invalid fax request.', 403:'This operation is not permitted.',
                   404:'Fax not found.'}[status]
        if error.code == 'reset_required':
            message = _reset_message(request)
    elif isinstance(error, (SessionDeniedError, MutationDeniedError)):
        code = error.code
        status = {'invalid_input':400, 'duplicate':400, 'invalid_target':404, 'stale_version':409}.get(code, 403)
        message = {400:'Invalid access request.', 404:'Access target not found.',
            409:'Access policy changed. Reload and try again.', 403:'This operation is not permitted.'}[status]
        message = {'duplicate':'That name is already in use.', 'last_owner':'The installation must keep at least one owner.',
            'owner_required':'Only an owner can do this.'}.get(code, message)
        if code == 'reset_required':
            message = _reset_message(request)
    elif isinstance(error, InvalidCredentialInputError):
        status, message = 400, 'Invalid credential input.'
    else:
        status, message = 503, 'Access service is temporarily unavailable.'
    return JSONResponse({'detail':message}, status_code=status, headers=headers)


class PrivateAuthRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def bounded(request):
            # Before FastAPI's JSON/Pydantic parser, including chunked requests.
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 1024 * 1024 + 16384:
                    return JSONResponse({'detail':'Authentication request is too large.'},
                        status_code=413, headers=PRIVATE_HEADERS)
                body.extend(chunk)
            request._body = bytes(body)
            response = await handler(request)
            response.headers.update(PRIVATE_HEADERS)
            return response
        return bounded


class PrivateAuthMiddleware:
    """Apply response privacy headers even to validation/authorization failures."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not private_response_path(scope.get('path', '')):
            return await self.app(scope, receive, send)
        async def private_send(message):
            if message['type'] == 'http.response.start':
                message = dict(message)
                fixed = {k.lower().encode():v.encode() for k, v in PRIVATE_HEADERS.items()}
                message['headers'] = [(k,v) for k,v in message.get('headers', []) if k.lower() not in fixed] + list(fixed.items())
            await send(message)
        await self.app(scope, receive, private_send)


def runtime(request):
    result = getattr(request.app.state, 'access_runtime', None)
    if result is None:
        raise AccessUnavailableError()
    return result


def transport(request, *, require_origin=False):
    return request.app.state.credential_transport.validate(request.scope,
        public_url=settings.public_api_url, require_origin=require_origin)


@dataclass(frozen=True, repr=False)
class RequestIdentity:
    actor: object
    source: str
    cookie_token: str | None = field(default=None, repr=False)


@private_operation
def _cookie_identity(service, token, csrf, unsafe):
    with service.store.transaction() as connection:
        now = utcnow()
        actor = service.sessions.authenticate_on(connection, token, now=now)
        table = service.store.tables['access_sessions']
        session_id = actor.credential.session_id
        if unsafe:
            stored = connection.execute(sa.select(table.c.csrf_hash).where(table.c.id == session_id)).scalar_one()
            if not service.session_codec.verify_csrf(token, csrf, stored):
                raise BrowserRequestVerificationError()
        # Origin/CSRF are validated before the first write; absolute expiry stays fixed.
        connection.execute(table.update().where(table.c.id == session_id).values(last_used_at=now))
    return actor


async def require_identity(request: Request,
        x_api_key: str | None = Header(default=None, description='Explicit API key takes precedence over a browser session.'),
        x_csrf_token: str | None = Header(default=None, description='Required with browser cookies for state-changing requests.')):
    existing = getattr(request.state, 'access_identity', None)
    if existing is not None:
        return existing
    selected = transport(request)
    source, value = credential_source(request.scope, selected.cookie_name)
    service = runtime(request)
    if source == 'key':
        actor = await service.work.run(lambda: service.authentication.header_key(value))
        identity = RequestIdentity(actor, 'key')
    elif source == 'session':
        unsafe = request.method not in {'GET', 'HEAD', 'OPTIONS'}
        if unsafe:
            transport(request, require_origin=True)
        csrf = single_header(request.scope, b'x-csrf-token') if unsafe else None
        actor = await run_lifecycle_step(lambda: _cookie_identity(service, value, csrf, unsafe))
        identity = RequestIdentity(actor, 'session', value)
    else:
        raise AuthenticationError()
    request.state.access_identity = identity
    return identity


class StrictInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class PasswordLogin(StrictInput):
    login: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class KeyLogin(StrictInput):
    api_key: str = Field(min_length=1, max_length=1024 * 1024, repr=False)


class PasswordChange(StrictInput):
    current_password: str = Field(min_length=1, max_length=1024, repr=False)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class SessionRevoke(StrictInput):
    expected_policy_version: int = Field(ge=1)


class OwnerEnrollmentInput(StrictInput):
    login: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    expected_policy_version: int = Field(ge=1)


class AuthOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AuthErrorResponse(AuthOutput):
    detail: str = Field(description='Fixed public error message.')


class AuthSessionResponse(AuthOutput):
    ok: Literal[True]
    password_change_required: bool


class AuthLogoutResponse(AuthOutput):
    ok: Literal[True]


class AuthPrincipalResponse(AuthOutput):
    id: str
    kind: Literal['user', 'integration', 'bootstrap']
    display_name: str
    version: int


class AuthCurrentSessionResponse(AuthOutput):
    id: str
    source_kind: Literal['password', 'key', 'bootstrap']
    expires_at: datetime


class AuthGrantableResponse(AuthOutput):
    installation: list[str] = Field(description='Permissions this actor may grant at the installation resource.')


class AuthMeResponse(AuthOutput):
    principal: AuthPrincipalResponse
    source: Literal['key', 'session']
    password_change_required: bool
    policy_version: int
    permissions: list[str]
    session: AuthCurrentSessionResponse | None
    can_enroll_owner: bool
    is_owner: bool
    grantable: AuthGrantableResponse
    csrf_token: str | None = Field(default=None, repr=False,
        description='Present only for an authenticated browser cookie session.')


class ConsoleNavigationResponse(AuthOutput):
    jobs: bool
    inbox: bool
    send: bool
    work: bool = False


class ConsoleSendResponse(AuthOutput):
    fax_disabled: bool
    max_file_size_mb: int
    default_country: str
    number_example: str


class ConsoleBrandingResponse(AuthOutput):
    docs_base: str
    logo_path: Literal['/admin/ui/faxbot_full_logo.png']


class ConsoleProviderResponse(AuthOutput):
    plugins_enabled: bool
    install_enabled: bool
    active_outbound: str
    active_inbound: str


class ConsoleContextResponse(AuthOutput):
    policy_version: int
    active_revision_id: str
    generation: int
    permissions: list[str]
    navigation: ConsoleNavigationResponse
    send: ConsoleSendResponse | None
    inbound_enabled: bool | None
    branding: ConsoleBrandingResponse
    provider_view: ConsoleProviderResponse | None


class AuthSessionSummaryResponse(AuthOutput):
    session_id: str
    source_kind: Literal['password', 'key', 'bootstrap']
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    current: bool


class AuthSessionCursorResponse(AuthOutput):
    created_at: datetime
    session_id: str


class AuthSessionPageResponse(AuthOutput):
    items: list[AuthSessionSummaryResponse]
    next_cursor: AuthSessionCursorResponse | None


class AuthSessionRevokeResponse(AuthOutput):
    session_id: str
    changed: bool
    policy_version: int


AUTH_ERROR_RESPONSES = {status: {'model': AuthErrorResponse, 'description': description}
    for status, description in (
        (400, 'Invalid credential or session request.'),
        (401, 'Authentication required or credentials no longer valid.'),
        (403, 'Transport, browser request verification or operation is not permitted.'),
        (404, 'Access target not found.'),
        (409, 'Access policy changed. Reload and try again.'),
        (413, 'Authentication request is too large.'),
        (422, 'Invalid access request.'),
        (429, 'Too many authentication attempts. Try again later.'),
        (503, 'Authentication or access service is temporarily unavailable.'),
    )}


router = APIRouter(prefix='/auth', tags=['Authentication'], route_class=PrivateAuthRoute,
    responses={status: AUTH_ERROR_RESPONSES[status] for status in (401, 403, 413, 422, 429, 503)})


def _publish_session(response, selected, result):
    receipt, prepared = result
    # Reached only after the awaited synchronous service confirms commit.
    response.set_cookie(selected.cookie_name, prepared._token_for_committed_adapter(),
        expires=receipt.expires_at.replace(tzinfo=timezone.utc), secure=selected.secure,
        httponly=True, samesite='strict', path='/')
    return {'ok':True, 'password_change_required':receipt.password_change_required}


@router.post('/login', response_model=AuthSessionResponse)
async def password_login(body: PasswordLogin, request: Request, response: Response):
    selected = transport(request, require_origin=True)
    service = runtime(request)
    result = await service.work.run(lambda: service.authentication.password_login(body.login, body.password))
    return _publish_session(response, selected, result)


@router.post('/key-login', response_model=AuthSessionResponse)
async def key_login(body: KeyLogin, request: Request, response: Response):
    selected = transport(request, require_origin=True)
    service = runtime(request)
    result = await service.work.run(lambda: service.authentication.key_login(body.api_key))
    return _publish_session(response, selected, result)


@private_operation
def _me(service, identity):
    with service.store.transaction() as connection:
        now = utcnow()
        actor = identity.actor
        source = service.control._current_source_on(connection, actor, now)
        p, r, s = (service.store.tables[name] for name in
            ('access_principals', 'access_resources', 'access_sessions'))
        principal = connection.execute(sa.select(p.c.id, p.c.kind, p.c.display_name, p.c.version)
            .where(p.c.id == actor.principal_id)).mappings().one()
        permissions = set(service.control.effective_access_on(connection, actor, ResourceRef('installation'), now=now))
        personal = connection.execute(sa.select(r.c.id).where(r.c.kind == 'personal', r.c.principal_id == actor.principal_id)).scalar_one_or_none()
        if personal is not None and service.control.authorize_on(connection, actor, 'fax:send', ResourceRef(personal), now=now).allowed:
            permissions.add('fax:send')
        session_id = getattr(actor.credential, 'session_id', None)
        session = None
        if session_id is not None:
            row = connection.execute(sa.select(s.c.id, s.c.source_kind, s.c.expires_at).where(s.c.id == session_id)).mappings().one()
            session = dict(row)
        result = {'principal':dict(principal), 'source':identity.source,
            'password_change_required':source.reset_required,
            'policy_version':service.store.require_lock_on(connection),
            'permissions':sorted(permissions), 'session':session,
            # The console's first-owner prompt: the bootstrap credential before any named Owner exists.
            # POST /auth/owner/enroll itself also accepts a complete Owner, and bootstrap recovery later.
            'can_enroll_owner':source.bootstrap and service.control.is_complete_owner_on(connection, actor, now=now)
                and not _Graph(connection, service.store.tables).owners(service.credential_codec)}
        if identity.source == 'session':
            result['csrf_token'] = service.session_codec.csrf_value(identity.cookie_token)
    result.update(service.reads.me_extras(actor))
    return result


def committed_view(read, fallback):
    """Reread a committed entity for the response; a failed reread must not lose a disclosed secret."""
    try:
        return read()
    except Exception:
        return fallback


def user_fallback(receipt, login, display_name, *, kind='user'):
    """The created or reset principal as its receipt knows it, used only when the reread failed."""
    return {'id':receipt.target.id, 'kind':kind, 'login':login if kind == 'user' else None,
            'display_name':display_name, 'enabled':True,
            'password_change_required':True if kind == 'user' else None,
            'created_at':None, 'last_login_at':None, 'version':receipt.target.version}


@router.get('/me', response_model=AuthMeResponse, response_model_exclude_unset=True)
async def me(request: Request, identity=Depends(require_identity)):
    return await run_lifecycle_step(lambda: _me(runtime(request), identity))


class AuthSetupResponse(BaseModel):
    first_owner: bool


@router.get('/setup', response_model=AuthSetupResponse, summary='Sign-in setup',
    description='Public. Whether the installation still has no named Owner, so the sign-in page asks for the '
        'installation key that creates the first one. Nothing else is disclosed; once an Owner exists it is false.')
async def sign_in_setup(request: Request):
    service = runtime(request)

    @private_operation
    def read():
        with service.store.transaction() as connection:
            return {'first_owner': not _Graph(connection, service.store.tables).owners(service.credential_codec)}
    return await run_lifecycle_step(read)


@router.get('/context', response_model=ConsoleContextResponse,
    summary='Console context',
    description='Current permission-scoped navigation and active configuration hints. '
        'These hints do not authorize later operations; each operation checks current access again.')
async def console_context(request: Request, identity=Depends(require_identity)):
    service = runtime(request)
    @private_operation
    def snapshot():
        result = service.context.snapshot(identity.actor)
        # The work queue is visible with work:read on any received document, like the Inbox.
        with service.store.transaction() as connection:
            source = service.control._current_source_on(connection, identity.actor, utcnow())
            result['navigation']['work'] = bool(not source.reset_required and service.context._has_scope_on(
                connection, identity.actor, source, 'work:read', ('mailbox', 'legacy', 'inbound')))
        return result
    return await run_lifecycle_step(snapshot)


@router.post('/logout', response_model=AuthLogoutResponse)
async def logout(request: Request, response: Response, identity=Depends(require_identity)):
    selected = transport(request)
    service = runtime(request)
    @private_operation
    def revoke():
        return service.authentication._complete(service.sessions.logout_on, identity.actor)
    await run_lifecycle_step(revoke)
    if identity.source == 'session':
        response.delete_cookie(selected.cookie_name, path='/', secure=selected.secure, httponly=True, samesite='strict')
    return {'ok':True}


@router.post('/password', response_model=AuthSessionResponse,
    responses={400: AUTH_ERROR_RESPONSES[400]})
async def change_password(body: PasswordChange, request: Request, response: Response, identity=Depends(require_identity)):
    selected = transport(request)
    service = runtime(request)
    result = await service.work.run(lambda: service.authentication.change_password(
        identity.actor, body.current_password, body.password))
    return _publish_session(response, selected, result)


@router.get('/sessions', response_model=AuthSessionPageResponse,
    responses={400: AUTH_ERROR_RESPONSES[400]})
async def sessions(request: Request, limit: int = Query(default=50, ge=1, le=100),
        cursor_time: datetime | None = None, cursor_id: str | None = Query(default=None, max_length=40),
        principal_id: str | None = Query(default=None, max_length=40,
            description="Another principal's sessions (requires sessions:read); omit for your own."),
        identity=Depends(require_identity)):
    if (cursor_time is None) != (cursor_id is None) or (cursor_time is not None and cursor_time.tzinfo is not None):
        raise HTTPException(400, detail='Invalid session cursor.')
    cursor = SessionCursor(cursor_time, cursor_id) if cursor_time is not None else None
    target = None if principal_id == identity.actor.principal_id else principal_id
    service = runtime(request)
    @private_operation
    def read():
        with service.store.transaction() as connection:
            return asdict(service.sessions.list_sessions_on(connection, identity.actor, principal_id=target,
                cursor=cursor, limit=limit, now=utcnow()))
    return await run_lifecycle_step(read)


@router.post('/sessions/{session_id}/revoke', response_model=AuthSessionRevokeResponse,
    responses={status: AUTH_ERROR_RESPONSES[status] for status in (400, 404, 409)})
async def revoke_session(session_id: str, body: SessionRevoke, request: Request,
        response: Response, identity=Depends(require_identity)):
    service = runtime(request)
    @private_operation
    def revoke():
        # _complete's clock is sampled after the access lock, including contention.
        return service.authentication._complete(lambda connection, actor, now:
            service.sessions.revoke_session_on(connection, actor, session_id,
                expected_policy_version=body.expected_policy_version, now=now), identity.actor)
    result = await run_lifecycle_step(revoke)
    if identity.source == 'session' and getattr(identity.actor.credential, 'session_id', None) == session_id:
        selected = transport(request)
        response.delete_cookie(selected.cookie_name, path='/', secure=selected.secure, httponly=True, samesite='strict')
    return asdict(result)


@router.post('/owner/enroll', responses={status: AUTH_ERROR_RESPONSES[status] for status in (400, 404, 409)},
    summary='Enroll an owner',
    description='Create a named Owner with a temporary password shown once. Allowed when /auth/me reports '
        'can_enroll_owner: the installation bootstrap credential or a complete Owner.')
async def enroll_owner(body: OwnerEnrollmentInput, request: Request, identity=Depends(require_identity)):
    service = runtime(request)
    @private_operation
    def enroll():
        prepared = service.credential_codec.prepare_temporary_password()
        receipt = service.mutations.enroll_owner(identity.actor, OwnerEnrollment(body.login, body.display_name),
            prepared, expected_policy_version=body.expected_policy_version, now=utcnow())
        return receipt, prepared
    receipt, prepared = await service.work.run(enroll)
    user = await run_lifecycle_step(lambda: committed_view(lambda: service.reads.user(identity.actor, receipt.target.id),
        user_fallback(receipt, body.login, body.display_name)))
    return {'temporary_password':prepared._temporary_secret_for_committed_adapter(), 'user':user,
            'policy_version':receipt.policy_version}
