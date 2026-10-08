"""Finding recipients that run Faxbot (M16, D13): from calls already made, partner introductions and trusted directories.

**From calls.** A Faxbot whose SSL Fax engine answers a call names its own
listener in the call's CSA frame (``ssl://passcode@host:port``), and one that
places a call names it in the TSA. When a call's record shows such an address
(the built-in engine's ``fax_call_frames``, and the SSL Fax engine's session
log as ``hylafax/bin/notify`` reports it), Faxbot keeps a hint: the host, the
port and the far end's number, never the passcode. A background step later
makes one HTTPS GET to ``https://host/.well-known/faxbot-direct`` (port 443,
never the SSL Fax port). That GET is never a fax call, and nothing is sent to
the host but the request. When the answer is a valid, self-signed Faxbot card
for the number that was called, and not this installation's own, the
recipient becomes a suggestion: "This recipient runs Faxbot. Enroll as a
direct partner to send to them without a phone call."

**Controls.** One GET per host a day at most, and at most ``LOOKUPS_PER_HOUR``
background lookups an hour, both read from the lookup log in the database so
they hold across workers and restarts. Every answer, a failure too, is kept
with an expiry and reused until then, never retried in a loop. A host that
resolves to a private, loopback or link-local address is not contacted unless
partners on private networks are allowed (``direct_allow_private_peers``).
The GET follows no redirect, reads at most ``MAX_DOCUMENT_BYTES`` and gives
up after ``FETCH_TIMEOUT`` seconds. A host name is checked with full TLS
verification. A bare IP address (Faxbot's SSL Fax engine advertises its public
IP) cannot be named by an ordinary certificate, so for one the certificate
must still chain to a trusted authority and must name the host of the card's
own endpoint. On a private network, and only when the administrator allows
private partners, a Faxbot's own (self-signed) certificate is accepted: the
answer is only a hint, the challenge fax is the authentication, and
enrollment keeps the partner's key. The certificate's SHA-256 fingerprint is
kept with the lookup and the suggestion and, when the partner is enrolled,
as a pin (``direct_certificate_pins``); a later lookup of that host that sees
another certificate is recorded as ``certificate_changed``, and Faxbot checks
each pinned host about once a week.

**Introductions.** An enrolled partner B can introduce two of its verified
partners A and C to each other, only when both of their records at B say
"may be introduced" (off by default, recorded after their current
verification). B sends each a signed hint about the other: organization,
fax number, endpoint and signing key. The receiver keeps it as a suggestion;
enrolling fetches the card from that endpoint's well-known document and
refuses it unless the key matches.

**Directories (D13).** An installation may publish, under a directory domain
it controls, a DNS TXT record at ``_faxbot.<reversed digits>.<directory>``
(ENUM's digit order, RFC 6116 2.4, under an underscored leaf, RFC 8552) saying
the number reaches its Faxbot, signed with its identity key over the record
name, number, endpoint, key and expiry. Nothing is published by default, and
only the number on its partner card, when it is one the installation receives
on, can be published. Faxbot never writes DNS: it gives the administrator the
record to add. A sender looks numbers up only in directories its
administrator lists as trusted, checks the signature and expiry before
anything is shown, and then reads the card from the record's endpoint.

In every case the challenge fax remains the only authentication: a
suggestion is a hint, and enrolling makes a partner that is not yet verified.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import asyncio
import ipaddress
import json
import re
import ssl
from typing import NamedTuple
from uuid import uuid4

import httpx
import sqlalchemy as sa

from ..config_runtime import run_lifecycle_step
from ..routing.database import read_connection, reflect, utcnow, write_transaction
from . import dnstxt
from .addresses import pinned_request, public, resolve
from .crypto import PROTOCOL, DirectProtocolError, canonical, check_card, check_signed, parse_timestamp, signed, \
    timestamp, unb64, verify
from .identity import IdentityUnavailable
from .store import DirectConflict


WELL_KNOWN_PATH = '/.well-known/faxbot-direct'
MAX_DOCUMENT_BYTES = 16384
FETCH_TIMEOUT = 10.0
LOOKUPS_PER_HOUR = 20
HOST_EVERY = timedelta(days=1)
FOUND_FOR = timedelta(days=30)
NOT_FOUND_FOR = timedelta(days=7)
FAILED_FOR = timedelta(days=1)
HINT_DAYS = 30
PIN_CHECK_EVERY = timedelta(days=7)
OWN_NETWORK = ('This Faxbot is on your own network and uses its own certificate, not one from a trusted authority. '
               'The challenge fax still confirms who it is before anything is sent.')
SCAN_DAYS = 2
INTRODUCTIONS_PER_DAY = 20
INTRODUCTION_FRESHNESS = timedelta(hours=24)
PUBLISHED_FOR = timedelta(days=365)
RECENT_DAYS = 30
FRAME_OCTETS = 32  # Asterisk patch 0004 first kept at most 32 octets of each frame (``csa``, ``tsa``).
# Builder AW's 0048 keeps the whole frame (``csa_full``, ``tsa_full``): T.30's 83 FIF octets after the
# address, control and FCF octets.
FULL_FRAME_OCTETS = 3 + 83
DIRECTORY_PREFIX = '_faxbot'
SKIPPED = 'skipped'  # A hint's lookup_id when no lookup was needed: its number is already a partner's, or unreadable.
RECORD_VERSION = 'faxbot1'

FINDING = 'This recipient runs Faxbot. Enroll as a direct partner to send to them without a phone call.'
OUTCOME_TEXT = {
    'faxbot': 'Runs Faxbot.',
    'private': ('Its address is on a private or local network, which Faxbot looks up only when partners on '
                'private networks are allowed.'),
    'unreachable': 'Faxbot could not reach it.',
    'not_faxbot': 'It did not answer as a Faxbot.',
    'invalid': 'Its answer was not a validly signed Faxbot card.',
    'own': 'It is this Faxbot.',
    'unverified': "Its certificate does not name the address on its card, so Faxbot did not trust its answer.",
    'key_mismatch': "Its card does not carry the key it was introduced or listed with.",
    'other_number': 'Its card names a different fax number.',
    'certificate_changed': ('Its certificate is not the one it had when you enrolled it. Check with the partner that '
                            'they replaced it.'),
    'listed': 'Listed with a valid signature.',
    'not_listed': 'Not listed.',
    'bad_record': 'Listed, but the record is not signed correctly or has expired, so it was ignored.',
    'directory_unreachable': 'Faxbot could not ask the directory.',
}
EXPIRY = {'faxbot': FOUND_FOR, 'listed': FOUND_FOR, 'private': FAILED_FOR, 'unreachable': FAILED_FOR,
          'directory_unreachable': FAILED_FOR}

_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
_HOST = re.compile(rf'{_LABEL}(?:\.{_LABEL})+')
_PORT_AFTER = re.compile(rf'({_LABEL}(?:\.{_LABEL})+):([0-9]{{0,5}})(?![0-9])')
_KEY = re.compile(r'[A-Za-z0-9_-]{43}')
_NUMBER = re.compile(r'\+[1-9][0-9]{7,14}')
_ENDPOINT = re.compile(r'https?://[^\s/?#;]+(?:/[^\s?#;]*)?')


class LookupRefused(RuntimeError):
    """Nothing usable came back; ``outcome`` is a key of OUTCOME_TEXT."""

    def __init__(self, outcome):
        super().__init__(outcome)
        self.outcome = outcome


# -- addresses ---------------------------------------------------------------------------------

def ssl_address(text):
    """(host, port or None) from an SSL Fax address ``ssl://[passcode@]host:port``, or None.

    Only what follows the last ``@`` is read, so the passcode is never kept.
    The host must be a dotted name or an IPv4 address followed by its port's
    colon, which a cut-off passcode is not.
    """
    if not isinstance(text, str):
        return None
    at = text.find('ssl://')
    if at < 0:
        return None
    words = text[at + 6:].rpartition('@')[2].split()  # the log shows "ssl://(passcode hidden)@host:port"
    if not words:
        return None
    rest = words[0].lower()
    match = _PORT_AFTER.match(rest)
    if match is None or len(match.group(1)) > 253:
        return None
    host, port = match.group(1), match.group(2)
    if re.fullmatch(r'[0-9.]+', host):
        try:
            host = str(ipaddress.IPv4Address(host))
        except ValueError:
            return None
    elif not _HOST.fullmatch(host) or host.rsplit('.', 1)[1].isdigit():
        return None
    number = int(port) if port else None
    if number is not None and not 1 <= number <= 65535:
        return None
    return host, number


def _bit_reversed(octet):
    return int(f'{octet:08b}'[::-1], 2)


def frame_address(hex_frame, *, cap=FRAME_OCTETS):
    """(host, port) from a CSA or TSA frame as patch 0004 keeps it (hex: address, control, FCF, FIF), or None.

    The FIF is T.30's layout (sequence, type, length, then the address) or
    the older one (type, then the address); octets that are not printable,
    and a length octet that happens to be (``%``), sit outside ``ssl://``.
    T.30 sends the characters of some fields last first, and Class 1 modems
    hand over bits in transmission order, so the FIF is read as kept, reversed,
    bit-reversed and both, and the first that holds an SSL Fax address wins.
    A frame as long as ``cap`` may have been cut, so it is used only when the
    passcode's end (its ``@``) is inside it.
    """
    text = str(hex_frame or '').strip().lower()
    if not text or len(text) % 2 or not re.fullmatch(r'[0-9a-f]+', text):
        return None
    frame = bytes.fromhex(text)
    if len(frame) < 6:
        return None
    fif = frame[3:]
    for candidate in (fif, fif[::-1], bytes(_bit_reversed(o) for o in fif),
                      bytes(_bit_reversed(o) for o in fif[::-1])):
        shown = ''.join(chr(o) if 33 <= o < 127 else ' ' for o in candidate)
        if 'ssl://' not in shown:
            continue
        if len(frame) >= cap and '@' not in shown[shown.find('ssl://'):]:
            return None
        return ssl_address(shown)
    return None


def call_address(row):
    """(host, port) from one ``fax_call_frames`` row: its CSA on a sent call, its TSA on a received one.

    A whole frame (``csa_full``/``tsa_full``, Builder AW's 0048) is read with
    ``engine_frames.far_address`` when that exists, else from its bytes; an
    older row's frame, which patch 0004 cut at 32 octets, only from its bytes.
    """
    column = 'csa' if row.get('direction') == 'out' else 'tsa'
    full = row.get(f'{column}_full')
    if full:
        try:
            from .. import engine_frames
            reader = getattr(engine_frames, 'far_address', None)
            found = reader(row) if reader is not None else None
        except Exception:
            found = None
        if isinstance(found, dict) and isinstance(found.get('address'), str):
            address = ssl_address(found['address'])
            if address is not None:
                return address
        address = frame_address(full, cap=FULL_FRAME_OCTETS)
        if address is not None:
            return address
    return frame_address(row.get(column))


def record_name(number, directory):
    digits = re.sub(r'[^0-9]', '', number or '')
    return f"{DIRECTORY_PREFIX}.{'.'.join(reversed(digits))}.{directory}"


def directory_domain(value):
    """A directory domain in lower case, or None."""
    text = str(value or '').strip().lower().rstrip('.')
    if not text or len(text) > 200 or not _HOST.fullmatch(text) or text.rsplit('.', 1)[1].isdigit():
        return None
    return text


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _host_of(endpoint):
    from urllib.parse import urlsplit
    try:
        return (urlsplit(endpoint).hostname or '').lower()
    except ValueError:
        return ''


def _names_cover(names, host):
    """Whether certificate names (DNS names and IP addresses) cover ``host``; one wildcard label at most."""
    host = host.lower()
    for name in names or ():
        name = name.lower()
        if name == host:
            return True
        if name.startswith('*.') and '.' in host and host.split('.', 1)[1] == name[2:]:
            return True
    return False


# -- the HTTPS GET -----------------------------------------------------------------------------

class Fetched(NamedTuple):
    status: int
    body: bytes | None  # None: larger than MAX_DOCUMENT_BYTES
    names: tuple | None  # the certificate's names, read only for a bare public IP address
    fingerprint: str | None = None  # SHA-256 of the certificate, read only on a private network
    private: bool = False  # the host is on a private network (looked up because private partners are allowed)


def _ssl_object(response):
    stream = response.extensions.get('network_stream')
    return stream.get_extra_info('ssl_object') if stream is not None else None


def _certificate_names(response):
    ssl_object = _ssl_object(response)
    certificate = ssl_object.getpeercert() if ssl_object is not None else None
    if not certificate:
        return ()
    return tuple(value for kind, value in certificate.get('subjectAltName', ()) if kind in ('DNS', 'IP Address'))


def _fingerprint(response):
    import hashlib
    ssl_object = _ssl_object(response)
    der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    return hashlib.sha256(der).hexdigest() if der else None


def fingerprint_text(value):
    """A SHA-256 fingerprint as people compare it: upper-case hex pairs, colon-separated."""
    return ':'.join(value[at:at + 2] for at in range(0, len(value), 2)).upper() if value else None


class WellKnownFetcher:
    """One bounded HTTPS GET of a host's well-known document; never follows a redirect."""

    def __init__(self, *, resolver=resolve, timeout=FETCH_TIMEOUT, transport=None, port=443):
        """``port`` is 443 (the well-known address of a host); only tests use another."""
        self.resolver, self.timeout, self.transport, self.port = resolver, timeout, transport, port

    async def get(self, host, *, allow_private):
        literal = _is_ip(host)
        url = (f"https://{f'[{host}]' if ':' in host else host}{'' if self.port == 443 else f':{self.port}'}"
               f'{WELL_KNOWN_PATH}')
        options = {'headers': {'Accept': 'application/json'}}
        # Resolve once, check every address, and connect to the one checked (a later answer cannot redirect it).
        try:
            addresses = await asyncio.to_thread(self.resolver, host, self.port)
            private = not addresses or not all(public(address) for address in addresses)
        except (OSError, UnicodeError, ValueError):
            raise LookupRefused('unreachable') from None
        if not addresses:
            raise LookupRefused('unreachable')
        if private and not allow_private:
            raise LookupRefused('private')
        if not literal:
            url, options = pinned_request(url, addresses[0], options)
        context = httpx.create_ssl_context()
        if private:
            # A Faxbot on your own network may use its own certificate; its fingerprint is kept instead.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        elif literal:
            context.check_hostname = False  # The chain is still verified; the card's host is checked after.
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, verify=context,
                                         transport=self.transport, trust_env=False) as client:
                async with client.stream('GET', url, **options) as response:
                    names = _certificate_names(response) if literal and not private else None
                    fingerprint = _fingerprint(response) if private else None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_DOCUMENT_BYTES:
                            return Fetched(response.status_code, None, names, fingerprint, private)
                    return Fetched(response.status_code, bytes(body), names, fingerprint, private)
        except (httpx.HTTPError, ssl.SSLError, OSError):
            raise LookupRefused('unreachable') from None


