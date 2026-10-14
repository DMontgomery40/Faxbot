"""Fax servers that send through Faxbot: store and forward for OpenText RightFax and other fax servers.

A fax server such as RightFax sends its faxes through a fax board, today often Brooktrout's SR140, which places
each fax as a SIP call with T.38 to a gateway it names (its "Primary Gateway"). Faxbot can be that gateway. It
answers the call, takes the whole fax, and sends it on to the number the server dialled as an ordinary Faxbot fax,
so every saving applies to it: route choice, coding, packing and direct delivery to partners. A call to one of
Faxbot's own numbers stays a received fax, as before. It builds on copier extensions (``copier_extensions.py``),
with the differences a fax server needs:

- **Who may send.** Only a fax server you enrolled, from its one address on your private network (RFC 1918), with
  SIP digest authentication on every call. It never registers. Faxbot keeps only pjsip's MD5 digest of the password
  and shows the password once. An address that is not enrolled reaches no fax server endpoint at all.
- **Before answering** (``admit``), Faxbot checks the number the server dialled. It refuses an emergency or short
  number, a country you did not allow for that server, a number Faxbot's dialing guard does not allow, a fax its
  sending rules would hold, and a call over the server's caps per hour or per day. It refuses each with a SIP code,
  unanswered, so the fax server shows that fax as failed itself.
- **One call is one fax.** Each call is recorded under its SIP Call-ID, unique per server, before it is answered,
  and the outbound fax's ID is derived from it, so a repeated hand-over or a repeated call never makes a second fax.
- **Only whole faxes are sent.** A call that ended before every page arrived is not sent on: the fax server saw the
  failure and will try again itself.
- **The real outcome.** The fax server shows "sent" once Faxbot has the pages. Faxbot keeps one record per call,
  and the fax is in Sent under the server's own sender. When it is not delivered (failed, uncertain, or refused
  after it arrived), Faxbot sends one notice to the server's contact email and webhook. An uncertain fax is never
  sent again, and an uncertain notice is never sent twice.

Built from Brooktrout's and OpenText's published SR140 gateway settings, and tested with contract tests. Not yet run
in Asterisk, against the SR140 stand-in (``tools/sr140-emulator``, ``tests/test_fax_server_loopback.py``) or against a
real RightFax server.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import ipaddress
import json
import logging
import re
import secrets
import uuid

import sqlalchemy as sa

from .copier_extensions import REALM, digest

CONTEXT = 'faxbot-fax-server'
TRANSPORT = 'transport-fax-server'
TRANSPORT_TCP = 'transport-fax-server-tcp'
# Asterisk listens for fax servers on its own port inside the container; docker-compose.fax-server.yml publishes it
# on your network's address only (port 5060 there by default), never on the carrier's connection.
CONTAINER_PORT = 5080
ADDRESS = '@FAXBOT_FAX_SERVER_ADDRESS@'
PORT = '@FAXBOT_FAX_SERVER_PORT@'
FOLDER = 'fax-server'
PRIVATE = (ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
           ipaddress.ip_network('192.168.0.0/16'))
DEFAULT_PER_HOUR, DEFAULT_PER_DAY = 60, 500
MAX_PER_HOUR, MAX_PER_DAY = 3600, 50000
NOTICE_TRIES = 3
# A call answered this long ago whose hand-over never came is closed as not sent, with a notice (never sent late),
# but only once its image has been quiet this long: a long fax can still be arriving long after the call began.
HANDOVER_WAIT = timedelta(minutes=20)
IMAGE_QUIET = timedelta(minutes=10)
DIAL_PREFIX = re.compile(r'[0-9*#]{1,4}')
TABLES = ('fax_servers', 'fax_server_calls', 'fax_server_events')
OUTCOMES = ('not_received', 'accepted', 'refused')
FINAL = ('delivered', 'failed', 'uncertain')
NOT_DELIVERED = ('failed', 'uncertain', 'refused')
log = logging.getLogger(__name__)


class FaxServerError(ValueError):
    """A fax server change Faxbot refuses, in one sentence."""


@dataclass(frozen=True)
class FaxServer:
    id: str
    name: str
    address: str
    username: str
    md5_cred: str
    mailbox_id: str | None
    countries: tuple[str, ...]
    per_hour: int
    per_day: int
    dial_prefix: str | None
    contact_email: str | None
    contact_webhook: str | None
    state: str
    secret_envelope: str | None = None
    key_binding_id: str | None = None
    key_principal_id: str | None = None

    @property
    def endpoint(self):
        return f'faxserver-{self.id}'


def _tables(bind):
    from .routing.database import reflect
    return reflect(bind, TABLES)


def _server(row):
    return FaxServer(row['server_id'], row['name'], row['address'], row['username'], row['md5_cred'],
                     row['mailbox_id'], tuple(json.loads(row['countries'] or '[]')), row['per_hour'], row['per_day'],
                     row['dial_prefix'], row['contact_email'], row['contact_webhook'], row['state'],
                     row['secret_envelope'], row['key_binding_id'], row['key_principal_id'])


def servers(engine, *, active_only=True):
    """Every fax server, newest row per server."""
    table = _tables(engine)['fax_servers']
    with engine.connect() as connection:
        rows = connection.execute(sa.select(table).order_by(table.c.created_at, table.c.id)).mappings().all()
    newest = {}
    for row in rows:
        newest[row['server_id']] = row
    found = [_server(row) for row in newest.values()]
    return [item for item in found if item.state == 'active'] if active_only else found


def find(engine, server_id, *, active_only=True):
    return next((item for item in servers(engine, active_only=active_only) if item.id == server_id), None)


class ServerSecrets:
    """Seal a fax server's own sending key with the installation configuration key, bound to the server."""

    KIND = 'fax_server'

    def __init__(self, configuration):
        self.configuration = configuration

    def _context(self):
        with self.configuration.engine.connect() as connection:
            head = self.configuration._head(connection)
        if head is None:
            raise FaxServerError('Faxbot is not set up yet.')
        return self.configuration._cipher(), head['installation_id']

    def seal(self, token, server_id):
        cipher, installation = self._context()
        return cipher.seal({'key_token': token}, installation_id=installation, kind=self.KIND, record_id=server_id)

    def token(self, server):
        cipher, installation = self._context()
        return dict(cipher.open(server.secret_envelope, installation_id=installation, kind=self.KIND,
                                record_id=server.id))['key_token']


