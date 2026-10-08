"""Sign-in tokens for XOAUTH2 (OAuth 2.0 bearer tokens over IMAP and SMTP).

Three ways the administrator can let Faxbot sign in without a password:

- Microsoft 365 app (client credentials): POST to
  ``https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`` with scope
  ``https://outlook.office365.com/.default``.
- Google service account with domain-wide delegation: an RS256-signed JWT
  (``iss`` the service account, ``sub`` the mailbox, scope
  ``https://mail.google.com/``, ``aud`` the token address) exchanged at
  ``https://oauth2.googleapis.com/token`` (RFC 7523 JWT bearer grant).
- Any other service: a refresh token, client ID and client secret exchanged at
  the token address the administrator enters (https only).

The XOAUTH2 initial response is ``user=<mailbox>^Aauth=Bearer <token>^A^A``
(^A is byte 0x01). imaplib and smtplib base64-encode it once. Tokens are kept
in memory only, until a minute before they expire, and never logged.
"""
import base64
import json
import threading
import time

from .settings import service_account


MICROSOFT_TOKEN = 'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token'
MICROSOFT_SCOPE = 'https://outlook.office365.com/.default'
GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token'
GOOGLE_SCOPE = 'https://mail.google.com/'
JWT_BEARER = 'urn:ietf:params:oauth:grant-type:jwt-bearer'


class TokenUnavailable(RuntimeError):
    """One plain sentence; never the response body, the secret or the token."""


def xoauth2(user, token):
    """The XOAUTH2 initial client response, before base64 (imaplib and smtplib encode it)."""
    return f'user={user}\x01auth=Bearer {token}\x01\x01'


def _post(url, data, *, timeout=20.0):
    import httpx
    try:
        response = httpx.post(url, data=data, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError:
        raise TokenUnavailable('the sign-in service could not be reached.') from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def google_assertion(key, user, *, now=None, scope=GOOGLE_SCOPE):
    """An RS256-signed JWT asking for ``user``'s mailbox on behalf of the service account."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    parts = service_account(key)
    issued = int(now if now is not None else time.time())
    header = {'alg': 'RS256', 'typ': 'JWT', **({'kid': parts['private_key_id']} if parts['private_key_id'] else {})}
    claims = {'iss': parts['client_email'], 'sub': user, 'scope': scope, 'aud': GOOGLE_TOKEN, 'iat': issued,
              'exp': issued + 3600}
    signing_input = (_b64(json.dumps(header, separators=(',', ':')).encode()) + '.'
                     + _b64(json.dumps(claims, separators=(',', ':')).encode()))
    try:
        private = serialization.load_pem_private_key(parts['private_key'].encode(), password=None)
    except (ValueError, TypeError):
        raise TokenUnavailable('the service account key could not be read.') from None
    signature = private.sign(signing_input.encode('ascii'), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + '.' + _b64(signature)


class Tokens:
    """Fetch and cache one access token per connector; ``post`` is replaceable in tests."""

    def __init__(self, *, post=_post, clock=time.time):
        self.post, self.clock = post, clock
        self._cache = {}
        self._lock = threading.Lock()

    def forget(self, source_id):
        with self._lock:
            self._cache.pop(source_id, None)

    def token(self, source_id, settings, secret):
        with self._lock:
            cached = self._cache.get(source_id)
            if cached and cached[1] > self.clock() + 60:
                return cached[0]
        token, lifetime = self._fetch(settings, secret)
        with self._lock:
            self._cache[source_id] = (token, self.clock() + max(60, lifetime))
        return token

    def _fetch(self, settings, secret):
        method = settings['sign_in']
        if method == 'microsoft_app':
            url = MICROSOFT_TOKEN.format(tenant=settings['tenant_id'])
            data = {'grant_type': 'client_credentials', 'client_id': settings['client_id'],
                    'client_secret': secret['client_secret'], 'scope': MICROSOFT_SCOPE}
        elif method == 'google_service_account':
            url = GOOGLE_TOKEN
            data = {'grant_type': JWT_BEARER, 'assertion': google_assertion(secret['service_account'],
                                                                            settings['username'])}
        elif method == 'oauth_refresh':
            url = settings['token_url']
            if not url.startswith('https://'):
                raise TokenUnavailable('the token address must start with https://.')
            data = {'grant_type': 'refresh_token', 'client_id': settings['client_id'],
                    'client_secret': secret['client_secret'], 'refresh_token': secret['refresh_token']}
        else:
            raise TokenUnavailable('this connector signs in with a password.')
        status, body = self.post(url, data)
        token = body.get('access_token')
        if status != 200 or not isinstance(token, str) or not token:
            if status in (400, 401, 403):
                raise TokenUnavailable('the sign-in service refused the app, key or secret.')
            raise TokenUnavailable('the sign-in service did not give a token.')
        lifetime = body.get('expires_in')
        return token, int(lifetime) if isinstance(lifetime, (int, float)) and lifetime > 0 else 3000
