"""What Faxbot learns per number from its own fax calls, and what it changes because of it (T8, T9).

Learning uses only calls Faxbot was already making; it never places a call to
find something out. Every value comes from records the engines already keep:

- ``sip_call_records``: each trunk call, both engines, both directions (whether
  it ended on fax over IP or audio, and how it ended);
- ``fax_engine_calls``: the engine that handled it and what it negotiated
  (``fax_negotiation``: the SSL Fax engine reports the whole call);
- ``fax_call_frames``: the built-in engine's T.30 frames (Asterisk patch 0004:
  the far machine's DIS, each DCS, trainings, when T.38 started and who asked).

``joined_calls`` joins the three into one view per call. From those views:

- **Per-number memory (T8).** A call on which the far fax machine answered
  over T.38 and the fax did not finish teaches "T.38 failed" for that number
  and direction; the same over audio teaches "audio failed". A failure that
  already moved the whole trunk (no T.38 data came back, ``sip_fax_mode``) or
  the whole SSL Fax engine (no fax machine heard) to audio teaches nothing
  about the number: the network or the engine is the cause. With only T.38
  failing, new calls to (or from) the number use audio fax; with only audio
  failing, Faxbot asks for T.38 at the answer; with both, it keeps its usual
  settings. A memory lasts ``MEMORY_DAYS``, ends when a later call in the same
  mode goes through, and a person can tell Faxbot to forget a number.
- **Learning epochs.** Everything learned belongs to the trunk configuration
  and fax engine builds it was learned on (``fax_learning_epochs``). When either
  changes, Faxbot starts learning again, and changing back does not bring the
  old evidence back. This is stricter than GOfax.IP, whose entries never expire.
- **The decision rule (T9).** Per number, from the joined views: the starting
  speed (``engine_frames.learn_rate``: lower after training failures, one speed
  lower again after failing at the learned speed, never below 4,800 bit/s),
  compression on the SSL Fax engine (one step more robust after repeated
  failures with one compression; every compression Faxbot uses is lossless, so
  pages look the same), and error correction, which Faxbot only ever turns on
  (when pages failed without it and the far machine supports it) and never
  off. Resolution is never changed. Nothing changes before a number has
  ``MIN_CALLS`` answered calls, and a person's own limits for a number win.

``decide`` returns the changes for one new call with one sentence each and
only reads; ``learn_recent`` (the background work, on the settings in force)
keeps epochs and memory. Each sent call's changes are written once
(``fax_call_choices``) when it is placed, so a fax's details say what its call
used and why.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import uuid
import weakref

import sqlalchemy as sa

from . import engine_frames
from .engine_frames import same_number

LEARN_DAYS = engine_frames.LEARN_DAYS
MEMORY_DAYS = 30
MIN_CALLS = 3
MARK_LAG = timedelta(seconds=10)
COMPRESSION_FAILS = 2
ECM_FAILS = 2
# Most compact first; every one is lossless, so a later one sends the same pages in a little more time.
COMPRESSIONS = ('JBIG', 'MMR', 'MR', 'MH')
SETTING_COMPRESSION = {'jbig': 'JBIG', 'mmr': 'MMR', 'mr': 'MR', 'mh': 'MH'}
KINDS = ('t38_failed', 'audio_failed')
# Received calls: what failed is kept and shown, but Faxbot does not answer a caller with audio fax because of it
# (faxbot-inmode stays empty). Measured on two Asterisks (2026-10-07): a caller that stays silent about ten seconds
# before asking for T.38, as Asterisk's SendFAX z and Faxbot's own built-in engine do, misses the three DIS of an
# audio-only answer and fails. Turn on only with live evidence from the callers it would apply to.
INBOUND_AUDIO = False
# The trunk settings that decide the path a call takes (carrier, account, transport, network, T.38); a change
# to any starts learning again. Faxbot's own fax preferences (speed, error correction, compression) are not
# here: the rules read them for each call, and a learned value only ever narrows them.
CONFIG_FIELDS = ('sip_trunk_preset', 'sip_trunk_auth', 'sip_trunk_host', 'sip_trunk_port', 'sip_trunk_transport',
                 'sip_trunk_username', 'sip_trunk_outbound_proxy', 'sip_trunk_codecs', 'sip_trunk_dial_format',
                 'sip_trunk_dial_prefix', 'sip_external_address', 'sip_router_ports', 'sip_t38_enabled',
                 'sip_t38_error_correction', 'sip_t38_max_datagram')
ENGINE_PARTS = ('asterisk', 'hylafax')
_VERSION = re.compile(r'[A-Za-z0-9 .+_:-]{1,120}')
_TABLES = ('fax_learning_epochs', 'fax_destination_memory', 'fax_call_choices', 'sip_call_records',
           'fax_engine_calls', 'fax_call_frames')
_ID = re.compile(r'[A-Za-z0-9_-]{1,40}')

FORGET_NOTHING = 'Faxbot has nothing to forget for this number.'


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _database():
    try:
        from . import db
        return db.engine
    except Exception:
        return None


_REFLECTED = weakref.WeakKeyDictionary()


def _tables(db):
    """The tables this module reads, reflected once per database (a decision runs on every call placed)."""
    try:
        return _REFLECTED[db]
    except (KeyError, TypeError):
        pass
    metadata = sa.MetaData()
    metadata.reflect(db, only=list(_TABLES))
    tables = {name: metadata.tables[name] for name in _TABLES}
    try:
        _REFLECTED[db] = tables
    except TypeError:
        pass
    return tables


def _digits(number):
    return re.sub(r'[^0-9]', '', str(number or ''))


def _day(moment):
    """2 October, in the installation's own time zone."""
    from .people_time import _local, installation_zone_name
    local = _local(moment, installation_zone_name())
    return f'{local.day} {local:%B}'


