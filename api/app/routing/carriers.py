"""Match a SIP trunk carrier's billing records to Faxbot's own call records and keep their charges.

Every call Faxbot's fax engine places or answers has a ``sip_call_records``
row. The carrier bills the same call later, under its own record identity.
A record belongs to a call when:

1. the ledger already holds that record for the call (later reports of it);
2. otherwise, its SIP Call-ID equals the one Faxbot captured for exactly one call;
3. otherwise, exactly one call has the same direction and numbers and was
   answered and ended within ``TOLERANCE`` of the record, and that record
   fits no other call. A received call whose dialled number Faxbot did not
   learn is compared by the caller's number alone, never by time alone. Anything else is ambiguous: it stays unmatched and is
   counted, never guessed.

Priced records on the trunk's own numbers that fit no Faxbot call at all (for
example a received fax whose hand-over failed before its call was recorded)
are kept in ``carrier_records`` so spending is not under-reported. One is
attached to a received fax only when exactly one fax received over the trunk
fits it by called number and time, and that fax fits no other such record.

Charges are append-only observations (``carrier_charges``): a repeated report
has one effect, a different amount reported later supersedes the earlier one
with both kept, and a report dated before the one in effect is kept as
history without taking effect. An unpriced record is not recorded at all, so
unknown stays unknown. For sent faxes the amount in effect is also recorded
against the attempt in ``delivery_charges``. Delivery status is never read
for a decision here and never changed.
"""
from dataclasses import dataclass
from datetime import timedelta
import logging
import re
from uuid import uuid4

import sqlalchemy as sa

from .database import read_connection, reflect, utcnow, write_transaction
from .telnyx import CarrierRateLimited


TOLERANCE = timedelta(seconds=45)
SETTLE_AFTER = timedelta(hours=24)
GIVE_UP = timedelta(days=7)
FIRST_RETRY = timedelta(minutes=5)
LONGEST_RETRY = timedelta(hours=6)
BUCKET = timedelta(hours=6)
MARGIN = timedelta(minutes=10)
# Records that fit no call: the trunk's last two days, checked hourly; a record
# within five minutes of any Faxbot call with its numbers is never called unrecorded.
UNRECORDED_WINDOW = timedelta(days=2)
UNRECORDED_EVERY = timedelta(hours=1)
UNRECORDED_GRACE = timedelta(minutes=15)
NEAR_A_CALL = timedelta(minutes=5)
OPEN_STATES = ('waiting', 'ambiguous', 'matched')
CARRIER_LABELS = {'telnyx': 'Telnyx', 'signalwire': 'SignalWire'}


def carrier_label(provider_id):
    """The plain name of a carrier or SIP trunk preset (Avaya IP Office, BT One Voice), never its id."""
    if provider_id in CARRIER_LABELS:
        return CARRIER_LABELS[provider_id]
    from ..sip_trunk import PRESETS
    preset = PRESETS.get(provider_id)
    return preset.label if preset else provider_id


@dataclass(frozen=True)
class CallView:
    """The parts of a Faxbot call record a carrier record is compared with."""
    id: str
    direction: str
    sip_call_id: str | None
    local: str | None   # Faxbot's number: the caller ID it sent, or the number dialled in
    remote: str | None  # the other party: the number Faxbot dialled, or the caller
    started_at: object
    answered_at: object
    ended_at: object


def _digits(value):
    return re.sub(r'[^0-9]', '', value) if isinstance(value, str) else ''


def same_number(first, second):
    """Equal digits, or one complete national number ending the other (+1 3035550100 and 3035550100)."""
    a, b = _digits(first), _digits(second)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= 10 and long.endswith(short)


def _near(first, second, tolerance):
    return first is not None and second is not None and abs((first - second).total_seconds()) <= tolerance.total_seconds()


