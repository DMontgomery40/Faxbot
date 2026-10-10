"""The station check (N5) and the cap on waiting for a fax answer: what each sent call checks before any page.

**Station check.** In phase B a called fax machine sends its station identifier
(CSI, usually its own number) before the sender commits to a page. The
built-in engine (asterisk patch 0007) compares it with the stations Faxbot
expects at the number: the number dialled, and the stations it showed on
earlier successful calls (``recipient_stations``: learned from calls for
``CALL_DAYS``, or confirmed by a person for ``PERSON_DAYS``). A CSI is a hint,
never proof, so it is used only in the negative: it contradicts them when it
carries a full number (``MIN_DIGITS`` digits or more) that is none of them. A
blank or short CSI is no signal. ``check`` here and ``faxbot_csi_check`` in
``asterisk/patches/faxbot_t38_gateway.h`` follow the same rules with the same
examples.

What happens then is a choice per recipient number, else per mailbox
(``station_check_settings``): ``warn`` (the default: the fax goes on, and Sent
details say the station differed) or ``refuse`` (the call ends before any page,
a definite failure with the reason "the number answered as …" that no other
route takes, and a Work item asks a person to confirm the number). The SSL Fax
engine has no such hook, so on its calls the check runs after the call: one
call late, which Sent details say.

**T0 cap.** spandsp waits T0 = 60 s for the first fax message after answer
(T.30 5.4.3.1: T0 is 60 +-5 s from the end of dialling; once answered, the
machines must identify each other within T1 = 35 +-5 s). On a trunk billed by
the minute in steps of 60 s or more, a person who stays on the line would cost
a second minute, so the call ends ``T0_CAP_MS`` after answer when no fax has
answered: 10 s above T1's longest value, before the first step ends.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import uuid

import sqlalchemy as sa


MIN_DIGITS = 7
DIGITS_MAX = 20
CALL_DAYS = 180
PERSON_DAYS = 365
T0_CAP_MS = 50000
# The answer cap's switch, one per trunk (accounts.FIELDS['sip'] gives each extra trunk its own): on by default.
CAP_SETTING = 'sip_fax_answer_cap'
# Without the cap, spandsp's own T0 (60 s) ends a call no fax machine answers just after a minute.
UNCAPPED_SECONDS = 61
MODES = ('warn', 'refuse')
NO_SIGNAL, MATCHES, DIFFERS = 'no_signal', 'matches', 'differs'
log = logging.getLogger(__name__)


def digits(text):
    """The digits of ``text``, at most ``DIGITS_MAX``."""
    return ''.join(character for character in str(text or '') if character.isdigit())[:DIGITS_MAX]


def same_number(a, b):
    """Whether digit strings ``a`` and ``b`` name the same number: after leading zeros (a national trunk prefix
    such as the UK's 0), the shorter, at least ``MIN_DIGITS`` long, is the end of the longer."""
    a, b = a.lstrip('0'), b.lstrip('0')
    shorter = min(len(a), len(b))
    return shorter >= MIN_DIGITS and a[-shorter:] == b[-shorter:]


def check(csi, expected):
    """``NO_SIGNAL``, ``MATCHES`` or ``DIFFERS`` for the far end's ``csi`` against the ``expected`` numbers."""
    far = digits(csi)
    known = [digits(item) for item in expected if len(digits(item)) >= MIN_DIGITS]
    if len(far) < MIN_DIGITS or not known:
        return NO_SIGNAL
    return MATCHES if any(same_number(far, item) for item in known) else DIFFERS


def shown(station):
    """A station's digits as people read them: '+1 720 555 0199' for NANP, '+44 20 7946 0000' when it parses."""
    import phonenumbers
    text = digits(station)
    try:
        parsed = phonenumbers.parse('+' + text, None)
    except phonenumbers.NumberParseException:
        return text
    if phonenumbers.is_possible_number(parsed):
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    return text


# Records -----------------------------------------------------------------------------------------------------------

def _stations():
    return sa.table('recipient_stations', sa.column('id'), sa.column('phone_number'), sa.column('station'),
                    sa.column('source'), sa.column('job_id'), sa.column('actor_principal_id'),
                    sa.column('actor_name'), sa.column('seen_at', sa.DateTime()), sa.column('expires_at', sa.DateTime()))


def _settings():
    return sa.table('station_check_settings', sa.column('id'), sa.column('scope'), sa.column('scope_key'),
                    sa.column('mode'), sa.column('actor_principal_id'), sa.column('actor_name'),
                    sa.column('created_at', sa.DateTime()))


def _results():
    return sa.table('station_check_results', sa.column('id'), sa.column('job_id'), sa.column('phone_number'),
                    sa.column('station'), sa.column('outcome'), sa.column('engine'),
                    sa.column('created_at', sa.DateTime()))


def stations_on(connection, number, now):
    """The unexpired stations ``number`` answered as or a person confirmed, newest first, one row per station."""
    table = _stations()
    rows = connection.execute(sa.select(table).where(table.c.phone_number == number, table.c.expires_at > now)
                              .order_by(table.c.seen_at.desc(), table.c.id.desc())).mappings().all()
    found = {}
    for row in rows:
        found.setdefault(row['station'], dict(row))
    return list(found.values())


def expected_on(connection, number, now):
    """The numbers a call to ``number`` may answer as: the number itself, then its known stations."""
    return [digits(number)] + [row['station'] for row in stations_on(connection, number, now)]


def _newest_mode(connection, scope, key):
    table = _settings()
    return connection.execute(sa.select(table.c.mode).where(table.c.scope == scope, table.c.scope_key == key)
                              .order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)).scalar_one_or_none()