# Checking what you enter -----------------------------------------------------------------------------------------

def private_address(text):
    """The fax server's one IPv4 address on your private network, or FaxServerError."""
    try:
        address = ipaddress.ip_address(str(text or '').strip())
    except ValueError:
        raise FaxServerError("Enter the fax server's address on your network, such as 192.168.1.40.") from None
    if address.version != 4 or not any(address in network for network in PRIVATE):
        raise FaxServerError('Faxbot takes faxes to send on only from a fax server on your private network '
                             '(10.x, 172.16-31.x or 192.168.x).')
    return str(address)


def clean_countries(codes, home):
    import phonenumbers
    found = []
    for code in codes or (home,):
        code = str(code or '').strip().upper()
        if not re.fullmatch(r'[A-Z]{2}', code) or phonenumbers.country_code_for_region(code) == 0:
            raise FaxServerError(f"{code or 'That'} isn't a country code Faxbot knows; use two letters, such as US.")
        if code not in found:
            found.append(code)
    return tuple(found)


def _limit(value, default, top, what):
    value = default if value is None else value
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > top:
        raise FaxServerError(f'Faxes {what} is a whole number from 1 to {top:,}.')
    return value


def _email(text):
    text = (text or '').strip()
    if not text:
        return None
    if len(text) > 254 or not re.fullmatch(r'[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+', text):
        raise FaxServerError(f"{text} isn't an email address Faxbot can send a notice to.")
    return text


def _webhook(text):
    from urllib.parse import urlparse
    text = (text or '').strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or len(text) > 512:
        raise FaxServerError('A notice address for your systems starts with https:// (or http:// on your own '
                             'network).')
    if parsed.scheme == 'http':
        try:
            local = any(ipaddress.ip_address(parsed.hostname) in network for network in PRIVATE)
        except ValueError:
            local = False
        if not local:
            raise FaxServerError('A notice address outside your network must start with https://.')
    return text


def _prefix(text):
    text = (text or '').strip()
    if not text:
        return None
    if not DIAL_PREFIX.fullmatch(text):
        raise FaxServerError('An outside-line prefix is 1 to 4 digits, such as 9.')
    return text


def _row(server_id, name, address, username, md5_cred, mailbox_id, countries, per_hour, per_day, dial_prefix,
         contact_email, contact_webhook, state, envelope, binding, principal, actor, now):
    return dict(id=uuid.uuid4().hex, server_id=server_id, name=name, address=address, username=username,
                md5_cred=md5_cred, mailbox_id=mailbox_id, countries=json.dumps(list(countries)), per_hour=per_hour,
                per_day=per_day, dial_prefix=dial_prefix, contact_email=contact_email,
                contact_webhook=contact_webhook, state=state, secret_envelope=envelope, key_binding_id=binding,
                key_principal_id=principal, recorded_by_name=(actor or {}).get('name'), created_at=now)


def add(engine, *, name, address, home='US', countries=(), per_hour=None, per_day=None, dial_prefix=None,
        contact_email=None, contact_webhook=None, mailbox_id=None, taken=(), actor=None, now=None, key=None,
        seal=None):
    """A new fax server and its password, shown once: (FaxServer, password).

    ``taken`` are addresses Faxbot already recognises as something else (a phone system or a partner's tunnel), which
    a fax server may not share. ``key`` is (token, binding id, principal id) of the server's own sending key and
    ``seal`` a ``ServerSecrets``; the token is kept sealed."""
    from .routing.database import utcnow, write_transaction
    name = ' '.join(str(name or '').split())
    if not name or len(name) > 100:
        raise FaxServerError('Name the fax server, such as RightFax.')
    address = private_address(address)
    if address in set(taken):
        raise FaxServerError(f'Faxbot already knows {address} as your phone system or a partner; a fax server needs '
                             'its own address.')
    if any(item.address == address for item in servers(engine)):
        raise FaxServerError(f'{address} is already a fax server.')
    server_id = uuid.uuid4().hex
    username = f'faxserver-{server_id[:8]}'
    password = secrets.token_urlsafe(18)
    envelope = seal.seal(key[0], server_id) if key is not None and seal is not None else None
    row = _row(server_id, name, address, username, digest(username, password), mailbox_id,
               clean_countries(countries, home), _limit(per_hour, DEFAULT_PER_HOUR, MAX_PER_HOUR, 'an hour'),
               _limit(per_day, DEFAULT_PER_DAY, MAX_PER_DAY, 'a day'), _prefix(dial_prefix), _email(contact_email),
               _webhook(contact_webhook), 'active', envelope, key[1] if key else None, key[2] if key else None, actor,
               now or utcnow())
    with write_transaction(engine) as connection:
        connection.execute(_tables(connection)['fax_servers'].insert().values(**row))
    return find(engine, server_id), password


