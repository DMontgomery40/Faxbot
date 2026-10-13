"""Line windows between partners (brief 82, research CE4): a receiving Faxbot with limited lines grants senders
short, signed windows to start a call, so separate senders queue instead of retrying into a busy line.

Both sides opt in per partner (``admission_partners.take_part``). Only a fax that goes **by phone** to a verified
partner waits for a window: a fax delivered directly places no call. The sending side asks in the background
(``Requester``) and the claim only reads what is stored (``hold_on``), so no network call ever runs inside a claim.

- **A grant is only permission to start a call.** It is not a receipt, a delivery guarantee or permission to retry
  an uncertain call: a fax whose last call has an unknown result is never claimed here (it is not ``ready``), and a
  fax gets a new window only after a definite outcome such as a confirmed busy line.
- Urgent faxes and faxes that would miss their send-by time follow the receiver's policy (``go`` starts the call
  without waiting, ``wait`` keeps the window); the policy travels in the grant, so both sides see the same one.
- A partner that does not take part, answers 404 (an older Faxbot) or does not answer within ``ASK_LIMIT`` leaves
  the fax as it was before this feature: it goes without a window, and Sent says so.
- The receiver keeps ``reserve`` lines for callers that don't take part, and counts that traffic (``legacy``).

The retry-amplification picture (``advice``): attempts offered = arrivals / (1 - p), where p is the measured share
of calls to the partner that met a busy line; with nothing measured, Faxbot says so rather than assuming a value.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import logging
import uuid

import sqlalchemy as sa

from ..routing.database import read_connection, utcnow, write_transaction

log = logging.getLogger(__name__)

DURATIONS = {'short': timedelta(minutes=3), 'medium': timedelta(minutes=10), 'long': timedelta(minutes=30)}
DEFAULTS = {'lines': None, 'reserve': 1, 'window_minutes': 10, 'urgent': 'go', 'deadline': 'go'}
# How long a fax waits for its partner's first answer before it goes as it would have without this feature.
ASK_LIMIT = timedelta(minutes=10)
# The claim looks at a waiting fax again at least this often.
RECHECK = timedelta(seconds=30)
# A grant whose earliest start would miss the send-by time by less than this counts as a deadline exception.
DEADLINE_MARGIN = timedelta(minutes=5)
FRESHNESS = timedelta(minutes=10)


class AdmissionError(ValueError):
    """A setting Faxbot refuses, in one sentence."""


def _table(bind, name):
    return sa.Table(name, sa.MetaData(), autoload_with=bind)


def duration_class(pages):
    """The window a fax of ``pages`` pages asks for: short (3 minutes), medium (10) or long (30)."""
    pages = max(1, int(pages or 1))
    return 'short' if pages <= 3 else 'medium' if pages <= 15 else 'long'


def job_ref(identity_key, job_id):
    """An opaque reference for one fax that the partner can't trace back to it."""
    return hashlib.sha256(f'{identity_key}:{job_id}'.encode('ascii')).hexdigest()[:32]


# Settings ---------------------------------------------------------------------------------------------------------

def settings_on(connection):
    table = sa.table('admission_settings', sa.column('lines'), sa.column('reserve'), sa.column('window_minutes'),
                     sa.column('urgent'), sa.column('deadline'), sa.column('created_at'), sa.column('id'))
    row = connection.execute(sa.select(table).order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)
                             ).mappings().first()
    return {key: row[key] for key in DEFAULTS} if row is not None else dict(DEFAULTS)


def settings(engine):
    with read_connection(engine) as connection:
        return settings_on(connection)


def save_settings(engine, *, lines=None, reserve=1, window_minutes=10, urgent='go', deadline='go', actor=None):
    if lines is not None and not 0 <= int(lines) <= 500:
        raise AdmissionError('Lines for partners are a number from 0 to 500, or empty to use the trunk.')
    if not 0 <= int(reserve) <= 500:
        raise AdmissionError('Lines kept for other callers are a number from 0 to 500.')
    if not 1 <= int(window_minutes) <= 240:
        raise AdmissionError('A window stays open from 1 to 240 minutes.')
    if urgent not in ('go', 'wait') or deadline not in ('go', 'wait'):
        raise AdmissionError('Choose go or wait.')
    with write_transaction(engine) as connection:
        connection.execute(_table(connection, 'admission_settings').insert().values(
            id=uuid.uuid4().hex, lines=lines, reserve=int(reserve), window_minutes=int(window_minutes),
            urgent=urgent, deadline=deadline, recorded_by_name=(actor or {}).get('name'), created_at=utcnow()))
    return settings(engine)


