"""Channels at measured peak (N24): how many calls a fax system really carried at once, from its call records.

Fax server channel licences are bought for a guessed peak and renewed every year. This reads call records, Faxbot's
own (``sip_call_records``) or another system's imported file, and reports for each:

- the most calls at once over the whole period, and when; that many channels would have carried every call;
- for each clock hour, the most calls at once within it, and the 99th percentile of those hourly peaks (99 hours in
  100 never needed more), overall and for each hour of the day;
- the channels that never carried a call: from the channel numbers when the file names them (RightFax does), else
  the licensed channels above the peak, which were never needed at the same time as the others.

**Formats read** (each layout read from the vendor's own documentation on 10 October 2026):

- ``asterisk``: Asterisk's CDR CSV (Master.csv), no header row; fields in the order cdr_csv.c writes them:
  accountcode, src, dst, dcontext, clid, channel, dstchannel, lastapp, lastdata, start, answer, end, duration,
  billsec, disposition, amaflags (then optional uniqueid, userfield, peeraccount, linkedid, sequence). Times are
  ``YYYY-MM-DD HH:MM:SS`` in the server's local time unless ``usegmtime`` is set. Source: Asterisk's cdr/cdr_csv.c.
- ``cucm``: Cisco Unified CM CDR files: line 1 the field names, line 2 their types, then one call per line;
  ``dateTimeOrigination``, ``dateTimeConnect`` (0 when the call never connected) and ``dateTimeDisconnect`` are
  UTC epoch seconds. Fields are found by name, since their positions change by release. Source: Cisco's CDR
  Analysis and Reporting guide, Release 15, "Call Details Records Overview", and 12.5(1)SU1 "CDR Field
  Descriptions".
- ``rightfax``: OpenText RightFax DocTransport audit log, level 3 (comma-delimited, 15 fields: direction S or R,
  date MM/DD/YYYY, time HH:MM, channel, duration in seconds, phone number, remote ID, result, page count, ...) or
  level 4 (tab-delimited, one file per hour, date YYYYMMDD, time HHMM, channel, duration, ...). Source: RightFax CE
  21.2 Administrator Guide, DocTransport logging. Times are to the minute in the server's local time.
- ``faxmaker``: GFI FaxMaker's activity export (CSV with a header: Direction, Date, Time, Line, Call duration
  (seconds), Status and more). Source: GFI FaxMaker manual, "Export to CSV".
- ``csv``: any other system, as a CSV with the columns ``start`` (date and time), ``end`` or ``duration``
  (seconds), and optionally ``direction`` (sent or received), ``channel`` and ``number``.

OpenText XM Fax (XMedius) publishes no call-record export layout that Faxbot could find; export its history as the
documented CSV. Local times in a file are read in the time zone you name (the installation's by default) and kept
as UTC. Nothing here changes a licence or contacts a vendor.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import io
import math
import re
from uuid import uuid4

import sqlalchemy as sa

from .workbook import normal


FORMATS = ('asterisk', 'cucm', 'rightfax', 'faxmaker', 'csv')
FORMAT_LABELS = {'asterisk': 'Asterisk call records (Master.csv)', 'cucm': 'Cisco Unified CM call records',
                 'rightfax': 'RightFax DocTransport audit log', 'faxmaker': 'GFI FaxMaker activity export',
                 'csv': 'Call records (CSV)'}
SOURCES = {
    'asterisk': 'https://raw.githubusercontent.com/asterisk/asterisk/master/cdr/cdr_csv.c',
    'cucm': ('https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cucm/callReportingBillingAdmin/12_5_1/'
             'cucm_b_reporting-billing-administration-guide-1251SU1/'
             'cucm_b_reporting-and-billing-administration-guide_chapter_01010.pdf'),
    'rightfax': 'https://atnystore01.blob.core.windows.net/webfiles/resources/RightFax_CE_212_Administrator_Guide.pdf',
    'faxmaker': 'https://manuals.gfi.com/en/fax19/Content/ACM/Using/Export_CSV.htm',
}
READ_ON = '2026-10-10'
PERCENTILE = 0.99
# A call with no recorded end held its channel this long at most (capacity.HOLD's reasoning: a long fax call).
LONGEST_CALL = timedelta(minutes=30)
MAX_CALLS = 1_000_000
MAX_SKIPPED_SHOWN = 20
WINDOW_DAYS = 90
_ASTERISK_TIME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}')


class CallFileError(ValueError):
    """A call-record file Faxbot cannot read; the message is one plain sentence."""


@dataclass(frozen=True)
class Call:
    started_at: datetime        # naive UTC
    ended_at: datetime          # naive UTC
    direction: str | None = None
    channel: str | None = None
    number: str | None = None


@dataclass
class Parsed:
    calls: list
    source_format: str
    skipped: list
    skipped_count: int = 0

    def skip(self, sentence):
        self.skipped_count += 1
        if len(self.skipped) < MAX_SKIPPED_SHOWN:
            self.skipped.append(sentence)


# -- reading files ---------------------------------------------------------------------------------------------------

def _zone(name):
    from ..people_time import zone
    return zone(name)


def _utc(local, zone):
    """A naive local time in ``zone`` as naive UTC."""
    return local.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)


def _rows(text):
    text = text.lstrip('﻿')
    first = text.splitlines()[0] if text.strip() else ''
    delimiter = '\t' if first.count('\t') > first.count(',') else ','
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def detect(text):
    """The format a call-record file looks like, or None."""
    rows = _rows(text)[:5]
    if not rows:
        return None
    names = {normal(cell) for cell in rows[0]}
    if 'datetimeorigination' in names:
        return 'cucm'
    if 'call duration (seconds)' in names and 'date' in names:
        return 'faxmaker'
    if 'start' in names and ({'end', 'duration'} & names):
        return 'csv'
    first = rows[0]
    if len(first) >= 16 and _ASTERISK_TIME.fullmatch(first[9].strip() or ''):
        return 'asterisk'
    if len(first) >= 5 and first[0].strip().upper() in ('S', 'R') and (
            re.fullmatch(r'[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}', first[1].strip())
            or re.fullmatch(r'[0-9]{8}', first[1].strip())):
        return 'rightfax'
    return None


def parse_calls(data, *, source_format=None, time_zone=None, date_order='mdy', numbers=None):
    """``Parsed`` calls from one file; ``numbers`` (digits) keeps only calls to or from those numbers."""
    from .inventory import parse_day
    text = data.decode('utf-8-sig', errors='replace') if isinstance(data, (bytes, bytearray)) else str(data or '')
    if not text.strip():
        raise CallFileError('The file is empty.')
    found = source_format or detect(text)
    if found not in FORMATS:
        raise CallFileError('Faxbot does not recognise this file. Choose its format: asterisk, cucm, rightfax, '
                            'faxmaker or csv.')
    zone = _zone(time_zone)
    wanted = {re.sub(r'\D', '', item)[-10:] for item in (numbers or ()) if re.sub(r'\D', '', item)}
    parsed = Parsed([], found, [])
    reader = {'asterisk': _asterisk, 'cucm': _cucm, 'rightfax': _rightfax, 'faxmaker': _faxmaker,
              'csv': _generic}[found]
    for position, call, reason in reader(_rows(text), zone, date_order, parse_day):
        if reason:
            parsed.skip(f'Line {position}: {reason}')
            continue
        if call is None:
            continue
        if wanted and not ({re.sub(r'\D', '', call.number or '')[-10:]} & wanted):
            continue
        if call.ended_at < call.started_at:
            parsed.skip(f'Line {position}: the call ends before it starts.')
            continue
        parsed.calls.append(call)
        if len(parsed.calls) > MAX_CALLS:
            raise CallFileError(f'The file has more than {MAX_CALLS:,} calls; import it a month at a time.')
    if not parsed.calls:
        raise CallFileError('The file has no call Faxbot can read.' if not parsed.skipped
                            else f'The file has no call Faxbot can read. {parsed.skipped[0]}')
    return parsed


def _asterisk(rows, zone, _order, _parse_day):
    for position, row in enumerate(rows, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) < 16:
            yield position, None, 'it has fewer than the 16 fields Asterisk writes.'
            continue
        try:
            start = datetime.strptime(row[9].strip(), '%Y-%m-%d %H:%M:%S')
            end = datetime.strptime(row[11].strip(), '%Y-%m-%d %H:%M:%S') if row[11].strip() else None
        except ValueError:
            yield position, None, 'its start or end is not a time.'
            continue
        if end is None:
            try:
                end = start + timedelta(seconds=int(row[12] or 0))
            except ValueError:
                end = start + LONGEST_CALL
        yield position, Call(_utc(start, zone), _utc(end, zone), None, None, row[2].strip()[:32] or None), None


def _cucm(rows, _zone_unused, _order, _parse_day):
    if not rows:
        return
    names = [normal(cell) for cell in rows[0]]
    at = {name: names.index(name) for name in names}

    def value(row, name):
        index = at.get(name)
        return row[index].strip() if index is not None and index < len(row) else ''
    for position, row in enumerate(rows[1:], start=2):
        if not row or not any(cell.strip() for cell in row):
            continue
        origin = value(row, 'datetimeorigination')
        if not origin.isdigit():
            if position == 2:
                continue  # the line of field types
            yield position, None, 'its origination time is not a number of seconds.'
            continue
        if origin == '0':
            yield position, None, 'it has no origination time.'
            continue
        disconnect = value(row, 'datetimedisconnect')
        start = datetime.fromtimestamp(int(origin), tz=timezone.utc).replace(tzinfo=None)
        end = (datetime.fromtimestamp(int(disconnect), tz=timezone.utc).replace(tzinfo=None)
               if disconnect.isdigit() and disconnect != '0' else start + LONGEST_CALL)
        number = value(row, 'finalcalledpartynumber') or value(row, 'originalcalledpartynumber')
        yield position, Call(start, end, None, None, number[:32] or None), None


def _rightfax(rows, zone, _order, _parse_day):
    for position, row in enumerate(rows, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) < 5:
            yield position, None, 'it has fewer than the 5 fields Faxbot reads (direction, date, time, channel, ' \
                                  'duration).'
            continue
        direction = {'S': 'sent', 'R': 'received'}.get(row[0].strip().upper())
        day, clock = row[1].strip(), row[2].strip()
        try:
            if re.fullmatch(r'[0-9]{8}', day):  # level 4: YYYYMMDD and HHMM
                start = datetime.strptime(day + clock.zfill(4), '%Y%m%d%H%M')
            else:
                start = datetime.strptime(f'{day} {clock}', '%m/%d/%Y %H:%M')
            seconds = int(float(row[4].strip() or 0))
        except ValueError:
            yield position, None, 'its date, time or duration is not readable.'
            continue
        number = row[5].strip()[:32] if len(row) > 5 else None
        yield position, Call(_utc(start, zone), _utc(start + timedelta(seconds=seconds), zone), direction,
                             row[3].strip()[:32] or None, number or None), None


def _clock(text):
    text = text.strip().upper()
    for pattern in ('%H:%M:%S', '%H:%M', '%I:%M:%S %p', '%I:%M %p'):
        try:
            return datetime.strptime(text, pattern).time()
        except ValueError:
            continue
    raise ValueError(text)


def _faxmaker(rows, zone, order, parse_day):
    names = [normal(cell) for cell in rows[0]]
    at = {name: names.index(name) for name in names}

    def value(row, name):
        index = at.get(name)
        return row[index].strip() if index is not None and index < len(row) else ''
    for position, row in enumerate(rows[1:], start=2):
        if not row or not any(cell.strip() for cell in row):
            continue
        try:
            day = parse_day(value(row, 'date'), order=order)
            clock = _clock(value(row, 'time')) if value(row, 'time') else time(int(value(row, 'hour') or 0),
                                                                                  int(value(row, 'minute') or 0))
            seconds = int(float(value(row, 'call duration (seconds)') or 0))
        except ValueError:
            yield position, None, 'its date, time or call duration is not readable.'
            continue
        if day is None:
            yield position, None, 'it has no date.'
            continue
        start = datetime.combine(day, clock)
        direction = value(row, 'direction').lower()
        direction = 'sent' if direction.startswith(('out', 'sen')) else 'received' if direction.startswith(
            ('in', 'rec')) else None
        number = value(row, 'recipient number') if direction == 'sent' else value(row, 'sender number')
        yield position, Call(_utc(start, zone), _utc(start + timedelta(seconds=seconds), zone), direction,
                             value(row, 'line')[:32] or None, number[:32] or None), None


def _moment(text, order, parse_day):
    text = text.strip()
    iso = re.fullmatch(r'([0-9]{4}-[0-9]{2}-[0-9]{2})[T ]([0-9:]{4,8})', text)
    if iso:
        return datetime.combine(date.fromisoformat(iso.group(1)), _clock(iso.group(2)))
    day, _, clock = text.partition(' ')
    found = parse_day(day, order=order)
    if found is None:
        raise ValueError(text)
    return datetime.combine(found, _clock(clock) if clock else time())


def _generic(rows, zone, order, parse_day):
    names = [normal(cell) for cell in rows[0]]
    at = {name: names.index(name) for name in names}

    def value(row, name):
        index = at.get(name)
        return row[index].strip() if index is not None and index < len(row) else ''
    for position, row in enumerate(rows[1:], start=2):
        if not row or not any(cell.strip() for cell in row):
            continue
        try:
            start = _moment(value(row, 'start'), order, parse_day)
            end = (_moment(value(row, 'end'), order, parse_day) if value(row, 'end')
                   else start + timedelta(seconds=int(float(value(row, 'duration') or 0))))
        except ValueError:
            yield position, None, 'its start, end or duration is not readable.'
            continue
        direction = value(row, 'direction').lower()
        direction = direction if direction in ('sent', 'received') else None
        yield position, Call(_utc(start, zone), _utc(end, zone), direction, value(row, 'channel')[:32] or None,
                             value(row, 'number')[:32] or None), None


# -- the report ------------------------------------------------------------------------------------------------------

def _hour(moment):
    return moment.replace(minute=0, second=0, microsecond=0)


def hourly_peaks(intervals):
    """{hour start (naive UTC): most calls at once within that hour} for every hour from the first call to the
    last; a call ending at the moment another starts does not overlap it."""
    events = sorted([(start, 1) for start, end in intervals] + [(end, -1) for start, end in intervals],
                    key=lambda item: (item[0], item[1]))
    if not events:
        return {}
    peaks, current = {}, 0
    hour = _hour(events[0][0])
    peaks[hour] = 0
    for moment, step in events:
        while moment >= hour + timedelta(hours=1):
            hour += timedelta(hours=1)
            peaks[hour] = current
        current += step
        peaks[hour] = max(peaks[hour], current)
    return peaks


def percentile(values, share=PERCENTILE):
    """The nearest-rank percentile: the smallest value at least ``share`` of the values do not exceed."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def report(calls, *, licensed=None, time_zone=None):
    """What the calls say about channels: peak, hourly 99th percentile, hour-of-day profile, channels never used."""
    intervals = [(call.started_at, max(call.ended_at, call.started_at)) for call in calls]
    if not intervals:
        return {'calls': 0, 'peak': 0, 'sentence': 'There are no calls to measure.'}
    peaks = hourly_peaks(intervals)
    peak = max(peaks.values())
    when = min(hour for hour, value in peaks.items() if value == peak)
    zone = _zone(time_zone)
    by_hour_of_day = {}
    for hour, value in peaks.items():
        local = hour.replace(tzinfo=timezone.utc).astimezone(zone)
        by_hour_of_day.setdefault(local.hour, []).append(value)
    profile = [{'hour': hour, 'p99': percentile(by_hour_of_day.get(hour, [0])),
                'peak': max(by_hour_of_day.get(hour, [0]))} for hour in range(24)]
    p99 = percentile(list(peaks.values()))
    busy = [value for value in peaks.values() if value]
    named = sorted({call.channel for call in calls if call.channel})
    first, last = min(start for start, _ in intervals), max(end for _, end in intervals)
    days = max(1, (last - first).days + (1 if (last - first).seconds else 0))
    result = {
        'calls': len(calls), 'first': first.isoformat(), 'last': last.isoformat(), 'days': days,
        'hours': len(peaks), 'peak': peak, 'peak_hour': when.isoformat(), 'p99': p99,
        'busy_p99': percentile(busy) if busy else 0, 'profile': profile, 'channels_named': named,
        'licensed': licensed, 'sufficed': peak,
    }
    if licensed:
        if named:
            result['never_used'] = max(0, licensed - len(named))
            result['never_used_basis'] = 'channels'
        else:
            result['never_used'] = max(0, licensed - peak)
            result['never_used_basis'] = 'peak'
    result['sentence'] = _sentence(result)
    return result