def change(engine, server_id, *, home='US', actor=None, now=None, **fields):
    """Save new limits or contacts for a fax server (a new row; its address and password stay)."""
    from .routing.database import utcnow, write_transaction
    found = find(engine, server_id)
    if found is None:
        raise FaxServerError('No fax server has that ID.')
    countries = clean_countries(fields['countries'], home) if fields.get('countries') is not None else found.countries
    per_hour = _limit(fields.get('per_hour'), found.per_hour, MAX_PER_HOUR, 'an hour')
    per_day = _limit(fields.get('per_day'), found.per_day, MAX_PER_DAY, 'a day')
    prefix = _prefix(fields['dial_prefix']) if 'dial_prefix' in fields and fields['dial_prefix'] is not None \
        else found.dial_prefix
    email = _email(fields['contact_email']) if fields.get('contact_email') is not None else found.contact_email
    webhook = (_webhook(fields['contact_webhook']) if fields.get('contact_webhook') is not None
               else found.contact_webhook)
    row = _row(found.id, found.name, found.address, found.username, found.md5_cred, found.mailbox_id, countries,
               per_hour, per_day, prefix, email, webhook, 'active', found.secret_envelope, found.key_binding_id,
               found.key_principal_id, actor, now or utcnow())
    with write_transaction(engine) as connection:
        connection.execute(_tables(connection)['fax_servers'].insert().values(**row))
    return find(engine, server_id)


def remove(engine, server_id, *, actor=None, now=None):
    from .routing.database import utcnow, write_transaction
    found = find(engine, server_id)
    if found is None:
        raise FaxServerError('No fax server has that ID.')
    row = _row(found.id, found.name, found.address, found.username, found.md5_cred, found.mailbox_id,
               found.countries, found.per_hour, found.per_day, found.dial_prefix, found.contact_email,
               found.contact_webhook, 'removed', None, found.key_binding_id, found.key_principal_id, actor,
               now or utcnow())
    with write_transaction(engine) as connection:
        connection.execute(_tables(connection)['fax_servers'].insert().values(**row))
    return found


# pjsip.conf ------------------------------------------------------------------------------------------------------

def render(items) -> str:
    """The pjsip.conf sections for fax servers ('' for none): their own transport (UDP and TCP) on your network's
    address, and per server an MD5 auth, an endpoint that only its one address may use and that is found only by
    that address (``identify_by=ip``: a SIP user name never selects it), and the identify by that address. No AOR:
    Faxbot never calls a fax server, and nothing registers."""
    items = sorted((item for item in items or () if item.state == 'active'), key=lambda item: item.address)
    if not items:
        return ''
    lines = ['; Fax servers that send their faxes through Faxbot (fax_servers.py), such as RightFax with an SR140.',
             '; Faxbot writes these from Delivery setup; edits here are replaced.']
    for name, protocol in ((TRANSPORT, 'udp'), (TRANSPORT_TCP, 'tcp')):
        # The address Faxbot is published on in your network (docker-compose.fax-server.yml); Asterisk fills it in
        # at start, or drops these lines. No local_net: every fax server is on that network.
        lines += [f'[{name}]', 'type=transport', f'protocol={protocol}', f'bind=0.0.0.0:{CONTAINER_PORT}',
                  f'external_media_address={ADDRESS}', f'external_signaling_address={ADDRESS}',
                  f'external_signaling_port={PORT}', '']
    for item in items:
        name = item.endpoint
        lines += [f'[{name}-auth]', 'type=auth', 'auth_type=md5', f'username={item.username}', f'realm={REALM}',
                  f'md5_cred={item.md5_cred}', '',
                  f'[{name}]', 'type=endpoint', f'transport={TRANSPORT}', f'auth={name}-auth', f'context={CONTEXT}',
                  'identify_by=ip', 'disallow=all', 'allow=ulaw,alaw', 't38_udptl=yes', 't38_udptl_ec=redundancy',
                  't38_udptl_maxdatagram=400', 'direct_media=no', 'rtp_symmetric=yes', 'force_rport=yes',
                  'rewrite_contact=no', 'allow_subscribe=no', 'deny=0.0.0.0/0.0.0.0',
                  f'permit={item.address}/255.255.255.255', f'set_var=FAXBOT_FAX_SERVER={item.id}',
                  f'set_var=FAXBOT_FAX_SERVER_ADDRESS={item.address}', '',
                  f'[{name}-identify]', 'type=identify', f'endpoint={name}', f'match={item.address}', '']
    return '\n'.join(lines)


# Before answering ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Admission:
    verdict: str            # 'send' (answer and send on), 'receive' (one of your numbers), 'refuse'
    number: str | None = None
    code: int | None = None
    reason: str = ''

    @property
    def answer(self):
        """The one line the dialplan reads: send:+E164, receive:+E164 or refuse:CODE."""
        if self.verdict == 'refuse':
            return f'refuse:{self.code}'
        return f'{self.verdict}:{self.number}'