# -- settings ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    well_known: bool = True
    from_calls: bool = True
    directories: tuple = ()
    recorded_at: datetime | None = None
    recorded_by_name: str | None = None


def _directories(text):
    found = []
    for line in str(text or '').splitlines():
        domain = directory_domain(line)
        if domain and domain not in found:
            found.append(domain)
    return tuple(found)


class _WithoutDirect:
    """Configuration values as they would be with direct delivery off (its own number is not a received one)."""

    def __init__(self, values):
        self._values = values

    def __getattr__(self, name):
        if name == 'direct_delivery_enabled':
            return False
        return getattr(self._values, name)


def _e164(number, values):
    from ..routing.numbers import InvalidNumber, normalize_number
    try:
        return normalize_number(str(number or ''), country=getattr(values, 'fax_default_country', 'US') or 'US')
    except (InvalidNumber, ValueError):
        return None


# -- storage -------------------------------------------------------------------------------------

class DiscoveryStore:
    TABLES = ('direct_discovery_settings', 'direct_discovery_hints', 'direct_discovery_lookups',
              'direct_discovery_suggestions', 'direct_certificate_pins', 'direct_introduction_consents', 'direct_introductions',
              'direct_dns_publications', 'direct_peers', 'fax_call_frames', 'fax_jobs')

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, self.TABLES)
        self.settings_table = tables['direct_discovery_settings']
        self.hints = tables['direct_discovery_hints']
        self.lookups = tables['direct_discovery_lookups']
        self.suggestions = tables['direct_discovery_suggestions']
        self.pins = tables['direct_certificate_pins']
        self.consents = tables['direct_introduction_consents']
        self.introductions = tables['direct_introductions']
        self.publications = tables['direct_dns_publications']
        self.peers = tables['direct_peers']
        self.frames = tables['fax_call_frames']
        self.jobs = tables['fax_jobs']

    # Settings
    def settings(self):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.settings_table).order_by(
                self.settings_table.c.created_at.desc(), self.settings_table.c.id.desc()).limit(1)).mappings().first()
        if row is None:
            return Settings()
        return Settings(bool(row['well_known']), bool(row['from_calls']), _directories(row['directories']),
                        row['created_at'], row['recorded_by_name'])

    def save_settings(self, *, well_known=None, from_calls=None, directories=None, actor_id=None, actor_name=None):
        current = self.settings()
        if directories is not None:
            checked = []
            for value in directories:
                domain = directory_domain(value)
                if domain is None:
                    raise DirectConflict(f'"{str(value)[:80]}" is not a domain name. Enter a directory such as '
                                         'faxdirectory.example.org.')
                if domain not in checked:
                    checked.append(domain)
            directories = tuple(checked[:20])
        row = {'id': uuid4().hex, 'well_known': int(current.well_known if well_known is None else bool(well_known)),
               'from_calls': int(current.from_calls if from_calls is None else bool(from_calls)),
               'directories': '\n'.join(current.directories if directories is None else directories),
               'recorded_by': actor_id, 'recorded_by_name': (actor_name or None) and str(actor_name)[:200],
               'created_at': utcnow()}
        with write_transaction(self.engine) as connection:
            connection.execute(self.settings_table.insert().values(**row))
        return self.settings()

    # Partners
    def peer(self, peer_id, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.peers).where(self.peers.c.id == peer_id)).mappings().first()
            return dict(row) if row else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def partner_for(self, connection, *, number=None, signing_key=None):
        """A partner that is not removed, by its key or its number."""
        conditions = []
        if signing_key:
            conditions.append(self.peers.c.signing_key == signing_key)
        if number:
            conditions.append(self.peers.c.phone_number == number)
        if not conditions:
            return None
        row = connection.execute(sa.select(self.peers).where(self.peers.c.state != 'revoked', sa.or_(*conditions))
                                 .limit(1)).mappings().first()
        return dict(row) if row else None

    def peer_list(self):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.peers).where(
                self.peers.c.state != 'revoked').order_by(self.peers.c.organization, self.peers.c.id)).mappings()]

    # Hints
    def add_hint(self, *, source, source_ref, direction, number, host, port, now=None):
        row = {'id': uuid4().hex, 'source': source, 'source_ref': str(source_ref)[:80], 'direction': direction,
               'number': number, 'host': host, 'port': port, 'created_at': now or utcnow(), 'lookup_id': None}
        try:
            with write_transaction(self.engine) as connection:
                if connection.execute(sa.select(self.hints.c.id).where(
                        self.hints.c.source == source, self.hints.c.source_ref == row['source_ref'])).first():
                    return False
                connection.execute(self.hints.insert().values(**row))
            return True
        except Exception:
            return False

    def pending_hints(self, *, now, limit=10):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.hints).where(
                self.hints.c.lookup_id.is_(None), self.hints.c.created_at >= now - timedelta(days=HINT_DAYS))
                .order_by(self.hints.c.created_at, self.hints.c.id).limit(limit)).mappings()]

    def answer_hint(self, hint_id, lookup_id):
        with write_transaction(self.engine) as connection:
            connection.execute(self.hints.update().where(self.hints.c.id == hint_id,
                                                         self.hints.c.lookup_id.is_(None)).values(lookup_id=lookup_id))

    # Lookups
    def fresh_lookup(self, *, host, kind, number=None, now):
        """The newest lookup of a host still in force (for a directory: of that number), or None."""
        query = sa.select(self.lookups).where(self.lookups.c.host == host, self.lookups.c.kind == kind,
                                              self.lookups.c.expires_at > now)
        if number is not None:
            query = query.where(self.lookups.c.number == number)
        with read_connection(self.engine) as connection:
            row = connection.execute(query.order_by(self.lookups.c.started_at.desc()).limit(1)).mappings().first()
        return dict(row) if row else None

    def host_asked_since(self, host, since):
        with read_connection(self.engine) as connection:
            return connection.execute(sa.select(sa.func.count()).select_from(self.lookups).where(
                self.lookups.c.host == host, self.lookups.c.started_at >= since)).scalar() > 0

    def lookups_since(self, since):
        with read_connection(self.engine) as connection:
            return connection.execute(sa.select(sa.func.count()).select_from(self.lookups).where(
                self.lookups.c.started_at >= since)).scalar()

    def record_lookup(self, *, kind, host, url, outcome, number=None, card=None, record=None, certificate=None,
                      now=None):
        now = now or utcnow()
        row = {'id': uuid4().hex, 'kind': kind, 'host': host[:253], 'url': url[:600], 'number': number,
               'outcome': outcome, 'signing_key': None, 'organization': None, 'fax_number': None, 'endpoint': None,
               'card': None, 'record': record, 'certificate_sha256': certificate, 'started_at': now,
               'expires_at': now + EXPIRY.get(outcome, NOT_FOUND_FOR)}
        if card is not None:
            row.update(signing_key=card['signing_key'], organization=card['organization'][:200],
                       fax_number=card['fax_number'], endpoint=card['endpoint'],
                       card=canonical(card).decode('ascii'))
        with write_transaction(self.engine) as connection:
            connection.execute(self.lookups.insert().values(**row))
        return row

    def last_lookup(self, *, host, kind):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.lookups).where(
                self.lookups.c.host == host, self.lookups.c.kind == kind).order_by(
                self.lookups.c.started_at.desc()).limit(1)).mappings().first()
        return dict(row) if row else None

    # Certificate pins
    def pins_for(self, host):
        with read_connection(self.engine) as connection:
            return set(connection.execute(sa.select(self.pins.c.certificate_sha256).where(
                self.pins.c.host == host)).scalars())

    def pin(self, *, peer_id, host, certificate, suggestion_id=None):
        with write_transaction(self.engine) as connection:
            connection.execute(self.pins.insert().values(
                id=uuid4().hex, peer_id=peer_id, host=host[:253], certificate_sha256=certificate,
                suggestion_id=suggestion_id, created_at=utcnow()))

    def pinned(self):
        """The newest pin of each partner that is not removed: {peer_id: pin row}."""
        with read_connection(self.engine) as connection:
            found = {}
            for row in connection.execute(sa.select(self.pins).order_by(self.pins.c.created_at)).mappings():
                found[row['peer_id']] = dict(row)
            live = set(connection.execute(sa.select(self.peers.c.id).where(self.peers.c.state != 'revoked')).scalars())
        return {peer_id: row for peer_id, row in found.items() if peer_id in live}

    def recent_lookups(self, limit=20):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.lookups).order_by(
                self.lookups.c.started_at.desc()).limit(limit)).mappings()]

    # Suggestions
    def suggest(self, *, number, source, organization, signing_key, endpoint, card=None, lookup_id=None,
                introduced_by=None, introduction=None, directory=None, certificate=None, now=None):
        """A new suggestion, or None when the recipient is already a partner or was already suggested."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            if self.partner_for(connection, number=number, signing_key=signing_key) is not None:
                return None
            if connection.execute(sa.select(self.suggestions.c.id).where(
                    self.suggestions.c.number == number, self.suggestions.c.signing_key == signing_key)).first():
                return None
            row = {'id': uuid4().hex, 'number': number, 'source': source, 'lookup_id': lookup_id,
                   'introduced_by': introduced_by, 'introduction': introduction, 'directory': directory,
                   'organization': organization[:200], 'signing_key': signing_key, 'endpoint': endpoint[:512],
                   'card': canonical(card).decode('ascii') if card is not None else None,
                   'certificate_sha256': certificate, 'created_at': now}
            connection.execute(self.suggestions.insert().values(**row))
            return row

    def suggestion(self, suggestion_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.suggestions).where(
                self.suggestions.c.id == suggestion_id)).mappings().first()
        return dict(row) if row else None

    def open_suggestions(self):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.suggestions).where(
                self.suggestions.c.dismissed_at.is_(None), self.suggestions.c.enrolled_at.is_(None))
                .order_by(self.suggestions.c.created_at.desc()).limit(200)).mappings()]

    def introductions_from(self, peer_id, since):
        with read_connection(self.engine) as connection:
            return connection.execute(sa.select(sa.func.count()).select_from(self.suggestions).where(
                self.suggestions.c.introduced_by == peer_id, self.suggestions.c.created_at >= since)).scalar()

    def close_suggestion(self, suggestion_id, *, enrolled_peer_id=None, actor_id=None, actor_name=None):
        now = utcnow()
        name = (actor_name or None) and str(actor_name)[:200]
        values = ({'enrolled_at': now, 'enrolled_peer_id': enrolled_peer_id, 'enrolled_by': actor_id,
                   'enrolled_by_name': name} if enrolled_peer_id else
                  {'dismissed_at': now, 'dismissed_by': actor_id, 'dismissed_by_name': name})
        with write_transaction(self.engine) as connection:
            done = connection.execute(self.suggestions.update().where(
                self.suggestions.c.id == suggestion_id, self.suggestions.c.dismissed_at.is_(None),
                self.suggestions.c.enrolled_at.is_(None)).values(**values)).rowcount
        return bool(done)

    # Consents
    def consent(self, peer):
        """Whether a partner may be introduced: a yes recorded at or after its current verification, while verified."""
        if peer is None or peer['state'] != 'verified' or peer.get('verified_at') is None:
            return False
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.consents).where(self.consents.c.peer_id == peer['id']).order_by(
                self.consents.c.created_at.desc(), self.consents.c.id.desc()).limit(1)).mappings().first()
        return bool(row and row['allowed'] == 1 and row['created_at'] >= peer['verified_at'])

    def record_consent(self, peer_id, allowed, *, actor_id=None, actor_name=None):
        with write_transaction(self.engine) as connection:
            connection.execute(self.consents.insert().values(
                id=uuid4().hex, peer_id=peer_id, allowed=1 if allowed else 0, recorded_by=actor_id,
                recorded_by_name=(actor_name or None) and str(actor_name)[:200], created_at=utcnow()))

    def record_introduction(self, first_id, second_id, first_outcome, second_outcome, *, actor_id=None,
                            actor_name=None):
        row = {'id': uuid4().hex, 'first_peer_id': first_id, 'second_peer_id': second_id,
               'first_outcome': first_outcome, 'second_outcome': second_outcome, 'created_by': actor_id,
               'created_by_name': (actor_name or None) and str(actor_name)[:200], 'created_at': utcnow()}
        with write_transaction(self.engine) as connection:
            connection.execute(self.introductions.insert().values(**row))
        return row

    def recent_introductions(self, limit=20):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.introductions).order_by(
                self.introductions.c.created_at.desc()).limit(limit)).mappings()]

    # Publications
    def active_publications(self, now=None):
        now = now or utcnow()
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.publications).where(
                self.publications.c.withdrawn_at.is_(None)).order_by(self.publications.c.created_at.desc())).mappings()]

    def publication(self, publication_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.publications).where(
                self.publications.c.id == publication_id)).mappings().first()
        return dict(row) if row else None

    def add_publication(self, row):
        with write_transaction(self.engine) as connection:
            if connection.execute(sa.select(self.publications.c.id).where(
                    self.publications.c.number == row['number'], self.publications.c.directory == row['directory'],
                    self.publications.c.withdrawn_at.is_(None),
                    self.publications.c.expires_at > row['created_at'])).first():
                raise DirectConflict(f"{row['number']} is already published in {row['directory']}. Withdraw it "
                                     'first to publish it again.')
            connection.execute(self.publications.insert().values(**row))
        return row

    def withdraw_publication(self, publication_id, *, actor_id=None, actor_name=None):
        with write_transaction(self.engine) as connection:
            return bool(connection.execute(self.publications.update().where(
                self.publications.c.id == publication_id, self.publications.c.withdrawn_at.is_(None)).values(
                withdrawn_at=utcnow(), withdrawn_by=actor_id,
                withdrawn_by_name=(actor_name or None) and str(actor_name)[:200])).rowcount)

    # Calls
    def frame_hints(self, since):
        f = self.frames
        # The whole frames (csa_full, tsa_full) exist once Builder AW's 0048 is in; older rows keep 32 octets.
        full = [name for name in ('csa_full', 'tsa_full') if name in f.c]
        out = [f.c.csa.is_not(None)] + ([f.c.csa_full.is_not(None)] if 'csa_full' in f.c else [])
        received = [f.c.tsa.is_not(None)] + ([f.c.tsa_full.is_not(None)] if 'tsa_full' in f.c else [])
        query = sa.select(f.c.id, f.c.direction, f.c.number, f.c.csa, f.c.tsa, *(f.c[name] for name in full),
                          f.c.created_at).where(
            f.c.created_at >= since, sa.or_(sa.and_(f.c.direction == 'out', sa.or_(*out)),
                                            sa.and_(f.c.direction == 'in', sa.or_(*received))))
        with read_connection(self.engine) as connection:
            rows = [dict(row) for row in connection.execute(query.order_by(f.c.created_at).limit(500)).mappings()]
            known = set(connection.execute(sa.select(self.hints.c.source_ref).where(
                self.hints.c.source == 'frames', self.hints.c.created_at >= since - timedelta(days=1))).scalars())
        return [row for row in rows if row['id'] not in known]

    def job_number(self, job_id):
        if not job_id:
            return None
        with read_connection(self.engine) as connection:
            return connection.execute(sa.select(self.jobs.c.to_number).where(self.jobs.c.id == job_id)).scalar()

    def recent_numbers(self, *, since, limit=10):
        """The numbers faxed most since ``since``, most first."""
        j = self.jobs
        count = sa.func.count().label('faxes')
        with read_connection(self.engine) as connection:
            return [row[0] for row in connection.execute(
                sa.select(j.c.to_number, count).where(j.c.created_at >= since, j.c.to_number.is_not(None))
                .group_by(j.c.to_number).order_by(count.desc(), j.c.to_number).limit(limit))]


# -- the service -------------------------------------------------------------------------------

class DiscoveryService:
    """Discovery for one installation; ``direct`` is its DirectService, which keeps keys and enrolls partners."""

    def __init__(self, direct, *, fetcher=None, txt=None):
        self.direct = direct
        self.store = DiscoveryStore(direct.store.engine)
        self.fetcher = fetcher or WellKnownFetcher(resolver=direct.resolver)
        self.txt = txt or dnstxt.txt_records

    def values(self):
        return self.direct.values()

    def _allow_private(self):
        return bool(getattr(self.values(), 'direct_allow_private_peers', False))

    def own_key(self):
        try:
            return self.direct.identity().signing_key
        except IdentityUnavailable:
            return None

    # The well-known document ------------------------------------------------------------------
    def well_known(self):
        """Our card for ``/.well-known/faxbot-direct``, or None: off, direct delivery off, or no keys yet."""
        values = self.values()
        if not getattr(values, 'direct_delivery_enabled', False) or not self.store.settings().well_known:
            return None
        if not self.direct.ready():  # never create keys for an anonymous request
            return None
        try:
            return {'faxbot_direct': PROTOCOL, 'card': self.direct.own_card()}
        except (DirectConflict, IdentityUnavailable):
            return None

    # Fetching and judging a card --------------------------------------------------------------
    def judge(self, fetched, *, expect_key=None):
        """(outcome, card or None) for one well-known answer."""
        if fetched.status != 200 or fetched.body is None:
            return 'not_faxbot', None
        try:
            document = json.loads(fetched.body)
        except ValueError:
            return 'not_faxbot', None
        if not isinstance(document, dict) or document.get('faxbot_direct') != PROTOCOL or 'card' not in document:
            return 'not_faxbot', None
        try:
            card = check_card(document['card'])
        except DirectProtocolError:
            return 'invalid', None
        card = dict(document['card'])
        if card['signing_key'] == self.own_key():
            return 'own', None
        if expect_key is not None and card['signing_key'] != expect_key:
            return 'key_mismatch', None
        if fetched.names is not None and not _names_cover(fetched.names, _host_of(card['endpoint'])):
            return 'unverified', None
        if fetched.private and not fetched.fingerprint:
            return 'unverified', None
        return 'faxbot', card

    async def fetch_card(self, host, *, kind, number=None, expect_key=None, now=None):
        """One GET of a host's well-known document, recorded in the lookup log; returns the lookup row."""
        url = f'https://{host}{WELL_KNOWN_PATH}'
        certificate = None
        try:
            fetched = await self.fetcher.get(host, allow_private=await run_lifecycle_step(self._allow_private))
            outcome, card = await run_lifecycle_step(lambda: self.judge(fetched, expect_key=expect_key))
            certificate = fetched.fingerprint if fetched.private else None
            pins = await run_lifecycle_step(lambda: self.store.pins_for(host)) if certificate else set()
            if pins and certificate not in pins:
                outcome, card = 'certificate_changed', None
        except LookupRefused as refused:
            outcome, card = refused.outcome, None
        return await run_lifecycle_step(lambda: self.store.record_lookup(
            kind=kind, host=host, url=url, outcome=outcome, number=number, card=card, certificate=certificate,
            now=now))

    # From calls --------------------------------------------------------------------------------
    def collect_from_frames(self, *, now=None):
        """Hints from the built-in engine's call frames of the last two days; how many were new."""
        now = now or utcnow()
        added = 0
        for row in self.store.frame_hints(now - timedelta(days=SCAN_DAYS)):
            address = call_address(row)
            if address is None or not row['number']:
                continue
            added += self.store.add_hint(source='frames', source_ref=row['id'], direction=row['direction'],
                                         number=row['number'], host=address[0], port=address[1],
                                         now=row['created_at'])
        return added

    def _suggest_from(self, lookup, number):
        """A suggestion when a found card is for this number."""
        values = self.values()
        wanted = _e164(number, values)
        if lookup['outcome'] != 'faxbot' or wanted is None or _e164(lookup['fax_number'], values) != wanted:
            return None
        card = json.loads(lookup['card'])
        return self.store.suggest(number=wanted, source='call', organization=lookup['organization'],
                                  signing_key=lookup['signing_key'], endpoint=lookup['endpoint'], card=card,
                                  lookup_id=lookup['id'], certificate=lookup.get('certificate_sha256'))

    def _may_ask(self, host, now):
        return (not self.store.host_asked_since(host, now - HOST_EVERY)
                and self.store.lookups_since(now - timedelta(hours=1)) < LOOKUPS_PER_HOUR)

    async def answer_hints(self, *, now=None):
        """Answer waiting hints from the lookup log or with one GET per host; True when more are waiting."""
        now = now or utcnow()
        hints = await run_lifecycle_step(lambda: self.store.pending_hints(now=now))
        for hint in hints:
            values = await run_lifecycle_step(self.values)
            number = _e164(hint['number'], values)
            if number is None or await run_lifecycle_step(lambda: self._partner_number(number)):
                # Already a partner (or no number to suggest for): nothing to ask.
                await run_lifecycle_step(lambda: self.store.answer_hint(hint['id'], SKIPPED))
                continue
            lookup = await run_lifecycle_step(lambda: self.store.fresh_lookup(host=hint['host'], kind='call', now=now))
            if lookup is None:
                if not await run_lifecycle_step(lambda: self._may_ask(hint['host'], now)):
                    continue  # Asked lately, or the hour's lookups are used up: it waits.
                lookup = await self.fetch_card(hint['host'], kind='call', number=hint['number'])
            await run_lifecycle_step(lambda: self.store.answer_hint(hint['id'], lookup['id']))
            await run_lifecycle_step(lambda: self._suggest_from(lookup, hint['number']))
        return False

    async def step(self):
        """The background step: hints from new calls, then lookups, then trusted directories."""
        values = await run_lifecycle_step(self.values)
        if not getattr(values, 'direct_delivery_enabled', False) or not await run_lifecycle_step(self.direct.ready):
            return False
        settings = await run_lifecycle_step(self.store.settings)
        if settings.from_calls:
            await run_lifecycle_step(self.collect_from_frames)
            await self.answer_hints()
        if settings.directories:
            await self.look_up_recent()
        await self.check_pins()
        return False

    async def check_pins(self, *, now=None):
        """About once a week, ask each pinned partner's host again whether its certificate is the same."""
        now = now or utcnow()
        for peer_id, pin in (await run_lifecycle_step(self.store.pinned)).items():
            last = await run_lifecycle_step(lambda: self.store.last_lookup(host=pin['host'], kind='certificate'))
            recent = pin['created_at'] > now - PIN_CHECK_EVERY
            if recent or (last is not None and last['started_at'] > now - PIN_CHECK_EVERY):
                continue
            used = await run_lifecycle_step(lambda: self.store.lookups_since(now - timedelta(hours=1)))
            if used >= LOOKUPS_PER_HOUR:
                return False
            peer = await run_lifecycle_step(lambda: self.store.peer(peer_id))
            await self.fetch_card(pin['host'], kind='certificate', expect_key=peer['signing_key'], now=now)
        return False

    # Introductions ------------------------------------------------------------------------------
    def set_consent(self, peer_id, allowed, *, actor_id=None, actor_name=None):
        peer = self.store.peer(peer_id)
        if peer is None or peer['state'] == 'revoked':
            raise DirectConflict('This partner is not enrolled.')
        if allowed and peer['state'] != 'verified':
            raise DirectConflict(f"{peer['organization']} is not verified yet. Only a verified partner can be "
                                 'introduced.')
        self.store.record_consent(peer_id, allowed, actor_id=actor_id, actor_name=actor_name)
        return self.store.consent(self.store.peer(peer_id))

    def _introducible(self, peer_id, now):
        peer = self.store.peer(peer_id)
        if peer is None or peer['state'] == 'revoked':
            raise DirectConflict('This partner is not enrolled.')
        if peer['state'] != 'verified' or (peer.get('expires_at') is not None and peer['expires_at'] <= now):
            raise DirectConflict(f"{peer['organization']} is not verified yet. Only a verified partner can be "
                                 'introduced.')
        if not self.store.consent(peer):
            raise DirectConflict(f"{peer['organization']} has not agreed to be introduced. Turn on \"May be "
                                 f"introduced\" for {peer['organization']} once they agree.")
        return peer

    async def _tell(self, identity, recipient, introduced):
        statement = {'type': 'introduction', 'recipient': recipient['signing_key'], 'introduced_at': timestamp(),
                     'introduced': {'organization': introduced['organization'],
                                    'fax_number': introduced['phone_number'], 'endpoint': introduced['endpoint_url'],
                                    'signing_key': introduced['signing_key']}}
        try:
            status, body = await self.direct.http.request('POST', recipient['endpoint_url'] + '/direct/introductions',
                                                          json=signed(identity, statement))
        except Exception:
            return 'unreachable'
        if status == 200 and isinstance(body, dict) and 'recorded' in body:
            return 'told'
        if status in (404, 405):
            return 'unsupported'
        if 400 <= status < 500:
            return 'refused'
        return 'unreachable'

    async def introduce(self, first_id, second_id, *, actor_id=None, actor_name=None):
        """Tell two verified partners that both agreed about each other; returns (row, sentence)."""
        if first_id == second_id:
            raise DirectConflict('Choose two different partners to introduce.')
        now = utcnow()
        first = await run_lifecycle_step(lambda: self._introducible(first_id, now))
        second = await run_lifecycle_step(lambda: self._introducible(second_id, now))
        identity = await run_lifecycle_step(lambda: self.direct.identity(create=True))
        first_outcome = await self._tell(identity, first, second)
        second_outcome = await self._tell(identity, second, first)
        row = await run_lifecycle_step(lambda: self.store.record_introduction(
            first_id, second_id, first_outcome, second_outcome, actor_id=actor_id, actor_name=actor_name))
        return row, introduction_text(first['organization'], first_outcome, second['organization'], second_outcome)

    def receive_introduction(self, envelope, *, now=None):
        """A partner's signed introduction to one of its partners: (status, body). No network here."""
        from .service import DirectUnavailable
        now = now or utcnow()
        values = self.values()
        if not getattr(values, 'direct_delivery_enabled', False) or not self.direct.ready():
            raise DirectUnavailable()
        own = self.direct.identity().signing_key
        refused = (400, {'recorded': False, 'detail': 'This introduction could not be checked.'})
        try:
            statement = json.loads(envelope['statement'])
            signer = statement.get('signer') if isinstance(statement, dict) else None
            if (not isinstance(statement, dict)
                    or set(statement) != {'type', 'recipient', 'introduced', 'introduced_at', 'signer'}
                    or statement['type'] != 'introduction' or statement['recipient'] != own
                    or not isinstance(signer, str)):
                return refused
            peer = self.direct.store.peer_by_key(signer)
            if (peer is None or peer['state'] != 'verified'
                    or (peer.get('expires_at') is not None and peer['expires_at'] <= now)):
                return 403, {'recorded': False, 'detail': 'Introductions are taken only from verified partners.'}
            check_signed(envelope, peer['signing_key'])
            if abs(parse_timestamp(statement['introduced_at']) - now) > INTRODUCTION_FRESHNESS:
                return refused
            introduced = statement['introduced']
            if (not isinstance(introduced, dict)
                    or set(introduced) != {'organization', 'fax_number', 'endpoint', 'signing_key'}
                    or not isinstance(introduced['organization'], str)
                    or not 0 < len(introduced['organization'].strip()) <= 200
                    or not isinstance(introduced['fax_number'], str) or not _NUMBER.fullmatch(introduced['fax_number'])
                    or not isinstance(introduced['endpoint'], str) or len(introduced['endpoint']) > 512
                    or not _ENDPOINT.fullmatch(introduced['endpoint'])
                    or not isinstance(introduced['signing_key'], str) or not _KEY.fullmatch(introduced['signing_key'])
                    or introduced['signing_key'] in (own, peer['signing_key'])):
                return refused
            unb64(introduced['signing_key'], length=32)
        except (KeyError, TypeError, ValueError, DirectProtocolError, UnicodeEncodeError):
            return refused
        if self.store.introductions_from(peer['id'], now - timedelta(days=1)) >= INTRODUCTIONS_PER_DAY:
            return 429, {'recorded': False, 'detail': 'Too many introductions today; try again tomorrow.'}
        self.store.suggest(number=introduced['fax_number'], source='introduction',
                           organization=introduced['organization'].strip(), signing_key=introduced['signing_key'],
                           endpoint=introduced['endpoint'].rstrip('/'), introduced_by=peer['id'],
                           introduction=json.dumps(envelope, sort_keys=True), now=now)
        return 200, {'recorded': True}

    # Enrolling from a suggestion ----------------------------------------------------------------
    async def enroll(self, suggestion_id, *, actor_id=None, actor_name=None):
        """Enroll a suggested recipient as a partner that still has to be verified; returns (peer, sentence)."""
        suggestion = await run_lifecycle_step(lambda: self.store.suggestion(suggestion_id))
        if suggestion is None or suggestion['dismissed_at'] or suggestion['enrolled_at']:
            raise DirectConflict('This suggestion is no longer open.')
        card = json.loads(suggestion['card']) if suggestion['card'] else None
        certificate = suggestion.get('certificate_sha256')
        if card is None:
            host = _host_of(suggestion['endpoint'])
            lookup = await self.fetch_card(host, kind='introduction', number=suggestion['number'],
                                           expect_key=suggestion['signing_key'])
            if lookup['outcome'] != 'faxbot':
                raise DirectConflict(f"Faxbot could not read {suggestion['organization']}'s card from their "
                                     'Faxbot. Exchange cards with them instead.')
            card, certificate = json.loads(lookup['card']), lookup['certificate_sha256']
        values = await run_lifecycle_step(self.values)
        if _e164(card['fax_number'], values) != suggestion['number']:
            raise DirectConflict(f"{card['organization']}'s card names another fax number, so it was not added.")
        peer = await run_lifecycle_step(lambda: self.direct.enroll(card))
        await run_lifecycle_step(lambda: self.store.close_suggestion(
            suggestion_id, enrolled_peer_id=peer['id'], actor_id=actor_id, actor_name=actor_name))
        if certificate:
            # Its own certificate on your network: kept with the enrollment, so a later change is noticed.
            await run_lifecycle_step(lambda: self.store.pin(peer_id=peer['id'], host=_host_of(card['endpoint']),
                                                            certificate=certificate, suggestion_id=suggestion_id))
        return peer, f"{peer['organization']} added. Send them a code by fax to confirm their number."

    def dismiss(self, suggestion_id, *, actor_id=None, actor_name=None):
        if not self.store.close_suggestion(suggestion_id, actor_id=actor_id, actor_name=actor_name):
            raise DirectConflict('This suggestion is no longer open.')
        return 'Dismissed. Faxbot will not suggest this recipient again.'

    # Directories (D13) --------------------------------------------------------------------------
    def publishable(self):
        """(the card's number, whether this Faxbot receives on it); raises DirectConflict without a card."""
        from ..routing.own_numbers import receiving_numbers
        values = self.values()
        if not getattr(values, 'direct_delivery_enabled', False):
            raise DirectConflict('Turn on direct delivery first; senders reach this Faxbot through it.')
        card = self.direct.own_card()
        return card, card['fax_number'] in receiving_numbers(_WithoutDirect(values))

    def publish(self, number, directory, *, actor_id=None, actor_name=None, now=None):
        """The signed record for a number in a directory; Faxbot never writes DNS itself."""
        now = now or utcnow()
        domain = directory_domain(directory)
        if domain is None:
            raise DirectConflict('Enter the directory as a domain name you control, such as faxdirectory.example.org.')
        values = self.values()
        wanted = _e164(number, values)
        if wanted is None:
            raise DirectConflict('Enter the fax number with its country code, such as +13035550100.')
        if not self.store.settings().well_known:
            raise DirectConflict('Turn on "Answer Faxbot lookups" first: senders read your partner card there.')
        card, receives = self.publishable()
        if wanted != card['fax_number']:
            raise DirectConflict(f"Partners deliver directly to {card['fax_number']}, the number on your partner "
                                 'card, so publish that number.')
        if not receives:
            raise DirectConflict(f'Faxbot does not receive faxes on {wanted}, so it cannot be published. Only a '
                                 'number your trunk or receiving account delivers to this Faxbot can be published.')
        endpoint = card['endpoint']
        if not _ENDPOINT.fullmatch(endpoint):
            raise DirectConflict("Your Faxbot's public address cannot be written in a directory record.")
        identity = self.direct.identity(create=True)
        name = record_name(wanted, domain)
        expires = (now + PUBLISHED_FOR).replace(hour=0, minute=0, second=0, microsecond=0)
        value = record_value(identity, name=name, number=wanted, endpoint=endpoint, expires=expires)
        row = {'id': uuid4().hex, 'number': wanted, 'directory': domain, 'record_name': name, 'record_value': value,
               'expires_at': expires, 'created_by': actor_id,
               'created_by_name': (actor_name or None) and str(actor_name)[:200], 'created_at': now,
               'withdrawn_at': None, 'withdrawn_by': None, 'withdrawn_by_name': None}
        return self.store.add_publication(row)

    async def check_publication(self, publication_id):
        """``live``, ``missing``, ``different`` or ``unreachable`` for one publication's record."""
        row = await run_lifecycle_step(lambda: self.store.publication(publication_id))
        if row is None or row['withdrawn_at'] is not None:
            raise DirectConflict('This number is not published.')
        try:
            records = await asyncio.to_thread(self.txt, row['record_name'])
        except dnstxt.DnsError:
            return row, 'unreachable'
        ours = [record for record in records if record.startswith(f'v={RECORD_VERSION}')]
        if row['record_value'] in ours:
            return row, 'live'
        return row, 'different' if ours else 'missing'

    def withdraw(self, publication_id, *, actor_id=None, actor_name=None):
        row = self.store.publication(publication_id)
        if row is None or not self.store.withdraw_publication(publication_id, actor_id=actor_id,
                                                              actor_name=actor_name):
            raise DirectConflict('This number is not published.')
        return row

    async def look_up(self, number, *, background=False, now=None):
        """Look a number up in each trusted directory; returns (suggestion or None, sentence)."""
        now = now or utcnow()
        values = await run_lifecycle_step(self.values)
        wanted = _e164(number, values)
        if wanted is None:
            raise DirectConflict('Enter the fax number with its country code, such as +13035550100.')
        settings = await run_lifecycle_step(self.store.settings)
        if not settings.directories:
            raise DirectConflict('Add a trusted directory first; Faxbot looks numbers up only in directories you '
                                 'trust.')
        found, flawed = None, None
        for domain in settings.directories:
            lookup = await run_lifecycle_step(lambda: self.store.fresh_lookup(host=domain, kind='directory',
                                                                              number=wanted, now=now))
            if lookup is None:
                if await run_lifecycle_step(lambda: self.store.lookups_since(now - timedelta(hours=1))) \
                        >= LOOKUPS_PER_HOUR:
                    if background:
                        return None, None
                    raise DirectConflict('Faxbot has made many lookups in the last hour; try again later.')
                lookup = await self._directory_lookup(domain, wanted, now=now)
            if lookup['outcome'] == 'faxbot':
                found = await run_lifecycle_step(lambda: self.store.suggest(
                    number=wanted, source='directory', organization=lookup['organization'],
                    signing_key=lookup['signing_key'], endpoint=lookup['endpoint'], card=json.loads(lookup['card']),
                    lookup_id=lookup['id'], directory=domain, certificate=lookup.get('certificate_sha256'),
                    now=now)) or found
                return found, f"{lookup['organization']} runs Faxbot at {wanted}, listed in {domain}."
            if lookup['outcome'] in ('bad_record', 'invalid', 'key_mismatch', 'other_number', 'own', 'unverified'):
                flawed = domain
        if flawed:
            return None, f'{flawed} lists {wanted}, but its record could not be trusted, so it was ignored.'
        return None, f'No trusted directory lists {wanted}.'

    async def _directory_lookup(self, domain, number, *, now):
        name = record_name(number, domain)
        try:
            records = await asyncio.to_thread(self.txt, name)
        except dnstxt.DnsError:
            return await run_lifecycle_step(lambda: self.store.record_lookup(
                kind='directory', host=domain, url=name, outcome='directory_unreachable', number=number, now=now))
        listing, text = None, None
        for record in records:
            if not record.startswith(f'v={RECORD_VERSION}'):
                continue
            text = record[:2000]
            listing = read_record(record, name=name, number=number, now=now)
            if listing is not None:
                break
        if listing is None:
            return await run_lifecycle_step(lambda: self.store.record_lookup(
                kind='directory', host=domain, url=name, outcome='bad_record' if text else 'not_listed',
                number=number, record=text, now=now))
        lookup = await self.fetch_card(_host_of(listing['endpoint']), kind='directory', number=number,
                                       expect_key=listing['key'])
        if lookup['outcome'] == 'faxbot' and _e164(lookup['fax_number'], self.values()) != number:
            lookup = dict(lookup, outcome='other_number')
        # The directory's answer is kept under the directory's name, with the card read from its endpoint.
        return await run_lifecycle_step(lambda: self.store.record_lookup(
            kind='directory', host=domain, url=name, outcome=lookup['outcome'], number=number,
            card=json.loads(lookup['card']) if lookup['outcome'] == 'faxbot' else None, record=text,
            certificate=lookup.get('certificate_sha256'), now=now))

    async def look_up_recent(self, *, now=None):
        """Recently faxed numbers that are not partners, in trusted directories, within the hour's lookups."""
        now = now or utcnow()
        numbers = await run_lifecycle_step(lambda: self.store.recent_numbers(since=now - timedelta(days=RECENT_DAYS)))
        values = await run_lifecycle_step(self.values)
        for number in numbers:
            wanted = _e164(number, values)
            if wanted is None:
                continue
            partner = await run_lifecycle_step(lambda: self._partner_number(wanted))
            if partner:
                continue
            try:
                _, sentence = await self.look_up(wanted, background=True, now=now)
            except DirectConflict:
                return False
            if sentence is None:
                return False
        return False

    def _partner_number(self, number):
        with read_connection(self.store.engine) as connection:
            return self.store.partner_for(connection, number=number) is not None