def partner_on(connection, peer_id):
    """{'take_part', 'refused_classes', 'refusal_reason', 'version'} for one partner; off until you turn it on."""
    table = sa.table('admission_partners', sa.column('peer_id'), sa.column('take_part'),
                     sa.column('refused_classes'), sa.column('refusal_reason'), sa.column('created_at'),
                     sa.column('id'))
    rows = connection.execute(sa.select(table).where(table.c.peer_id == peer_id)
                              .order_by(table.c.created_at, table.c.id)).mappings().all()
    if not rows:
        return {'take_part': False, 'refused_classes': [], 'refusal_reason': None, 'version': 0}
    row = rows[-1]
    return {'take_part': bool(row['take_part']), 'refused_classes': json.loads(row['refused_classes'] or '[]'),
            'refusal_reason': row['refusal_reason'], 'version': len(rows)}


def partner(engine, peer_id):
    with read_connection(engine) as connection:
        return partner_on(connection, peer_id)


def save_partner(engine, peer_id, *, take_part, refused_classes=(), refusal_reason=None, actor=None):
    classes = sorted({str(item).strip().casefold() for item in refused_classes or () if str(item).strip()})
    if any(len(item) > 64 for item in classes) or len(classes) > 50:
        raise AdmissionError('Refuse at most 50 document classes, each up to 64 characters.')
    reason = ' '.join(str(refusal_reason or '').split())[:300] or None
    if classes and not reason:
        raise AdmissionError('Give the reason your partner sees when you refuse a document class.')
    with write_transaction(engine) as connection:
        connection.execute(_table(connection, 'admission_partners').insert().values(
            id=uuid.uuid4().hex, peer_id=peer_id, take_part=1 if take_part else 0,
            refused_classes=json.dumps(classes) if classes else None, refusal_reason=reason,
            recorded_by_name=(actor or {}).get('name'), created_at=utcnow()))
    return partner(engine, peer_id)


def partner_lines(values, chosen):
    """The lines this installation offers partners: the setting, or the trunk's lines less the reserve."""
    if chosen['lines'] is not None:
        return int(chosen['lines'])
    from ..capacity import trunk_calls_at_once, trunk_in_use
    total = trunk_calls_at_once(values) if trunk_in_use(values) else 1
    return max(0, int(total) - int(chosen['reserve']))


# The receiving side: issuing a window ------------------------------------------------------------------------------

def issue(engine, values, peer, request, now=None):
    """The window for one sender's request: a dict for the signed answer, and the row kept. Idempotent: asking again
    for the same fax returns the same open window."""
    now = now or utcnow()
    grants = _table(engine, 'admission_grants')
    chosen = settings(engine)
    taking = partner(engine, peer['id'])['take_part']
    duration = request['duration_class']
    with write_transaction(engine) as connection:
        existing = connection.execute(sa.select(grants).where(
            grants.c.direction == 'issued', grants.c.peer_id == peer['id'], grants.c.job_ref == request['job_ref'],
            grants.c.outcome == 'granted', grants.c.latest > now)
            .order_by(grants.c.created_at.desc()).limit(1)).mappings().first()
        if existing is not None:
            return _answer(existing)
        lines = partner_lines(values, chosen)
        if not taking or lines <= 0:
            outcome, earliest, latest = 'not_taking_part', None, None
        else:
            busy = connection.execute(sa.select(grants.c.earliest, grants.c.duration_class).where(
                grants.c.direction == 'issued', grants.c.outcome == 'granted',
                grants.c.latest > now - DURATIONS['long'])).all()
            earliest = _first_slot(now, DURATIONS[duration], busy, lines)
            latest = earliest + timedelta(minutes=int(chosen['window_minutes']))
            outcome = 'granted'
        row = {'id': uuid.uuid4().hex, 'direction': 'issued', 'peer_id': peer['id'], 'job_ref': request['job_ref'],
               'job_id': None, 'outcome': outcome, 'earliest': earliest, 'latest': latest,
               'duration_class': duration,
               'policy': json.dumps({'urgent': chosen['urgent'], 'deadline': chosen['deadline']}),
               'statement': None, 'signature': None, 'created_at': now}
        connection.execute(grants.insert().values(**row))
    return _answer(row)


