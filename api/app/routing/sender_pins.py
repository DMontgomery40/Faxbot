"""Registered senders (N17): recipients that recognise your faxes by the number they come from.

Some recipients authenticate a fax by the sending number. Garanti BBVA's 2025
banking services agreement (clause 7.4, read from the PDF on 2026-10-08) treats
faxed instructions as originals, makes the bank's own fax copy conclusive, and
asks for no further confirmation when an instruction comes from a fax number
registered in its system; Halkbank's terms have the customer notify the numbers
it will send from in advance. Brokers and some payers work the same way.

A pin names, for one recipient, the exact trunk account, caller ID and station
ID (TSI) registered with it. For that recipient Faxbot:

- sends only by the pinned account, and only while that account's calls show
  the pinned caller ID and station ID, by a plain phone call: no other account, no approved alternate number, no
  partner relay, direct delivery, delivery inside Faxbot or peer tunnel, no
  sending together, and the pages as they are (no packing, trimming, shading or
  encoded pages), so the recipient's binding copy is the document itself;
- never fails over to another number. When the pinned account cannot take the
  fax, or would not show the pinned caller ID, or its call ends before any page,
  the fax waits in Sent with one sentence, and "send anyway" offers only the
  pinned account;
- keeps the fax's page images past the usual retention, with the answering
  station (the recipient's CSI) and the call record, as the sender's evidence;
- records a recipient's request for the original and when it was sent
  (``original_requests``).

The decision at acceptance is narrowed here (``narrow``), and dispatch skips
any other route, and the pinned account itself while it would not show the
pinned caller ID and station ID (``dispatch_refusal``, the transport's one
hook). Saving a pin and dispatch use the same check (``presentable``), which
works the identity out the way the call does. Faxbot verifies the identity; it
never changes a trunk's caller ID to match a pin. Nothing here contacts a
recipient or a carrier.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import re
from uuid import uuid4

import sqlalchemy as sa


ACTIVE, REMOVED = 'active', 'removed'
REQUESTED, SENT, CANCELLED = 'requested', 'sent', 'cancelled'
REQUEST_STATES = (REQUESTED, SENT, CANCELLED)
SKIP = 'pin'
SKIP_TEXT = 'does not show the caller ID this recipient has registered'
# A station ID is what a fax machine prints at the top of a received page: T.30 allows 20 characters, which
# Faxbot keeps to digits, spaces and a plus sign.
_STATION = re.compile(r'[0-9+ ]{1,20}')

_TYPES = {'created_at': sa.DateTime()}


def _light(name, columns):
    return sa.table(name, *(sa.column(column, _TYPES.get(column, sa.String())) for column in columns))


PINS = _light('registered_sender_pins', ('id', 'recipient', 'account', 'caller_id', 'station_id', 'state', 'note',
                                         'recorded_by', 'recorded_by_name', 'created_at'))
ORIGINALS = _light('original_requests', ('id', 'job_id', 'recipient', 'state', 'note', 'recorded_by',
                                         'recorded_by_name', 'created_at'))


class PinError(ValueError):
    """A pin Faxbot cannot record; the message is one plain sentence."""


@dataclass(frozen=True)
class Pin:
    recipient: str
    account: str
    caller_id: str
    station_id: str | None
    state: str = ACTIVE
    note: str | None = None
    recorded_by: str | None = None
    recorded_at: datetime | None = None

    @property
    def active(self):
        return self.state == ACTIVE

    @property
    def station(self):
        """The station ID the fax shows: the pinned one, else the pinned caller ID."""
        return self.station_id or self.caller_id


def _pin(row):
    return Pin(row['recipient'], row['account'], row['caller_id'], row['station_id'], row['state'], row['note'],
               row['recorded_by_name'], row['created_at'])


def _canonical(number):
    from .numbers import InvalidNumber, canonical_number
    try:
        return canonical_number(str(number or '').strip())
    except InvalidNumber:
        return None


# -- the pins --------------------------------------------------------------------------------------------------------

def _newest(connection, recipient):
    return connection.execute(sa.select(PINS).where(PINS.c.recipient == recipient).order_by(
        PINS.c.created_at.desc(), PINS.c.id.desc()).limit(1)).mappings().first()


def current_on(connection, recipient):
    """The recipient's active pin, read on ``connection``; None without one."""
    number = _canonical(recipient)
    if number is None:
        return None
    row = _newest(connection, number)
    return _pin(row) if row is not None and row['state'] == ACTIVE else None


def current(engine, recipient):
    """The recipient's active pin; None without one (or before migration 0069's tables can be read)."""
    from .database import read_connection
    if engine is None:
        return None
    with read_connection(engine) as connection:
        return current_on(connection, recipient)


