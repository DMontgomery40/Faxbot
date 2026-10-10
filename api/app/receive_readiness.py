"""Receiving readiness, number by number, and who owns each number's faxes (brief 92, RF).

Sending and receiving fail separately. A green internet or sending check says nothing about whether a fax sent
*to* this office can arrive: an office on a mobile backup line may send fine while no carrier can reach it. So
this check takes **no** sending evidence at all. For each number Faxbot receives on it reads:

- **for a trunk**: whether the trunk is signed in (the fax engine's registration, or "not needed" for a trunk the
  carrier reaches by address), and when the last call to the number arrived (the carrier's last INVITE, from
  ``sip_call_records``). Faxbot keeps no record of the carrier's OPTIONS checks, so they are not read;
- **for a fax service that posts received faxes to Faxbot** (Sinch, Phaxio, eFax): whether its receiving address
  reaches this Faxbot through the configured public address. Faxbot asks its own address, through the same
  tunnel or proxy the service uses, for a one-time code only this server issued (``probe``), so a tunnel that
  passes only the fax paths is tested on those paths;
- **for every number**: when the last fax to it was received.

**Receive owners.** ``receive_owner_claims`` names, per number, the one endpoint that accepts its faxes: one of
your receiving accounts, or a system outside this Faxbot (an old fax machine, another site's Faxbot). Each claim
is a new row with the next ``generation``; a number owned by another endpoint is refused unless the person says
they are moving it, and two claims made at the same moment cannot both win. Faxbot never blocks a fax because
of it: a fax that arrives on another endpoint is kept and named.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import secrets
import threading
import time
import uuid

import sqlalchemy as sa


READY, ATTENTION, NOT_RECEIVING, OFF = 'receiving', 'attention', 'not_receiving', 'off'
ELSEWHERE = 'elsewhere'
PROBE_SECONDS = 60
PROBE_TIMEOUT = 5.0
RECENT = timedelta(days=7)
WEBHOOK_PROVIDERS = ('sinch', 'phaxio', 'efax')


class OwnerConflict(ValueError):
    """A claim refused because another endpoint owns the number; one plain sentence."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# -- one-time codes for the address check --------------------------------------------------------------------

_codes: dict[str, float] = {}
_codes_lock = threading.Lock()


def issue_code() -> str:
    code = secrets.token_urlsafe(18)
    now = time.monotonic()
    with _codes_lock:
        for old in [key for key, at in _codes.items() if now - at > PROBE_SECONDS]:
            _codes.pop(old, None)
        _codes[code] = now
    return code


def take_code(code) -> bool:
    """Whether ``code`` was issued here within the last minute; each code answers once."""
    with _codes_lock:
        issued = _codes.pop(str(code or ''), None)
    return issued is not None and time.monotonic() - issued <= PROBE_SECONDS


def reach_address(webhook_address, code):
    return f'{webhook_address}?faxbot_reach={code}'


def probe(webhook_address, *, client=None, timeout=PROBE_TIMEOUT) -> tuple[bool, str | None]:
    """(reached, why not): ask this Faxbot's own receiving address, through the public address, for a fresh code.

    Reached only when the answer is that same code, so another server answering on the address never counts."""
    import httpx
    code = issue_code()
    try:
        if client is not None:
            response = client.get(reach_address(webhook_address, code))
        else:
            with httpx.Client(timeout=timeout, follow_redirects=False) as fresh:
                response = fresh.get(reach_address(webhook_address, code))
    except httpx.HTTPError:
        take_code(code)
        return False, 'no answer'
    if response.status_code == 200 and response.text.strip() == code:
        return True, None
    take_code(code)
    return False, f'answer {response.status_code}'


# -- numbers and their endpoints -----------------------------------------------------------------------------

@dataclass
class Endpoint:
    key: str
    label: str
    provider: str


@dataclass
class NumberView:
    number: str
    endpoints: list = field(default_factory=list)
    owner: str | None = None
    owner_label: str | None = None
    owner_generation: int = 0
    checks: list = field(default_factory=list)   # [{'title', 'status', 'sentence'}]
    last_received: datetime | None = None
    status: str = READY
    sentence: str = ''


def _key(number, country):
    """E.164, or None for a number in settings Faxbot cannot read (left out, never guessed)."""
    from .routing.numbers import InvalidNumber, normalize_number
    try:
        return normalize_number(str(number or ''), country=country)
    except InvalidNumber:
        return None


