"""Which fax engine handled each trunk call, what SSL Fax did, and per-recipient fax limits (migration 0017).

``fax_engine_calls`` has one row per trunk fax call: the engine (built-in or
the SSL Fax engine), the reason when the built-in engine handled it, and what
the SSL Fax engine saw. A row is written once and later reports only fill in
what is still unknown. ``sslfax_observations`` is append-only: each engine
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
            changes = {name: value for name, value in values.items() if row[name] is None and value is not None}
            if row['engine'] != 'hylafax' and row['engine_ref'] is None:
                # The engine handled the call after all (the built-in choice was not used).
                changes['engine'] = 'hylafax'
                changes['reason'] = None
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

    def for_call(self, direction, call_key):
        """The engine record for one call, or None."""
        table = self._table('fax_engine_calls')
        row = self._read(lambda connection: connection.execute(sa.select(table).where(
            table.c.direction == direction, table.c.call_key == str(call_key))).mappings().first())
        if row is None:
            return None
        result = {name: row[name] for name in ('engine', 'reason', 'number', 'transfer_seconds', 'session_seconds',
                                               'signal_rate', 'data_format')}
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


def records_for(engine):
    return FaxEngineRecords(engine)


def safely(operation, *args, **kwargs):
    """Run a recording step; a failure is logged and never reaches the fax."""
    try:
        return operation(*args, **kwargs)
    except Exception:
        logging.getLogger(__name__).warning('A fax engine record could not be saved.')
        return None