def _first_slot(now, duration, busy, lines):
    """The first minute from ``now`` at which fewer than ``lines`` granted calls would overlap a call of
    ``duration``."""
    now = now.replace(microsecond=0)
    start = now.replace(second=0)
    if start < now:
        start += timedelta(minutes=1)
    calls = [(earliest, earliest + DURATIONS[kind]) for earliest, kind in busy if earliest is not None]
    # Now, then each whole minute, and the end of each granted call (so a window starts as soon as a line frees).
    candidates = sorted({now, *(start + timedelta(minutes=minute) for minute in range(24 * 60)),
                         *(last for _, last in calls if last > now)})
    for begin in candidates:
        end = begin + duration
        if sum(1 for first, last in calls if first < end and begin < last) < lines:
            return begin
    return start + timedelta(hours=24)


def _answer(row):
    policy = json.loads(row['policy'] or '{}') if isinstance(row['policy'], str) else (row['policy'] or {})
    return {'type': 'grant', 'grant_id': row['id'], 'job_ref': row['job_ref'], 'outcome': row['outcome'],
            'earliest': _iso(row['earliest']), 'latest': _iso(row['latest']),
            'duration_class': row['duration_class'], 'policy': policy}


def _iso(moment):
    return None if moment is None else moment.replace(microsecond=0).isoformat() + 'Z'


def _parse(moment):
    if moment is None:
        return None
    from .crypto import parse_timestamp
    return parse_timestamp(moment)


# The sending side: what the claim reads ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Wait:
    until: datetime
    sentence: str


_TIMES = ('created_at', 'send_by', 'earliest', 'latest', 'expires_at')


def _light(name, *columns):
    """A table by name with its columns typed, so SQLite returns times as times (no reflection on the claim path)."""
    return sa.table(name, *(sa.column(column, sa.DateTime()) if column in _TIMES else sa.column(column)
                            for column in columns))


_JOBS = _light('fax_jobs', 'id', 'to_number', 'pages', 'urgent', 'send_by', 'created_at')
_GRANTS = _light('admission_grants', 'id', 'direction', 'peer_id', 'job_ref', 'job_id', 'outcome', 'earliest', 'latest',
                 'duration_class', 'policy', 'statement', 'signature', 'created_at')
_ANSWERS = _light('preflight_answers', 'id', 'direction', 'peer_id', 'job_id', 'document_class', 'purpose', 'mailbox',
                  'accepts', 'reason', 'version', 'expires_at', 'statement', 'signature', 'created_at')
_TAKING = sa.table('admission_partners', sa.column('take_part'))


def anyone_takes_part_on(connection):
    """Whether any partner was ever set to take part: one cheap query, so the claim pays nothing when none is."""
    return connection.execute(sa.select(_TAKING.c.take_part).where(_TAKING.c.take_part == 1).limit(1)).first() \
        is not None


def _job_on(connection, job_id):
    row = connection.execute(sa.select(_JOBS).where(_JOBS.c.id == job_id)).mappings().first()
    return dict(row) if row is not None else None


def _peer_for_on(connection, number):
    peers = sa.table('direct_peers', sa.column('id'), sa.column('phone_number'), sa.column('state'),
                     sa.column('organization'), sa.column('signing_key'), sa.column('endpoint_url'))
    row = connection.execute(sa.select(peers).where(peers.c.phone_number == number, peers.c.state == 'verified')
                             ).mappings().first()
    return dict(row) if row is not None else None


def by_phone_on(connection, job, values):
    """Whether this fax goes to its partner by a phone call rather than directly: direct delivery is off here, or its
    route envelope does not allow direct delivery (a rule, the court profile or a registered sender). A fax that may
    go directly places no call and never waits for a window."""
    if not getattr(values, 'direct_delivery_enabled', False):
        return True
    from ..routing import envelope as envelopes
    try:
        pinned = envelopes.load_on(connection, job['id'])
    except envelopes.UnreadableDecision:
        return True
    return pinned is not None and not pinned.envelope.direct