def receiving_endpoints(values) -> dict:
    """{number: [Endpoint]}: every number each receiving account (trunks included) takes faxes on."""
    from .accounts import receiving_accounts
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    found: dict[str, list] = {}
    if not getattr(values, 'inbound_enabled', False):
        return found
    for account in receiving_accounts(values):
        if not account.enabled:
            continue
        for raw in account.numbers:  # a trunk's numbers are its DIDs
            number = _key(raw, country)
            if not number or not number.startswith('+'):
                continue
            endpoints = found.setdefault(number, [])
            if all(item.key != account.key for item in endpoints):
                endpoints.append(Endpoint(account.key, account.label, account.provider))
    return found


# -- receive owners ------------------------------------------------------------------------------------------

def _claims(db):
    return sa.Table('receive_owner_claims', sa.MetaData(), autoload_with=db)


def owners(db) -> dict:
    """{number: newest claim row}; a row whose ``owner`` is None means released."""
    table = _claims(db)
    with db.connect() as connection:
        rows = connection.execute(sa.select(table).order_by(table.c.number, table.c.generation)).mappings().all()
    newest = {}
    for row in rows:
        newest[row['number']] = dict(row)
    return newest


def _current(db, table, number):
    with db.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.number == number)
                                 .order_by(table.c.generation.desc()).limit(1)).mappings().one_or_none()
    return dict(row) if row else None


def claim(db, number, owner, *, label=None, move=False, note=None, actor_id=None, actor_name=None, now=None) -> dict:
    """Name the one endpoint that accepts ``number``'s faxes (``owner`` None releases it).

    Refused (``OwnerConflict``) when another endpoint owns it and ``move`` is not set, and when someone else
    changed the owner at the same moment."""
    table = _claims(db)
    number = str(number)[:32]
    if owner == ELSEWHERE and not (label or '').strip():
        raise ValueError('Name the system outside Faxbot that receives this number, such as "the fax machine at '
                         'reception".')
    current = _current(db, table, number)
    held_by = current['owner'] if current else None
    if held_by and owner and held_by != owner and not move:
        name = current.get('owner_label') or held_by
        raise OwnerConflict(f'{name} already receives the faxes for {number}. Move the number to the new receiver '
                            'on purpose, or release it first, so two places never take its faxes without you '
                            'knowing.')
    row = {'id': uuid.uuid4().hex, 'number': number, 'generation': (current['generation'] if current else 0) + 1,
           'owner': (str(owner)[:64] if owner else None),
           'owner_label': ((label or '').strip()[:120] or None) if owner else None,
           'note': (note or '').strip()[:300] or None, 'claimed_at': now or utcnow(),
           'claimed_by': (str(actor_id)[:40] if actor_id else None),
           'claimed_by_name': (str(actor_name)[:200] if actor_name else None)}
    try:
        with db.begin() as connection:
            connection.execute(table.insert().values(**row))
    except sa.exc.IntegrityError:
        raise OwnerConflict(f'Someone else changed who receives {number} just now. Reload and try again.') from None
    return row


# -- evidence ------------------------------------------------------------------------------------------------

def _digits(number):
    return ''.join(ch for ch in str(number or '') if ch.isdigit())


def last_received(db, numbers) -> dict:
    """{number: when the last real fax to it was received}, from received-fax imports (tests left out)."""
    found = {}
    if not numbers:
        return found
    imports = sa.Table('inbound_imports', sa.MetaData(), autoload_with=db)
    tails = {_digits(number)[-10:]: number for number in numbers}
    with db.connect() as connection:
        rows = connection.execute(sa.select(imports.c.to_number, sa.func.max(imports.c.acquired_at)).where(
            imports.c.source != 'test', imports.c.acquired_at.is_not(None), imports.c.to_number.is_not(None))
            .group_by(imports.c.to_number)).all()
    for to_number, when in rows:
        number = tails.get(_digits(to_number)[-10:])
        if number and when and (number not in found or when > found[number]):
            found[number] = when
    return found