def pins(engine):
    """Every active pin, newest first."""
    from .database import read_connection
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(PINS).order_by(PINS.c.created_at, PINS.c.id)).mappings().all()
    newest = {}
    for row in rows:
        newest[row['recipient']] = row
    return sorted((_pin(row) for row in newest.values() if row['state'] == ACTIVE),
                  key=lambda pin: pin.recorded_at or datetime.min, reverse=True)


def _after_newest(connection, recipient, now):
    newest = connection.execute(sa.select(sa.func.max(PINS.c.created_at)).where(
        PINS.c.recipient == recipient)).scalar()
    if isinstance(newest, str):
        newest = datetime.fromisoformat(newest)
    return max(now, newest + timedelta(microseconds=1)) if newest is not None else now


def presentable(values, account_key, caller_id, station_id=None, *, engine=None):
    """(True, None) when a fax sent by ``account_key`` now would show exactly ``caller_id`` (and ``station_id``,
    when one is pinned), else (False, one sentence).

    The identity is worked out the way the call sets it (``origin_classes.presented_identity``, mirroring
    ``ami.originate_fields_for``): a trunk shows the reply number when the carrier gives it to you on that trunk,
    else its own caller ID, and sends the reply number (else the caller ID) as its station ID. A cloud fax service
    sets its own sending number, so it cannot carry a pin.
    """
    from ..accounts import account_named
    from .origin_classes import presented_identity
    account = account_named(values, account_key)
    if account is None or not account.sends:
        return False, f'{account_key} is not one of your sending accounts.'
    if not account.enabled:
        return False, f'{account.label} is turned off.'
    caller, station, how = presented_identity(values, account_key, engine=engine)
    if how == 'provider':
        from ..provider_labels import provider_label
        return False, (f'{provider_label(account.provider)} sets its own sending number, so it cannot show a '
                       'registered caller ID. Choose one of your trunks.')
    if caller is None:
        return False, f'{account.label} is not set up to show a caller ID yet.'
    if caller != caller_id:
        return False, (f'{account.label} shows {caller} as caller ID now, not {caller_id}. Set its caller ID, or your '
                       'reply number, to the registered number.')
    if station_id and station != station_id:
        # Only a reply number (or the station ID setting) is sent as the station ID by both fax engines.
        return False, (f'{account.label} sends {station or "no station ID of its own"} as station ID now, not '
                       f'{station_id}. Set your station ID, or your reply number, to the registered one.')
    return True, None


def record(engine, values, recipient, *, account, caller_id, station_id=None, note=None, actor=None, now=None):
    """Pin ``recipient`` to the account, caller ID and station ID registered with it. Earlier pins stay as history."""
    from .database import utcnow, write_transaction
    number = _canonical(recipient)
    if number is None:
        raise PinError('Enter the recipient’s fax number with its country code, such as +902122220000.')
    caller = _canonical(caller_id)
    if caller is None:
        raise PinError('Enter the registered caller ID with its country code, such as +902123334455.')
    station = (station_id or '').strip() or None
    if station is not None and _STATION.fullmatch(station) is None:
        raise PinError('A station ID is up to 20 digits, spaces and a plus sign, such as +90 212 333 4455.')
    if note is not None and len(note) > 2000:
        raise PinError('Keep the note to 2,000 characters.')
    ok, why = presentable(values, account, caller, station, engine=engine)
    if not ok:
        raise PinError(why)
    actor = actor or {}
    with write_transaction(engine) as connection:
        connection.execute(PINS.insert().values(
            id=uuid4().hex, recipient=number, account=account, caller_id=caller, station_id=station, state=ACTIVE,
            note=(note or '').strip() or None, recorded_by=actor.get('id'), recorded_by_name=actor.get('name'),
            created_at=_after_newest(connection, number, now or utcnow())))
    return current(engine, number)


def remove(engine, recipient, *, note=None, actor=None, now=None):
    """End a recipient's pin; its history stays. Faxes to it go by your sending rules again."""
    from .database import utcnow, write_transaction
    number = _canonical(recipient)
    found = current(engine, number) if number else None
    if found is None:
        raise PinError('This recipient has no registered sender.')
    actor = actor or {}
    with write_transaction(engine) as connection:
        connection.execute(PINS.insert().values(
            id=uuid4().hex, recipient=number, account=found.account, caller_id=found.caller_id,
            station_id=found.station_id, state=REMOVED, note=(note or '').strip()[:2000] or None,
            recorded_by=actor.get('id'), recorded_by_name=actor.get('name'),
            created_at=_after_newest(connection, number, now or utcnow())))
    return found


# -- the decision at acceptance ----------------------------------------------------------------------------------------

