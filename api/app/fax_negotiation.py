"""What each fax call negotiated: the engines' reports, the detail sentence and the summary (migration 0023).

Measurement only. Nothing here chooses a speed, a compression or error
correction for a call, and recording runs after the fax's own result is saved
(``hylafax_records.safely``), so it never changes delivery.

What each engine reports (checked against the source each engine runs:
HylaFAX+ 7.0.11, Asterisk 22.11.0 with spandsp 0.0.6):

=========================  =========================  ===============================
Metric                     Built-in engine (Asterisk)  SSL Fax engine (HylaFAX+)
=========================  =========================  ===============================
Starting speed             not reported               whole call (session log)
Lowest speed               not reported               whole call (session log)
Speed at the end           last page (FAXBITRATE)     last page (session log)
Trainings                  not reported               whole call (session log)
Compression                not reported               whole call (session log)
Resolution                 last page (FAXRESOLUTION)  whole call (session log)
Error correction (ECM)     not reported *             whole call (session log)
Pages confirmed            whole call (FAXPAGES)      whole call (job file / image)
Page transfer seconds      not reported               whole call (session log, image)
Session seconds            not reported               whole call (session log)
=========================  =========================  ===============================

* ``FAXOPT(ecm)`` after SendFAX or ReceiveFAX returns the setting Faxbot asked
  for, not what the call agreed, so it is never recorded as a measurement.
  spandsp also reports its starting speed and no image on a call that never
  trained, so the built-in engine's values are kept only when a page was
  confirmed. The job file's ``signalrate``/``dataformat`` and faxinfo's values
  are single-page values; the whole-call values here come from the session log.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta, timezone
import json
import re

import sqlalchemy as sa

RATES = frozenset({2400, 4800, 7200, 9600, 12000, 14400, 16800, 19200, 21600, 24000, 26400, 28800, 31200, 33600})
COMPRESSIONS = ('MH', 'MR', 'MMR', 'JBIG', 'JPEG', 'mixed')
RESOLUTIONS = ('standard', 'fine', 'superfine', 'mixed')
ECM = ('on', 'off', 'mixed')
ENGINES = ('builtin', 'hylafax')
COLUMNS = ('negotiation_by', 'rate_first', 'rate_lowest', 'rate_last_page', 'trainings', 'compression', 'resolution',
           'resolution_last_page', 'ecm')
DAYS = (7, 30, 90)
NOT_REPORTED = 'not reported by this engine'
MEASURE_ONLY = ('Faxbot only measures these for now; it does not change speed, compression or error correction '
                'because of them.')


def _rate(value):
    if isinstance(value, bool):
        return None
    try:
        rate = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return rate if rate in RATES else None


def _choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def _count(value, highest):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= highest else None


def spandsp_resolution(value):
    """'standard', 'fine' or 'superfine' from spandsp's '<x>x<y>' in pixels per metre; None for '0x0' or junk."""
    found = re.fullmatch(r'([0-9]{1,6})x([0-9]{1,6})', str(value or '').strip())
    if not found or not int(found.group(1)):
        return None
    vertical = int(found.group(2))
    # 3850 standard, 7700 fine, 11811 (300 dpi), 15400 superfine (15748 at 400 dpi).
    if 3000 <= vertical < 5000:
        return 'standard'
    if 5000 <= vertical < 10000:
        return 'fine'
    if 10000 <= vertical <= 24000:
        return 'superfine'
    return None


def _pages(value):
    try:
        pages = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return pages if pages > 0 else 0


def builtin_values(rate, resolution, pages):
    """The built-in engine's values for one call: the last page's speed and resolution, kept only when a page
    was confirmed (before any page spandsp reports its starting speed and no image)."""
    if not _pages(pages):
        return {}
    values = {'rate_last_page': _rate(rate), 'resolution_last_page': spandsp_resolution(resolution)}
    return {name: value for name, value in values.items() if value is not None}