# -- epochs: what counts as the same trunk and the same engines -------------------------------

def config_key(values) -> str:
    data = {name: getattr(values, name, None) for name in CONFIG_FIELDS}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _clean(text):
    text = ' '.join(str(text or '').split())[:120]
    return text if _VERSION.fullmatch(text) else None


def engine_versions(values) -> dict:
    """Each fax engine's build as its container wrote it at start: the Asterisk image writes its version and
    the digest of Faxbot's patches (``engine-version``), the SSL Fax engine its own in its status. None
    when unknown (not started yet, or not set up)."""
    found = dict.fromkeys(ENGINE_PARTS)
    try:
        lines = (Path(values.fax_data_dir) / 'asterisk' / 'engine-version').read_text(encoding='utf-8').splitlines()
        found['asterisk'] = _clean(lines[0]) if lines else None
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        from .hylafax_engine import status_path
        record = json.loads(status_path(values).read_text(encoding='utf-8'))
        found['hylafax'] = _clean(record.get('version')) if isinstance(record, dict) else None
    except Exception:
        pass
    return found


def engine_key(versions) -> str:
    return ';'.join(f'{name}={versions.get(name) or ""}' for name in ENGINE_PARTS)


def _parse_engine_key(text):
    parts = dict(part.split('=', 1) for part in str(text or '').split(';') if '=' in part)
    return {name: parts.get(name) or None for name in ENGINE_PARTS}


def current_epoch(db, values, *, now=None, write=True) -> dict:
    """The epoch new evidence belongs to: {'id', 'started_at', 'first', 'trunk'}. A changed trunk
    configuration or engine build starts a new one (written when ``write``). An engine build Faxbot cannot
    read right now keeps the last one it knew, so a container still starting never resets learning. Before
    any epoch exists, the first covers earlier calls too (AK's frames already keep their trunk)."""
    now = now or utcnow()
    epochs = _tables(db)['fax_learning_epochs']
    key, versions = config_key(values), engine_versions(values)
    with db.connect() as connection:
        rows = connection.execute(sa.select(epochs).order_by(epochs.c.started_at.desc(), epochs.c.id.desc())
                                  .limit(2)).mappings().all()
    newest = dict(rows[0]) if rows else None
    if newest is not None:
        previous = _parse_engine_key(newest['engine_key'])
        merged = {name: versions[name] or previous[name] for name in ENGINE_PARTS}
        if newest['config_key'] == key and engine_key(merged) == newest['engine_key']:
            return {**newest, 'first': len(rows) == 1}
    else:
        merged = versions
    row = {'id': uuid.uuid4().hex, 'config_key': key, 'engine_key': engine_key(merged),
           'trunk': engine_frames.trunk_key(values) or None, 'started_at': now}
    if write:
        with db.begin() as connection:
            connection.execute(epochs.insert().values(**row))
    return {**row, 'first': newest is None}


def evidence_since(epoch, now, days):
    """The oldest call that still counts: inside the window and inside the epoch."""
    window = now - timedelta(days=days)
    return window if epoch.get('first') else max(window, epoch['started_at'])


# -- one view per call ------------------------------------------------------------------------