def fits(call, record, tolerance=TOLERANCE):
    """Same direction and numbers, answered and ended at the same moments within ``tolerance``."""
    if call.direction != record.direction:
        return False
    called, calling = (call.remote, call.local) if call.direction == 'outbound' else (call.local, call.remote)
    if _digits(called):
        if not same_number(called, record.cld):
            return False
    elif not (_digits(calling) and record.cli):
        return False  # with neither number known on both sides, nothing ties this record to the call
    if _digits(calling) and record.cli and not same_number(calling, record.cli):
        return False
    if (call.answered_at is None) != (record.answered_at is None):
        return False
    if call.answered_at is not None:
        # Faxbot's outbound start is its submission time, so answered and ended decide.
        return _near(call.answered_at, record.answered_at, tolerance) and _near(call.ended_at, record.finished_at, tolerance)
    return _near(call.started_at, record.started_at, tolerance) and _near(call.ended_at, record.finished_at, tolerance)


def match_records(targets, calls, records, *, attached=None, holding=(), tolerance=TOLERANCE):
    """Assign carrier records to target calls.

    ``calls`` are every Faxbot call that could compete for these records (the
    targets included); ``attached`` maps record ids the ledger already holds to
    their call; ``holding`` are calls that already hold a record. Returns
    ``({call id: [(record, method)]}, ambiguous call ids)`` for the targets.
    """
    attached = attached or {}
    everyone = {call.id: call for call in calls}
    everyone.update({call.id: call for call in targets})
    matches, ambiguous, pool = {}, set(), []
    for record in records:
        owner = attached.get(record.id)
        if owner is None:
            pool.append(record)
        elif owner in everyone:
            matches.setdefault(owner, []).append((record, 'known'))
    by_call_id = {}
    for call in everyone.values():
        if call.sip_call_id:
            by_call_id.setdefault((call.direction, call.sip_call_id), []).append(call.id)
    rest = []
    for record in pool:
        owners = by_call_id.get((record.direction, record.sip_call_id)) if record.sip_call_id else None
        if owners and len(owners) == 1:
            matches.setdefault(owners[0], []).append((record, 'call_id'))
        elif owners:
            ambiguous.update(owners)
        else:
            rest.append(record)
    decided = set(matches) | ambiguous | set(holding)
    candidates = {call.id: [record for record in rest if fits(call, record, tolerance)]
                  for call in everyone.values() if call.id not in decided}
    claimants = {}
    for call_id, found in candidates.items():
        for record in found:
            claimants.setdefault(record.id, []).append(call_id)
    for call in targets:
        found = candidates.get(call.id) or []
        if not found:
            continue
        if len(found) == 1 and claimants[found[0].id] == [call.id]:
            matches[call.id] = [(found[0], 'time_window')]
        else:
            ambiguous.add(call.id)
    wanted = {call.id for call in targets}
    return {call_id: found for call_id, found in matches.items() if call_id in wanted}, ambiguous & wanted