def engine_values(encoded):
    """The SSL Fax engine's values for one call, from hylafax/bin/negotiation (base64 JSON); {} when absent."""
    if not isinstance(encoded, str) or not encoded or len(encoded) > 1024:
        return {}
    try:
        data = json.loads(base64.b64decode(encoded, validate=True).decode('ascii'))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    values = {'rate_first': _rate(data.get('rate_first')), 'rate_lowest': _rate(data.get('rate_lowest')),
              'rate_last_page': _rate(data.get('rate_last')), 'trainings': _count(data.get('trainings'), 99),
              'compression': _choice(data.get('compression'), COMPRESSIONS),
              'resolution': _choice(data.get('resolution'), RESOLUTIONS), 'ecm': _choice(data.get('ecm'), ECM),
              'session_seconds': _count(data.get('session'), 86400)}
    # A lowest speed above the first one, or a speed at the end below the lowest, is not a real call.
    if values['rate_first'] and values['rate_lowest'] and values['rate_lowest'] > values['rate_first']:
        values['rate_first'] = values['rate_lowest'] = None
    if values['rate_lowest'] and values['rate_last_page'] and values['rate_last_page'] < values['rate_lowest']:
        values['rate_last_page'] = None
    if not values['trainings']:
        values['trainings'] = None
    return {name: value for name, value in values.items() if value is not None}


# Reading ---------------------------------------------------------------------------

def measured(row):
    """The negotiation values one fax_engine_calls row holds (None for not reported)."""
    row = row or {}
    return {name: row.get(name) for name in COLUMNS}


def _coding(compression, ecm):
    if compression == 'mixed':
        compression = 'more than one compression'
    if compression and ecm:
        return {'on': f'{compression} with error correction', 'off': f'{compression} without error correction',
                'mixed': f'{compression} with error correction on some pages'}[ecm]
    if compression:
        return f'{compression} (error correction {NOT_REPORTED})'
    if ecm:
        return {'on': 'error correction', 'off': 'no error correction',
                'mixed': 'error correction on some pages'}[ecm] + f' (compression {NOT_REPORTED})'
    return None


def _resolution_text(value):
    return 'more than one resolution' if value == 'mixed' else f'{value} resolution'


def _pages_text(pages, transfer, connected):
    if pages is None:
        return None
    if not pages:
        if connected:
            return f'no pages confirmed in a {connected} s call'
        return 'no pages confirmed'
    noun = 'page' if pages == 1 else 'pages'
    if transfer is not None:
        return f'{pages} {noun} in {transfer} s'
    if connected:
        return f'{pages} {noun} in a {connected} s call'
    return f'{pages} {noun}'


def call_sentence(row, call=None):
    """One sentence for a fax's detail: what the call negotiated, and 'not reported by this engine' where the
    engine reported nothing. ``row`` is the fax_engine_calls row, ``call`` its sip_call_records row."""
    values, call = measured(row), call or {}
    pages = call.get('pages')
    tail = _pages_text(pages, (row or {}).get('transfer_seconds'), call.get('connected_seconds'))
    coding = _coding(values['compression'], values['ecm'])
    if (row or {}).get('sslfax') == 1:
        sentence = 'The pages went over the internet instead of the phone line'
        if coding:
            sentence += f', using {coding}'
        if values['resolution']:
            sentence += f', {_resolution_text(values["resolution"])}'
    elif values['rate_first'] or values['rate_lowest']:
        first, lowest = values['rate_first'] or values['rate_lowest'], values['rate_lowest'] or values['rate_first']
        speed = (f'at {first} bit/s' if first == lowest
                 else f'starting at {first} bit/s and dropping to {lowest} bit/s')
        sentence = f'The call used {coding} {speed}' if coding else (
            f'The call ran {speed} (compression and error correction {NOT_REPORTED})')
        if values['resolution']:
            sentence += f', {_resolution_text(values["resolution"])}'
    elif values['rate_last_page'] or values['resolution_last_page']:
        parts = []
        if values['rate_last_page']:
            parts.append(f'went at {values["rate_last_page"]} bit/s')
        if values['resolution_last_page']:
            parts.append(f'had {values["resolution_last_page"]} resolution')
        sentence = 'The last page ' + ' and '.join(parts)
        if coding:
            sentence += f', using {coding}'
        else:
            sentence += f'; compression and error correction are {NOT_REPORTED}'
    elif values['trainings']:
        tries = 'once' if values['trainings'] == 1 else f'{values["trainings"]} times'
        sentence = f'The call tried {tries} to agree a speed with the other fax machine and never did'
    elif coding:
        sentence = f'The call used {coding}; its speed is {NOT_REPORTED}'
    else:
        sentence = f'Speed, compression and error correction were {NOT_REPORTED}'
    return f'{sentence}; {tail}.' if tail else f'{sentence}.'


