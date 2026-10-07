"""Junk senders turned away before Asterisk answers (M14).

An administrator marks a received fax's sender as junk ("Mark sender as
junk", or Numbers → Blocked senders), with a reason; the entry expires after
90 days unless they choose otherwise, and removing it is one click. Nothing
else ever adds an entry, and a caller with no number (anonymous or withheld)
can never be blocked.

Asterisk checks every call the carrier sends before it answers
(``extensions.conf``, ``[faxbot-screen]``): it reads the caller's digits in
its database, family ``faxbot-screen``, where Faxbot keeps one key per way a
carrier may present the number (E.164 without the plus, and the national
form: ``13035550123`` and ``3035550123``; ``441782684953`` and
``01782684953``), each holding the entry's expiry as epoch seconds. A key
whose time has passed is ignored by Asterisk itself, so an entry expires on
time even while Faxbot is not running. A listed caller gets SIP 603 Decline:
the call is never answered, so the carrier bills nothing (billing starts at
the answer), and no fax arrives to triage. Withheld numbers are never
screened.

Each rejection is queued in Asterisk's database (family ``faxbot-screened``,
key ``<epoch>.<call>``, value ``<caller>:<called>:<Call-ID base64>``) and announced with a ``FaxScreened`` event; Faxbot
records each queued call once (``screened_call_rejections``) and then removes
it from the queue, so a rejection made while Faxbot was not connected is still
recorded when it connects again.

``sync`` makes Asterisk's keys exactly the active entries. It runs whenever
Faxbot connects to Asterisk (a restarted Asterisk may have lost its database,
which is not on a volume), after every change, and every few minutes.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import re
import uuid

import phonenumbers
import sqlalchemy as sa


FAMILY = 'faxbot-screen'
QUEUE = 'faxbot-screened'
DEFAULT_DAYS = 90
MAX_DAYS = 365
_QUEUE_KEY = re.compile(r'[0-9]{9,12}\.[0-9.]{1,40}')
_DIGITS = re.compile(r'[0-9]{3,20}')

NO_NUMBER = ("This fax's sender sent no number, so it can't be blocked. Faxbot never blocks callers who withhold "
             'their number.')
NOT_A_NUMBER = '{number} is not a phone number Faxbot can read. Enter it with its country code, such as +13035550100.'
NO_REASON = 'Say why this sender is junk, in a few words.'


class ScreeningRefused(ValueError):
    """A sender that can't be blocked; the message is one plain sentence."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _epoch(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def read_number(text, country='US') -> str:
    """A caller's number in E.164, or ScreeningRefused."""
    raw = str(text or '').strip()
    digits = re.sub(r'[^0-9]', '', raw)
    if not digits or raw.lower() in {'anonymous', 'restricted', 'unknown', 'unavailable', 'private'}:
        raise ScreeningRefused(NO_NUMBER)
    try:
        parsed = phonenumbers.parse(raw, None if raw.startswith('+') else country)
    except phonenumbers.NumberParseException:
        raise ScreeningRefused(NOT_A_NUMBER.format(number=raw[:40])) from None
    if not phonenumbers.is_possible_number(parsed):
        raise ScreeningRefused(NOT_A_NUMBER.format(number=raw[:40]))
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def keys_for(number) -> set:
    """The digit strings a carrier may present for ``number`` (E.164), as Asterisk looks them up."""
    parsed = phonenumbers.parse(number, None)
    keys = {number.lstrip('+')}
    national = re.sub(r'[^0-9]', '', phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL))
    keys.add(national)
    keys.add(str(parsed.national_number))
    return {key for key in keys if _DIGITS.fullmatch(key)}


def desired_keys(entries) -> dict:
    """{key: expiry epoch as text} for the active entries; the latest expiry wins when two share a key."""
    found = {}
    for entry in entries:
        expiry = _epoch(entry['expires_at'])
        for key in keys_for(entry['number']):
            found[key] = max(found.get(key, 0), expiry)
    return {key: str(value) for key, value in found.items()}


@dataclass(frozen=True)
class Rejection:
    key: str
    number: str
    called: str | None
    call_id: str | None
    at: datetime


def parse_queued(key, value) -> Rejection | None:
    """One queued rejection from Asterisk's database (``<epoch>.<call>`` = ``<caller>:<called>:<Call-ID base64>``)."""
    key = str(key or '').rsplit('/', 1)[-1]
    if not _QUEUE_KEY.fullmatch(key):
        return None
    parts = str(value or '').split(':')
    caller = re.sub(r'[^0-9+]', '', parts[0])[:32] if parts else ''
    if not caller:
        return None
    called = re.sub(r'[^0-9+]', '', parts[1])[:32] if len(parts) > 1 else ''
    call_id = None
    if len(parts) > 2 and parts[2]:
        try:
            call_id = base64.b64decode(parts[2], validate=True).decode('utf-8', 'replace')[:255]
        except (ValueError, TypeError):
            call_id = None
    at = datetime.fromtimestamp(int(key.split('.', 1)[0]), timezone.utc).replace(tzinfo=None)
    return Rejection(key, caller, called or None, call_id, at)