class _Refuse(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


def call_key(call_id):
    return hashlib.sha256(str(call_id).encode('utf-8')).hexdigest()


def job_id_for(server_id, key):
    """The outbound fax's ID for one fax server call, so a repeated hand-over never makes a second fax."""
    return hashlib.sha256(f'fax-server:{server_id}:{key}'.encode('utf-8')).hexdigest()[:32]


def read_number(server, dialled, home):
    """The E.164 number a fax server dialled, after its outside-line prefix; ``_Refuse`` for an emergency, short or
    unreadable number."""
    import phonenumbers
    from phonenumbers import shortnumberinfo
    text = re.sub(r'[^0-9+*#]', '', str(dialled or ''))[:40]
    if server.dial_prefix and text.startswith(server.dial_prefix) and len(text) > len(server.dial_prefix) + 6:
        text = text[len(server.dial_prefix):]
    digits = text.lstrip('+')
    if not digits or not digits.isdigit():
        raise _Refuse(484, 'it dialled no number Faxbot can read')
    if shortnumberinfo.connects_to_emergency_number(digits, home) or len(digits) < 7:
        raise _Refuse(403, 'Faxbot never sends a fax to an emergency or short number')
    for candidate in ((text,) if text.startswith('+') else (text, '+' + digits)):
        try:
            parsed = phonenumbers.parse(candidate, None if candidate.startswith('+') else home)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed):
            if shortnumberinfo.is_emergency_number(str(parsed.national_number), home):
                raise _Refuse(403, 'Faxbot never sends a fax to an emergency or short number')
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    raise _Refuse(484, 'it dialled a number Faxbot cannot read')


def _region(number):
    import phonenumbers
    return phonenumbers.region_code_for_number(phonenumbers.parse(number, None))


def _guard(connection, number, home, now):
    """Faxbot's dialing guard on the dialled number (its class: country, premium-rate, special and satellite)."""
    from .routing import guard
    found = guard.dial_class(number, home)
    if found is None:
        raise _Refuse(403, 'Faxbot cannot tell what kind of number it is')
    guard.ensure_history_on(connection, home, now)
    verdict = guard.verdict_on(connection, found, guard.policy_on(connection), home, number=number)
    if not verdict.allowed:
        raise _Refuse(403, f'Faxbot may not dial {guard.class_text(found.key)}')


def _local_day_start(now, zone_name):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from datetime import timezone
    try:
        zone = ZoneInfo(zone_name) if zone_name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    local = now.replace(tzinfo=timezone.utc).astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def _answered_since(connection, calls, server_id, since):
    return connection.execute(sa.select(sa.func.count()).select_from(calls).where(
        calls.c.server_id == server_id, calls.c.answered == 1, calls.c.created_at >= since)).scalar() or 0


def _event_on(connection, events, call_id, kind, now, *, detail=None, job_id=None, pages=None):
    connection.execute(events.insert().values(id=uuid.uuid4().hex, call_id=call_id, kind=kind,
                                              detail=(detail or None) and detail[:300], job_id=job_id, pages=pages,
                                              created_at=now))


def admit(engine, values, server_id, *, address, dialled, call_id, caller=None, uniqueid=None, rules=None, now=None):
    """Decide, before the call is answered, whether Faxbot takes this fax server's fax, and record the call.

    ``rules(number)`` is the sending rules' check (None when the rules would send it, else why not); it runs before
    the record is written. Every refusal is recorded too (``turned_away``), except one from an address or server
    Faxbot does not know, which never reaches a fax server record. Returns an ``Admission``."""
    from .routing.database import utcnow, write_transaction
    now = now or utcnow()
    home = getattr(values, 'fax_default_country', 'US') or 'US'
    server = find(engine, str(server_id or ''))
    if server is None:
        return Admission('refuse', code=403, reason='no fax server is enrolled with that ID')
    if str(address or '').strip() != server.address:
        log.warning('A call for fax server %s came from another address and was refused.', server.name)
        return Admission('refuse', code=403, reason='the call came from another address')
    if not call_id:
        return Admission('refuse', code=400, reason='the call has no SIP Call-ID')
    key = call_key(call_id)
    number, refusal = None, None
    try:
        number = read_number(server, dialled, home)
        from .routing.own_numbers import receiving_numbers
        if number in receiving_numbers(values):
            return Admission('receive', number=number)
        region = _region(number)
        if region not in server.countries:
            raise _Refuse(403, f'{server.name} may not send to {region or "that country"}')
        if rules is not None:
            why = rules(number)
            if why:
                raise _Refuse(403, why)
    except _Refuse as stop:
        refusal = stop
    tables = _tables(engine)
    calls, events = tables['fax_server_calls'], tables['fax_server_events']
    call_id_row = uuid.uuid4().hex
    with write_transaction(engine) as connection:
        if connection.execute(sa.select(calls.c.id).where(calls.c.server_id == server.id,
                                                          calls.c.call_key == key)).first() is not None:
            # One SIP Call-ID is one fax: a second call with it is never a second fax.
            return Admission('refuse', code=482, reason='this call was already taken')
        if refusal is None:
            try:
                _guard(connection, number, home, now)
                if _answered_since(connection, calls, server.id, now - timedelta(hours=1)) >= server.per_hour:
                    raise _Refuse(486, f'{server.name} reached its {server.per_hour:,} faxes an hour')
                day = _local_day_start(now, getattr(values, 'time_zone', '') or '')
                if _answered_since(connection, calls, server.id, day) >= server.per_day:
                    raise _Refuse(486, f'{server.name} reached its {server.per_day:,} faxes a day')
            except _Refuse as stop:
                refusal = stop
        connection.execute(calls.insert().values(
            id=call_id_row, server_id=server.id, call_key=key, uniqueid=(str(uniqueid)[:40] if uniqueid else None),
            dialled=re.sub(r'[^0-9+*#]', '', str(dialled or ''))[:40] or None, destination=number,
            caller=re.sub(r'[^0-9+]', '', str(caller or ''))[:32] or None, answered=0 if refusal else 1,
            created_at=now))
        if refusal is not None:
            _event_on(connection, events, call_id_row, 'turned_away', now,
                      detail=f'Turned away before answering: {refusal.reason}.')
            return Admission('refuse', number=number, code=refusal.code, reason=refusal.reason)
        _event_on(connection, events, call_id_row, 'admitted', now)
    return Admission('send', number=number)