def mode_on(connection, number, mailbox_id=None):
    """``(mode, source)``: the recipient's own choice, else the mailbox's, else ``warn`` by default."""
    found = _newest_mode(connection, 'recipient', number)
    if found:
        return found, 'recipient'
    if mailbox_id:
        found = _newest_mode(connection, 'mailbox', mailbox_id)
        if found:
            return found, 'mailbox'
    return 'warn', 'default'


def set_mode_on(connection, scope, key, mode, *, actor_principal_id=None, actor_name=None, now):
    if scope not in ('recipient', 'mailbox') or mode not in MODES:
        raise ValueError('Choose warn or refuse.')
    connection.execute(_settings().insert().values(
        id=uuid.uuid4().hex, scope=scope, scope_key=key[:40], mode=mode, actor_principal_id=actor_principal_id,
        actor_name=(actor_name or None) and actor_name[:200], created_at=now))


def confirm_on(connection, number, station, *, actor_principal_id=None, actor_name=None, now):
    """A person confirms the station ``number`` answers as (from the recipient's letterhead, or a call)."""
    found = digits(station)
    if len(found) < MIN_DIGITS:
        raise ValueError(f'Enter the full fax number the machine shows, at least {MIN_DIGITS} digits.')
    connection.execute(_stations().insert().values(
        id=uuid.uuid4().hex, phone_number=number, station=found, source='person', job_id=None,
        actor_principal_id=actor_principal_id, actor_name=(actor_name or None) and actor_name[:200], seen_at=now,
        expires_at=now + timedelta(days=PERSON_DAYS)))
    return found


def learn_on(connection, number, station, job_id, now):
    """Keep the station a successful call to ``number`` answered as; a short or blank one is not kept."""
    found = digits(station)
    if len(found) < MIN_DIGITS:
        return None
    connection.execute(_stations().insert().values(
        id=uuid.uuid4().hex, phone_number=number, station=found, source='call', job_id=job_id,
        actor_principal_id=None, actor_name=None, seen_at=now, expires_at=now + timedelta(days=CALL_DAYS)))
    return found


def record_on(connection, *, attempt_id, job_id, number, station, outcome, engine, now):
    table = _results()
    if connection.execute(sa.select(table.c.id).where(table.c.id == attempt_id)).first() is not None:
        return
    connection.execute(table.insert().values(id=attempt_id, job_id=job_id, phone_number=number,
                                             station=digits(station), outcome=outcome, engine=engine,
                                             created_at=now))


# Per call ------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CallGuard:
    t0_ms: int | None = None       # FAXBOT_T0_MS, or None for spandsp's own T0
    expect: str | None = None      # FAXBOT_CSI_EXPECT: digits separated by dots (a comma ends an Originate variable)
    refuse: bool = False           # FAXBOT_CSI_REFUSE


def bills_by_minute(card):
    """Whether a rate card bills calls by the minute in steps of 60 s or more (the T0 cap's condition)."""
    return card is not None and getattr(card, 'per_minute_micros', 0) > 0 and \
        (getattr(card, 'billing_increment_seconds', 0) or 0) >= 60


