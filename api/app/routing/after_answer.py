"""Digits after answer (research N7): the keys Faxbot presses once a recipient's phone menu answers.

Some fax machines sit behind a phone menu ("press 2 for the fax"). Without a
way to answer the menu, such a call ends as "no fax answered". A recipient
number can carry a dial suffix: after the call is answered, and while it is
still an ordinary voice call, Faxbot sends these keys as telephone events
(RFC 4733, which Faxbot's calls already offer), and only then starts the fax.

- **Keys.** ``0``-``9``, ``*`` and ``#``, with ``w`` for a half-second pause and
  ``W`` for a one-second pause, at most ``MAX_KEYS`` characters: what Asterisk's
  ``SendDTMF`` accepts (docs.asterisk.org, Dialplan Applications, SendDTMF, read
  2026-10-10). A comma, the usual pause on a phone, is read as ``W``.
- **Built-in engine.** The Originate carries ``FAXBOT_DTMF``; ``[faxbot-send]``
  runs ``SendDTMF`` before ``SendFAX``, so the keys go out before any fax tone.
- **SSL Fax engine.** The call plan's eighth field carries them, and
  ``[faxbot-engine-out]`` dials with ``D(keys)``: Asterisk sends them "after the
  called party has answered, but before the call gets bridged" (docs.asterisk.org,
  Dial, read 2026-10-10), so the engine's own fax tones start after them.
- **Only a trunk can press keys.** A cloud fax service, or a partner's relay,
  dials the number itself and cannot answer the menu, so dispatch skips every
  route but a trunk for such a number (``dispatch_refusal``, ``SKIP``); a fax
  with no trunk route waits in Sent with one sentence. Delivery straight to a
  partner or inside Faxbot places no call and is unaffected.
- **The station check and the wait for a fax answer.** The fax machine behind
  a menu usually shows its own number, not the switchboard's, so on such a
  call the dialled number is not an expected station (only stations learned or
  confirmed for the number are), and the 50-second cap on waiting for a fax
  answer (``stations.T0_CAP_MS``), which counts from the answer, is left off:
  the time in the menu would use it up. spandsp's own T0 (60 s from the start
  of the fax) still applies.
- **Billing.** The carrier bills from the answer, so the seconds spent in the
  menu are part of the call. Sent details say which keys each call pressed
  (``after_answer_calls``, kept per attempt, so a later change to the number's
  setting never rewrites what an earlier call did).

A setting that cannot be read never lets a call go without its keys: the
lookup raises ``AfterAnswerUnavailable`` before anything is dialled.
"""
from __future__ import annotations

from datetime import datetime
import logging
import re
import uuid

import sqlalchemy as sa


MAX_KEYS = 32
KEYS = re.compile(r'[0-9*#wW]{1,32}')
SKIP = 'digits'
ENGINES = ('builtin', 'sslfax')
# Only a trunk account (provider ``sip``) places the call itself and can press keys after answer.
PRESSING_PROVIDERS = ('sip',)
# Delivery inside Faxbot and straight to a partner place no call.
NO_CALL_ROUTES = ('local', 'direct')
CLEARED = ('', 'none', 'off', 'clear')
log = logging.getLogger(__name__)


class AfterAnswerUnavailable(RuntimeError):
    """The keys to press after answer could not be read; nothing is dialled without them."""


# Keys ----------------------------------------------------------------------------------------------------------------

def clean(text):
    """The keys to press, as Faxbot sends them, or None to press none. Spaces, dashes and dots are dropped, and a
    comma becomes a one-second pause. Raises ValueError with one sentence for anything else."""
    if text is None:
        return None
    value = str(text).strip()
    if value.lower() in CLEARED:
        return None
    value = re.sub(r'[\s.\-()]', '', value).replace(',', 'W')
    if not KEYS.fullmatch(value):
        raise ValueError(f'Use the keys 0 to 9, * and #, with w for a short pause or W for a one-second pause, '
                         f'at most {MAX_KEYS} in all.')
    if not re.search(r'[0-9*#]', value):
        raise ValueError('Add at least one key to press, not only pauses.')
    return value


def spoken(keys):
    """The keys as people read them: '2', '2, pause, 105'. A run of pauses reads as one pause."""
    if not keys:
        return ''
    parts = [part for part in re.split(r'[wW]+', keys) if part]
    text = ', pause, '.join(parts)
    if keys[0] in 'wW':
        text = 'pause, ' + text
    return text


# Records -------------------------------------------------------------------------------------------------------------

def _settings():
    return sa.table('recipient_after_answer', sa.column('id'), sa.column('phone_number'), sa.column('digits'),
                    sa.column('actor_principal_id'), sa.column('actor_name'), sa.column('created_at', sa.DateTime()))


def _calls():
    return sa.table('after_answer_calls', sa.column('id'), sa.column('job_id'), sa.column('phone_number'),
                    sa.column('digits'), sa.column('engine'), sa.column('created_at', sa.DateTime()))