def _mode(record, frame):
    t38 = (record or {}).get('t38')
    if t38 == 'yes':
        return 't38'
    if t38 == 'no':
        return 'audio'
    mode = (frame or {}).get('mode')
    return 't38' if mode == 'T38' else 'audio' if mode == 'audio' else None


def _coded(first, last, name):
    values = {item[name] for item in (first, last) if item}
    if not values:
        return None
    return values.pop() if len(values) == 1 else 'mixed'


def merge(record=None, engine_row=None, frame=None, choice=None) -> dict:
    """One call from its call record, its engine record and its frames (any may be missing)."""
    from .hylafax_records import handled_by
    from .sip_calls import stored_verdict
    record, engine_row, frame = record or None, engine_row or None, frame or None
    direction = (record['direction'] if record else 'outbound' if frame['direction'] == 'out' else 'inbound')
    engine = handled_by(engine_row)[0] if engine_row else ('builtin' if frame else None)
    first, last = (engine_frames.decode_dcs(frame.get('dcs_first')), engine_frames.decode_dcs(frame.get('dcs_last'))) \
        if frame else (None, None)
    negotiated = engine_row or {}
    trainings = negotiated.get('trainings')
    ftt = frame.get('ftt') if frame else None
    if ftt is None and trainings:
        ftt = trainings - 1
    status = (record or {}).get('fax_status') or (frame or {}).get('status')
    far = engine_frames.decode_dis(frame.get('dis')) if frame else None
    if record:
        number = record['called'] if direction == 'outbound' else record['caller']
    else:
        number = frame.get('number')
    ecm = negotiated.get('ecm')
    if ecm is None and (first or last):
        coded = _coded(first, last, 'ecm')
        ecm = 'mixed' if coded == 'mixed' else 'on' if coded else 'off'
    return {
        'key': (record or {}).get('id') or frame['id'], 'record_id': (record or {}).get('id'),
        'when': record['started_at'] if record else frame['created_at'],
        'ended': (record or {}).get('ended_at'), 'direction': direction, 'number': number,
        'job_id': (record or {}).get('job_id') or (frame or {}).get('job_id'),
        'attempt_id': (record or {}).get('attempt_id') or (frame or {}).get('attempt_id'),
        'engine': engine, 'mode': _mode(record, frame), 'status': status,
        'answered': bool(record['answered_at']) if record else frame is not None,
        'verdict': stored_verdict(record) if record else None,
        'pages': (record or {}).get('pages'), 'seconds': (record or {}).get('connected_seconds'),
        'rate_first': negotiated.get('rate_first') or (frame or {}).get('rate_first'),
        'rate_lowest': negotiated.get('rate_lowest') or (frame or {}).get('rate_lowest'),
        'trainings': trainings if trainings is not None else (frame or {}).get('trainings'), 'ftt': ftt,
        'compression': negotiated.get('compression') or _coded(first, last, 'compression'),
        'ecm': ecm, 'resolution': negotiated.get('resolution') or _coded(first, last, 'resolution'),
        'far_ecm': far['ecm'] if far else None, 'far_max_rate': far['max_rate'] if far else None,
        't38_after_ms': (frame or {}).get('t38_after_ms'), 't38_by': (frame or {}).get('t38_by'),
        't38_now': (frame or {}).get('t38_now') or 0, 'iaf': (frame or {}).get('iaf'),
        'sslfax': negotiated.get('sslfax') == 1,
        'record': record, 'engine_row': engine_row, 'frame': frame, 'choice': choice,
    }


