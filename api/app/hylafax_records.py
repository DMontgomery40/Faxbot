"""Which fax engine handled each trunk call, what SSL Fax did, and per-recipient fax limits (migration 0017).

``fax_engine_calls`` has one row per trunk fax call: the engine chosen for it
(built-in or the SSL Fax engine), the reason when the built-in engine was
chosen, and what the SSL Fax engine saw. A row is written once and later
reports only fill in what is still unknown: when the SSL Fax engine reports a
call the built-in engine was chosen for, its result goes in its own fields and
the chosen engine and reason stay as written; the views name the engine that
handled the call (the one that reported it). ``sslfax_observations`` is append-only: each engine
call that showed whether the other number takes SSL Fax adds one row, and the
newest row is the answer. ``recipient_fax_settings`` holds a person's limits
for one fax number. Recording is evidence only and never changes delivery.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from uuid import uuid4

import sqlalchemy as sa

_NUMBER = re.compile(r'\+?[0-9]{3,20}', re.ASCII)
_KEY = re.compile(r'[A-Za-z0-9_.:-]{1,100}', re.ASCII)
_ID = re.compile(r'[A-Za-z0-9_-]{1,40}', re.ASCII)
_REF = re.compile(r'[A-Za-z0-9_.:-]{1,100}', re.ASCII)
RATES = (14400, 9600, 7200, 4800)


class EngineRecordError(RuntimeError):
    """Sanitized storage failure; never includes SQL or values."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _number(value):
    text = str(value or '').strip()
    return text if _NUMBER.fullmatch(text) else None


def _flag(value):
    return 1 if value is True else 0 if value is False else None