def _sentence(found):
    days = found['days']
    text = (f"{found['calls']:,} {'call' if found['calls'] == 1 else 'calls'} over {days:,} "
            f"{'day' if days == 1 else 'days'}: at most {found['peak']} at once, and in 99 hours out of 100 no more "
            f"than {found['p99']}.")
    licensed = found.get('licensed')
    if licensed:
        never = found.get('never_used', 0)
        if found.get('never_used_basis') == 'channels':
            text += (f" The records name {len(found['channels_named'])} channels, so {never} of your {licensed} "
                     f"licensed {'channel' if licensed == 1 else 'channels'} never carried a call.")
        elif never:
            text += (f" {found['peak']} {'channel' if found['peak'] == 1 else 'channels'} would have carried every "
                     f"call, so {never} of your {licensed} licensed channels were never needed at the same time as "
                     'the others.')
        else:
            text += f" Every one of your {licensed} licensed channels was needed at the busiest moment."
    return text


# -- stored imports --------------------------------------------------------------------------------------------------

def _tables(engine):
    from .database import reflect
    return reflect(engine, ('channel_call_imports', 'channel_calls'))


def import_calls(engine, parsed, *, system, data, file_name=None, licensed=None, time_zone=None, actor=None,
                 now=None):
    """Store one file's calls for ``system``; returns the import's id."""
    from .database import utcnow, write_transaction
    system = ' '.join(str(system or '').split())[:100]
    if not system:
        raise CallFileError('Name the system these calls come from, such as RightFax at HQ.')
    if licensed is not None and not 0 < int(licensed) <= 10_000:
        raise CallFileError('Enter the licensed channels as a whole number from 1 to 10,000.')
    now = (now or utcnow()).replace(microsecond=0)
    actor = actor or {}
    tables = _tables(engine)
    imports, calls = tables['channel_call_imports'], tables['channel_calls']
    import_id = uuid4().hex
    with write_transaction(engine) as connection:
        connection.execute(imports.insert().values(
            id=import_id, system=system, source_format=parsed.source_format,
            file_name=(file_name or '')[:200] or None,
            file_sha256=hashlib.sha256(bytes(data) if isinstance(data, (bytes, bytearray))
                                       else str(data).encode()).hexdigest(),
            calls=len(parsed.calls), first_call=min(call.started_at for call in parsed.calls),
            last_call=max(call.ended_at for call in parsed.calls), licensed_channels=licensed,
            time_zone=(time_zone or '')[:64] or None, removed_at=None, removed_by_name=None,
            imported_by=actor.get('id'), imported_by_name=actor.get('name'), created_at=now))
        batch = []
        for call in parsed.calls:
            batch.append({'id': uuid4().hex, 'import_id': import_id, 'started_at': call.started_at,
                          'ended_at': call.ended_at, 'direction': call.direction, 'channel': call.channel,
                          'number': call.number})
            if len(batch) >= 5_000:
                connection.execute(calls.insert(), batch)
                batch = []
        if batch:
            connection.execute(calls.insert(), batch)
    return import_id