def joined_calls(db, number, *, direction=None, since=None, limit=20) -> list:
    """The latest calls with a number, newest first, each one view of its call record, engine record and
    frames (``merge``). Calls with frames but no call record are kept."""
    digits = _digits(number)
    if len(digits) < 3:
        return []
    tail = '%' + digits[-7:]
    tables = _tables(db)
    records, engines, frames, choices = (tables['sip_call_records'], tables['fax_engine_calls'],
                                         tables['fax_call_frames'], tables['fax_call_choices'])
    sent = sa.and_(records.c.direction == 'outbound', records.c.called.like(tail))
    received = sa.and_(records.c.direction == 'inbound', records.c.caller.like(tail))
    where = sent if direction == 'outbound' else received if direction == 'inbound' else sa.or_(sent, received)
    query = sa.select(records).where(where)
    frame_query = sa.select(frames).where(frames.c.number.like(tail))
    if direction:
        frame_query = frame_query.where(frames.c.direction == ('out' if direction == 'outbound' else 'in'))
    if since is not None:
        query = query.where(records.c.started_at >= since)
        frame_query = frame_query.where(frames.c.created_at >= since)
    with db.connect() as connection:
        found = [dict(row) for row in connection.execute(
            query.order_by(records.c.started_at.desc(), records.c.id.desc()).limit(limit * 3)).mappings()]
        found = [row for row in found if same_number(row['called' if row['direction'] == 'outbound' else 'caller'],
                                                     number)][:limit]
        keys = [row['call_id'] for row in found]
        engine_rows = {}
        if keys:
            for row in connection.execute(sa.select(engines).where(engines.c.call_key.in_(keys))).mappings():
                engine_rows[(row['direction'], row['call_key'])] = dict(row)
        frame_ids = [('out:' if row['direction'] == 'outbound' else 'in:') + row['call_id'] for row in found]
        frame_rows = {}
        if frame_ids:
            for row in connection.execute(sa.select(frames).where(frames.c.id.in_(frame_ids))).mappings():
                frame_rows[row['id']] = dict(row)
        loose = [dict(row) for row in connection.execute(
            frame_query.order_by(frames.c.created_at.desc()).limit(limit * 3)).mappings()
            if row['id'] not in frame_rows and same_number(row['number'], number)]
        attempts = [row['attempt_id'] for row in found if row['attempt_id']]
        attempts += [row['attempt_id'] for row in loose if row.get('attempt_id')]
        choice_rows = {}
        if attempts:
            for row in connection.execute(sa.select(choices).where(choices.c.attempt_id.in_(attempts))).mappings():
                choice_rows[row['attempt_id']] = dict(row)
    views = [merge(row, engine_rows.get((row['direction'], row['call_id'])),
                   frame_rows.get(('out:' if row['direction'] == 'outbound' else 'in:') + row['call_id']),
                   choice_rows.get(row['attempt_id'])) for row in found]
    views += [merge(None, None, row, choice_rows.get(row.get('attempt_id'))) for row in loose]
    views.sort(key=lambda view: view['when'], reverse=True)
    return views[:limit]


def on_trunk(views, values) -> list:
    """The views of calls made on this trunk: frames name the trunk they used (carrier preset and server),
    which matters in the first epoch, the one that also covers calls from before epochs were kept."""
    trunk = engine_frames.trunk_key(values)
    return [view for view in views if not (view['frame'] and view['frame'].get('trunk')
                                           and view['frame']['trunk'] != trunk)]


def as_frames(view) -> dict:
    """A joined view in the shape ``engine_frames.learn`` reads."""
    return {'direction': 'out' if view['direction'] == 'outbound' else 'in',
            'mode': {'t38': 'T38', 'audio': 'audio'}.get(view['mode']), 'status': view['status'],
            'rate_first': view['rate_first'], 'rate_lowest': view['rate_lowest'], 'ftt': view['ftt'],
            't38_after_ms': view['t38_after_ms'], 't38_by': view['t38_by'], 't38_now': view['t38_now'],
            'created_at': view['when'], 'engine': view['engine'] or 'builtin'}


# -- per-number memory (T8) ----------------------------------------------------------------------

def memory_kind(view) -> str | None:
    """'t38_failed' or 'audio_failed' when the far fax machine answered and the fax did not finish; None
    for everything else, including calls that already moved the whole trunk or engine to audio fax (no
    data back, no fax machine heard), Internet Aware Fax and pages sent over the internet."""
    if not view['record_id'] or not view['answered'] or view['status'] != 'FAILED':
        return None
    if view['verdict'] != 'remote_fax_failed' or view['iaf'] or view['sslfax']:
        return None
    return {'t38': 't38_failed', 'audio': 'audio_failed'}.get(view['mode'])


def learn_memories(db, values, number, *, epoch, now=None, views=None) -> int:
    """Keep what this number's recent calls taught (one row per call, once); how many rows were added."""
    now = now or utcnow()
    since = evidence_since(epoch, now, MEMORY_DAYS)
    views = views if views is not None else joined_calls(db, number, since=since, limit=50)
    memory = _tables(db)['fax_destination_memory']
    added = 0
    for view in views:
        kind = memory_kind(view)
        if kind is None or view['when'] < since:
            continue
        learned = view['ended'] or view['when']
        row = {'id': uuid.uuid4().hex, 'number': str(view['number'] or number)[:32], 'direction': view['direction'],
               'kind': kind, 'engine': view['engine'], 'evidence': view['record_id'], 'epoch_id': epoch['id'],
               'learned_at': learned, 'expires_at': learned + timedelta(days=MEMORY_DAYS), 'forgotten_at': None,
               'forgotten_by': None, 'forgotten_by_name': None, 'created_at': now}
        try:
            with db.begin() as connection:
                if connection.execute(sa.select(memory.c.id).where(memory.c.evidence == row['evidence'],
                                                                   memory.c.kind == kind)).first():
                    continue
                connection.execute(memory.insert().values(**row))
            added += 1
        except sa.exc.IntegrityError:
            continue
    return added