class ScreeningStore:
    """The blocked senders and the calls turned away; the tables of migration 0035."""

    def __init__(self, engine):
        self.engine = engine
        metadata = sa.MetaData()
        metadata.reflect(engine, only=['screened_callers', 'screened_call_rejections'])
        self.callers = metadata.tables['screened_callers']
        self.rejections_table = metadata.tables['screened_call_rejections']

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    def add(self, number, reason, *, actor_id=None, actor_name=None, inbound_fax_id=None, days=DEFAULT_DAYS,
            now=None) -> dict:
        reason = ' '.join(str(reason or '').split())[:200]
        if not reason:
            raise ScreeningRefused(NO_REASON)
        if type(days) is not int or not 1 <= days <= MAX_DAYS:
            raise ScreeningRefused(f'Choose how long to block this sender: 1 to {MAX_DAYS} days.')
        now = now or utcnow()
        row = {'id': uuid.uuid4().hex, 'number': number, 'reason': reason, 'inbound_fax_id': inbound_fax_id,
               'added_by': actor_id, 'added_by_name': (actor_name or None) and str(actor_name)[:200],
               'created_at': now, 'expires_at': now + timedelta(days=days), 'removed_at': None,
               'removed_by': None, 'removed_by_name': None}
        with self.engine.begin() as connection:
            connection.execute(self.callers.insert().values(**row))
        return row

    def get(self, entry_id) -> dict | None:
        with self.engine.connect() as connection:
            return self._row(connection.execute(sa.select(self.callers).where(self.callers.c.id == entry_id))
                             .mappings().first())

    def remove(self, entry_id, *, actor_id=None, actor_name=None, now=None) -> dict | None:
        """Removed once: a second removal leaves the first one's record as it is."""
        now = now or utcnow()
        with self.engine.begin() as connection:
            connection.execute(self.callers.update()
                               .where(self.callers.c.id == entry_id, self.callers.c.removed_at.is_(None))
                               .values(removed_at=now, removed_by=actor_id,
                                       removed_by_name=(actor_name or None) and str(actor_name)[:200]))
            return self._row(connection.execute(sa.select(self.callers).where(self.callers.c.id == entry_id))
                             .mappings().first())

    def entries(self, *, limit=500) -> list:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                sa.select(self.callers).order_by(self.callers.c.created_at.desc(), self.callers.c.id).limit(limit))
                .mappings()]

    def active(self, now=None) -> list:
        now = now or utcnow()
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                sa.select(self.callers).where(self.callers.c.removed_at.is_(None), self.callers.c.expires_at > now)
                .order_by(self.callers.c.created_at, self.callers.c.id)).mappings()]

    def matching(self, number, now) -> dict | None:
        """The active entry a presented caller number matches, if one still does."""
        digits = re.sub(r'[^0-9]', '', number or '')
        for entry in self.active(now):
            if digits in keys_for(entry['number']):
                return entry
        return None

    def record_rejection(self, rejection: Rejection, *, now=None) -> bool:
        """Keep one rejected call; True when it was new."""
        now = now or utcnow()
        entry = self.matching(rejection.number, rejection.at)
        try:
            with self.engine.begin() as connection:
                exists = connection.execute(sa.select(self.rejections_table.c.id)
                                            .where(self.rejections_table.c.id == rejection.key)).first()
                if exists is not None:
                    return False
                connection.execute(self.rejections_table.insert().values(
                    id=rejection.key, number=rejection.number, called=rejection.called,
                    entry_id=entry['id'] if entry else None, call_id=rejection.call_id,
                    rejected_at=rejection.at, created_at=now))
        except sa.exc.IntegrityError:
            return False
        return True

    def rejections(self, *, limit=200) -> list:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                sa.select(self.rejections_table).order_by(self.rejections_table.c.rejected_at.desc(),
                                                          self.rejections_table.c.id).limit(limit)).mappings()]

    def counts(self) -> dict:
        """{entry ID: rejected calls}."""
        table = self.rejections_table
        with self.engine.connect() as connection:
            return {row.entry_id: row.count for row in connection.execute(
                sa.select(table.c.entry_id, sa.func.count().label('count')).where(table.c.entry_id.is_not(None))
                .group_by(table.c.entry_id))}


def sender_of(engine, inbound_fax_id) -> str | None:
    """The number a received fax came from, as stored."""
    faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return connection.execute(sa.select(faxes.c.from_number).where(faxes.c.id == inbound_fax_id)).scalar()


# -- Asterisk's database ---------------------------------------------------------------------

async def _tree(ami, family) -> dict:
    response, events = await ami.status_query({'Action': 'DBGetTree', 'Family': family}, collect=True)
    if response['response'].lower() != 'success':
        raise ConnectionError('Asterisk did not list its database')
    found = {}
    for event in events:
        key = str(event.get('Key', '')).rsplit('/', 1)[-1]
        if key:
            found[key] = str(event.get('Val', ''))
    return found


async def sync(ami, store, *, now=None) -> dict:
    """Make Asterisk's ``faxbot-screen`` keys exactly the active entries; {'added', 'removed'} counts."""
    import asyncio
    entries = await asyncio.to_thread(store.active, now)
    desired = desired_keys(entries)
    current = await _tree(ami, FAMILY)
    removed = added = 0
    for key, value in current.items():
        if desired.get(key) != value:
            await ami.db_del(FAMILY, key)
            removed += 1
    for key, value in desired.items():
        if current.get(key) != value:
            await ami.db_put(FAMILY, key, value)
            added += 1
    return {'added': added, 'removed': removed}


async def drain(ami, store, *, now=None) -> int:
    """Record every rejection Asterisk queued, once, then take it off the queue; how many were new."""
    import asyncio
    queued = await _tree(ami, QUEUE)
    new = 0
    for key, value in sorted(queued.items()):
        rejection = parse_queued(key, value)
        if rejection is not None:
            new += bool(await asyncio.to_thread(store.record_rejection, rejection, now=now))
        await ami.db_del(QUEUE, key)
    return new