def last_invites(db, numbers) -> dict:
    """{number: when the last call to it arrived on the trunk}: the carrier's last INVITE Faxbot recorded."""
    found = {}
    if not numbers:
        return found
    records = sa.Table('sip_call_records', sa.MetaData(), autoload_with=db)
    tails = {_digits(number)[-10:]: number for number in numbers}
    with db.connect() as connection:
        rows = connection.execute(sa.select(records.c.called, records.c.did, sa.func.max(records.c.started_at))
                                  .where(records.c.direction == 'inbound')
                                  .group_by(records.c.called, records.c.did)).all()
    for called, did, when in rows:
        for candidate in (did, called):
            number = tails.get(_digits(candidate)[-10:]) if candidate else None
            if number and when and (number not in found or when > found[number]):
                found[number] = when
    return found


def arrivals_by_endpoint(db, numbers, since) -> dict:
    """{number: set of account keys faxes to it arrived on since ``since``}."""
    found = {}
    imports = sa.Table('inbound_imports', sa.MetaData(), autoload_with=db)
    if 'account_key' not in imports.c or not numbers:
        return found
    tails = {_digits(number)[-10:]: number for number in numbers}
    with db.connect() as connection:
        rows = connection.execute(sa.select(imports.c.to_number, imports.c.account_key, imports.c.source).where(
            imports.c.source != 'test', imports.c.acquired_at >= since, imports.c.to_number.is_not(None))
            .distinct()).all()
    for to_number, account_key, source in rows:
        number = tails.get(_digits(to_number)[-10:])
        if number:
            found.setdefault(number, set()).add(account_key or source)
    return found


# -- the judgement (pure) ------------------------------------------------------------------------------------

def _when(moment):
    from .people_time import short
    return short(moment) if moment else ''


def judge(view, *, registration=None, auth_mode='registration', reached=None, invite=None, arrivals=(),
          now=None) -> NumberView:
    """Fill ``view``'s checks, status and sentence. ``registration``: the trunk's sign-in ('registered',
    'rejected', 'unknown') or None without a trunk; ``reached``: {account key: (reached, why)} for webhook
    accounts; ``invite``: the last INVITE; ``arrivals``: account keys faxes arrived on lately. No sending
    evidence is an input. Pure."""
    now = now or utcnow()
    checks = []
    statuses = []
    reached = reached or {}
    for endpoint in view.endpoints:
        if endpoint.provider == 'sip':
            if auth_mode == 'ip':
                sign_in = (READY, 'Your carrier reaches this trunk by its address; no sign-in is needed.')
            elif registration == 'registered':
                sign_in = (READY, 'The trunk is signed in to your carrier.')
            elif registration == 'rejected':
                sign_in = (NOT_RECEIVING, 'The trunk is not signed in to your carrier, so calls to this number '
                                          'cannot reach Faxbot. Check the trunk on Providers.')
            else:
                sign_in = (ATTENTION, 'Faxbot cannot see whether the trunk is signed in right now; the fax engine '
                                      'did not say.')
            checks.append({'title': f'{endpoint.label}: sign-in', 'status': sign_in[0], 'sentence': sign_in[1]})
            statuses.append(sign_in[0])
            if invite:
                checks.append({'title': f'{endpoint.label}: last call in', 'status': READY,
                               'sentence': f'The last call to this number reached Faxbot {_when(invite)}.'})
            else:
                checks.append({'title': f'{endpoint.label}: last call in', 'status': ATTENTION,
                               'sentence': 'No call to this number has reached Faxbot yet. Send it a test fax '
                                           'from another line to prove it.'})
                statuses.append(ATTENTION)
        elif endpoint.provider in WEBHOOK_PROVIDERS and endpoint.key in reached:
            ok, why = reached[endpoint.key]
            if ok:
                checks.append({'title': f'{endpoint.label}: receiving address', 'status': READY,
                               'sentence': f'{endpoint.label} can reach Faxbot at its receiving address.'})
                statuses.append(READY)
            else:
                checks.append({'title': f'{endpoint.label}: receiving address', 'status': NOT_RECEIVING,
                               'sentence': f'Faxbot could not reach its own receiving address for {endpoint.label} '
                                           'through your public address, so faxes from it cannot arrive. Check '
                                           'the public address and any tunnel in front of Faxbot.'})
                statuses.append(NOT_RECEIVING)
        else:
            checks.append({'title': f'{endpoint.label}: collecting', 'status': READY,
                           'sentence': f'Faxbot collects faxes for this number from {endpoint.label} itself.'})
            statuses.append(READY)
    keys = {endpoint.key for endpoint in view.endpoints}
    if len(view.endpoints) > 1 and not view.owner:
        names = ' and '.join(endpoint.label for endpoint in view.endpoints)
        checks.append({'title': 'Who receives it', 'status': ATTENTION,
                       'sentence': f'Faxes to this number can arrive on both {names}, and none is named as its '
                                   'receiver. Choose which one receives it.'})
        statuses.append(ATTENTION)
    elif view.owner and view.owner != ELSEWHERE and view.owner not in keys:
        checks.append({'title': 'Who receives it', 'status': ATTENTION,
                       'sentence': f'{view.owner_label or view.owner} is named as its receiver, but that account '
                                   'does not list this number.'})
        statuses.append(ATTENTION)
    elif view.owner:
        checks.append({'title': 'Who receives it', 'status': READY,
                       'sentence': f'{view.owner_label or view.owner} receives this number\'s faxes.'})
    stray = sorted(key for key in arrivals if view.owner and view.owner != ELSEWHERE and key != view.owner)
    if stray:
        checks.append({'title': 'Who receives it', 'status': ATTENTION,
                       'sentence': f'Faxes to this number also arrived on {", ".join(stray)} this week. Faxbot kept '
                                   'them; check which receiver your carrier sends the number to.'})
        statuses.append(ATTENTION)
    if view.last_received:
        checks.append({'title': 'Last fax received', 'status': READY,
                       'sentence': f'The last fax to this number arrived {_when(view.last_received)}.'})
    else:
        checks.append({'title': 'Last fax received', 'status': OFF, 'sentence': 'No fax to this number has '
                                                                                 'arrived yet.'})
    view.checks = checks
    if NOT_RECEIVING in statuses:
        view.status = NOT_RECEIVING
        view.sentence = 'Not receiving: ' + next(check['sentence'] for check in checks
                                                 if check['status'] == NOT_RECEIVING)
    elif ATTENTION in statuses:
        view.status = ATTENTION
        view.sentence = next(check['sentence'] for check in checks if check['status'] == ATTENTION)
    else:
        view.status = READY
        view.sentence = 'Receiving: every check for this number passed.'
    return view