def current_on(connection, number):
    """The newest row for ``number`` (a dict), or None when nobody ever set keys for it."""
    table = _settings()
    row = connection.execute(sa.select(table).where(table.c.phone_number == number)
                             .order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().first()
    return dict(row) if row is not None else None


def set_on(connection, number, keys, *, actor_principal_id=None, actor_name=None, now):
    """Record a change: ``keys`` (already ``clean``) or None to press none. Earlier rows are kept, never changed."""
    if keys is not None and not KEYS.fullmatch(keys):
        raise ValueError('Unsupported keys')
    connection.execute(_settings().insert().values(
        id=uuid.uuid4().hex, phone_number=str(number)[:32], digits=keys, actor_principal_id=actor_principal_id,
        actor_name=(actor_name or None) and actor_name[:200], created_at=now))


def for_number(engine, number):
    """The keys to press after ``number`` answers, or None. Raises AfterAnswerUnavailable when they cannot be read
    (logged); an installation whose database has no such table yet has none to press."""
    if engine is None or not number:
        return None
    try:
        with engine.connect() as connection:
            if not sa.inspect(connection).has_table('recipient_after_answer'):
                return None
            row = current_on(connection, number)
    except sa.exc.SQLAlchemyError as error:
        log.warning('The keys to press after %s answers could not be read (%s); the call is not placed.', number,
                    type(error).__name__)
        raise AfterAnswerUnavailable('The keys to press after this number answers could not be read.') from None
    keys = (row or {}).get('digits')
    return keys if keys and KEYS.fullmatch(keys) else None


def call_guard(values, job_id, number, *, engine, mailbox_id=None):
    """The station check for a call that presses keys after answer (``stations.CallGuard``): no cap on waiting for
    a fax answer, and only stations learned or confirmed for the number are expected, never the dialled number."""
    from . import stations
    try:
        now = datetime.utcnow()
        with engine.connect() as connection:
            expected = [row['station'] for row in stations.stations_on(connection, number, now)]
            mode, _ = stations.mode_on(connection, number, mailbox_id)
    except sa.exc.SQLAlchemyError as error:
        log.warning('The station check for fax %s could not be read (%s); the call goes without it.', job_id,
                    type(error).__name__)
        return stations.CallGuard()
    expect = '.'.join(item for item in dict.fromkeys(expected) if len(item) >= stations.MIN_DIGITS)[:200]
    return stations.CallGuard(None, expect or None, bool(expect) and mode == 'refuse')


# Calls ---------------------------------------------------------------------------------------------------------------

_ATTEMPT = re.compile(r'[A-Za-z0-9_-]{1,40}')


def record_submission(engine, event, *, now=None) -> bool:
    """Keep which keys one placed call pressed (the Submission event's ``Digits``), once; True when new.

    A poll request or anything that is not a sent fax is not kept."""
    keys = event.get('Digits') if isinstance(event, dict) else None
    attempt_id, job_id = str(event.get('AttemptID') or ''), str(event.get('JobID') or '')
    if not keys or not KEYS.fullmatch(str(keys)) or not _ATTEMPT.fullmatch(attempt_id) \
            or not _ATTEMPT.fullmatch(job_id):
        return False
    number = re.sub(r'[^0-9+]', '', str(event.get('Called') or ''))[:32]
    engine_name = 'sslfax' if event.get('Engine') == 'sslfax' else 'builtin'
    jobs = sa.table('fax_jobs', sa.column('id'))
    table = _calls()
    with engine.begin() as connection:
        if connection.execute(sa.select(jobs.c.id).where(jobs.c.id == job_id)).first() is None:
            return False
        if connection.execute(sa.select(table.c.id).where(table.c.id == attempt_id)).first() is not None:
            return False
        connection.execute(table.insert().values(id=attempt_id, job_id=job_id, phone_number=number or '-',
                                                 digits=str(keys), engine=engine_name,
                                                 created_at=now or datetime.utcnow()))
    return True


# Routes --------------------------------------------------------------------------------------------------------------

def dispatch_refusal(engine, values, destination, route_key):
    """``SKIP`` when ``route_key`` cannot press the keys ``destination`` needs after answer, else None.

    Only a trunk account places the call itself. Delivery inside Faxbot, straight to a partner, or by a digital
    route places no call and is never refused; a partner's relay dials the number itself and is. Raises
    AfterAnswerUnavailable when the setting cannot be read, so no route dials the number without its keys."""
    from ..rules.model import is_digital
    if route_key in NO_CALL_ROUTES or is_digital(route_key):
        return None
    keys = for_number(engine, destination)
    if keys is None:
        return None
    from ..accounts import account_named
    try:
        account = account_named(values, route_key)
    except Exception:
        account = None
    provider = account.provider if account is not None else route_key
    return None if provider in PRESSING_PROVIDERS else SKIP


# Views ---------------------------------------------------------------------------------------------------------------

BILLED = 'The carrier bills from the moment the call is answered, so the seconds in the phone menu are part of the call.'


def recipient_view(engine, number):
    with engine.connect() as connection:
        row = current_on(connection, number)
    keys = (row or {}).get('digits')
    changed = (row or {}).get('created_at')
    return {'number': number, 'digits': keys, 'spoken': spoken(keys) or None,
            'changed_at': changed.isoformat(timespec='seconds') + 'Z' if isinstance(changed, datetime) else None,
            'changed_by': (row or {}).get('actor_name'),
            'sentence': (f'After this number answers, Faxbot presses {spoken(keys)} and then starts the fax. '
                         + BILLED) if keys else
            'Faxbot starts the fax as soon as this number answers.'}


def change_sentence(keys):
    if keys:
        return (f'Faxbot will press {spoken(keys)} after this number answers, only on calls over your trunk. '
                + BILLED)
    return 'Faxbot no longer presses any keys after this number answers.'


def fax_sentences(engine, job_id):
    """What Sent details say about the keys a fax's calls pressed, one sentence per call."""
    table = _calls()
    with engine.connect() as connection:
        if not sa.inspect(connection).has_table('after_answer_calls'):
            return []
        rows = connection.execute(sa.select(table).where(table.c.job_id == job_id)
                                  .order_by(table.c.created_at, table.c.id)).mappings().all()
    return [f'After the call was answered, Faxbot pressed {spoken(row["digits"])} to reach the fax machine. '
            + BILLED for row in rows]