def call_view(row, call=None):
    """The detail screens' block: which engine reported, each value (None: not reported) and the sentence."""
    if row is None:
        return None
    from .hylafax_records import handled_by
    engine, _ = handled_by(row)
    return {'engine': row.get('negotiation_by') or engine, **{name: row.get(name) for name in COLUMNS[1:]},
            'sslfax': None if row.get('sslfax') is None else bool(row.get('sslfax')),
            'transfer_seconds': row.get('transfer_seconds'), 'session_seconds': row.get('session_seconds'),
            'pages': (call or {}).get('pages'), 'call_seconds': (call or {}).get('connected_seconds'),
            'sentence': call_sentence(row, call)}


def received_view(engine, inbound_fax_id):
    """The negotiation block for one received fax (by its ID), or None when no trunk call carried it."""
    from .routing.database import reflect
    tables = reflect(engine, ('fax_engine_calls', 'sip_call_records'))
    calls, records = tables['fax_engine_calls'], tables['sip_call_records']
    with engine.connect() as connection:
        call = connection.execute(sa.select(records).where(
            records.c.direction == 'inbound', records.c.job_id == inbound_fax_id)
            .order_by(records.c.started_at.desc(), records.c.id.desc()).limit(1)).mappings().first()
        if call is None:
            return None
        row = connection.execute(sa.select(calls).where(
            calls.c.direction == 'inbound', calls.c.call_key == call['call_id'])).mappings().first()
    return call_view(dict(row), dict(call)) if row is not None else None


# Summary ---------------------------------------------------------------------------

def speed_of(row):
    """(scope, bit/s) a call is grouped by: the lowest speed of the whole call, the last page's speed when the
    engine reports only that, the internet (SSL Fax), or (None, None) when not reported."""
    if row.get('sslfax') == 1:
        return 'internet', None
    if row.get('rate_lowest'):
        return 'call', row['rate_lowest']
    if row.get('rate_last_page'):
        return 'last_page', row['rate_last_page']
    return None, None


def speed_label(scope, rate):
    if scope == 'internet':
        return 'over the internet'
    if scope == 'call':
        return f'{rate} bit/s'
    if scope == 'last_page':
        return f'{rate} bit/s on the last page'
    return NOT_REPORTED


def coding_label(compression, ecm):
    if not compression and not ecm:
        return NOT_REPORTED
    return _coding(compression, ecm).replace('more than one compression', 'mixed compression')


def _order(group):
    scope, rate = group['speed_scope'], group['speed'] or 0
    return ({'internet': 0, 'call': 1, 'last_page': 2}.get(scope, 3), -rate, group['compression'] or '~',
            group['ecm'] or '~')


