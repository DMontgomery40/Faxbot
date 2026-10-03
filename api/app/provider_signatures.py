"""Pure callback verification using captured public URLs and callback secrets.

Phaxio: https://www.phaxio.com/docs/security/callbacks
SignalWire Scheme B, pinned official Python validator (3.5.1):
https://github.com/signalwire/signalwire-python/blob/6bdf2ebee27e591690cf67dd2b1ed75218f798d2/signalwire/signalwire/core/security/webhook_validator.py
No environment, current configuration, or request/proxy headers are consulted.
"""
import base64
from collections.abc import Sequence
import hashlib
import hmac
import re
from urllib.parse import SplitResult, urlsplit, urlunsplit


def _public_url(value: str) -> SplitResult | None:
    if not isinstance(value, str) or not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or '#' in value):
        return None
    # Accessing port rejects invalid syntax and out-of-range values.
    parsed.port
    value.encode('utf-8')
    return parsed


def _pairs(values, value_type):
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return None
    pairs = []
    for pair in values:
        if (not isinstance(pair, Sequence) or isinstance(pair, (str, bytes, bytearray))
                or len(pair) != 2 or not isinstance(pair[0], str) or not isinstance(pair[1], value_type)):
            return None
        pairs.append((pair[0], pair[1]))
    # Stable sorting retains wire order for repeated names and never mutates input.
    return sorted(pairs, key=lambda pair: pair[0])


def _signalwire_urls(original: str, parsed: SplitResult) -> tuple[str, ...]:
    standard = {'http': 80, 'https': 443}[parsed.scheme]
    if parsed.port is not None and parsed.port != standard:
        return (original,)
    hostname = parsed.hostname
    if ':' in hostname:
        hostname = '[' + hostname + ']'
    port = ':' + str(standard) if parsed.port is None else ''
    alternate = urlunsplit((parsed.scheme, hostname + port, parsed.path, parsed.query, parsed.fragment))
    return (original, alternate)


def verify_phaxio_signature(
    callback_token: str,
    callback_url: str,
    fields: Sequence[tuple[str, str]],
    files: Sequence[tuple[str, bytes]],
    signature: str,
) -> bool:
    """Verify Phaxio's lowercase hex SHA1 HMAC with its distinct Callback Token.

    URL is the exact submitted public URL, including query/trailing slash.
    Ordered repeated fields/file parts are retained; file content uses SHA1 hex.
    Malformed input fails closed. The send API secret is not a substitute token.
    """
    try:
        if (not isinstance(callback_token, str) or not callback_token or not isinstance(signature, str)
                or re.fullmatch(r'[0-9a-f]{40}', signature) is None or _public_url(callback_url) is None):
            return False
        parameters, attachments = _pairs(fields, str), _pairs(files, bytes)
        if parameters is None or attachments is None:
            return False
        message = callback_url + ''.join(name + value for name, value in parameters)
        message += ''.join(name + hashlib.sha1(content).hexdigest() for name, content in attachments)
        expected = hmac.new(callback_token.encode('utf-8'), message.encode('utf-8'), hashlib.sha1).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError):
        return False


def verify_signalwire_signature(
    signing_key: str,
    callback_url: str,
    fields: Sequence[tuple[str, str]],
    signature: str,
) -> bool:
    """Verify Compatibility form Scheme B (UTF-8 SHA1 HMAC, standard base64).

    Caller supplies the captured public URL. Only default 80/443 port variants
    are tried, per the pinned Python source; nonstandard ports remain bound.
    Raw JSON/SWML schemes and request header reconstruction are outside this API.
    """
    try:
        if (not isinstance(signing_key, str) or not signing_key or not isinstance(signature, str)
                or re.fullmatch(r'[A-Za-z0-9+/]{27}=', signature) is None):
            return False
        parsed, parameters = _public_url(callback_url), _pairs(fields, str)
        if parsed is None or parameters is None:
            return False
        suffix = ''.join(name + value for name, value in parameters)
        key = signing_key.encode('utf-8')
        matched = False
        for url in _signalwire_urls(callback_url, parsed):
            digest = hmac.new(key, (url + suffix).encode('utf-8'), hashlib.sha1).digest()
            expected = base64.b64encode(digest).decode('ascii')
            matched |= hmac.compare_digest(expected, signature)
        return matched
    except (TypeError, ValueError):
        return False