def _seconds(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 86400 else None


def _text(value, limit):
    text = ''.join(character for character in str(value or '') if character.isprintable()).strip()
    return text[:limit] or None


def _iso(value):
    return value.replace(microsecond=0).isoformat() + 'Z' if isinstance(value, datetime) else None


class FaxEngineRecords:
    def __init__(self, engine):
        self.engine = engine
        self._tables = None

    def _table(self, name):
        if self._tables is None:
            metadata = sa.MetaData()
            try:
                self._tables = {table: sa.Table(table, metadata, autoload_with=self.engine)
                                for table in ('fax_engine_calls', 'sslfax_observations', 'recipient_fax_settings')}
            except sa.exc.SQLAlchemyError:
                raise EngineRecordError('Fax engine records are unavailable.') from None
        return self._tables[name]

    def _write(self, operation):
        try:
            with self.engine.begin() as connection:
                return operation(connection)
        except sa.exc.SQLAlchemyError:
            raise EngineRecordError('Fax engine record could not be saved.') from None

    def _read(self, operation):
        try:
            with self.engine.connect() as connection:
                return operation(connection)
        except sa.exc.SQLAlchemyError:
            raise EngineRecordError('Fax engine records are unavailable.') from None

    # Calls -------------------------------------------------------------------

    def record_call(self, *, direction, call_key, engine, job_id=None, reason=None, number=None, now=None):
        """The engine that handles one call (and why, for the built-in engine); written once."""
        if direction not in ('outbound', 'inbound') or engine not in ('builtin', 'hylafax'):
            raise ValueError('Unsupported fax engine record')
        if not _KEY.fullmatch(str(call_key or '')):
            raise ValueError('Unsupported fax engine record')
        now = now or utcnow()
        table = self._table('fax_engine_calls')

        def apply(connection):
            row = connection.execute(sa.select(table).where(table.c.direction == direction,
                                                            table.c.call_key == call_key)).mappings().first()
            if row is not None:
                return row['id']
            identity = uuid4().hex
            connection.execute(table.insert().values(
                id=identity, direction=direction, call_key=call_key,
                job_id=job_id if _ID.fullmatch(str(job_id or '')) else None, engine=engine,
                reason=_text(reason, 200), number=_number(number), created_at=now, updated_at=now))
            return identity
        return self._write(apply)

    def record_result(self, *, direction, call_key, details, job_id=None, number=None, now=None):
        """What the SSL Fax engine saw on one call; fills only what is still unknown, and adds one observation.

        ``details``: engine_ref, sslfax, sslfax_offered (booleans or None),
        transfer_seconds, session_seconds, signal_rate, data_format.
        """
        if not isinstance(details, dict):
            return None
        engine_ref = str(details.get('engine_ref') or '')
        if not _REF.fullmatch(engine_ref):
            return None
        self.record_call(direction=direction, call_key=call_key, engine='hylafax', job_id=job_id, number=number,
                         now=now)
        now = now or utcnow()
        calls, observations = self._table('fax_engine_calls'), self._table('sslfax_observations')
        values = {'engine_ref': engine_ref, 'sslfax': _flag(details.get('sslfax')),
                  'sslfax_offered': _flag(details.get('sslfax_offered')),
                  'transfer_seconds': _seconds(details.get('transfer_seconds')),
                  'session_seconds': _seconds(details.get('session_seconds')),
                  'signal_rate': _text(details.get('signal_rate'), 32),
                  'data_format': _text(details.get('data_format'), 32), 'number': _number(number)}

        def apply(connection):
            row = connection.execute(sa.select(calls).where(calls.c.direction == direction,
                                                            calls.c.call_key == call_key)).mappings().first()
            if row['engine_ref'] not in (None, engine_ref):
                return row['id']
            # Only what is still unknown; the engine chosen and its reason stay as written (handled_by reads both).
            changes = {name: value for name, value in values.items() if row[name] is None and value is not None}
            if changes:
                connection.execute(calls.update().where(calls.c.id == row['id']).values(updated_at=now, **changes))
            accepts = 1 if values['sslfax'] or values['sslfax_offered'] else 0 if values['sslfax_offered'] == 0 else None
            who = values['number']
            if who and accepts is not None:
                exists = connection.execute(sa.select(observations.c.id).where(
                    observations.c.number == who, observations.c.source == engine_ref)).first()
                if exists is None:
                    connection.execute(observations.insert().values(
                        id=uuid4().hex, number=who, direction=direction, accepts=accepts, source=engine_ref,
                        observed_at=now))
            return row['id']
        return self._write(apply)

    def record_negotiation(self, *, direction, call_key, engine, values, job_id=None, number=None, now=None):
        """What one call negotiated, as the engine that handled it reported it (fax_negotiation's values).

        Fills only columns that are still unknown and never rewrites one, so a report posted twice changes
        nothing; a report from a different engine than the one already recorded is ignored. The call's row is
        written first when there is none yet (a call the built-in engine received). Evidence only.
        """
        from .fax_negotiation import COLUMNS, ENGINES
        if engine not in ENGINES or not isinstance(values, dict):
            raise ValueError('Unsupported fax engine record')
        self.record_call(direction=direction, call_key=call_key, engine=engine, job_id=job_id, number=number,
                         now=now)
        now = now or utcnow()
        table = self._table('fax_engine_calls')
        wanted = {name: values.get(name) for name in COLUMNS[1:] + ('session_seconds',)
                  if values.get(name) is not None and name in table.c}

        def apply(connection):
            row = connection.execute(sa.select(table).where(table.c.direction == direction,
                                                            table.c.call_key == call_key)).mappings().first()
            if row['negotiation_by'] not in (None, engine):
                return row['id']
            changes = {name: value for name, value in wanted.items() if row[name] is None}
            if changes and row['negotiation_by'] is None and any(name != 'session_seconds' for name in changes):
                changes['negotiation_by'] = engine
            if changes:
                connection.execute(table.update().where(table.c.id == row['id']).values(updated_at=now, **changes))
            return row['id']
        return self._write(apply)

    def record_page_capability(self, *, call_key, values, job_id=None, number=None, engine='hylafax', now=None):
        """What the other machine said it accepts on one sent call (fax_negotiation.page_capability): its
        longest and widest page, fine resolution, error correction, scan line time, and the call's measured
        time between pages. One row per call in page_capability_observations (migration 0028), never
        rewritten; evidence only, used by dense pages (``pages``)."""
        from .pages.capability import PageRecordError, records_for
        try:
            return records_for(self.engine).record_observation(number, source=call_key, engine=engine,
                                                               values=values, job_id=job_id, now=now)
        except PageRecordError:
            raise EngineRecordError('Fax engine record could not be saved.') from None

    def for_call(self, direction, call_key):
        """The engine record for one call, or None."""
        table = self._table('fax_engine_calls')
        row = self._read(lambda connection: connection.execute(sa.select(table).where(
            table.c.direction == direction, table.c.call_key == str(call_key))).mappings().first())
        if row is None:
            return None
        result = {name: row[name] for name in ('number', 'transfer_seconds', 'session_seconds', 'signal_rate',
                                               'data_format')}
        result['engine'], result['reason'] = handled_by(row)
        result['sslfax'] = None if row['sslfax'] is None else bool(row['sslfax'])
        result['sslfax_offered'] = None if row['sslfax_offered'] is None else bool(row['sslfax_offered'])
        result['created_at'] = _iso(row['created_at'])
        return result

    def accepts_sslfax(self, number):
        """{'accepts': bool, 'observed_at': ISO time, 'direction': ...} from the newest observation, or None."""
        number = _number(number)
        if number is None:
            return None
        table = self._table('sslfax_observations')
        row = self._read(lambda connection: connection.execute(sa.select(table).where(table.c.number == number)
            .order_by(table.c.observed_at.desc(), table.c.id.desc()).limit(1)).mappings().first())
        if row is None:
            return None
        return {'accepts': bool(row['accepts']), 'observed_at': _iso(row['observed_at']),
                'direction': row['direction']}

    # Recipient limits ------------------------------------------------------

    def recipient_settings(self, number):
        """{'max_rate': int|None, 'ecm': bool|None, 'updated_at': ...} for one fax number, or None."""
        number = _number(number)
        if number is None:
            return None
        table = self._table('recipient_fax_settings')
        row = self._read(lambda connection: connection.execute(
            sa.select(table).where(table.c.number == number)).mappings().first())
        if row is None:
            return None
        return {'max_rate': row['max_rate'], 'ecm': None if row['ecm'] is None else bool(row['ecm']),
                'updated_at': _iso(row['updated_at'])}

    def set_recipient_settings(self, number, *, max_rate=None, ecm=None, actor=None, now=None):
        """Save one number's limits; both None removes them (the installation's settings apply)."""
        number = _number(number)
        if number is None:
            raise ValueError('Enter the fax number with its country code.')
        if max_rate is not None and max_rate not in RATES:
            raise ValueError('Choose one of the listed fax speeds.')
        if ecm is not None and not isinstance(ecm, bool):
            raise ValueError('Choose on or off for error correction.')
        now = now or utcnow()
        table = self._table('recipient_fax_settings')

        def apply(connection):
            connection.execute(table.delete().where(table.c.number == number))
            if max_rate is None and ecm is None:
                return None
            connection.execute(table.insert().values(id=uuid4().hex, number=number, max_rate=max_rate,
                                                     ecm=None if ecm is None else int(ecm), updated_at=now,
                                                     updated_by=_text(actor, 100)))
            return number
        return self._write(apply)


    def unfinished_sends(self, *, before, since):
        """(job, attempt) the engine took between ``since`` and ``before`` and never reported on."""
        table = self._table('fax_engine_calls')

        def read(connection):
            return [(row['job_id'], row['call_key']) for row in connection.execute(sa.select(
                table.c.job_id, table.c.call_key).where(
                    table.c.direction == 'outbound', table.c.engine == 'hylafax', table.c.engine_ref.is_(None),
                    table.c.created_at < before, table.c.created_at >= since,
                    table.c.job_id.is_not(None)).order_by(table.c.created_at).limit(500)).mappings()]
        return self._read(read)

    # Screens --------------------------------------------------------------

    def sent_detail(self, job_id):
        """The engine line for Sent details: {'engine', 'sslfax', 'sentence'} for the fax's newest call, or None."""
        if not _ID.fullmatch(str(job_id or '')):
            return None
        calls = self._table('fax_engine_calls')
        try:
            records = sa.Table('sip_call_records', sa.MetaData(), autoload_with=self.engine)
        except sa.exc.SQLAlchemyError:
            raise EngineRecordError('Fax engine records are unavailable.') from None

        def read(connection):
            row = connection.execute(sa.select(calls).where(calls.c.direction == 'outbound', calls.c.job_id == job_id)
                                     .order_by(calls.c.created_at.desc(), calls.c.id.desc()).limit(1)).mappings().first()
            if row is None:
                return None, None
            call = connection.execute(sa.select(records).where(
                records.c.direction == 'outbound', records.c.call_id == row['call_key'])).mappings().first()
            return dict(row), dict(call) if call is not None else None
        row, call = self._read(read)
        if row is None:
            return None
        pages = (call or {}).get('pages')
        engine, reason = handled_by(row)
        sentence = None
        if engine == 'builtin':
            sentence = reason
        elif row['sslfax'] == 1 and row['transfer_seconds'] is not None and pages:
            sentence = sslfax_sentence(row['transfer_seconds'], pages)
        # What the call negotiated (measurement only), once the call has a result.
        from .fax_negotiation import call_view
        negotiation = call_view(row, call) if (call or {}).get('fax_status') is not None else None
        return {'engine': engine, 'sslfax': None if row['sslfax'] is None else bool(row['sslfax']),
                'sentence': sentence, 'negotiation': negotiation}

    def recipient_detail(self, number):
        """Recipients, Details: whether the number takes SSL Fax (and since when) and its own fax limits."""
        accepts = self.accepts_sslfax(number)
        limits = self.recipient_settings(number)
        return {'accepts_sslfax': accepts['accepts'] if accepts else None,
                'accepts_sslfax_at': accepts['observed_at'] if accepts else None,
                'max_rate': (limits or {}).get('max_rate'), 'ecm': (limits or {}).get('ecm')}


# A page over an ordinary fax call takes about this long at 14,400 bit/s (measured on 2026-10-03:
# six pages in 50 s); SSL Fax sends a page in about a second.
ORDINARY_SECONDS_PER_PAGE = 8


def ordinary_seconds(pages):
    return ORDINARY_SECONDS_PER_PAGE * max(1, pages)


def sslfax_sentence(transfer_seconds, pages):
    return (f'The pages were sent faster during the call: {transfer_seconds} seconds instead of about '
            f'{ordinary_seconds(pages)}.')


def sslfax_savings(routes, engine, *, since, days):
    """What faxes that went over SSL Fax saved on the line, priced with the trunk carrier's own billing.

    Always an estimate: the same fax's ordinary call is the measured call with
    the measured page transfer replaced by about eight seconds a page. A
    carrier that bills whole minutes often charges a short fax the same either way.
    """
    from .routing.costs import attempt_cost
    from .routing.database import reflect
    tables = reflect(engine, ('fax_engine_calls', 'sip_call_records'))
    calls, records = tables['fax_engine_calls'], tables['sip_call_records']
    with engine.connect() as connection:
        rows = connection.execute(sa.select(
            calls.c.direction, calls.c.transfer_seconds, records.c.connected_seconds, records.c.pages,
        ).join(records, sa.and_(records.c.direction == calls.c.direction, records.c.call_id == calls.c.call_key))
            .where(calls.c.sslfax == 1, calls.c.created_at >= since)).all()
    result = {'faxes': 0, 'seconds_saved': 0, 'priced': 0, 'in_plan': 0, 'unpriced': 0, 'saved': {},
              'same_cost': 0}
    cards = {}
    for row in rows:
        if row.connected_seconds is None or row.transfer_seconds is None or not row.pages:
            continue
        actual = row.connected_seconds
        ordinary = max(actual, actual - row.transfer_seconds + ordinary_seconds(row.pages))
        result['faxes'] += 1
        result['seconds_saved'] += ordinary - actual
        if row.direction not in cards:
            cards[row.direction] = routes.card_for('sip', row.direction)
        card = cards[row.direction]
        if card is None:
            result['unpriced'] += 1
        elif card.flat_plan:
            result['in_plan'] += 1
        else:
            result['priced'] += 1
            saved = (attempt_cost(card, seconds=ordinary, pages=row.pages, delivered=True)
                     - attempt_cost(card, seconds=actual, pages=row.pages, delivered=True))
            if saved > 0:
                result['saved'][card.currency] = result['saved'].get(card.currency, 0) + saved
            else:
                result['same_cost'] += 1
    result['sentence'] = savings_sentence(result, days)
    return result


def savings_sentence(result, days):
    from .routing.costs import money_text
    count = result['faxes']
    if not count:
        return f'No faxes were sent faster in the last {days} days.'
    minutes = max(1, round(result['seconds_saved'] / 60))
    sentence = (f"{count} {'fax' if count == 1 else 'faxes'} had {'its' if count == 1 else 'their'} pages sent "
                f"faster: about {minutes} {'minute' if minutes == 1 else 'minutes'} less on the phone")
    money = ' + '.join(money_text(micros, currency) for currency, micros in sorted(result['saved'].items()))
    sentence += f' and about {money} saved.' if money else '.'
    if result['same_cost'] and result['same_cost'] == result['priced']:
        sentence += ' Your carrier charges whole minutes, so ' + ('it' if count == 1 else 'they') + ' cost the same.'
    return sentence


def handled_by(row):
    """(engine, reason) for one call: the SSL Fax engine when it chose it or reported on it (its reference),
    else the built-in engine and why it was chosen."""
    if row['engine'] == 'hylafax' or row['engine_ref'] is not None:
        return 'hylafax', None
    return row['engine'], row['reason']


def records_for(engine):
    return FaxEngineRecords(engine)


def safely(operation, *args, **kwargs):
    """Run a recording step; a failure is logged and never reaches the fax."""
    try:
        return operation(*args, **kwargs)
    except Exception:
        logging.getLogger(__name__).warning('A fax engine record could not be saved.')
        return None