class CarrierChargeStore:
    TABLES = ('sip_call_records', 'carrier_charges', 'carrier_call_checks', 'carrier_records', 'inbound_faxes',
              'inbound_imports')

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, self.TABLES)
        self.calls = tables['sip_call_records']
        self.charges = tables['carrier_charges']
        self.checks = tables['carrier_call_checks']
        self.records = tables['carrier_records']
        self.faxes = tables['inbound_faxes']
        self.imports = tables['inbound_imports']

    @staticmethod
    def view(row):
        outbound = row['direction'] == 'outbound'
        return CallView(row['id'], row['direction'], row['sip_call_id'],
                        row['caller'] if outbound else row['did'], row['called'] if outbound else row['caller'],
                        row['started_at'], row['answered_at'], row['ended_at'])

    # Scheduling ------------------------------------------------------------
    def due_calls(self, preset, *, now=None, force=False, give_up=GIVE_UP, limit=200):
        """Finished calls on this carrier's trunk whose charge is still open, oldest first."""
        now = now or utcnow()
        calls, checks = self.calls, self.checks
        query = (sa.select(calls, checks.c.state, checks.c.checks)
                 .select_from(calls.outerjoin(checks, checks.c.id == calls.c.id))
                 .where(calls.c.trunk_preset == preset, calls.c.ended_at.is_not(None),
                        calls.c.ended_at >= now - give_up,
                        sa.or_(checks.c.id.is_(None), checks.c.state.in_(OPEN_STATES)))
                 .order_by(calls.c.started_at, calls.c.id).limit(limit))
        if not force:
            query = query.where(sa.or_(checks.c.id.is_(None), checks.c.next_check_at <= now))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def calls_between(self, start, end, preset):
        """Every call on this trunk (any trunk when ``preset`` is None) overlapping ``[start, end)``."""
        calls = self.calls
        query = sa.select(calls).where(calls.c.started_at < end,
                                       sa.or_(calls.c.ended_at.is_(None), calls.c.ended_at >= start))
        if preset is not None:
            query = query.where(calls.c.trunk_preset == preset)
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def holders(self, record_ids, call_ids):
        """``({record id: call id} for these records, call ids among these that already hold a record)``."""
        charges = self.charges
        with read_connection(self.engine) as connection:
            attached = dict(connection.execute(sa.select(charges.c.record_id, charges.c.call_record_id).where(
                charges.c.record_id.in_(tuple(record_ids) or ('',)))).all())
            holding = set(connection.execute(sa.select(charges.c.call_record_id).where(
                charges.c.call_record_id.in_(tuple(call_ids) or ('',)))).scalars())
        return attached, holding

    def mark(self, call_id, provider_id, state, *, now=None, next_check_at=None):
        now = now or utcnow()
        checks = self.checks
        with write_transaction(self.engine) as connection:
            row = connection.execute(sa.select(checks).where(checks.c.id == call_id)).mappings().one_or_none()
            count = (row['checks'] if row is not None else 0) + 1
            wait = min(FIRST_RETRY * (2 ** min(count - 1, 10)), LONGEST_RETRY)
            values = dict(provider_id=provider_id, state=state, checks=count, checked_at=now,
                          next_check_at=next_check_at or now + wait, updated_at=now)
            if row is None:
                connection.execute(checks.insert().values(id=call_id, created_at=now, **values))
            else:
                connection.execute(checks.update().where(checks.c.id == call_id).values(**values))

    def expire(self, preset, *, now=None, give_up=GIVE_UP):
        """Stop asking about calls the carrier never priced within ``give_up``; they stay unreported."""
        now = now or utcnow()
        calls, checks = self.calls, self.checks
        old = sa.select(calls.c.id).where(calls.c.trunk_preset == preset, calls.c.ended_at < now - give_up)
        with write_transaction(self.engine) as connection:
            connection.execute(checks.update().where(checks.c.id.in_(old), checks.c.state.in_(('waiting', 'ambiguous')))
                               .values(state='unreported', updated_at=now))

    # Charges ---------------------------------------------------------------
    def record(self, call_id, *, provider_id, record, method, effective_at, final, now=None):
        """Record one priced report; returns ``new``, ``duplicate``, ``corrected``, ``settled``, ``older`` or ``conflict``."""
        if record.amount_micros is None or method not in ('call_id', 'time_window'):
            raise ValueError('Only a priced record with a match method can be recorded.')
        now = now or utcnow()
        charges = self.charges
        content = (record.amount_micros, record.currency, record.billed_seconds)
        with write_transaction(self.engine) as connection:
            owner = connection.execute(sa.select(charges.c.call_record_id).where(
                charges.c.provider_id == provider_id, charges.c.record_id == record.id,
                charges.c.call_record_id != call_id)).first()
            if owner is not None:
                return 'conflict'  # this carrier record already belongs to another call
            rows = connection.execute(sa.select(charges).where(
                charges.c.call_record_id == call_id, charges.c.record_id == record.id)
                .order_by(charges.c.version)).mappings().all()
            current = next((row for row in reversed(rows) if row['applied']), None)
            same = lambda row: (row['amount_micros'], row['currency'], row['billed_seconds']) == content
            values = dict(call_record_id=call_id, provider_id=provider_id, record_id=record.id,
                          version=len(rows) + 1, amount_micros=record.amount_micros, raw_amount=record.raw_amount,
                          currency=record.currency, billed_seconds=record.billed_seconds,
                          call_seconds=record.call_seconds,
                          match_method=method if current is None else current['match_method'],
                          effective_at=effective_at, observed_at=now, created_at=now)
            if current is not None and same(current):
                if final and not current['is_final']:
                    connection.execute(charges.insert().values(id=uuid4().hex, supersedes_id=current['id'],
                                                               applied=1, is_final=1, **values))
                    return 'settled'
                return 'duplicate'
            if current is not None and effective_at <= current['effective_at']:
                # An older report: keep it as history, never let it replace the amount in effect.
                if any(same(row) and row['effective_at'] == effective_at for row in rows):
                    return 'duplicate'
                connection.execute(charges.insert().values(id=uuid4().hex, supersedes_id=None, applied=0,
                                                           is_final=int(final), **values))
                return 'older'
            connection.execute(charges.insert().values(
                id=uuid4().hex, supersedes_id=current['id'] if current is not None else None, applied=1,
                is_final=int(final), **values))
            return 'new' if current is None else 'corrected'

    def in_effect(self, call_ids, connection=None):
        """{call id: [the observation in effect for each carrier record]}."""
        charges = self.charges

        def read(conn):
            rows = conn.execute(sa.select(charges).where(
                charges.c.call_record_id.in_(tuple(call_ids) or ('',)), charges.c.applied == 1)
                .order_by(charges.c.call_record_id, charges.c.record_id, charges.c.version)).mappings().all()
            latest = {}
            for row in rows:
                latest[(row['call_record_id'], row['record_id'])] = dict(row)
            result = {}
            for (call_id, _), row in sorted(latest.items()):
                result.setdefault(call_id, []).append(row)
            return result
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def history(self, call_id):
        charges = self.charges
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(charges).where(
                charges.c.call_record_id == call_id).order_by(charges.c.record_id, charges.c.version)).mappings()]


    # Records that fit no call ------------------------------------------------
    def charged_record_ids(self, record_ids):
        """Carrier record ids already held against a Faxbot call."""
        charges = self.charges
        with read_connection(self.engine) as connection:
            return set(connection.execute(sa.select(charges.c.record_id).where(
                charges.c.record_id.in_(tuple(record_ids) or ('',)))).scalars())

    def received_faxes(self, start, end):
        """Faxes received over the SIP trunk between ``start`` and ``end`` that no call record names.

        Their time is the source's receipt time when the fax was brought in later, else when it was received.
        """
        faxes, imports, calls = self.faxes, self.imports, self.calls
        received = sa.func.coalesce(imports.c.source_received_at, faxes.c.received_at, faxes.c.created_at)
        named = sa.select(calls.c.job_id).where(calls.c.direction == 'inbound', calls.c.job_id.is_not(None))
        query = (sa.select(faxes.c.id, faxes.c.to_number, received.label('received'))
                 .select_from(faxes.outerjoin(imports, imports.c.inbound_fax_id == faxes.c.id))
                 .where(faxes.c.backend == 'sip', faxes.c.id.not_in(named), received >= start, received < end))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def record_unrecorded(self, record, *, provider_id, inbound_fax_id, effective_at, now=None):
        """Keep one priced record that fits no call; ``new``, ``duplicate``, ``corrected`` or ``older``.

        A received fax once attached stays attached; a later sweep never detaches it.
        """
        if record.amount_micros is None:
            raise ValueError('Only a priced record can be kept.')
        now = now or utcnow()
        table = self.records
        with write_transaction(self.engine) as connection:
            rows = connection.execute(sa.select(table).where(
                table.c.provider_id == provider_id, table.c.record_id == record.id)
                .order_by(table.c.version)).mappings().all()
            current = next((row for row in reversed(rows) if row['applied']), None)
            if current is not None and inbound_fax_id is None:
                inbound_fax_id = current['inbound_fax_id']
            content = (record.amount_micros, record.currency, record.billed_seconds, inbound_fax_id)
            same = lambda row: (row['amount_micros'], row['currency'], row['billed_seconds'],
                                row['inbound_fax_id']) == content
            values = dict(provider_id=provider_id, record_id=record.id, version=len(rows) + 1,
                          direction=record.direction, calling=(record.cli or '')[:32] or None,
                          called=(record.cld or '')[:32] or None, started_at=record.started_at,
                          answered_at=record.answered_at, finished_at=record.finished_at,
                          amount_micros=record.amount_micros, raw_amount=record.raw_amount, currency=record.currency,
                          billed_seconds=record.billed_seconds, call_seconds=record.call_seconds,
                          inbound_fax_id=inbound_fax_id, effective_at=effective_at, observed_at=now, created_at=now)
            if current is not None and same(current):
                return 'duplicate'
            if current is not None and effective_at <= current['effective_at']:
                if any(same(row) and row['effective_at'] == effective_at for row in rows):
                    return 'duplicate'
                connection.execute(table.insert().values(id=uuid4().hex, supersedes_id=None, applied=0, **values))
                return 'older'
            connection.execute(table.insert().values(
                id=uuid4().hex, supersedes_id=current['id'] if current is not None else None, applied=1, **values))
            return 'new' if current is None else 'corrected'

    def unrecorded_in_effect(self, *, since=None, inbound_fax_ids=None, connection=None):
        """The version in effect of each kept record, leaving out any a Faxbot call now holds."""
        table, charges = self.records, self.charges

        def read(conn):
            query = sa.select(table).where(table.c.applied == 1,
                                           table.c.record_id.not_in(sa.select(charges.c.record_id)))
            if since is not None:
                query = query.where(table.c.started_at >= since)
            if inbound_fax_ids is not None:
                query = query.where(table.c.inbound_fax_id.in_(tuple(inbound_fax_ids) or ('',)))
            latest = {}
            for row in conn.execute(query.order_by(table.c.provider_id, table.c.record_id, table.c.version)).mappings():
                latest[(row['provider_id'], row['record_id'])] = dict(row)
            return list(latest.values())
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)