def remove_import(engine, import_id, *, actor=None, now=None):
    from .database import utcnow, write_transaction
    imports = _tables(engine)['channel_call_imports']
    with write_transaction(engine) as connection:
        done = connection.execute(imports.update().where(imports.c.id == import_id, imports.c.removed_at.is_(None))
                                  .values(removed_at=(now or utcnow()).replace(microsecond=0),
                                          removed_by_name=(actor or {}).get('name'))).rowcount
    if not done:
        raise CallFileError('That import is not there or was already removed.')


def imports(engine):
    """Every import that is not removed, newest first."""
    from .database import read_connection
    table = _tables(engine)['channel_call_imports']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(table).where(table.c.removed_at.is_(None))
                                  .order_by(table.c.created_at.desc(), table.c.id)).mappings().all()
    return [dict(row) for row in rows]


def system_calls(engine, system):
    """(calls, licensed, time zone, imports) for one system, from its imports that are not removed."""
    from .database import read_connection
    calls = _tables(engine)['channel_calls']
    found = [row for row in imports(engine) if row['system'] == system]
    if not found:
        return [], None, None, []
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(calls).where(calls.c.import_id.in_([row['id'] for row in found]))
                                  .order_by(calls.c.started_at)).mappings().all()
    seen, result = set(), []
    for row in rows:  # the same call in two overlapping files counts once
        key = (row['started_at'], row['ended_at'], row['channel'], row['number'])
        if key in seen:
            continue
        seen.add(key)
        result.append(Call(row['started_at'], row['ended_at'], row['direction'], row['channel'], row['number']))
    licensed = next((row['licensed_channels'] for row in found if row['licensed_channels']), None)
    return result, licensed, found[0]['time_zone'], found