def numbers_view(db, values, endpoints=None) -> list:
    """Each receiving number with its endpoints, owner and last fax; checks are filled by ``judge``."""
    endpoints = endpoints if endpoints is not None else receiving_endpoints(values)
    claims = owners(db)
    received = last_received(db, list(endpoints))
    views = []
    for number in sorted(set(endpoints) | {number for number, row in claims.items() if row['owner']}):
        row = claims.get(number) or {}
        views.append(NumberView(number, list(endpoints.get(number, ())), row.get('owner'), row.get('owner_label'),
                                row.get('generation', 0), last_received=received.get(number)))
    return views


def check(db, values, *, registration=None, prober=None, now=None) -> list:
    """Every receiving number, judged. ``registration`` is the trunk's sign-in as the fax engine reports it."""
    from .accounts import account_named, webhook_address
    now = now or utcnow()
    prober = prober or probe
    views = numbers_view(db, values)
    invites = last_invites(db, [view.number for view in views])
    arrivals = arrivals_by_endpoint(db, [view.number for view in views], now - RECENT)
    reached = {}
    for view in views:
        for endpoint in view.endpoints:
            if endpoint.provider in WEBHOOK_PROVIDERS and endpoint.key not in reached:
                account = account_named(values, endpoint.key)
                address = webhook_address(values, account) if account is not None else None
                if address:
                    reached[endpoint.key] = prober(address)
    for view in views:
        judge(view, registration=registration, auth_mode=getattr(values, 'sip_trunk_auth', 'registration'),
              reached=reached, invite=invites.get(view.number), arrivals=arrivals.get(view.number, ()), now=now)
    return views


def as_dict(view) -> dict:
    return {'number': view.number, 'status': view.status, 'sentence': view.sentence,
            'owner': view.owner, 'owner_label': view.owner_label,
            'endpoints': [{'key': item.key, 'label': item.label, 'provider': item.provider} for item in view.endpoints],
            'checks': view.checks, 'last_received_text': _when(view.last_received) or None}