def summary(engine, *, days=30, now=None):
    """Per compression, error correction and speed over the last ``days`` days: calls, success rate, seconds per
    confirmed page and (sent faxes) attempts per delivered fax.

    Counted: trunk fax calls that were answered and have a result. Seconds per confirmed page divides the
    connected time of every call in the group (including calls that confirmed no pages) by the pages confirmed,
    over calls whose connected time is known. Attempts per delivered fax: for each sent fax a call in the group
    delivered, how many calls that fax took in all.
    """
    from .routing.database import reflect
    if days not in DAYS:
        raise ValueError('Choose 7, 30 or 90 days.')
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    since = now - timedelta(days=days)
    tables = reflect(engine, ('fax_engine_calls', 'sip_call_records'))
    calls, records = tables['fax_engine_calls'], tables['sip_call_records']
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(sa.select(
            calls.c.direction, calls.c.job_id, calls.c.sslfax, calls.c.negotiation_by, calls.c.rate_lowest,
            calls.c.rate_last_page, calls.c.compression, calls.c.ecm, records.c.fax_status, records.c.pages,
            records.c.connected_seconds,
        ).join(records, sa.and_(records.c.direction == calls.c.direction, records.c.call_id == calls.c.call_key))
            .where(calls.c.created_at >= since, records.c.answered_at.is_not(None),
                   records.c.fax_status.is_not(None))).mappings()]
        delivered_jobs = {row['job_id'] for row in rows
                          if row['direction'] == 'outbound' and row['fax_status'] == 'SUCCESS' and row['job_id']}
        attempts = {}
        if delivered_jobs:
            for job_id, count in connection.execute(sa.select(calls.c.job_id, sa.func.count()).where(
                    calls.c.direction == 'outbound', calls.c.job_id.in_(sorted(delivered_jobs)))
                    .group_by(calls.c.job_id)):
                attempts[job_id] = count
    groups = {}
    for row in rows:
        scope, rate = speed_of(row)
        key = (row['compression'], row['ecm'], scope, rate)
        group = groups.setdefault(key, {
            'compression': row['compression'], 'ecm': row['ecm'], 'speed_scope': scope, 'speed': rate,
            'calls': 0, 'sent': 0, 'received': 0, 'delivered': 0, 'pages': 0, '_seconds': 0, '_timed_pages': 0,
            '_timed': 0, '_delivered_attempts': 0, '_delivered_sent': 0})
        group['calls'] += 1
        group['sent' if row['direction'] == 'outbound' else 'received'] += 1
        pages = row['pages'] or 0
        group['pages'] += pages
        if row['fax_status'] == 'SUCCESS':
            group['delivered'] += 1
            if row['direction'] == 'outbound' and row['job_id'] in attempts:
                group['_delivered_sent'] += 1
                group['_delivered_attempts'] += attempts[row['job_id']]
        if row['connected_seconds'] is not None:
            group['_timed'] += 1
            group['_seconds'] += row['connected_seconds']
            group['_timed_pages'] += pages
    result = []
    for group in sorted(groups.values(), key=_order):
        timed_pages, delivered_sent = group.pop('_timed_pages'), group.pop('_delivered_sent')
        seconds, attempts_total = group.pop('_seconds'), group.pop('_delivered_attempts')
        group.pop('_timed')
        group['success_percent'] = round(100 * group['delivered'] / group['calls'])
        group['seconds_per_page'] = round(seconds / timed_pages, 1) if timed_pages else None
        group['attempts_per_delivered'] = round(attempts_total / delivered_sent, 2) if delivered_sent else None
        group['coding_label'] = coding_label(group['compression'], group['ecm'])
        group['speed_label'] = speed_label(group['speed_scope'], group['speed'])
        result.append(group)
    measured_calls = sum(1 for row in rows if row['negotiation_by'] or row['sslfax'] == 1)
    return {'days': days, 'calls': len(rows), 'measured_calls': measured_calls, 'groups': result,
            'sentence': summary_sentence(len(rows), measured_calls, days), 'note': MEASURE_ONLY}


def summary_sentence(calls, measured_calls, days):
    if not calls:
        return f'No answered fax calls on your phone line in the last {days} days.'
    noun = 'call' if measured_calls == 1 else 'calls'
    sentence = f'Measured on {measured_calls} {noun} in the last {days} days'
    rest = calls - measured_calls
    if rest:
        sentence += f"; the engine reported nothing for {rest} more {'call' if rest == 1 else 'calls'}"
    return sentence + '.'