def document_class_on(connection, job_id):
    """The document class and purpose a preflight names: the fax's workflow and labels, else 'general'."""
    from ..routing import envelope as envelopes
    try:
        pinned = envelopes.load_on(connection, job_id)
    except envelopes.UnreadableDecision:
        pinned = None
    facts = getattr(pinned, 'facts', None)
    labels = tuple(getattr(facts, 'labels', ()) or ())
    workflow = getattr(facts, 'workflow', None)
    # A case packet names its own class; its checklist's document classes are not sent (only the packet's kind).
    first = 'case packet' if getattr(facts, 'case_packet', False) else labels[0] if labels else workflow or 'general'
    document_class = first[:64].casefold()
    return document_class, (workflow or 'general')[:64]


def _newest(connection, name, job_id, **where):
    table = {'admission_grants': _GRANTS, 'preflight_answers': _ANSWERS}[name]
    query = sa.select(table).where(table.c.job_id == job_id, *[table.c[key] == value for key, value in where.items()])
    row = connection.execute(query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().first()
    return dict(row) if row is not None else None


def _attempted_since(connection, job_id, since):
    attempts = _light('outbound_attempts', 'job_id', 'created_at')
    return connection.execute(sa.select(sa.func.count()).select_from(attempts).where(
        attempts.c.job_id == job_id, attempts.c.created_at >= since)).scalar() > 0


def _clock(moment):
    from ..people_time import clock
    return clock(moment)


def hold_on(connection, job_id, values, now):
    """A ``Wait`` while this fax waits for its partner's preflight answer or line window, else None. Reads only."""
    if not anyone_takes_part_on(connection):
        return None
    job = _job_on(connection, job_id)
    if job is None:
        return None
    peer = _peer_for_on(connection, job.get('to_number'))
    if peer is None or not partner_on(connection, peer['id'])['take_part'] or not by_phone_on(connection, job, values):
        return None
    name = peer['organization']
    asked_for = now - (job.get('created_at') or now)
    answer = _newest(connection, 'preflight_answers', job_id, direction='asked')
    if answer is None:
        if asked_for < ASK_LIMIT:
            return Wait(now + RECHECK, f"Waiting: asking {name}'s Faxbot whether it accepts this fax before paying to "
                                       'send it.')
    elif answer['accepts'] == 0 and answer['expires_at'] is not None and answer['expires_at'] <= now:
        return Wait(now + RECHECK, f"Waiting: {name} refused this kind of fax earlier; that answer has expired, so "
                                   'Faxbot is asking again.')
    elif answer['accepts'] == 0:
        reason = (answer['reason'] or '').strip().rstrip('.')
        return Wait(now + RECHECK, f"Held: {name} doesn't accept {answer['document_class']} faxes at "
                                   f"{answer['mailbox'] or 'its fax number'}{': ' + reason if reason else ''}. "
                                   'Nothing was sent.')
    elif answer['accepts'] == 1 and answer['expires_at'] is not None and answer['expires_at'] <= now:
        return Wait(now + RECHECK, f"Waiting: {name}'s answer about this fax has expired, so Faxbot is asking again.")
    grant = _newest(connection, 'admission_grants', job_id, direction='received')
    if grant is None:
        if asked_for < ASK_LIMIT:
            return Wait(now + RECHECK, f"Waiting: asking {name} for a line window.")
        return None
    if grant['outcome'] != 'granted':
        return None
    policy = json.loads(grant['policy'] or '{}')
    if job.get('urgent') and policy.get('urgent') == 'go':
        return None
    if _attempted_since(connection, job_id, grant['created_at']):
        # The window was used by a call that ended without the fax (a confirmed busy line, say): a new one is asked.
        return Wait(now + RECHECK, f"Waiting: asking {name} for a new line window after the last call.")
    if now < grant['earliest']:
        send_by = job.get('send_by')
        if send_by is not None and grant['earliest'] > send_by - DEADLINE_MARGIN and policy.get('deadline') == 'go':
            return None
        return Wait(min(grant['earliest'], now + RECHECK),
                    f"Waiting for {name}'s line window until {_clock(grant['earliest'])}.")
    if grant['latest'] is not None and now > grant['latest']:
        return Wait(now + RECHECK, f"Waiting: {name}'s line window passed, so Faxbot is asking for a new one.")
    return None


def waiting_sentence(engine, job_id, values, now=None):
    with read_connection(engine) as connection:
        found = hold_on(connection, job_id, values, now or utcnow())
    return found.sentence if found is not None else None


def fax_view(engine, job_id, values, now=None):
    """What Sent details say about one fax to a partner you take part with: why it waits, what the partner's
    preflight said (a yes is never a receipt) and which window its call used; None when there is nothing to say."""
    now = now or utcnow()
    with read_connection(engine) as connection:
        answer = _newest(connection, 'preflight_answers', job_id, direction='asked')
        grant = _newest(connection, 'admission_grants', job_id, direction='received')
        waiting = hold_on(connection, job_id, values, now)
        job = _job_on(connection, job_id)
        peer = _peer_for_on(connection, (job or {}).get('to_number'))
        used = grant is not None and _attempted_since(connection, job_id, grant['created_at'])
    if answer is None and grant is None and waiting is None:
        return None
    name = peer['organization'] if peer is not None else 'Your partner'
    sentences = [waiting.sentence] if waiting is not None else []
    if answer is not None and answer['accepts'] == 1:
        where = answer['mailbox'] or 'its fax number'
        sentences.append(f"{name}'s Faxbot said it accepts {answer['document_class']} faxes at {where}. That is not a "
                         'receipt: it says nothing about whether this fax arrived.')
    elif answer is not None and answer['accepts'] is None:
        sentences.append(f"{name}'s Faxbot doesn't take part in preflight, so this fax went without asking.")
    if grant is not None and grant['outcome'] == 'granted' and used:
        sentences.append(f"Its call started in {name}'s line window from {_clock(grant['earliest'])} to "
                         f"{_clock(grant['latest'])}. A window is only permission to start a call.")
    elif grant is not None and grant['outcome'] != 'granted':
        sentences.append(f"{name} offered no line window, so the call went as it would have without one.")
    return {'sentences': sentences, 'waiting': waiting is not None}


# The sending side: asking in the background ------------------------------------------------------------------------

class Requester:
    """Asks partners, signed, for preflight answers and line windows for faxes waiting to go by phone."""

    def __init__(self, service, *, limit=20):
        self.service = service
        self.limit = limit

    def due(self, now=None):
        """Faxes ready to go by phone to a verified partner you take part with, that need an answer or a window."""
        now = now or utcnow()
        engine = self.service.store.engine
        values = self.service.values()
        with read_connection(engine) as connection:
            if not anyone_takes_part_on(connection):
                return []
        deliveries = sa.table('outbound_deliveries', sa.column('id'), sa.column('state'), sa.column('created_at'))
        found = []
        with read_connection(engine) as connection:
            ids = connection.execute(sa.select(deliveries.c.id).where(deliveries.c.state == 'ready')
                                     .order_by(deliveries.c.created_at).limit(200)).scalars().all()
            for job_id in ids:
                job = _job_on(connection, job_id)
                peer = _peer_for_on(connection, (job or {}).get('to_number'))
                if job is None or peer is None or not partner_on(connection, peer['id'])['take_part']:
                    continue
                if not by_phone_on(connection, job, values):
                    continue
                found.append((job, peer, self._needs(connection, job_id, now)))
                if len(found) >= self.limit:
                    break
        return [(job, peer, needs) for job, peer, needs in found if needs]

    @staticmethod
    def _needs(connection, job_id, now):
        needs = set()
        answer = _newest(connection, 'preflight_answers', job_id, direction='asked')
        expired = answer is not None and answer['expires_at'] is not None and answer['expires_at'] <= now
        if answer is None or (answer['accepts'] in (0, 1) and expired):
            needs.add('preflight')  # asked again once an answer, a refusal too, has expired
        elif answer['accepts'] == 0:
            return set()  # refused: held with the partner's reason until that answer expires
        grant = _newest(connection, 'admission_grants', job_id, direction='received')
        if grant is None or (grant['outcome'] == 'granted' and (
                (grant['latest'] is not None and grant['latest'] < now)
                or _attempted_since(connection, job_id, grant['created_at']))):
            needs.add('grant')
        return needs

    async def step(self, now=None):
        from ..config_runtime import run_lifecycle_step
        from . import preflight
        values = await run_lifecycle_step(self.service.values)
        if not getattr(values, 'direct_delivery_enabled', False) or not self.service.ready():
            return False
        for job, peer, needs in await run_lifecycle_step(lambda: self.due(now)):
            if 'preflight' in needs:
                accepted = await preflight.ask(self.service, job, peer)
                if accepted is False:
                    continue
            if 'grant' in needs:
                await self.request(job, peer)
        return False

    async def request(self, job, peer, now=None):
        """Ask ``peer`` for a window for ``job``; keeps what it answered, or that it did not take part."""
        from ..config_runtime import run_lifecycle_step
        from .crypto import DirectProtocolError, check_signed, signed, timestamp
        from .service import PartnerUnreachable
        import httpx
        identity = await run_lifecycle_step(lambda: self.service.identity(create=True))
        reference = job_ref(identity.signing_key, job['id'])
        duration = duration_class(job.get('pages'))
        envelope = signed(identity, {'type': 'grant_request', 'recipient': peer['signing_key'], 'job_ref': reference,
                                     'duration_class': duration, 'urgent': bool(job.get('urgent')),
                                     'asked_at': timestamp()})
        try:
            status, body = await self.service.http.request('POST', peer['endpoint_url'] + '/direct/admission/grants',
                                                           json=envelope)
        except (PartnerUnreachable, httpx.HTTPError):
            return None  # asked again at the next step; after ASK_LIMIT the fax goes without a window
        row = {'id': uuid.uuid4().hex, 'direction': 'received', 'peer_id': peer['id'], 'job_ref': reference,
               'job_id': job['id'], 'duration_class': duration, 'created_at': now or utcnow()}
        if status in (404, 405):
            row.update(outcome='not_taking_part', earliest=None, latest=None, policy=None, statement=None,
                       signature=None)
        elif status == 200 and isinstance(body, dict):
            try:
                answer = check_signed(body, peer['signing_key'])
                if (answer.get('type') != 'grant' or answer.get('recipient') != identity.signing_key
                        or answer.get('job_ref') != reference
                        or answer.get('outcome') not in ('granted', 'not_taking_part')):
                    raise DirectProtocolError('malformed', 'Not a window for this fax.')
                row.update(outcome=answer['outcome'], earliest=_parse(answer.get('earliest')),
                           latest=_parse(answer.get('latest')), policy=json.dumps(answer.get('policy') or {}),
                           statement=body['statement'], signature=body['signature'])
            except (DirectProtocolError, KeyError, TypeError, ValueError):
                log.warning("A partner's line window could not be checked; Faxbot asks again later.")
                return None
        else:
            return None

        def keep():
            with write_transaction(self.service.store.engine) as connection:
                connection.execute(_table(connection, 'admission_grants').insert().values(**row))
        await run_lifecycle_step(keep)
        return row


# What the Partners page shows ------------------------------------------------------------------------------------

def advice(engine, peer, now=None, *, days=30):
    """The retry-amplification picture for calls to this partner, from what was measured; one sentence."""
    now = now or utcnow()
    since = now - timedelta(days=days)
    jobs = sa.table('fax_jobs', sa.column('id'), sa.column('to_number'), sa.column('created_at'))
    # A call that ended before any fax data (a busy line or no answer), as the engine or the fax service said.
    attempts = sa.table('outbound_attempts', sa.column('job_id'), sa.column('ended_before_data'),
                        sa.column('created_at'))
    with read_connection(engine) as connection:
        faxes = connection.execute(sa.select(sa.func.count()).select_from(jobs).where(
            jobs.c.to_number == peer['phone_number'], jobs.c.created_at >= since)).scalar() or 0
        rows = connection.execute(sa.select(attempts.c.ended_before_data).select_from(
            attempts.join(jobs, jobs.c.id == attempts.c.job_id)).where(
            jobs.c.to_number == peer['phone_number'], attempts.c.created_at >= since)).scalars().all()
    calls = len(rows)
    busy = sum(1 for ended in rows if ended == 1)
    if faxes == 0 or calls == 0:
        return (f"Nothing measured yet: no calls to {peer['organization']} in the last {days} days, so Faxbot shows "
                'no retry picture.')
    share = busy / calls
    arrivals = faxes / days
    if share >= 1:
        return (f"All {calls} calls to {peer['organization']} in the last {days} days ended before any page, so the "
                'calls offered grow without bound until line windows replace the retries.')
    offered = arrivals / (1 - share)
    return (f"In the last {days} days, {faxes} faxes to {peer['organization']} ({arrivals:.1f} a day) met a busy line "
            f"or no answer on {busy} of {calls} calls ({share:.0%}), so its line is offered {offered:.1f} calls a day: "
            f"arrivals ÷ (1 − {share:.2f}). Line windows turn those retries into a queue.")


def legacy(engine, now=None, *, days=7):
    """Received calls in the last ``days`` from callers that don't take part, which the reserve lines carry."""
    now = now or utcnow()
    since = now - timedelta(days=days)
    inbound = sa.table('inbound_imports', sa.column('from_number'), sa.column('imported_at'))
    peers = sa.table('direct_peers', sa.column('id'), sa.column('phone_number'), sa.column('state'))
    with read_connection(engine) as connection:
        numbers = connection.execute(sa.select(inbound.c.from_number).where(inbound.c.imported_at >= since)
                                     ).scalars().all()
        taking = {row[1] for row in connection.execute(sa.select(peers.c.id, peers.c.phone_number).where(
            peers.c.state == 'verified')).all() if partner_on(connection, row[0])['take_part']}
    others = sum(1 for number in numbers if number not in taking)
    return {'calls': others, 'sentence': (f'{others} of the {len(numbers)} faxes received in the last {days} days came '
                                          'from callers that don\'t take part; your reserve lines carry them.')}


def partner_view(engine, values, peer, now=None):
    now = now or utcnow()
    with read_connection(engine) as connection:
        chosen = partner_on(connection, peer['id'])
        grants = _table(connection, 'admission_grants')
        answers = _table(connection, 'preflight_answers')
        granted = connection.execute(sa.select(grants).where(grants.c.peer_id == peer['id'])
                                     .order_by(grants.c.created_at.desc()).limit(20)).mappings().all()
        asked = connection.execute(sa.select(answers).where(answers.c.peer_id == peer['id'])
                                   .order_by(answers.c.created_at.desc()).limit(20)).mappings().all()
    return {
        'peer_id': peer['id'], 'organization': peer['organization'], **chosen,
        'grants': [{'direction': row['direction'], 'outcome': row['outcome'], 'earliest': row['earliest'],
                    'latest': row['latest'], 'duration_class': row['duration_class'], 'created_at': row['created_at'],
                    'job_id': row['job_id']} for row in granted],
        'preflights': [{'direction': row['direction'], 'document_class': row['document_class'],
                        'purpose': row['purpose'], 'mailbox': row['mailbox'], 'accepts': row['accepts'],
                        'reason': row['reason'], 'expires_at': row['expires_at'], 'created_at': row['created_at'],
                        'job_id': row['job_id']} for row in asked],
        'advice': advice(engine, peer, now),
    }


def view(engine, values, peers, now=None):
    chosen = settings(engine)
    lines = partner_lines(values, chosen)
    return {'settings': chosen, 'partner_lines': lines,
            'lines_sentence': (f'Partners that take part share {lines} line{"s" if lines != 1 else ""}; '
                               f'{chosen["reserve"]} stay{"s" if chosen["reserve"] == 1 else ""} for other callers.'),
            'policy_sentence': (f"Urgent faxes {'start without a window' if chosen['urgent'] == 'go' else 'wait for a window'}"
                                f"; a fax that would miss its send-by time "
                                f"{'starts without one' if chosen['deadline'] == 'go' else 'still waits'}."),
            'legacy': legacy(engine, now),
            'partners': [partner_view(engine, values, peer, now) for peer in peers if peer['state'] != 'revoked'],
            'not_a_receipt': 'A line window is only permission to start a call, never a receipt or a delivery.'}