def memories(db, number, *, epoch, now=None, views=None, direction=None) -> list:
    """This number's memory rows in the current epoch, newest first, each with ``active`` and why not."""
    now = now or utcnow()
    memory = _tables(db)['fax_destination_memory']
    digits = _digits(number)
    if len(digits) < 3:
        return []
    query = sa.select(memory).where(memory.c.number.like('%' + digits[-7:]), memory.c.epoch_id == epoch['id'])
    if direction:
        query = query.where(memory.c.direction == direction)
    with db.connect() as connection:
        rows = [dict(row) for row in connection.execute(query.order_by(memory.c.learned_at.desc())).mappings()
                if same_number(row['number'], number)]
    views = views or []
    for row in rows:
        mode = 't38' if row['kind'] == 't38_failed' else 'audio'
        later = any(view['direction'] == row['direction'] and view['mode'] == mode and view['status'] == 'SUCCESS'
                    and view['when'] > row['learned_at'] for view in views)
        row['ended'] = ('forgotten' if row['forgotten_at'] else 'expired' if row['expires_at'] <= now
                        else 'went_through' if later else None)
        row['active'] = row['ended'] is None
    return rows


def effect(rows, direction) -> str | None:
    """'audio' (only T.38 failed), 't38' (only audio failed) or None (nothing, or both failed)."""
    kinds = {row['kind'] for row in rows if row['active'] and row['direction'] == direction}
    if kinds == {'t38_failed'}:
        return 'audio'
    if kinds == {'audio_failed'}:
        return 't38'
    return None


def _failed_text(rows, kind):
    chosen = sorted((row for row in rows if row['active'] and row['kind'] == kind), key=lambda row: row['learned_at'])
    if not chosen:
        return '', None
    until = max(row['expires_at'] for row in chosen)
    if len(chosen) == 1:
        return f'on {_day(chosen[0]["learned_at"])}', until
    return f'on {len(chosen)} calls since {_day(chosen[0]["learned_at"])}', until


def memory_sentence(rows, direction) -> str | None:
    """One sentence for what this number's memory does in one direction, or None when there is none."""
    active = [row for row in rows if row['active'] and row['direction'] == direction]
    if not active:
        return None
    what = effect(rows, direction)
    if what is None:
        to = 'to' if direction == 'outbound' else 'from'
        return (f'Both fax over IP (T.38) and audio fax {to} this number failed recently, so Faxbot uses its usual '
                'settings for it.')
    if what == 'audio':
        when, until = _failed_text(rows, 't38_failed')
        if direction == 'outbound':
            return (f'Fax over IP (T.38) to this number failed {when}, so Faxbot uses audio fax for it until '
                    f'{_day(until)}.')
        if not INBOUND_AUDIO:
            return f'Fax over IP (T.38) from this number failed {when}; Faxbot still answers its calls the usual way.'
        return (f'Fax over IP (T.38) from this number failed {when}, so Faxbot answers its calls with audio fax until '
                f'{_day(until)}.')
    when, until = _failed_text(rows, 'audio_failed')
    if direction == 'outbound':
        return (f'Audio fax to this number failed {when}, so Faxbot asks for fax over IP (T.38) as soon as it '
                f'answers, until {_day(until)}.')
    return f'Audio fax from this number failed {when}; Faxbot already asks its calls for fax over IP (T.38) first.'


def forget(db, number, *, actor_id=None, actor_name=None, now=None) -> int:
    """A person tells Faxbot to forget what failed with this number (both directions); how many rows."""
    now = now or utcnow()
    memory = _tables(db)['fax_destination_memory']
    digits = _digits(number)
    with db.begin() as connection:
        ids = [row['id'] for row in connection.execute(sa.select(memory).where(
            memory.c.number.like('%' + digits[-7:]), memory.c.forgotten_at.is_(None))).mappings()
            if same_number(row['number'], number)]
        if ids:
            connection.execute(memory.update().where(memory.c.id.in_(ids), memory.c.forgotten_at.is_(None)).values(
                forgotten_at=now, forgotten_by=actor_id, forgotten_by_name=(actor_name or None) and str(actor_name)[:200]))
    return len(ids)