@dataclass
class Sweep:
    checked: int = 0
    matched: int = 0
    recorded: int = 0
    waiting: int = 0
    ambiguous: int = 0
    unavailable: bool = False
    unrecorded: int = 0

    def as_dict(self):
        return {'checked': self.checked, 'matched': self.matched, 'charges_recorded': self.recorded,
                'waiting': self.waiting, 'ambiguous': self.ambiguous, 'carrier_unavailable': self.unavailable,
                'unrecorded_calls': self.unrecorded}


class CarrierReconciler:
    """Bounded, backing-off job: ask the trunk carrier what each finished call cost.

    One sweep reads one window of carrier records (at most ``source.max_pages``
    pages) for the oldest open calls. A failed or rate-limited lookup pauses
    the job, doubling up to an hour; each call waits longer between checks up
    to six hours, a priced call is checked once more a day after it ended to
    settle it, and calls the carrier never prices within a week stop being
    asked about.
    """

    def __init__(self, store, routes, source, *, preset='telnyx', settle_after=SETTLE_AFTER, give_up=GIVE_UP,
                 tolerance=TOLERANCE, numbers=None):
        """``numbers()`` lists the trunk's own fax numbers (its DIDs and caller ID)."""
        self.store, self.routes, self.source = store, routes, source
        self.preset, self.settle_after, self.give_up, self.tolerance = preset, settle_after, give_up, tolerance
        self.numbers = numbers or (lambda: ())
        self.paused_until = None
        self.pause = timedelta(minutes=1)
        self.unrecorded_next = None

    @property
    def provider_id(self):
        return self.source.carrier

    def step(self, *, now=None):
        """One background sweep; never raises for a carrier failure."""
        if not self.source.ready():
            return False
        now = now or utcnow()
        if self.paused_until is not None and now < self.paused_until:
            return False
        result = self.sweep(now=now)
        if not result.unavailable and (self.unrecorded_next is None or now >= self.unrecorded_next):
            self.sweep_unrecorded(now=now)
        return False

    def run_now(self, *, now=None, windows=10):
        """Check every open call now (``faxbot routing reconcile``), up to ``windows`` carrier windows."""
        total = Sweep()
        now = now or utcnow()
        seen = set()
        for _ in range(windows):
            result = self.sweep(now=now, force=True, skip=seen)
            for name in ('checked', 'matched', 'recorded', 'waiting', 'ambiguous'):
                setattr(total, name, getattr(total, name) + getattr(result, name))
            total.unavailable = total.unavailable or result.unavailable
            if result.unavailable or result.checked == 0:
                break
        if not total.unavailable:
            unrecorded = self.sweep_unrecorded(now=now)
            total.unrecorded, total.unavailable = unrecorded.unrecorded, unrecorded.unavailable
        return total

    def _ours(self, record):
        mine = record.cld if record.direction == 'inbound' else record.cli
        try:
            numbers = list(self.numbers() or ())
        except Exception:
            return False
        return any(same_number(mine, number) for number in numbers)

    def sweep_unrecorded(self, *, now=None):
        """Keep the trunk's priced records from the last two days that fit no Faxbot call."""
        now = now or utcnow()
        result = Sweep()
        self.unrecorded_next = now + UNRECORDED_EVERY
        try:
            if not list(self.numbers() or ()):
                return result  # without the trunk's numbers no record can be shown to be this trunk's
        except Exception:
            return result
        start, end = now - UNRECORDED_WINDOW, now - UNRECORDED_GRACE
        try:
            records, complete = self.source.fetch(start, end)
        except Exception as error:
            self._back_off(error, now)
            result.unavailable = True
            return result
        if not complete:
            # Without every record of the window, a record cannot be shown to fit no call.
            logging.getLogger(__name__).warning('Too many carrier records to check for calls Faxbot did not record.')
            return result
        calls = [self.store.view(row) for row in self.store.calls_between(start - MARGIN, end + MARGIN, None)]
        charged = self.store.charged_record_ids([record.id for record in records])
        alone = []
        for record in records:
            if not record.amount_micros or record.id in charged or not self._ours(record):
                continue  # unpriced, zero, already held, or not on this trunk's numbers
            if any(call.sip_call_id and call.sip_call_id == record.sip_call_id for call in calls):
                continue
            if any(fits(call, record, NEAR_A_CALL) for call in calls):
                continue  # Faxbot has a call that may be this one; the normal match decides
            alone.append(record)
        faxes = self.store.received_faxes(start - timedelta(hours=1), end + timedelta(hours=1))
        fitting = {record.id: [fax['id'] for fax in faxes if same_number(fax['to_number'], record.cld)
                               and _near(fax['received'], record.finished_at, self.tolerance)]
                   for record in alone if record.direction == 'inbound'}
        claims = {}
        for found in fitting.values():
            for fax_id in found:
                claims[fax_id] = claims.get(fax_id, 0) + 1
        for record in alone:
            found = fitting.get(record.id, [])
            fax_id = found[0] if len(found) == 1 and claims[found[0]] == 1 else None
            self.store.record_unrecorded(record, provider_id=self.provider_id, inbound_fax_id=fax_id,
                                         effective_at=now, now=now)
            result.unrecorded += 1
        return result

    def _back_off(self, error, now):
        # One minute, doubling while failures continue, at most an hour; at least
        # five minutes when the carrier asked Faxbot to slow down.
        self.pause = timedelta(minutes=1) if self.paused_until is None else min(self.pause * 2, timedelta(hours=1))
        wait = max(self.pause, timedelta(minutes=5)) if isinstance(error, CarrierRateLimited) else self.pause
        self.paused_until = now + wait
        logging.getLogger(__name__).warning('Carrier call charges could not be read; Faxbot will ask again later.')

    def sweep(self, *, now=None, force=False, skip=None):
        now = now or utcnow()
        result = Sweep()
        self.store.expire(self.preset, now=now, give_up=self.give_up)
        due = [row for row in self.store.due_calls(self.preset, now=now, force=force, give_up=self.give_up)
               if skip is None or row['id'] not in skip]
        if not due:
            return result
        start = min(row['started_at'] for row in due) - MARGIN
        end = min(max(start + BUCKET, due[0]['ended_at'] + MARGIN), now + MARGIN)
        # Calls whose answer and end fall inside this window; the others wait for a later one.
        targets = [row for row in due if row['ended_at'] + self.tolerance <= end] or due[:1]
        try:
            records, complete = self.source.fetch(start, end)
        except Exception as error:
            self._back_off(error, now)
            result.unavailable = True
            return result
        self.paused_until, self.pause = None, timedelta(minutes=1)
        views = [self.store.view(row) for row in targets]
        competitors = [self.store.view(row) for row in self.store.calls_between(start, end, self.preset)]
        attached, holding = self.store.holders([record.id for record in records], [view.id for view in competitors])
        if not complete:
            # A partial window cannot prove a match is unique: use exact identities only.
            records = [record for record in records if record.id in attached
                       or any(record.sip_call_id and record.sip_call_id == view.sip_call_id for view in competitors)]
        matches, ambiguous = match_records(views, competitors, records, attached=attached, holding=holding,
                                           tolerance=self.tolerance)
        for row in targets:
            if skip is not None:
                skip.add(row['id'])
            result.checked += 1
            found = matches.get(row['id'], [])
            if row['id'] in ambiguous:
                result.ambiguous += 1
                self.store.mark(row['id'], self.provider_id, 'ambiguous', now=now)
                continue
            final = now - row['ended_at'] >= self.settle_after
            priced = 0
            for record, method in found:
                if record.amount_micros is None:
                    continue  # not priced yet: unknown stays unknown
                stored = self.store.record(row['id'], provider_id=self.provider_id, record=record,
                                           method='call_id' if method == 'known' else method,
                                           effective_at=now, final=final, now=now)
                if stored == 'conflict':
                    continue
                priced += 1
                result.recorded += stored in ('new', 'corrected', 'settled')
            if priced or row['id'] in holding:
                result.matched += 1
                self._attempt_charges(row, now=now)
                if final and priced:
                    self.store.mark(row['id'], self.provider_id, 'settled', now=now)
                else:
                    self.store.mark(row['id'], self.provider_id, 'matched', now=now,
                                    next_check_at=max(row['ended_at'] + self.settle_after, now + FIRST_RETRY))
            else:
                result.waiting += 1
                self.store.mark(row['id'], self.provider_id, 'waiting', now=now)
        return result

    def _attempt_charges(self, row, *, now):
        """Carry the amount in effect to the sent fax's attempt; idempotent, so it is repeated each check."""
        if row['direction'] != 'outbound' or not row['attempt_id']:
            return
        for charge in self.store.in_effect([row['id']]).get(row['id'], []):
            self.routes.ingest_charge(row['attempt_id'], provider_id=self.provider_id, charge_id=charge['record_id'],
                                      amount_micros=charge['amount_micros'], currency=charge['currency'],
                                      billed_seconds=charge['billed_seconds'], final=bool(charge['is_final']),
                                      now=now)