# -- directory records ---------------------------------------------------------------------------

def _record_bytes(*, name, number, endpoint, key, expires):
    return '\n'.join(('faxbot-directory-v1', name, number, endpoint, key, expires)).encode('ascii')


def record_value(identity, *, name, number, endpoint, expires):
    day = expires.strftime('%Y-%m-%d')
    signature = identity.sign(_record_bytes(name=name, number=number, endpoint=endpoint, key=identity.signing_key,
                                            expires=day))
    return f'v={RECORD_VERSION}; n={number}; e={endpoint}; k={identity.signing_key}; x={day}; s={signature}'


def read_record(text, *, name, number, now):
    """The listing in one TXT value when it is well-formed, for this name and number, unexpired and signed."""
    fields = {}
    for part in text.split(';'):
        key, _, value = part.strip().partition('=')
        if key in fields or not value:
            return None
        fields[key] = value
    if set(fields) != {'v', 'n', 'e', 'k', 'x', 's'} or fields['v'] != RECORD_VERSION:
        return None
    if fields['n'] != number or not _ENDPOINT.fullmatch(fields['e']) or not _KEY.fullmatch(fields['k']):
        return None
    try:
        expires = datetime.strptime(fields['x'], '%Y-%m-%d')
        verify(fields['k'], _record_bytes(name=name, number=number, endpoint=fields['e'], key=fields['k'],
                                          expires=fields['x']), fields['s'])
    except (ValueError, DirectProtocolError, UnicodeEncodeError):
        return None
    if expires <= now:
        return None
    return {'endpoint': fields['e'].rstrip('/'), 'key': fields['k'], 'expires': expires}