def inbound_audio_callers(db, values, *, now=None) -> set:
    """Numbers whose calls to Faxbot only failed over T.38 recently: Faxbot answers them with audio fax."""
    now = now or utcnow()
    epoch = current_epoch(db, values, now=now)
    memory = _tables(db)['fax_destination_memory']
    with db.connect() as connection:
        numbers = set(connection.execute(sa.select(memory.c.number).where(
            memory.c.direction == 'inbound', memory.c.epoch_id == epoch['id'], memory.c.forgotten_at.is_(None),
            memory.c.expires_at > now).distinct()).scalars())
    found = set()
    for number in numbers:
        views = joined_calls(db, number, direction='inbound', since=evidence_since(epoch, now, MEMORY_DAYS), limit=50)
        if effect(memories(db, number, epoch=epoch, now=now, views=views, direction='inbound'), 'inbound') == 'audio':
            found.add(number)
    return found


def learn_recent(db, values, *, now=None) -> int:
    """Background: keep what recent failed calls taught (received calls too); how many rows were added.

    Only call records changed since the epoch's ``learned_through`` are read (a record changes when its result
    arrives), and the mark moves to the newest one read, in the database, so a restart reads nothing again. A new
    epoch has no mark: it learns from its own start, never from before it. The mark stays ``MARK_LAG`` behind the
    clock, so a record written in the last seconds (a write still committing) is read again next time rather than
    missed; keeping what it taught is idempotent.
    """
    now = now or utcnow()
    epoch = current_epoch(db, values, now=now)
    tables = _tables(db)
    records, epochs = tables['sip_call_records'], tables['fax_learning_epochs']
    query = sa.select(records.c.direction, records.c.called, records.c.caller, records.c.updated_at).where(
        records.c.started_at >= evidence_since(epoch, now, MEMORY_DAYS), records.c.fax_status == 'FAILED',
        records.c.answered_at.is_not(None))
    if epoch.get('learned_through') is not None:
        query = query.where(records.c.updated_at > epoch['learned_through'])
    with db.connect() as connection:
        rows = connection.execute(query).all()
    numbers = {row.called if row.direction == 'outbound' else row.caller for row in rows}
    added = sum(learn_memories(db, values, number, epoch=epoch, now=now) for number in sorted(n for n in numbers if n))
    newest = max((row.updated_at for row in rows), default=None)
    newest = min(newest, now - MARK_LAG) if newest is not None else None
    if newest is not None and (epoch.get('learned_through') is None or newest > epoch['learned_through']):
        with db.begin() as connection:
            connection.execute(epochs.update().where(epochs.c.id == epoch['id']).values(learned_through=newest))
    return added


# -- the decision rule (T9) ----------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    """What Faxbot changes for one new call to a number, with one sentence per change."""
    audio: bool = False
    t38_now: bool = False
    max_rate: int | None = None
    compression: str | None = None  # the setting's value (jbig, mmr, mr, mh), SSL Fax engine only
    ecm_on: bool = False
    iaf: str | None = None
    reasons: tuple = ()
    engine: str = 'builtin'
    epoch_id: str | None = None
    notes: tuple = field(default=(), compare=False)

    def changed(self) -> bool:
        return bool(self.audio or self.t38_now or self.max_rate or self.compression or self.ecm_on or self.iaf)

    def payload(self) -> dict:
        """What the call record keeps (``record_choice``)."""
        return {'engine': self.engine, 'audio': self.audio, 't38_now': self.t38_now, 'max_rate': self.max_rate,
                'compression': self.compression, 'ecm_on': self.ecm_on, 'iaf': self.iaf,
                'reasons': list(self.reasons), 'epoch_id': self.epoch_id}