def narrow(decision, pin):
    """The sending rules' decision for a fax to a pinned recipient: only the pinned account, by a plain call.

    The pin is the most specific route there is, so it replaces the account the rules chose; it never overrides a
    limit. When a limit, a cap or "only direct delivery" leaves the pinned account out, or the rules already
    blocked the fax, it is blocked and waits in Sent (a cap that is not mandatory can still be approved around,
    for the pinned account only). Approval and time-window holds the rules asked for stay.
    """
    envelope = decision.envelope
    excluded = {item.account for item in decision.excluded}
    plain = dict(local=False, direct=False, require_direct=False, preferred=None, alternate='never', dial=None,
                 page_layout='one_per_sheet', strict_fallback=True, when_busy='wait')
    if decision.outcome != 'blocked' and not envelope.require_direct and pin.account not in excluded:
        return replace(decision, envelope=replace(envelope, mode='one', accounts=(pin.account,), **plain))
    caps = {item.account: item for item in decision.excluded if item.why in ('over_cap', 'unknown_cost')}
    if decision.outcome == 'blocked':
        reason = decision.reason
    else:
        reason = 'no_account_under_cap' if pin.account in caps else 'no_allowed_account'
    return replace(decision, outcome='blocked', reason=reason, envelope=replace(envelope, mode='one', accounts=(),
                                                                                 **plain))


def narrow_on(connection, decision, facts):
    """``narrow`` for the fax's destination when it has an active pin, read on the acceptance connection."""
    pin = current_on(connection, getattr(facts, 'destination', None))
    return narrow(decision, pin) if pin is not None else decision


def narrow_for(engine, decision, facts):
    """``narrow`` for a preview (the dry run and the Send page), read with its own connection."""
    pin = current(engine, getattr(facts, 'destination', None))
    return narrow(decision, pin) if pin is not None else decision


def pinned_options(engine, job_id, options):
    """The accounts a held fax may be sent by anyway: for a registered-sender recipient only its registered trunk;
    any other fax's ``options`` unchanged."""
    from .database import read_connection
    jobs = sa.table('fax_jobs', sa.column('id'), sa.column('to_number'))
    with read_connection(engine) as connection:
        destination = connection.execute(sa.select(jobs.c.to_number).where(jobs.c.id == job_id)).scalar()
        pin = current_on(connection, destination) if destination else None
    if pin is None:
        return options
    return [item for item in options if item.get('account') == pin.account]


# -- dispatch and the call ---------------------------------------------------------------------------------------------

def dispatch_refusal(engine, values, destination, route_key):
    """``SKIP`` when ``route_key`` may not take a fax to ``destination`` (a pinned recipient), else None.

    Any route but the pinned account is refused, and the pinned account too while it would not show the pinned
    caller ID. The planner then holds the fax in Sent (``holds.no_route_sentence``).
    """
    pin = current(engine, destination)
    if pin is None:
        return None
    if route_key != pin.account:
        return SKIP
    ok, _ = presentable(values, route_key, pin.caller_id, pin.station_id, engine=engine)
    return None if ok else SKIP


def pinned(engine, destination):
    """Whether ``destination`` has an active pin (cheap: one indexed read)."""
    return current(engine, destination) is not None


# -- evidence ----------------------------------------------------------------------------------------------------------

def _pin_at(connection, recipient, moment):
    """The pin that was in force for ``recipient`` at ``moment``; None when none was."""
    row = connection.execute(sa.select(PINS).where(PINS.c.recipient == recipient, PINS.c.created_at <= moment)
                             .order_by(PINS.c.created_at.desc(), PINS.c.id.desc()).limit(1)).mappings().first()
    return _pin(row) if row is not None and row['state'] == ACTIVE else None


def kept_jobs(engine, job_ids):
    """The faxes among ``job_ids`` sent to a recipient pinned when they were accepted: their files are evidence."""
    from .database import read_connection
    job_ids = [job for job in job_ids if job]
    if engine is None or not job_ids:
        return set()
    jobs = sa.table('fax_jobs', sa.column('id'), sa.column('to_number'), sa.column('created_at', sa.DateTime()))
    with read_connection(engine) as connection:
        if connection.execute(sa.select(PINS.c.id).limit(1)).first() is None:
            return set()
        kept = set()
        for start in range(0, len(job_ids), 500):
            rows = connection.execute(sa.select(jobs.c.id, jobs.c.to_number, jobs.c.created_at).where(
                jobs.c.id.in_(job_ids[start:start + 500]))).mappings().all()
            for row in rows:
                if row['to_number'] and _pin_at(connection, row['to_number'], row['created_at']) is not None:
                    kept.add(row['id'])
    return kept


def _originals(connection, job_id):
    rows = connection.execute(sa.select(ORIGINALS).where(ORIGINALS.c.job_id == job_id).order_by(
        ORIGINALS.c.created_at, ORIGINALS.c.id)).mappings().all()
    return [{'state': row['state'], 'note': row['note'], 'recorded_by': row['recorded_by_name'],
             'recorded_at': row['created_at'].isoformat() if row['created_at'] else None} for row in rows]