def zone_strings(value):
    """The record value as DNS character-strings of at most 255 bytes, quoted for a zone file."""
    return ' '.join(f'"{value[at:at + 255]}"' for at in range(0, len(value), 255))


# -- sentences -----------------------------------------------------------------------------------

def introduction_text(first, first_outcome, second, second_outcome):
    if first_outcome == 'told' and second_outcome == 'told':
        return (f'{first} and {second} were introduced. Each can now enroll the other and confirm the other\'s '
                'number with a code by fax.')
    parts = []
    for name, outcome, other in ((first, first_outcome, second), (second, second_outcome, first)):
        if outcome == 'told':
            parts.append(f'{name} was told about {other}.')
        elif outcome == 'unsupported':
            parts.append(f"{name}'s Faxbot does not take introductions yet.")
        elif outcome == 'refused':
            parts.append(f'{name} did not accept the introduction.')
        else:
            parts.append(f'Faxbot could not reach {name}; introduce them again later.')
    return ' '.join(parts)


def suggestion_source_text(row, introducer=None):
    if row['source'] == 'introduction':
        return f'Your partner {introducer or "a partner"} introduced them.'
    if row['source'] == 'directory':
        return f"Listed in {row['directory']}, a directory you trust."
    return 'Found from a fax call with this number.'