def compression_rule(views, configured):
    """(setting, reason): one compression more robust than the one that failed, after ``COMPRESSION_FAILS``
    failures in a row with it on the SSL Fax engine after the far machine answered; None when nothing should
    change. A later call that went through with a more compact compression ends it."""
    usable = [view for view in views if view['engine'] == 'hylafax' and view['answered']
              and view['compression'] in COMPRESSIONS and view['status'] in ('SUCCESS', 'FAILED')]
    if len(usable) < MIN_CALLS:
        return None, ''
    target, reason, streak = None, '', {}
    for view in sorted(usable, key=lambda item: item['when']):
        used = view['compression']
        if view['status'] == 'SUCCESS':
            streak[used] = 0
            if target and COMPRESSIONS.index(used) < COMPRESSIONS.index(target):
                target, reason = None, ''
            continue
        if view['verdict'] != 'remote_fax_failed':
            continue
        streak[used] = streak.get(used, 0) + 1
        if streak[used] >= COMPRESSION_FAILS and used != COMPRESSIONS[-1]:
            robust = COMPRESSIONS[COMPRESSIONS.index(used) + 1]
            if target is None or COMPRESSIONS.index(robust) > COMPRESSIONS.index(target):
                target = robust
                reason = (f'Faxes to this number with {used} compression failed {streak[used]} times in a row after '
                          f'its fax machine answered, so Faxbot uses {robust} compression for it; the pages are '
                          'the same, they take a little longer to send.')
            streak[used] = 0
    base = SETTING_COMPRESSION.get(str(configured or '').lower())
    if target is None or base is None or COMPRESSIONS.index(target) <= COMPRESSIONS.index(base):
        return None, ''
    return target.lower(), reason


def ecm_rule(views):
    """The reason to turn error correction on for a number, or ''. Only on, never off: pages failed without
    it at least ``ECM_FAILS`` times after the far machine answered, its fax machine said it supports it, and
    no call without it has gone through since."""
    answered = [view for view in views if view['answered'] and view['status'] in ('SUCCESS', 'FAILED')]
    if len(answered) < MIN_CALLS:
        return ''
    supports = [view['far_ecm'] for view in answered if view['far_ecm'] is not None]
    if not supports or not supports[0]:
        return ''
    failed = [view for view in answered if view['ecm'] == 'off' and view['verdict'] == 'remote_fax_failed']
    if len(failed) < ECM_FAILS:
        return ''
    newest = max(view['when'] for view in failed)
    if any(view['ecm'] == 'off' and view['status'] == 'SUCCESS' and view['when'] > newest for view in answered):
        return ''
    return (f'Pages to this number failed {len(failed)} times without error correction and its fax machine supports '
            'it, so Faxbot uses error correction for it.')


def _allowed(rate):
    return engine_frames._allowed_rate(rate) if rate else None


def decide(values, number, *, engine='builtin', t38=True, base_rate=None, rate_for=None, base_ecm=True,
           base_compression=None, recipient=None, db=None, now=None) -> Decision:
    """The changes for one new call to ``number`` on ``engine`` ('builtin' or 'hylafax'), each with a
    sentence. ``t38``: whether this call would try T.38 without them; ``rate_for(t38)`` the starting speed it
    would use (a person's limit for the number included); ``base_ecm`` and ``base_compression`` its error
    correction and compression. A person's own limits for the number (``recipient``) always win.

    Reads only. A fax keeps the settings it was accepted with, so a call placed after a trunk change may
    carry older ones: a decision must never start an epoch or keep memory from them. The background work
    (``learn_recent``, on the settings in force) keeps both; a decision on other settings than the newest
    epoch's simply finds nothing learned yet. Never raises: anything unreadable changes nothing."""
    db = db if db is not None else _database()
    if db is None or not number:
        return Decision(engine=engine)
    try:
        return _decide(values, number, engine=engine, t38=t38, base_rate=base_rate, rate_for=rate_for,
                       base_ecm=base_ecm, base_compression=base_compression, recipient=recipient or {}, db=db,
                       now=now or utcnow())
    except Exception:
        logging.getLogger(__name__).warning('Faxbot could not read what it learned about a fax number; this call '
                                            'uses the usual settings.')
        return Decision(engine=engine)