def rules_check(app, server):
    """The sending rules and the dialing guard's full check (its price ceiling too) for this server's fax, before
    answering: a fax the rules would hold, or send by no account, is refused rather than taken and held."""
    from .access.types import AccessError
    from .routing.background import installation_engine
    from .routing.database import DeliveryStoreError
    engine, runtime = installation_engine(app)
    access = getattr(app.state, 'access_runtime', None)
    if engine is None or runtime is None or access is None or server.secret_envelope is None:
        return lambda number: 'Faxbot was not ready to take a fax'

    def check(number):
        from .access.system_outbound import _hold_kind
        from .routing import guard
        from .routing.rules_acceptance import prepare
        try:
            actor = access.authentication.header_key(ServerSecrets(runtime.manager.store).token(server))
        except AccessError:
            return f"{server.name}'s sending key was revoked; enrol it again"
        revision = runtime.manager.store.read().active
        try:
            plan = prepare(engine, revision, actor=actor, destination=number, pages=1, mailbox=server.mailbox_id)
        except DeliveryStoreError:
            return 'Faxbot could not read its sending rules'
        held = _hold_kind(plan.decision)
        if held is not None:
            from .access.system_outbound import HOLD_REASONS
            return HOLD_REASONS[held][0]
        from .routing.database import write_transaction
        with write_transaction(engine) as connection:
            stop = guard.check_on(connection, plan.dialing, datetime.utcnow())
        return stop.sentence.split('. ')[0].rstrip('.') if stop is not None else None
    return check


# The fax, handed over once the call ends ---------------------------------------------------------------------------

def call_for(engine, server_id, *, call_id=None, uniqueid=None):
    calls = _tables(engine)['fax_server_calls']
    with engine.connect() as connection:
        if call_id:
            row = connection.execute(sa.select(calls).where(calls.c.server_id == server_id,
                                                            calls.c.call_key == call_key(call_id))).mappings().first()
            if row is not None:
                return dict(row)
        if uniqueid:
            row = connection.execute(sa.select(calls).where(calls.c.server_id == server_id,
                                                            calls.c.uniqueid == str(uniqueid)[:40])).mappings().first()
            if row is not None:
                return dict(row)
    return None


def events_for(engine, call_ids):
    """{call ID: [event rows, oldest first]}."""
    events = _tables(engine)['fax_server_events']
    found = {call_id: [] for call_id in call_ids}
    if not found:
        return found
    with engine.connect() as connection:
        for row in connection.execute(sa.select(events).where(events.c.call_id.in_(list(found))).order_by(
                events.c.created_at, events.c.id)).mappings():
            found[row['call_id']].append(dict(row))
    return found


def record(engine, call_id, kind, *, detail=None, job_id=None, pages=None, now=None):
    """One step of a call; False when that step was already recorded (each happens once, under the write lock)."""
    from .routing.database import utcnow, write_transaction
    events = _tables(engine)['fax_server_events']
    with write_transaction(engine) as connection:
        if connection.execute(sa.select(events.c.id).where(events.c.call_id == call_id,
                                                           events.c.kind == kind)).first() is not None:
            return False
        _event_on(connection, events, call_id, kind, now or utcnow(), detail=detail, job_id=job_id, pages=pages)
    return True