PUBLICATION_TEXT = {
    'created': ('Add this record to the DNS for {directory}. Senders who trust {directory} then find this Faxbot for '
                '{number}.'),
    'live': 'The record is in place in {directory}.',
    'missing': 'The record is not in the DNS for {directory} yet.',
    'different': '{directory} has a different record for {number}; replace it with this one.',
    'unreachable': 'Faxbot could not ask the DNS for {directory}; check again later.',
    'expired': 'This record expired on {expires}; publish {number} again.',
    'withdrawn': 'Withdrawn. Delete the record {name} from the DNS for {directory} too.',
}


def publication_text(state, row, *, expires_text=None):
    return PUBLICATION_TEXT[state].format(directory=row['directory'], number=row['number'], name=row['record_name'],
                                          expires=expires_text or '')


def record_engine_hint(engine, *, attempt_id, job_id, number, payload):
    """Keep the far end's SSL Fax address from the SSL Fax engine's report of one sent fax.

    ``hylafax/bin/notify`` sends only the ``host:port`` (``remote_address_b64``),
    never the passcode. No network here; the background step looks it up.
    """
    from ..hylafax_engine import _text64
    text = _text64(payload or {}, 'remote_address_b64', 300)
    address = ssl_address('ssl://' + text) if text else None
    if address is None or not attempt_id:
        return False
    store = DiscoveryStore(engine)
    if not store.settings().from_calls:
        return False
    number = number or store.job_number(job_id)
    if not number:
        return False
    return store.add_hint(source='engine', source_ref=attempt_id, direction='out', number=number, host=address[0],
                          port=address[1])