def _decide(values, number, *, engine, t38, base_rate, rate_for, base_ecm, base_compression, recipient, db, now):
    epoch = current_epoch(db, values, now=now, write=False)
    views = joined_calls(db, number, since=evidence_since(epoch, now, max(LEARN_DAYS, MEMORY_DAYS)), limit=50)
    rows = memories(db, number, epoch=epoch, now=now, views=views, direction='outbound')
    sent = [view for view in on_trunk(views, values) if view['direction'] == 'outbound'
            and view['when'] >= evidence_since(epoch, now, LEARN_DAYS)]
    reasons, notes = [], []
    what = effect(rows, 'outbound')
    audio = bool(what == 'audio' and t38)
    if audio:
        reasons.append(memory_sentence(rows, 'outbound'))
        t38 = False
    learned = engine_frames.learn([as_frames(view) for view in sent], values)
    t38_now = False
    if engine == 'builtin' and t38 and getattr(values, 'sip_t38_enabled', True):
        if what == 't38':
            t38_now = True
            reasons.append(memory_sentence(rows, 'outbound'))
        elif learned.t38_now:
            t38_now = True
            reasons.append(learned.t38_reason)
    if what is None and any(row['active'] for row in rows):
        notes.append(memory_sentence(rows, 'outbound'))  # both failed: the usual settings, said once
    base = rate_for(t38) if rate_for is not None else base_rate
    max_rate = None
    if learned.max_rate and (base is None or learned.max_rate < base):
        max_rate = learned.max_rate
        reasons.append(learned.rate_reason)
    if learned.rate_note:
        notes.append(learned.rate_note)
    compression = None
    if engine == 'hylafax':
        compression, reason = compression_rule(sent, base_compression)
        if compression:
            reasons.append(reason)
    ecm_on = False
    if not base_ecm and recipient.get('ecm') is None:
        reason = ecm_rule(sent)
        if reason:
            ecm_on = True
            reasons.append(reason)
    iaf = None
    if engine == 'builtin' and not audio:
        iaf = engine_frames.iaf_for(engine_frames.FrameStore(db), db, number)
        if iaf:
            reasons.append('Sent as Internet Aware Fax: this number is ' + (
                'another Faxbot you approved for it.' if iaf == 'peer' else 'a fax server you approved for it.'))
    answered = sum(1 for view in sent if view['answered'])
    if answered < MIN_CALLS:
        notes.append(sample_sentence(answered))
    return Decision(audio=audio, t38_now=t38_now, max_rate=max_rate, compression=compression, ecm_on=ecm_on,
                    iaf=iaf, reasons=tuple(reasons), engine=engine, epoch_id=epoch['id'], notes=tuple(notes))


def sample_sentence(answered):
    calls = 'call' if answered == 1 else 'calls'
    return (f'Faxbot changes compression or error correction for a number only after at least {MIN_CALLS} answered '
            f'calls to it; this number has had {answered} {calls} since Faxbot started learning.')


# -- what each sent call used ------------------------------------------------------------------

def record_choice(db, event, *, now=None) -> bool:
    """Keep what one placed call used (the Submission event's ``Learned``), once; True when new."""
    learned = event.get('Learned') if isinstance(event, dict) else None
    attempt_id, job_id = str(event.get('AttemptID') or ''), str(event.get('JobID') or '')
    if not isinstance(learned, dict) or not _ID.fullmatch(attempt_id) or not learned.get('reasons'):
        return False
    choices = _tables(db)['fax_call_choices']
    number = re.sub(r'[^0-9+]', '', str(event.get('Called') or ''))[:32] or None
    row = {'id': uuid.uuid4().hex, 'attempt_id': attempt_id, 'job_id': job_id if _ID.fullmatch(job_id) else None,
           'number': number,
           'engine': 'hylafax' if learned.get('engine') == 'hylafax' else 'builtin',
           'audio': int(bool(learned.get('audio'))), 't38_now': int(bool(learned.get('t38_now'))),
           'max_rate': learned.get('max_rate') if learned.get('max_rate') in engine_frames.STEP_RATES else None,
           'compression': learned.get('compression') if learned.get('compression') in SETTING_COMPRESSION else None,
           'ecm_on': int(bool(learned.get('ecm_on'))),
           'iaf': learned.get('iaf') if learned.get('iaf') in engine_frames.IAF_KINDS else None,
           'reasons': '\n'.join(' '.join(str(text).split())[:400] for text in learned['reasons'][:8]),
           'epoch_id': str(learned.get('epoch_id') or '')[:40] or None, 'created_at': now or utcnow()}
    try:
        with db.begin() as connection:
            if connection.execute(sa.select(choices.c.attempt_id).where(choices.c.attempt_id == attempt_id)).first():
                return False
            connection.execute(choices.insert().values(**row))
    except sa.exc.IntegrityError:
        return False
    return True


def choice_sentences(row) -> list:
    return [line for line in str((row or {}).get('reasons') or '').split('\n') if line]


def choice_for_attempt(db, attempt_id) -> dict | None:
    choices = _tables(db)['fax_call_choices']
    with db.connect() as connection:
        row = connection.execute(sa.select(choices).where(choices.c.attempt_id == str(attempt_id))).mappings().first()
    return dict(row) if row else None
