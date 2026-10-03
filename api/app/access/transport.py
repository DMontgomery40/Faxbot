"""Credential transport policy. ASGI provenance is owned by the server launcher."""
from dataclasses import dataclass
import ipaddress
import re
from urllib.parse import urlsplit

from .types import AccessError

_DIRECT_LOOPBACK = object()
_LOOPBACK_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})


class TransportError(AccessError):
    code = 'credential_transport_rejected'


@dataclass(frozen=True)
class ValidatedTransport:
    secure: bool
    cookie_name: str


def single_header(scope, name):
    values = [value for key, value in scope.get('headers', ()) if key.lower() == name]
    if len(values) > 1:
        raise TransportError()
    if not values:
        return None
    value = values[0]
    if type(value) is not bytes or len(value) > 1024 * 1024:
        raise TransportError()
    return value.decode('latin-1')


def canonical_origin(value, *, allow_path=False):
    if (type(value) is not str or not 0 < len(value) <= 2048
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
            or any(c in value for c in '\\?#%')):
        raise TransportError()
    result = None
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        if (parts.scheme in {'http', 'https'} and host and parts.username is None
                and parts.password is None and (allow_path or parts.path == '')):
            if ':' in host:
                ipaddress.IPv6Address(host)
                host = '[' + host + ']'
            elif re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) is None:
                raise ValueError()
            if port == 0 or parts.netloc.endswith(':'):
                raise ValueError()
            default = 443 if parts.scheme == 'https' else 80
            result = parts.scheme + '://' + host + (f':{port}' if port is not None and port != default else '')
    except (ValueError, UnicodeError):
        pass
    if result is None:
        raise TransportError()
    return result


def credential_source(scope, cookie_name):
    key = single_header(scope, b'x-api-key')
    if key is not None:
        return 'key', key
    tokens = []
    for name, value in scope.get('headers', ()):
        if name.lower() != b'cookie':
            continue
        if type(value) is not bytes or len(value) > 16384:
            raise TransportError()
        for part in value.decode('latin-1').split(';'):
            name, separator, token = part.strip().partition('=')
            if name == cookie_name:
                if not separator:
                    raise TransportError()
                tokens.append(token)
    if len(tokens) > 1:
        raise TransportError()
    return ('session', tokens[0]) if tokens else (None, None)


class CredentialTransport:
    def __init__(self, environment):
        self.allow_loopback = environment.get('FAXBOT_ALLOW_INSECURE_LOOPBACK') == 'true'
        override = environment.get('FAXBOT_CONSOLE_ORIGINS')
        if override is None:
            self.origins = None
        else:
            if type(override) is not str or len(override) > 16384 or any(ord(c) < 32 for c in override):
                raise TransportError()
            self.origins = frozenset(canonical_origin(value.strip(' ')) for value in override.split(','))

    def validate(self, scope, *, public_url, require_origin=False):
        secure = scope.get('scheme') in {'https', 'wss'}
        if not secure:
            allowed = False
            try:
                host = single_header(scope, b'host')
                allowed = (scope.get('scheme') in {'http', 'ws'} and self.allow_loopback
                    and scope.get('faxbot.direct_loopback') is _DIRECT_LOOPBACK
                    and ipaddress.ip_address(scope['client'][0]).is_loopback
                    and urlsplit(canonical_origin('http://' + host)).hostname in _LOOPBACK_HOSTS)
            except (KeyError, TypeError, ValueError):
                pass
            if not allowed:
                raise TransportError()
        origin = single_header(scope, b'origin')
        if require_origin and origin is None:
            raise TransportError()
        if origin is not None:
            allowed = self.origins if self.origins is not None else {canonical_origin(public_url, allow_path=True)}
            if canonical_origin(origin) != origin or origin not in allowed:
                raise TransportError()
            if not secure and (urlsplit(origin).scheme != 'http' or urlsplit(origin).hostname not in _LOOPBACK_HOSTS):
                raise TransportError()
        return ValidatedTransport(secure, '__Host-faxbot_session' if secure else 'faxbot_dev_session')