class HandoverError(RuntimeError):
    """The hand-over cannot be taken now; the image stays where it is."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _unlink(path):
    import os
    try:
        os.unlink(path)
    except OSError:
        pass


def hand_over(app, payload, tiff_path):
    """Take a fax server's fax after its call: send it on, or record why not. Returns {'status', 'detail', 'queued'}.

    Idempotent per call: a repeated hand-over answers with what the first one did."""
    import os
    import tempfile
    from .access.types import AccessError
    from .conversion import DocumentConversionError, tiff_to_pdf
    from .routing.background import installation_engine
    from .routing.database import DeliveryStoreError
    from .routing.submit import GeneratedFaxBusy, accept_generated_fax
    engine, runtime = installation_engine(app)
    if engine is None or runtime is None:
        raise HandoverError(503, 'Faxbot is starting.')
    server_id = str(payload.get('server') or '')
    call = call_for(engine, server_id, call_id=payload.get('call_id'), uniqueid=payload.get('uniqueid'))
    if call is None:
        raise HandoverError(404, 'Faxbot has no record of this call.')
    done = [event for event in events_for(engine, [call['id']])[call['id']] if event['kind'] in OUTCOMES]
    if done:
        if tiff_path:
            _unlink(tiff_path)
        first = done[0]
        return {'status': first['kind'], 'detail': first['detail'] or '', 'queued': first['job_id']}
    server = find(engine, server_id, active_only=False)
    name = server.name if server is not None else 'The fax server'
    pages = payload.get('faxpages') if isinstance(payload.get('faxpages'), int) else None
    if str(payload.get('faxstatus') or '').upper() != 'SUCCESS' or not tiff_path or not os.path.isfile(tiff_path):
        detail = (f'The call ended before every page arrived, so Faxbot sent nothing on; {name} shows this fax as '
                  'failed and can send it again.')
        record(engine, call['id'], 'not_received', detail=detail, pages=pages)
        if tiff_path:
            _unlink(tiff_path)
        return {'status': 'not_received', 'detail': detail, 'queued': None}

    def refuse(why):
        detail = f'{name} handed over this fax, but Faxbot did not send it: {why}'
        record(engine, call['id'], 'refused', detail=detail)
        _unlink(tiff_path)
        return {'status': 'refused', 'detail': detail, 'queued': None}
    if server is None or server.state != 'active' or server.secret_envelope is None:
        return refuse('the fax server was removed from Faxbot.')
    access = getattr(app.state, 'access_runtime', None)
    if access is None:
        raise HandoverError(503, 'Faxbot is starting.')
    try:
        actor = access.authentication.header_key(ServerSecrets(runtime.manager.store).token(server))
    except AccessError:
        return refuse("its sending key was revoked; enrol the fax server again.")
    folder = tempfile.mkdtemp(prefix='fax-server-')
    pdf = os.path.join(folder, 'fax.pdf')
    try:
        try:
            pages, _ = tiff_to_pdf(tiff_path, pdf)
            with open(pdf, 'rb') as handle:
                document = handle.read()
        except (DocumentConversionError, OSError):
            return refuse("its pages couldn't be read.")
    finally:
        for entry in os.listdir(folder):
            os.unlink(os.path.join(folder, entry))
        os.rmdir(folder)
    job_id = job_id_for(server.id, call['call_key'])
    events = _tables(engine)['fax_server_events']

    def accepted(connection, at, job):
        _event_on(connection, events, call['id'], 'accepted', at, job_id=job, pages=pages)
    revision = runtime.manager.store.read().active
    try:
        queued = accept_generated_fax(runtime, access, actor, revision, to_number=call['destination'],
                                      document=document, file_name=f'{server.username}.pdf', pages=pages,
                                      job_id=job_id, mailbox=server.mailbox_id, after=accepted)
    except GeneratedFaxBusy:
        raise HandoverError(503, 'This fax is being queued right now.') from None
    except AccessError:
        return refuse("its sending key may not send faxes any more; enrol the fax server again.")
    except RuntimeError as error:
        # Outbound delivery is off in this configuration (accept_generated_fax's documented refusal).
        return refuse(str(error).rstrip('.') + '.')
    except DeliveryStoreError:
        raise HandoverError(503, 'Faxbot could not record the fax just now.') from None
    _unlink(tiff_path)
    # A repeated hand-over of a fax already queued returns its ID without running ``accepted`` again.
    record(engine, call['id'], 'accepted', job_id=queued, pages=pages)
    return {'status': 'accepted', 'detail': '', 'queued': queued}


# The real outcome, and the notice ---------------------------------------------------------------------------------

def _outcome(engine, job_id, name):
    """(kind, detail) once the outbound fax is finished, else None."""
    from .routing.database import read_connection, reflect
    found = reflect(engine, ('outbound_deliveries', 'outbound_attempts', 'fax_jobs'))
    deliveries, attempts, jobs = found['outbound_deliveries'], found['outbound_attempts'], found['fax_jobs']
    with read_connection(engine) as connection:
        state = connection.execute(sa.select(deliveries.c.state).where(deliveries.c.id == job_id)).scalar()
        categories = connection.execute(sa.select(attempts.c.error_category).where(
            attempts.c.job_id == job_id)).scalars().all()
        error = connection.execute(sa.select(jobs.c.error).where(jobs.c.id == job_id)).scalar()
        # A fax a person settled while its call's outcome was unknown keeps that attempt uncertain.
        unknown = connection.execute(sa.select(attempts.c.phase).select_from(attempts.join(
            deliveries, deliveries.c.attempt_id == attempts.c.id)).where(deliveries.c.id == job_id)).scalar()
    if state == 'success':
        return 'delivered', 'Delivered.'
    if state == 'failed' and unknown == 'uncertain':
        return 'uncertain', ('Faxbot cannot confirm whether the fax arrived, so it does not send it again; check with '
                             'the recipient first.')
    if state in ('failed', 'cancelled'):
        if any(category in ('partly_sent', 'pages_unconfirmed') for category in categories):
            return 'uncertain', ("The call ended partway, and part of the fax may have arrived. Faxbot does not send "
                                 'it again; check with the recipient first.')
        reason = f': {error}' if isinstance(error, str) and error else '.'
        if state == 'cancelled':
            return 'failed', f'It was cancelled in Faxbot before it was delivered{reason}'
        return 'failed', f'Faxbot could not deliver it{reason}'
    if state == 'reconciliation_required':
        return 'uncertain', ('Faxbot cannot confirm whether the fax arrived, so it does not send it again; check with '
                             'the recipient first.')
    return None


def notice_body(server, call, kind, detail):
    """The notice for a fax that was not delivered: no page content, only what the fax server's admin needs."""
    word = {'failed': 'was not delivered', 'uncertain': 'may not have been delivered',
            'refused': 'was not sent'}[kind]
    return {'event': 'fax_not_delivered', 'state': kind, 'fax_server': server.name if server else None,
            'to': call['destination'], 'dialled': call['dialled'], 'call_received_at': call['created_at'].isoformat(),
            'sentence': f"The fax {server.name if server else 'your fax server'} handed to Faxbot for "
                        f"{call['destination']} {word}. {detail}".strip()}