def cap_on(values):
    """The trunk's own switch (``CAP_SETTING``); on unless someone turned it off."""
    return getattr(values, CAP_SETTING, True) is not False


def _billed(increment, minimum, seconds):
    from types import SimpleNamespace
    from .costs import billed_seconds
    return billed_seconds(SimpleNamespace(billing_increment_seconds=increment, minimum_seconds=minimum or 0), seconds)


_NUMBERS = {1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five'}


def _billed_text(seconds):
    if seconds % 60:
        return f'{seconds} seconds'
    minutes = seconds // 60
    return f'{_NUMBERS.get(minutes, minutes)} minute{"" if minutes == 1 else "s"}'


def _saving(increment, minimum):
    """('one minute instead of two', 'two minutes') when the cap puts an unanswered call in an earlier billed step:
    what it is billed as with the cap, and without it. None when it saves no step or the step is unknown."""
    if not increment:
        return None
    capped = _billed(increment, minimum, T0_CAP_MS // 1000)
    uncapped = _billed(increment, minimum, UNCAPPED_SECONDS)
    if capped >= uncapped:
        return None
    other = _NUMBERS.get(uncapped // 60, uncapped // 60) if not capped % 60 and not uncapped % 60 \
        else _billed_text(uncapped)
    return f'{_billed_text(capped)} instead of {other}', _billed_text(uncapped)


def cap_view(values, engine):
    """The answer cap for one trunk (``values`` as that trunk sees them): whether it is on, whether Faxbot uses it,
    and one sentence each for on and off, so the switch can show the right one before it is saved."""
    card = trunk_card(values, engine)
    applies = bills_by_minute(card)
    cap = T0_CAP_MS // 1000
    if applies:
        saving = _saving(card.billing_increment_seconds, card.minimum_seconds)
        step = card.billing_increment_seconds
        # Only the built-in fax engine (asterisk patch 0007) ends a call at the cap; the SSL Fax engine keeps its own.
        if saving:
            on = (f'On: your carrier bills this trunk by the minute, so when no fax machine answers within {cap} '
                  "seconds Faxbot's built-in fax engine hangs up, and the call is billed as "
                  f'{saving[0]}.')
            off = ('Off: Faxbot waits the usual 60 seconds for a fax machine to answer, so a call no fax machine '
                   f'answers is billed as {saving[1]}.')
        else:
            on = (f"On: when no fax machine answers within {cap} seconds, Faxbot's built-in fax engine hangs up; on "
                  f"this trunk's {step}-second billing steps that does not change the charge.")
            off = ('Off: Faxbot waits the usual 60 seconds for a fax machine to answer; on this trunk\'s '
                   f'{step}-second billing steps the charge is the same either way.')
    else:
        if card is None:
            on = ('On, but Faxbot does not use it yet: it has no call prices for this trunk, so it cannot tell '
                  'whether hanging up early saves anything. Enter your carrier\'s prices for this trunk to use it.')
        elif not getattr(card, 'per_minute_micros', 0):
            on = ('On, but Faxbot does not use it on this trunk: your carrier does not bill its calls by the minute, '
                  'so a longer wait for a fax machine costs nothing more.')
        else:
            on = ('On, but Faxbot does not use it on this trunk: your carrier bills its calls in '
                  f'{card.billing_increment_seconds}-second steps, so the usual 60-second wait for a fax machine '
                  'adds at most one short step.')
        off = 'Off: Faxbot waits the usual 60 seconds for a fax machine to answer.'
    switched_on = cap_on(values)
    return {'on': switched_on, 'applies': applies and switched_on, 'cap_seconds': cap,
            'sentence': on if switched_on else off, 'on_sentence': on, 'off_sentence': off}


def trunk_card(values, engine):
    """The trunk's outbound rate card: the one saved in Faxbot, else the shipped published one."""
    from .predict_facts import _card_for, shipped, stored_card
    preset = getattr(values, 'sip_trunk_preset', '') or ''
    card = None
    if engine is not None:
        card = stored_card(engine, 'sip', preset)
    if card is None:
        identity = f'sip-{preset}' if preset else 'sip'
        cards = shipped().get('cards') or {}
        card = _card_for(cards, identity) or _card_for(cards, 'sip')
    return card


def call_guard(values, job_id, number, *, engine, mailbox_id=None, peer=False):
    """The station check and T0 cap for one trunk call to ``number``. A database that cannot be read now gives
    no check (logged with the fax), never a refused call."""
    from .database import DeliveryStoreError
    if peer:
        return CallGuard()
    try:
        t0 = T0_CAP_MS if cap_on(values) and bills_by_minute(trunk_card(values, engine)) else None
        if engine is None:
            return CallGuard(t0)
        now = datetime.utcnow()
        with engine.connect() as connection:
            expected = expected_on(connection, number, now)
            mode, _ = mode_on(connection, number, mailbox_id)
    except (sa.exc.SQLAlchemyError, DeliveryStoreError) as error:
        log.warning('The station check for fax %s could not be read (%s); the call goes without it.', job_id,
                    type(error).__name__)
        return CallGuard()
    expect = '.'.join(item for item in dict.fromkeys(expected) if len(item) >= MIN_DIGITS)[:200]
    return CallGuard(t0, expect or None, mode == 'refuse')


def _dialed_on(connection, job_id, attempt_id):
    jobs = sa.table('fax_jobs', sa.column('id'), sa.column('to_number'))
    attempts = sa.table('outbound_attempts', sa.column('id'), sa.column('dialed_number'))
    dialed = connection.execute(sa.select(attempts.c.dialed_number).where(attempts.c.id == attempt_id)).scalar() \
        if attempt_id else None
    return dialed or connection.execute(sa.select(jobs.c.to_number).where(jobs.c.id == job_id)).scalar()


def _caps():
    return sa.table('answer_cap_calls', sa.column('id'), sa.column('job_id'), sa.column('cap_seconds'),
                    sa.column('increment_seconds'), sa.column('minimum_seconds'),
                    sa.column('created_at', sa.DateTime()))


def _call_card(connection, configuration, attempt_id, engine):
    """The rate card of the trunk this attempt went over, read when its result arrives (None when unknown)."""
    if configuration is None:
        return None
    from ..accounts import account_values
    values = configuration.read().active.values
    choices = sa.table('delivery_rule_choices', sa.column('id'), sa.column('account_key'))
    key = connection.execute(sa.select(choices.c.account_key).where(choices.c.id == attempt_id)).scalar() \
        if sa.inspect(connection).has_table('delivery_rule_choices') else None
    if key and key != 'sip':
        values = account_values(values, key)
    return trunk_card(values, engine)


def record_cap_on(connection, *, attempt_id, job_id, card, now):
    """Keep that the built-in engine ended this attempt at the answer cap, with the billing step it fitted in."""
    table = _caps()
    if connection.execute(sa.select(table.c.id).where(table.c.id == attempt_id)).first() is not None:
        return
    connection.execute(table.insert().values(
        id=attempt_id, job_id=job_id, cap_seconds=T0_CAP_MS // 1000,
        increment_seconds=getattr(card, 'billing_increment_seconds', None) if card is not None else None,
        minimum_seconds=getattr(card, 'minimum_seconds', None) if card is not None else None, created_at=now))


def after_call(engine, *, job_id, attempt_id, station, succeeded, check_result=None, engine_name='builtin',
               t0_capped=False, configuration=None):
    """After a sent call: keep the station every successful call answered as (one that differed included: it has
    now shown on a successful call, research N5's rule), and what the check found.

    ``check_result`` is the built-in engine's own (patch 0007's CsiCheck: matches, differs or refused); the SSL Fax
    engine has none, so its station is checked here, after the call. ``t0_capped`` (patch 0007's T0Capped) keeps
    that the call ended at the answer cap, with the trunk's billing step read from ``configuration`` now. Storage
    that cannot be written now is logged and skipped: it never changes the fax's outcome."""
    if engine is None or not job_id:
        return None
    now = datetime.utcnow()
    capped = bool(t0_capped and attempt_id and not succeeded)
    card = None
    if capped:
        from ..config_store import ConfigurationStoreError
        from .database import DeliveryStoreError
        try:
            with engine.connect() as connection:
                card = _call_card(connection, configuration, attempt_id, engine)
        except (sa.exc.SQLAlchemyError, DeliveryStoreError, ConfigurationStoreError, ValueError) as error:
            # Kept without the billing step: Sent details then say only that Faxbot hung up.
            log.warning('The trunk prices for fax %s could not be read (%s).', job_id, type(error).__name__)
    try:
        with engine.begin() as connection:
            number = _dialed_on(connection, job_id, attempt_id)
            if not number:
                return None
            if capped:
                record_cap_on(connection, attempt_id=attempt_id, job_id=job_id, card=card, now=now)
            outcome = check_result if check_result in ('differs', 'refused') else None
            if engine_name == 'sslfax' and check_result is None:
                found = check(station, expected_on(connection, number, now))
                outcome = 'differs' if found == DIFFERS else None
            if outcome and attempt_id:
                record_on(connection, attempt_id=attempt_id, job_id=job_id, number=number, station=station,
                          outcome=outcome, engine=engine_name, now=now)
            if succeeded:
                learn_on(connection, number, station, job_id, now)
            return outcome
    except sa.exc.SQLAlchemyError as error:
        log.warning('The station check result for fax %s could not be kept (%s).', job_id, type(error).__name__)
        return None


# Views ---------------------------------------------------------------------------------------------------------------

def cap_sentence(row):
    """What Sent details say about a call the answer cap ended, with the billed step it fitted in when known."""
    text = (f'Faxbot hung up {row["cap_seconds"]} seconds after the call was answered because no fax machine '
            'answered')
    saving = _saving(row['increment_seconds'], row['minimum_seconds'])
    return f'{text}, so it is billed as {saving[0]}.' if saving else f'{text}.'


def fax_sentences(engine, job_id):
    """What Sent details say about a fax's calls: one sentence per call that answered as another station, and one
    per call the answer cap ended, in call order."""
    table, caps = _results(), _caps()
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(sa.select(table).where(table.c.job_id == job_id))
                .mappings().all()]
        if sa.inspect(connection).has_table('answer_cap_calls'):
            rows += [{**row, 'outcome': 'capped'} for row in connection.execute(
                sa.select(caps).where(caps.c.job_id == job_id)).mappings().all()]
    rows.sort(key=lambda row: (row['created_at'], row['id']))
    sentences = []
    for row in rows:
        if row['outcome'] == 'capped':
            sentences.append(cap_sentence(row))
            continue
        station = shown(row['station'])
        if row['outcome'] == 'refused':
            sentences.append(f'The number answered as {station}, a fax machine Faxbot did not expect there, so '
                             'Faxbot hung up before any page. Check the number with the recipient.')
        elif row['engine'] == 'sslfax':
            sentences.append(f'The number answered as {station}, not the fax machine Faxbot expected there. The SSL '
                             'Fax engine can only check this after the call, so the fax was sent.')
        else:
            sentences.append(f'The number answered as {station}, not the fax machine Faxbot expected there; the fax '
                             'went on. Check the number with the recipient.')
    return sentences


MAILBOX_MODE_TEXT = {'warn': 'the fax goes on and Sent details say so',
                     'refuse': 'Faxbot hangs up before any page'}


def mailbox_view(engine, labels):
    """Each mailbox's choice (``labels``: {mailbox ID: name}): its mode, whether someone chose it, and one sentence.
    A recipient's own choice still comes first; this is what the mailbox's faxes do otherwise."""
    table = _settings()
    found = []
    with engine.connect() as connection:
        for mailbox_id, label in sorted(labels.items(), key=lambda item: (str(item[1]).lower(), item[0])):
            row = connection.execute(sa.select(table.c.mode, table.c.actor_name, table.c.created_at).where(
                table.c.scope == 'mailbox', table.c.scope_key == mailbox_id[:40])
                .order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().first()
            mode = row['mode'] if row else 'warn'
            who = f' Chosen by {row["actor_name"]}.' if row and row['actor_name'] else ''
            sentence = (f'When a number answers as another fax machine on a fax from {label}, '
                        f'{MAILBOX_MODE_TEXT[mode]}.' + (who if row else ' This is Faxbot\'s default.'))
            found.append({'mailbox_id': mailbox_id, 'label': label, 'mode': mode, 'chosen': row is not None,
                          'actor_name': row['actor_name'] if row else None, 'sentence': sentence})
    return found


def recipient_view(engine, number, now=None):
    now = now or datetime.utcnow()
    with engine.connect() as connection:
        mode, source = mode_on(connection, number)
        stations = stations_on(connection, number, now)
    return {'number': number, 'mode': mode, 'mode_source': source,
            'stations': [{'station': shown(row['station']), 'source': row['source'],
                          'seen_at': row['seen_at'].isoformat(timespec='seconds') if isinstance(row['seen_at'], datetime)
                          else row['seen_at'], 'actor_name': row['actor_name']} for row in stations]}
