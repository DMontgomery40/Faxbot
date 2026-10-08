"""Diverted calls (X4): the number a received call was forwarded from, and how far Faxbot could check it.

When a carrier forwards a call to one of your fax numbers, the network may say
where it came from in three ways, all read here from the received call:

- **Diversion** (RFC 5806): ``Diversion: <sip:+13035550100@carrier>;reason=
  unconditional;counter=1``, a list with the most recent diverting party first.
- **History-Info** (RFC 7044): the call's targets in order, each with an index;
  the entry that was retargeted carries a Reason with its cause (302, 486, ...),
  and the new target names it with ``mp`` or ``rc``.
- **A diversion PASSporT** (RFC 8946): an Identity header (RFC 8224) whose
  PASSporT (RFC 8225) has ``ppt`` "div" and a ``div`` claim naming the number
  it was forwarded from, signed by the forwarding provider with ES256 under a
  certificate it publishes at ``x5u`` (STIR/SHAKEN, RFC 8588, ATIS-1000074).

A diversion is the network's statement about the call, never proof of who
sent the fax. Faxbot records how far it checked it (``state``):

- ``signed`` (verified): the PASSporT's ES256 signature checks against the
  certificate it names, the certificate is valid at the call, the PASSporT is
  fresh (60 s) and its ``dest`` is the number that received the call, and that
  certificate chains to a STIR/SHAKEN certificate authority you trust
  (inbound/trust.py; RFC 8224 section 6.2.2, ATIS-1000074).
- ``unanchored``: all of that, but the certificate does not chain to a
  certificate authority you trust, or you trust none yet. The URL is the
  caller's choice, so anyone with a web server could make one: it is not
  verified, and the sentence says so.
- ``unchecked``: signed, but not checked (no receiving rule needs it, the
  certificate could not be fetched, or the compact form carries no claims).
- ``failed``: its signature, time or destination did not check. It never
  matches a rule.
- ``stated``: only a Diversion or History-Info header says so (unsigned).

Asterisk writes the headers of a received call that has any of them into
``<fax data>/inbound/<call token>.sip`` (one line each: the header name and its
value in base64; ``extensions.conf``, ``[faxbot-sip-headers]``), so both fax
engines' hand-overs read them by the call's token. The certificate is fetched
only when an enabled receiving rule depends on the diversion, only over HTTPS
from a public address, without redirects, at most ``CERT_BYTES`` and
``FETCH_SECONDS``, and kept for an hour.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import logging
from pathlib import Path
import re
import socket
import time
from urllib.parse import unquote, urlsplit

FRESH_SECONDS = 60
CERT_BYTES = 16 * 1024
FETCH_SECONDS = 3.0
CACHE_SECONDS = 3600
FAILED_CACHE_SECONDS = 300
HEADERS_BYTES = 64 * 1024
HEADER_NAMES = ('Diversion', 'History-Info', 'Identity')
SIGNED, UNANCHORED, UNCHECKED, FAILED, STATED = 'signed', 'unanchored', 'unchecked', 'failed', 'stated'
_TOKEN = re.compile(r'[0-9]{1,40}')
_USER = re.compile(r'(?:sips?|tel):\+?([0-9][0-9\-.() ]{2,31})', re.IGNORECASE)
_CACHE: dict = {}


@dataclass(frozen=True)
class Diversion:
    """Where a received call was forwarded from, how it was said, and how far that was checked."""
    diverted_from: str | None
    state: str
    source: str                 # 'passport', 'diversion' or 'history-info'
    reason: str | None = None   # Diversion's reason (unconditional, user-busy, no-answer, ...), when given
    sentence: str = ''


# -- reading the call's headers -------------------------------------------------------------------------------

def call_token(uniqueid) -> str | None:
    """The digits of the call's Asterisk UNIQUEID (the file name Asterisk used): "1791391994.7" (built-in engine)
    and "engine.17913919947" (the SSL Fax engine's hand-over) both give "17913919947"."""
    text = str(uniqueid or '')
    if text.startswith('engine.'):
        text = text[len('engine.'):]
    digits = ''.join(character for character in text if character.isdigit())
    return digits if _TOKEN.fullmatch(digits) and re.fullmatch(r'[0-9.]{1,41}', text) else None


def headers_path(data_dir, token) -> Path | None:
    if not token or not _TOKEN.fullmatch(token):
        return None
    return Path(data_dir) / 'inbound' / f'{token}.sip'


def read_headers(data_dir, uniqueid) -> dict:
    """{header name: [values]} Asterisk kept for this call; {} when it kept none (no file, or not readable)."""
    path = headers_path(data_dir, call_token(uniqueid))
    if path is None:
        return {}
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        data = path.read_bytes()[:HEADERS_BYTES]
    except OSError:
        return {}
    return parse_header_lines(data.decode('ascii', 'replace'))


def parse_header_lines(text) -> dict:
    found = {}
    for line in text.splitlines():
        name, _, encoded = line.strip().partition(' ')
        if name not in HEADER_NAMES or not encoded:
            continue
        try:
            value = base64.b64decode(encoded, validate=True).decode('utf-8', 'replace')
        except (binascii.Error, ValueError):
            continue
        found.setdefault(name, []).append(value[:8192])
    return found


def forget_headers(data_dir, uniqueid):
    """Remove the call's header file once the fax holds what it needed."""
    path = headers_path(data_dir, call_token(uniqueid))
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# -- Diversion (RFC 5806) and History-Info (RFC 7044) ------------------------------------------------------------

def _split_entries(value):
    """Comma-separated header entries, keeping commas inside <...> and quotes."""
    entries, current, depth, quoted = [], [], 0, False
    for character in value:
        if character == '"':
            quoted = not quoted
        elif character == '<' and not quoted:
            depth += 1
        elif character == '>' and not quoted:
            depth = max(0, depth - 1)
        if character == ',' and not depth and not quoted:
            entries.append(''.join(current).strip())
            current = []
            continue
        current.append(character)
    if ''.join(current).strip():
        entries.append(''.join(current).strip())
    return entries


def _uri_and_params(entry):
    match = re.search(r'<([^>]*)>(.*)$', entry)
    if match:
        uri, rest = match.group(1), match.group(2)
    else:
        uri, _, rest = entry.partition(';')
        rest = ';' + rest if rest else ''
    params = {}
    for part in rest.split(';')[1:]:
        key, _, value = part.partition('=')
        params[key.strip().lower()] = value.strip().strip('"')
    return uri.strip(), params


def _user(uri):
    match = _USER.search(uri or '')
    if not match:
        return None
    digits = ''.join(character for character in match.group(1) if character.isdigit())
    if not 3 <= len(digits) <= 20:
        return None
    return ('+' if '+' in uri.split('@')[0] else '') + digits


def parse_diversion(values):
    """(number, reason) of the most recent diverting party, or (None, None)."""
    for value in values or ():
        for entry in _split_entries(value):
            uri, params = _uri_and_params(entry)
            number = _user(uri)
            if number:
                return number, (params.get('reason') or None)
    return None, None


def _index(text):
    try:
        return tuple(int(part) for part in str(text).split('.'))
    except ValueError:
        return None


def parse_history_info(values):
    """The number the call was retargeted from, from History-Info entries, or None."""
    entries = []
    for value in values or ():
        for entry in _split_entries(value):
            uri, params = _uri_and_params(entry)
            index = _index(params.get('index'))
            if index is None:
                continue
            target, _, headers = uri.partition('?')
            entries.append({'index': index, 'number': _user(target), 'params': params,
                            'cause': 'cause' in unquote(headers).lower()})
    if len(entries) < 2:
        return None
    entries.sort(key=lambda item: item['index'])
    by_index = {item['index']: item for item in entries}
    last = entries[-1]
    for tag in ('mp', 'rc'):
        parent = by_index.get(_index(last['params'].get(tag))) if last['params'].get(tag) else None
        if parent is not None and parent['number'] and parent['number'] != last['number']:
            return parent['number']
    caused = [item for item in entries[:-1] if item['cause'] and item['number']]
    if caused:
        return caused[-1]['number']
    earlier = [item for item in entries[:-1] if item['number'] and item['number'] != last['number']]
    return earlier[-1]['number'] if earlier else None


# -- the diversion PASSporT (RFC 8946, 8225, 8224) -------------------------------------------------------------------

def _b64url(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


@dataclass(frozen=True)
class Passport:
    header: dict
    claims: dict
    signing_input: bytes
    signature: bytes
    info: str | None


def parse_identity(value):
    """The PASSporT in one Identity header (full form), or None for anything else, including the compact form."""
    token, _, rest = (value or '').strip().partition(';')
    params = {}
    for part in rest.split(';'):
        key, _, item = part.partition('=')
        params[key.strip().lower()] = item.strip().strip('"').strip('<>')
    pieces = token.strip().split('.')
    if len(pieces) != 3 or not pieces[0] or not pieces[1]:
        return None
    try:
        header, claims = json.loads(_b64url(pieces[0])), json.loads(_b64url(pieces[1]))
        signature = _b64url(pieces[2])
    except (ValueError, binascii.Error):
        return None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return None
    return Passport(header, claims, f'{pieces[0]}.{pieces[1]}'.encode('ascii'), signature, params.get('info'))


def div_passports(values):
    """Every diversion PASSporT among the call's Identity headers, the most recent diversion first, and whether a
    compact one (claims not carried) was seen."""
    found, compact = [], False
    for value in values or ():
        passport = parse_identity(value)
        if passport is None:
            if 'ppt=div' in (value or '').replace('"', '').replace(' ', ''):
                compact = True
            continue
        if passport.header.get('ppt') == 'div' and isinstance(passport.claims.get('div'), dict):
            found.append(passport)
    found.sort(key=lambda passport: passport.claims.get('iat') or 0, reverse=True)
    return found, compact


def _tn(value):
    if isinstance(value, list):
        value = value[0] if value else None
    digits = ''.join(character for character in str(value or '') if character.isdigit())
    return digits or None


def _public_host(host):
    """Every address ``host`` resolves to is a public one (the certificate URL is the caller's choice)."""
    from ..direct.addresses import public
    try:
        resolved = {item[4][0] for item in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except (OSError, UnicodeError):
        return False
    try:
        return bool(resolved) and all(public(str(ipaddress.ip_address(address.split('%')[0]))) for address in resolved)
    except ValueError:
        return False


def fetch_certificate(url, **options):
    """The signing certificate at ``url`` (the first of its chain), or None."""
    chain = fetch_chain(url, **options)
    return chain[0] if chain else None


def fetch_chain(url, *, get=None, resolve=_public_host, now=time.monotonic):
    """The certificates at ``url``, signing certificate first (PEM, a PEM chain, or one DER), or None: HTTPS only,
    a public host, no redirects, at most CERT_BYTES within FETCH_SECONDS; kept CACHE_SECONDS (a failure
    FAILED_CACHE_SECONDS)."""
    from .trust import _certificates
    cached = _CACHE.get(url)
    if cached is not None and cached[0] > now():
        return cached[1]
    certificate = None
    parts = urlsplit(url or '')
    if parts.scheme == 'https' and parts.hostname and not parts.username and resolve(parts.hostname):
        try:
            import httpx
            fetch = get or (lambda target: httpx.get(target, timeout=FETCH_SECONDS, follow_redirects=False))
            response = fetch(url)
            body = response.content[:CERT_BYTES + 1] if response.status_code == 200 else b''
            if body and len(body) <= CERT_BYTES:
                certificate = _certificates(body) or None
        except (OSError, ValueError, httpx.HTTPError):
            certificate = None
    _CACHE[url] = (now() + (CACHE_SECONDS if certificate is not None else FAILED_CACHE_SECONDS), certificate)
    return certificate


def check_passport(passport, *, did, at, fetch=None, trusted=()):
    """SIGNED, UNANCHORED, FAILED or UNCHECKED for one diversion PASSporT, with the reason in a few words. SIGNED
    only when the certificate chains to one of ``trusted`` (inbound/trust.py)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    if passport.header.get('alg') != 'ES256':
        return FAILED, 'it is not signed with ES256'
    iat = passport.claims.get('iat')
    if not isinstance(iat, int) or abs(iat - at.replace(tzinfo=timezone.utc).timestamp()) > FRESH_SECONDS:
        return FAILED, 'it was not signed at the time of the call'
    destinations = passport.claims.get('dest', {}).get('tn') if isinstance(passport.claims.get('dest'), dict) else None
    wanted = ''.join(character for character in str(did or '') if character.isdigit())
    if not wanted or not any(_tn(item) and (_tn(item) == wanted or wanted.endswith(_tn(item)) or _tn(item).endswith(
            wanted)) for item in (destinations if isinstance(destinations, list) else [destinations])):
        return FAILED, 'it names another destination'
    url = passport.header.get('x5u') or passport.info
    if passport.info and passport.header.get('x5u') and passport.info != passport.header['x5u']:
        return FAILED, 'it names two different certificates'
    found = (fetch or fetch_chain)(url) if url else None
    chain = found if isinstance(found, list) else ([found] if found is not None else [])
    if not chain:
        return UNCHECKED, 'its certificate could not be fetched'
    certificate, intermediates = chain[0], chain[1:]
    moment = at.replace(tzinfo=timezone.utc)
    if not certificate.not_valid_before_utc <= moment <= certificate.not_valid_after_utc:
        return FAILED, 'its certificate was not valid at the time of the call'
    key = certificate.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or len(passport.signature) != 64:
        return FAILED, 'its signature is not an ES256 signature'
    signature = utils.encode_dss_signature(int.from_bytes(passport.signature[:32], 'big'),
                                           int.from_bytes(passport.signature[32:], 'big'))
    try:
        key.verify(signature, passport.signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return FAILED, 'its signature does not match its certificate'
    host = urlsplit(url).hostname
    if not trusted:
        return UNANCHORED, f'{host}, but you trust no certificate authority for forwarded calls yet'
    from .trust import verify_chain
    if verify_chain(certificate, intermediates, trusted, moment):
        return SIGNED, None
    return UNANCHORED, f'{host}, which no certificate authority you trust issued'


# -- the call's diversion ---------------------------------------------------------------------------------------------

def _number(number, country):
    if not number:
        return None
    from .http import received_number
    return received_number(number, country) or number


def diversion_for(headers, *, did, at, check=False, country=None, fetch=None, trusted=()) -> Diversion | None:
    """The call's diversion from its headers, or None when it was not forwarded. ``check`` fetches the certificate
    and verifies the signature (only when a receiving rule needs it)."""
    passports, compact = div_passports(headers.get('Identity'))
    diverted, reason = parse_diversion(headers.get('Diversion'))
    history = parse_history_info(headers.get('History-Info'))
    if passports:
        passport = passports[0]
        number = _number(('+' if not str(passport.claims['div'].get('tn', '')).startswith('+') else '')
                         + str(_tn(passport.claims['div'].get('tn')) or ''), country) if _tn(
            passport.claims['div'].get('tn')) else None
        if not check:
            state, why = UNCHECKED, 'no receiving rule needed it checked'
        else:
            try:
                state, why = check_passport(passport, did=did, at=at, fetch=fetch, trusted=trusted)
            except ValueError as error:  # an unusable certificate or key (cryptography's documented error)
                logging.getLogger(__name__).info('A diversion signature could not be checked: %s', error)
                state, why = UNCHECKED, 'its certificate could not be read'
        return Diversion(number, state, 'passport', reason, sentence(number, state, why))
    number = _number(diverted or history, country)
    if number is None:
        return None
    state = STATED
    why = 'the network gave a compact signature Faxbot cannot read' if compact else None
    return Diversion(number, state, 'diversion' if diverted else 'history-info', reason, sentence(number, state, why))


def sentence(number, state, why=None):
    """One plain sentence for the received fax."""
    if state == SIGNED:
        return (f'Forwarded from {number}; the forwarding is verified: signed with a certificate from a certificate '
                'authority you trust.')
    if state == UNANCHORED:
        if not why:
            return f'Forwarded from {number}; signed, but not by a certificate authority you trust, so not verified.'
        return f'Forwarded from {number}; signed with the certificate at {why}, so the forwarding is not verified.'
    because = f': {why}' if why else ''
    if state == FAILED:
        return f'Forwarded from {number}, the network said, but its signature did not check{because}.'
    if state == UNCHECKED:
        return f'Forwarded from {number}; the network signed it, but Faxbot did not check the signature{because}.'
    extra = f' ({why})' if why else ''
    return f'Forwarded from {number}, the network said, without a signature{extra}.'


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def call_time(call, fallback):
    """The call's start as naive UTC (the hand-over's started_at in epoch seconds), else ``fallback``."""
    value = (call or {}).get('started_at') if isinstance(call, dict) else None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return fallback
    if not 946684800 <= seconds <= 4102444800:
        return fallback
    return datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)