def _send_email(values, address, body):
    from email.message import EmailMessage
    from email.utils import formatdate, make_msgid
    from types import SimpleNamespace
    from .intake.email import AmbiguousFailure, DefiniteFailure, send
    host = getattr(values, 'intake_smtp_host', '')
    sender = getattr(values, 'intake_email_from', '')
    if not host or not sender:
        return 'failed', 'Set up outgoing email (Delivery setup → Email and folders) so Faxbot can send notices.'
    settings = SimpleNamespace(host=host, port=int(getattr(values, 'intake_smtp_port', 587) or 587),
                               security=getattr(values, 'intake_smtp_security', 'starttls'),
                               username=getattr(values, 'intake_smtp_username', ''), from_address=sender,
                               recipients=(address,))
    message = EmailMessage()
    message['From'], message['To'] = sender, address
    outcome = 'may not have arrived' if body['state'] == 'uncertain' else 'was not delivered'
    message['Subject'] = f"Fax to {body['to']} {outcome}"
    message['Date'] = formatdate(usegmt=True)
    message['Message-ID'] = make_msgid(domain=sender.rpartition('@')[2] or 'faxbot.invalid')
    message.set_content(body['sentence'] + '\n')
    try:
        send(settings, getattr(values, 'intake_smtp_password', ''), message)
    except DefiniteFailure as failure:
        return 'failed', str(failure)
    except AmbiguousFailure:
        return 'uncertain', 'The email server stopped answering after the notice was sent; it is not sent again.'
    return 'sent', f'Notice emailed to {address}.'


def _send_webhook(url, body):
    import httpx
    try:
        response = httpx.post(url, json=body, timeout=10.0, follow_redirects=False)
    except httpx.ConnectError:
        return 'failed', 'Your notice address could not be reached.'
    except httpx.HTTPError:
        return 'uncertain', 'Your notice address stopped answering after the notice was sent; it is not sent again.'
    if 200 <= response.status_code < 300:
        return 'sent', 'Notice sent to your systems.'
    return 'failed', f'Your notice address answered {response.status_code}.'


def notify(engine, values, server, call, kind, detail, *, now=None, email=_send_email, webhook=_send_webhook):
    """Send the failure notice once: email and webhook, whichever the server has. Records notice_sent, notice_failed
    (tried again later, up to NOTICE_TRIES) or notice_uncertain (never sent again)."""
    if server is None or not (server.contact_email or server.contact_webhook):
        return None
    body = notice_body(server, call, kind, detail)
    results = []
    if server.contact_email:
        results.append(email(values, server.contact_email, body))
    if server.contact_webhook:
        results.append(webhook(server.contact_webhook, body))
    words = ' '.join(sentence for _, sentence in results)
    if any(state == 'sent' for state, _ in results):
        record(engine, call['id'], 'notice_sent', detail=words, now=now)
        return 'notice_sent'
    if any(state == 'uncertain' for state, _ in results):
        record(engine, call['id'], 'notice_uncertain', detail=words, now=now)
        return 'notice_uncertain'
    from .routing.database import utcnow, write_transaction
    events = _tables(engine)['fax_server_events']
    with write_transaction(engine) as connection:
        # One row per failed try: settle tries again later, up to NOTICE_TRIES in all.
        _event_on(connection, events, call['id'], 'notice_failed', now or utcnow(), detail=words)
    return 'notice_failed'