def evidence(engine, job_id, *, fax_data_dir=None):
    """The sender's evidence for one fax to a pinned recipient: the pin then, the kept pages, the calls with the
    answering station, and any request for the original. None when the fax was not sent under a pin."""
    from pathlib import Path
    from .database import read_connection
    jobs = sa.table('fax_jobs', sa.column('id'), sa.column('to_number'), sa.column('pages'), sa.column('status'),
                    sa.column('created_at', sa.DateTime()))
    calls = sa.table('sip_call_records', *(sa.column(name) for name in (
        'job_id', 'attempt_id', 'caller', 'called', 'disposition', 'pages', 'fax_status', 'remote_station_id',
        'connected_seconds')), *(sa.column(name, sa.DateTime()) for name in ('started_at', 'answered_at', 'ended_at')))
    with read_connection(engine) as connection:
        job = connection.execute(sa.select(jobs).where(jobs.c.id == job_id)).mappings().first()
        if job is None or not job['to_number']:
            return None
        pin = _pin_at(connection, job['to_number'], job['created_at'])
        if pin is None:
            return None
        found = connection.execute(sa.select(calls).where(calls.c.job_id == job_id).order_by(
            calls.c.started_at)).mappings().all()
        originals = _originals(connection, job_id)
    files = []
    if fax_data_dir:
        for suffix, kind in (('.tiff', 'fax image'), ('.pdf', 'document')):
            path = Path(fax_data_dir) / f'{job_id}{suffix}'
            if path.is_file() and not path.is_symlink():
                files.append(kind)
    when = lambda value: value.isoformat() if value else None  # noqa: E731
    return {'job_id': job_id, 'recipient': job['to_number'], 'pages': job['pages'], 'status': job['status'],
            'pin': pin_view(pin), 'kept': files,
            'calls': [{'caller_id': row['caller'], 'called': row['called'], 'answering_station': row['remote_station_id'],
                       'disposition': row['disposition'], 'pages': row['pages'], 'fax_status': row['fax_status'],
                       'connected_seconds': row['connected_seconds'], 'started_at': when(row['started_at']),
                       'answered_at': when(row['answered_at']), 'ended_at': when(row['ended_at']),
                       'matches_pin': row['caller'] == pin.caller_id} for row in found],
            'originals': originals,
            'original': originals[-1]['state'] if originals else None}


def record_original(engine, job_id, state, *, note=None, actor=None, now=None):
    """Record that the recipient asked for the original of a pinned fax, that it was sent, or that the request
    was withdrawn. Earlier entries stay."""
    from .database import read_connection, utcnow, write_transaction
    if state not in REQUEST_STATES:
        raise PinError('Choose requested, sent or cancelled.')
    if note is not None and len(note) > 2000:
        raise PinError('Keep the note to 2,000 characters.')
    jobs = sa.table('fax_jobs', sa.column('id'), sa.column('to_number'), sa.column('created_at', sa.DateTime()))
    with read_connection(engine) as connection:
        job = connection.execute(sa.select(jobs).where(jobs.c.id == job_id)).mappings().first()
        if job is None or _pin_at(connection, job['to_number'], job['created_at']) is None:
            raise PinError('Only a fax sent to a registered-sender recipient keeps a request for its original.')
        previous = _originals(connection, job_id)
    last = previous[-1]['state'] if previous else None
    if state == REQUESTED and last == REQUESTED:
        raise PinError('The original was already requested for this fax.')
    if state in (SENT, CANCELLED) and last != REQUESTED:
        raise PinError('Record the request for the original first.')
    actor = actor or {}
    with write_transaction(engine) as connection:
        newest = connection.execute(sa.select(sa.func.max(ORIGINALS.c.created_at)).where(
            ORIGINALS.c.job_id == job_id)).scalar()
        if isinstance(newest, str):
            newest = datetime.fromisoformat(newest)
        moment = now or utcnow()
        if newest is not None:
            moment = max(moment, newest + timedelta(microseconds=1))
        connection.execute(ORIGINALS.insert().values(
            id=uuid4().hex, job_id=job_id, recipient=job['to_number'], state=state, note=(note or '').strip() or None,
            recorded_by=actor.get('id'), recorded_by_name=actor.get('name'), created_at=moment))
    return evidence(engine, job_id)


def pin_view(pin):
    if pin is None:
        return None
    return {'recipient': pin.recipient, 'account': pin.account, 'caller_id': pin.caller_id,
            'station_id': pin.station, 'note': pin.note, 'recorded_by': pin.recorded_by,
            'recorded_at': pin.recorded_at.isoformat() if pin.recorded_at else None}