def faxbot_calls(engine, *, now=None, days=WINDOW_DAYS):
    """Faxbot's own trunk calls over the last ``days`` days, sent and received."""
    from .database import read_connection, reflect, utcnow
    now = now or utcnow()
    records = reflect(engine, ('sip_call_records',))['sip_call_records']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(records.c.direction, records.c.started_at, records.c.answered_at,
                                            records.c.ended_at, records.c.did, records.c.called)
                                  .where(records.c.started_at >= now - timedelta(days=days))).mappings().all()
    found = []
    for row in rows:
        start = row['started_at']
        end = row['ended_at'] or min(start + LONGEST_CALL, now)
        direction = 'received' if row['direction'] == 'inbound' else 'sent'
        found.append(Call(start, max(end, start), direction, None,
                          (row['did'] if direction == 'received' else row['called']) or None))
    return found


def view(engine, values=None, *, now=None):
    """Each system's channel report: Faxbot's own trunk calls, and each system you imported calls for."""
    from ..people_time import installation_zone_name
    zone = getattr(values, 'time_zone', None) if values is not None else None
    zone = zone if zone is not None else installation_zone_name()
    systems = []
    own = faxbot_calls(engine, now=now)
    licensed_own = _own_lines(values)
    if own:
        systems.append({'system': 'Faxbot', 'own': True, 'source': f'Faxbot\'s own trunk calls, last {WINDOW_DAYS} '
                        'days', 'report': report(own, licensed=licensed_own, time_zone=zone), 'imports': []})
    names = list(dict.fromkeys(row['system'] for row in imports(engine)))
    for name in names:
        calls, licensed, _, found = system_calls(engine, name)
        systems.append({'system': name, 'own': False,
                        'source': ', '.join(sorted({FORMAT_LABELS[row['source_format']] for row in found})),
                        'report': report(calls, licensed=licensed, time_zone=zone),
                        'imports': [_import_view(row) for row in found]})
    return {'systems': systems,
            'sentence': None if systems else ('No call records yet. Import another fax server\'s call records, or '
                                              'Faxbot measures its own once its trunk carries calls.'),
            'formats': [{'format': key, 'label': FORMAT_LABELS[key], 'source': SOURCES.get(key)} for key in FORMATS],
            'read_on': READ_ON,
            'note': 'Advice only: Faxbot never changes a licence or contacts a vendor.'}


def _own_lines(values):
    """The calls at once your trunks allow together (Providers → trunk), or None without a trunk."""
    if values is None:
        return None
    from ..capacity import trunk_calls_at_once
    from .. import sip_trunk
    total = sum(trunk_calls_at_once(trunk.values) or 0 for trunk in sip_trunk.trunk_accounts(values))
    return total or None


def _import_view(row):
    def iso(value):
        return value.isoformat() if hasattr(value, 'isoformat') else value
    return {'id': row['id'], 'format': row['source_format'], 'format_label': FORMAT_LABELS[row['source_format']],
            'file_name': row['file_name'], 'calls': row['calls'], 'first_call': iso(row['first_call']),
            'last_call': iso(row['last_call']), 'licensed_channels': row['licensed_channels'],
            'imported_by': row['imported_by_name'], 'imported_at': iso(row['created_at'])}