def settle(engine, values, *, now=None, email=_send_email, webhook=_send_webhook):
    """Close calls whose hand-over never came, record each sent-on fax's real outcome, and send failure notices.
    Returns [(call ID, kind)] for what changed."""
    import os
    from .routing.database import utcnow
    now = now or utcnow()
    tables = _tables(engine)
    calls = tables['fax_server_calls']
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(sa.select(calls).where(
            calls.c.answered == 1, calls.c.created_at >= now - timedelta(days=30))).mappings()]
    history = events_for(engine, [row['id'] for row in rows])
    known = {item.id: item for item in servers(engine, active_only=False)}
    changed = []
    for call in rows:
        kinds = {event['kind']: event for event in history[call['id']]}
        server = known.get(call['server_id'])
        name = server.name if server is not None else 'The fax server'
        if not any(kind in kinds for kind in OUTCOMES) and call['created_at'] < now - HANDOVER_WAIT:
            image = os.path.join(getattr(values, 'fax_data_dir', ''), FOLDER, f"{call['uniqueid']}.tiff")
            try:
                written = datetime.utcfromtimestamp(os.stat(image).st_mtime) if call['uniqueid'] else None
            except OSError:
                written = None
            if written is not None and written > now - IMAGE_QUIET:
                continue  # pages are still arriving, or the hand-over is on its way: never closed under it
            if written is not None:
                detail = (f'{name} sent this fax, but its hand-over to Faxbot never finished, so Faxbot did not send '
                          f'it; send it again from {name}.')
                if record(engine, call['id'], 'refused', detail=detail, now=now):
                    kinds['refused'] = {'kind': 'refused', 'detail': detail}
                    changed.append((call['id'], 'refused'))
                _unlink(image)
            else:
                detail = 'The call ended without a fax, so Faxbot sent nothing on.'
                if record(engine, call['id'], 'not_received', detail=detail, now=now):
                    changed.append((call['id'], 'not_received'))
                continue
        if 'accepted' in kinds and not any(kind in kinds for kind in FINAL):
            found = _outcome(engine, kinds['accepted']['job_id'], name)
            if found is not None and record(engine, call['id'], found[0], detail=found[1],
                                            job_id=kinds['accepted']['job_id'], now=now):
                kinds[found[0]] = {'kind': found[0], 'detail': found[1]}
                changed.append((call['id'], found[0]))
        bad = next((kind for kind in NOT_DELIVERED if kind in kinds), None)
        if bad is None or 'notice_sent' in kinds or 'notice_uncertain' in kinds:
            continue
        tries = [event for event in history[call['id']] if event['kind'] == 'notice_failed']
        if len(tries) >= NOTICE_TRIES or (tries and tries[-1]['created_at'] > now - timedelta(minutes=10)):
            continue
        result = notify(engine, values, server, call, bad, kinds[bad].get('detail') or '', now=now, email=email,
                        webhook=webhook)
        if result:
            changed.append((call['id'], result))
    return changed


def attach_settle(app):
    """The background step: settle calls every minute (copiers_http's router lifespan)."""
    from .config_runtime import run_lifecycle_step
    from .routing.background import installation_engine

    async def step():
        engine, runtime = installation_engine(app)
        if engine is None or runtime is None:
            return False
        values = runtime.manager.store.read().active.values
        await run_lifecycle_step(lambda: settle(engine, values))
        return False
    return step


# What the console and the command line show --------------------------------------------------------------------

STATE_WORDS = {
    'admitted': 'Receiving now', 'turned_away': 'Turned away', 'not_received': 'Not received',
    'accepted': 'Sending', 'refused': 'Not sent', 'delivered': 'Delivered', 'failed': 'Failed',
    'uncertain': 'Uncertain',
}


def call_view(call, history, server):
    kinds = [event['kind'] for event in history]
    state = next((kind for kind in ('delivered', 'failed', 'uncertain', 'refused', 'not_received', 'accepted',
                                    'turned_away', 'admitted') if kind in kinds), 'admitted')
    event = next(event for event in reversed(history) if event['kind'] == state) if state in kinds else {}
    accepted = next((event for event in history if event['kind'] == 'accepted'), {})
    notice = next((event for event in reversed(history) if event['kind'].startswith('notice_')), None)
    sentence = event.get('detail') or {
        'admitted': 'Receiving the fax now.', 'accepted': 'Faxbot has the fax and is sending it on.'}.get(state, '')
    if state == 'accepted' and server is not None:
        sentence = f'Faxbot has the fax from {server.name} and is sending it on; it is in Sent.'
    return {'id': call['id'], 'server_id': call['server_id'], 'server': server.name if server else None,
            'to': call['destination'], 'dialled': call['dialled'], 'state': state, 'state_text': STATE_WORDS[state],
            'sentence': sentence, 'fax_id': accepted.get('job_id'), 'pages': accepted.get('pages'),
            'received_at': call['created_at'],
            'notice': notice['kind'][len('notice_'):] if notice else None,
            'notice_text': notice['detail'] if notice else None}


def calls(engine, *, server_id=None, limit=100):
    table = _tables(engine)['fax_server_calls']
    query = sa.select(table).order_by(table.c.created_at.desc(), table.c.id).limit(max(1, min(limit, 500)))
    if server_id:
        query = query.where(table.c.server_id == server_id)
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(query).mappings()]
    history = events_for(engine, [row['id'] for row in rows])
    known = {item.id: item for item in servers(engine, active_only=False)}
    return [call_view(row, history[row['id']], known.get(row['server_id'])) for row in rows]


def where_to_point(values=None):
    """Where the fax server sends its calls: Faxbot's address on your network and port, from the overlay."""
    import os
    address = os.environ.get('FAXBOT_FAX_SERVER_ADDRESS') or os.environ.get('FAXBOT_LAN_ADDRESS')
    port = os.environ.get('FAXBOT_FAX_SERVER_PORT') or '5060'
    if address:
        return (f"In the fax server's SIP settings (on RightFax, the SR140's Primary Gateway), send fax calls to "
                f'{address}, port {port}, UDP, with T.38 on, and give it the user name and password below for '
                'authentication.')
    return ("Start Faxbot with docker-compose.fax-server.yml and FAXBOT_LAN_ADDRESS set to this computer's address on "
            'your network, on a Linux host, then send the fax server\'s calls (on RightFax, the SR140\'s Primary '
            'Gateway) to that address, port 5060, UDP, with T.38 on.')
