"""Lossless tuning of the page coding: what each call asks the SSL Fax engine for, your choices, and what it reported.

The SSL Fax engine (HylaFAX+ 7.0.11 with hylafax/patches/0003, faxd/LosslessTuning.h) changes only how the same
pixels are coded:

- MR: the fewest-bytes legal reset schedule (T.4 4.2.1.3.4 allows a one-dimensional line earlier than every K
  lines; every line carries its own tag bit). On by default everywhere: it is plain T.4.
- JBIG: the smallest of 12 T.85 settings (typical prediction, the two-line template and adaptive template moves
  up to MX 32 at L0 128; T.85 Table 1 puts all of them in the profile every receiver must decode).

Tuned JBIG travels only with error correction, so frames arrive intact, and a receiver whose decoder mishandles
typical prediction or a moved template pixel may still confirm the page (MCF) while it prints garbage; the sending
side would never know. So it is on by default only where the receiving machine is known to decode it:

1. pages that go over SSL Fax: only HylaFAX+ (and products built from it) speaks SSL Fax, and it stores the JBIG
   page for libtiff, which decodes with jbigkit's full decoder (faxd/CopyQuality.c++, HAVE_JBIGTIFF). faxd decides
   this per page (TuneJBIG "sslfax"), so a call that falls back to the telephone line sends its later pages plain;
2. an enrolled, verified Faxbot partner whose signed statement says its own Faxbot fax engine answers its number
   (``partner_own_engine``; a partner on a cloud fax service has someone else's decoder and does not qualify).

Everyone else gets tuned JBIG only when you turn it on for that number, with ``JBIG_WARNING``.

Learning (``record_call``): when the engine reports that the receiving machine refused a tuned page (RTN, error
correction that could not get the page through, or a tuned page never confirmed on a call that failed), that
number's later calls use plain settings for that coding. Nothing is resent: the engine's own job retries are the
only retries, and the learning applies to the next call. Saving your choice for the number again clears it.

The job carries the choice in its comments (``CallTuning.comment``), which hylafax/bin/jobcontrol turns into
faxsend's TuneMR and TuneJBIG.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import re
import uuid
import weakref

SETTING = 'sip_fax_tune_coding'
CHOICES = 'recipient_coding_tuning'
CALLS = 'coding_tuning_calls'
ENGINES = 'partner_fax_engines'
_TABLES = weakref.WeakKeyDictionary()
_DIGEST = re.compile(r'[0-9a-f]{64}')
_SETTINGS = re.compile(r'(?:[0-9]{1,3}/[0-9]{1,3})(?:,[0-9]{1,3}/[0-9]{1,3})*')

# One sentence each, for the console, the command line and the Sent detail.
SETTING_OFF = 'Smaller pages are turned off in your fax settings.'
NUMBER_OFF = 'You turned smaller pages off for this number.'
JBIG_WARNING = ('Turn this on only if this fax machine prints faxes from Faxbot correctly: a machine that cannot '
                'read the smallest page format may print garbled pages and still report them as received.')
JBIG_ON = 'You turned on the smallest page format for this number.'
PARTNER = "This number is a partner's Faxbot, which reads the smallest page format."
SSLFAX = 'The smallest page format is used only when pages go over the internet to this fax machine.'
REFUSED = {'JBIG': 'This fax machine refused a page in the smallest page format, so Faxbot sends it plain pages.',
           'MR': 'This fax machine refused a page Faxbot made smaller, so Faxbot sends it plain pages.'}


@dataclass(frozen=True)
class CallTuning:
    """What one call asks of the SSL Fax engine: the MR schedule on or off, and tuned JBIG 'always', 'sslfax' (only
    pages over SSL Fax) or 'never'; ``reasons`` explains anything turned off or on."""
    mr: bool = True
    jbig: str = 'sslfax'
    reasons: tuple = ()

    def comment(self) -> str:
        """The job's comments (JPARM COMMENTS) that hylafax/bin/jobcontrol reads."""
        return f'faxbot-tuning mr={"on" if self.mr else "off"} jbig={self.jbig}'

    def priced_jbig(self, sslfax_expected=False) -> bool:
        """Whether the call's JBIG is priced tuned: always, or over SSL Fax when the machine took it before."""
        return self.jbig == 'always' or (self.jbig == 'sslfax' and bool(sslfax_expected))


def _table(engine, name):
    """A tuning table, reflected once per database; raises ``routing.database.DeliveryStoreError`` before 0063."""
    found = _TABLES.setdefault(engine, {})
    if name not in found:
        from ..routing.database import reflect
        found[name] = reflect(engine, (name,))[name]
    return found[name]


def _number(value):
    return re.sub(r'[^0-9+]', '', str(value or ''))[:32] or None


# Your choices -------------------------------------------------------------------------------------------------

def recipient_choice(engine, number):
    """{'tune': None|False, 'tune_jbig': bool, 'saved_at': datetime|None} for one number (the newest row)."""
    import sqlalchemy as sa
    table = _table(engine, CHOICES)
    with engine.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.number == _number(number)).order_by(
            table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().first()
    if row is None:
        return {'tune': None, 'tune_jbig': False, 'saved_at': None}
    return {'tune': None if row['tune'] is None else False, 'tune_jbig': bool(row['tune_jbig']),
            'saved_at': row['created_at']}


def save_choice(engine, number, *, tune=None, tune_jbig=False, actor=None, now=None):
    """Keep your choice for one number (append-only): ``tune`` None (as set for all faxes) or False (off), and
    ``tune_jbig`` True to tune JBIG for it too. Saving again also clears what Faxbot learned for the number."""
    if tune not in (None, False) or not isinstance(tune_jbig, bool):
        raise ValueError('Unsupported tuning choice')
    target = _number(number)
    if not target:
        raise ValueError('A fax number is needed')
    table = _table(engine, CHOICES)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id=uuid.uuid4().hex, number=target, tune=None if tune is None else 0,
                                                 tune_jbig=1 if tune_jbig else 0,
                                                 recorded_by=str(actor or '')[:40] or None,
                                                 created_at=now or datetime.utcnow()))


# What the engine reported -------------------------------------------------------------------------------------

def report(negotiation) -> dict | None:
    """The tuning part of a call's negotiation (hylafax/bin/negotiation, already decoded), or None without one."""
    if not isinstance(negotiation, dict) or 'tuning_jbig_pages' not in negotiation:
        return None

    def count(name):
        value = negotiation.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** 9 else 0
    settings = negotiation.get('tuning_jbig_settings')
    digests = negotiation.get('tuning_digests')
    return {'jbig_pages': count('tuning_jbig_pages'), 'jbig_tuned': count('tuning_jbig_tuned'),
            'jbig_bytes': count('tuning_jbig_bytes'), 'jbig_plain': count('tuning_jbig_plain'),
            'jbig_settings': settings if isinstance(settings, str) and _SETTINGS.fullmatch(settings[:200]) else None,
            'mr_pages': count('tuning_mr_pages'), 'mr_tuned': count('tuning_mr_tuned'),
            'mr_bytes': count('tuning_mr_bytes'), 'mr_plain': count('tuning_mr_plain'),
            'digests': [item for item in (digests.split(',') if isinstance(digests, str) else [])
                        if _DIGEST.fullmatch(item)][:500],
            'refused': count('tuning_refused'), 'unconfirmed': count('tuning_unconfirmed') > 0}


def refusal_reason(found, *, success):
    """One sentence when the receiving machine refused a tuned page on this call, else None."""
    if found['refused']:
        return 'The receiving fax machine asked for a tuned page again or could not take it.'
    if found['unconfirmed'] and not success:
        return 'The call failed before the receiving fax machine confirmed a tuned page.'
    return None


def record_call(engine, *, call_key, job_id, number, negotiation, success, sslfax=False, now=None):
    """Keep what the engine reported tuning on one call, one row per coding it coded (JBIG, MR), once. A refused
    tuned page is recorded against the coding that call tuned. Returns the rows written."""
    import sqlalchemy as sa
    found = report(negotiation)
    if found is None:
        return 0
    reason = refusal_reason(found, success=bool(success))
    table = _table(engine, CALLS)
    written = 0
    rows = []
    for coding, pages, tuned, sent, plain in (
            ('JBIG', found['jbig_pages'], found['jbig_tuned'], found['jbig_bytes'], found['jbig_plain']),
            ('MR', found['mr_pages'], found['mr_tuned'], found['mr_bytes'], found['mr_plain'])):
        if not pages:
            continue
        rows.append({'id': uuid.uuid4().hex, 'call_key': str(call_key)[:64], 'job_id': str(job_id or '')[:40] or None,
                     'number': _number(number), 'coding': coding, 'pages': pages, 'tuned_pages': tuned,
                     'tuned_bytes': sent, 'plain_bytes': plain,
                     'settings': found['jbig_settings'] if coding == 'JBIG' else None,
                     'digests': ','.join(found['digests']) or None, 'sslfax': 1 if sslfax else 0,
                     'refused': 1 if reason and tuned else 0, 'reason': reason if tuned else None,
                     'created_at': now or datetime.utcnow()})
    with engine.begin() as connection:
        for row in rows:
            exists = connection.execute(sa.select(table.c.id).where(
                table.c.call_key == row['call_key'], table.c.coding == row['coding'])).first()
            if exists is None:
                connection.execute(table.insert().values(**row))
                written += 1
    return written


def call_tuning(engine, call_key):
    """{coding: row} of what the engine reported tuning on one call (empty when nothing was reported)."""
    import sqlalchemy as sa
    table = _table(engine, CALLS)
    with engine.connect() as connection:
        rows = connection.execute(sa.select(table).where(table.c.call_key == str(call_key))).mappings().all()
    return {row['coding']: dict(row) for row in rows}


def learned_plain(engine, number, *, since=None):
    """{coding: sentence} for codings this number refused when tuned, newer than your last choice for it."""
    import sqlalchemy as sa
    table = _table(engine, CALLS)
    query = sa.select(table.c.coding, table.c.created_at).where(table.c.number == _number(number),
                                                                 table.c.refused == 1)
    if since is not None:
        query = query.where(table.c.created_at > since)
    with engine.connect() as connection:
        rows = connection.execute(query).all()
    return {coding: REFUSED[coding] for coding, _ in rows if coding in REFUSED}


# Partners whose own Faxbot fax engine answers their number -----------------------------------------------------

def own_engine_answers(values) -> bool:
    """Whether this installation's own fax engines answer its partner fax number (the number is one of the SIP
    trunk's numbers and received faxes come over that trunk), as its signed capability statements say."""
    from ..routing.numbers import InvalidNumber, normalize_number
    if getattr(values, 'effective_inbound', '') != 'sip':
        return False
    country = getattr(values, 'fax_default_country', None)
    try:
        own = normalize_number(getattr(values, 'direct_fax_number', '') or '', country=country)
    except (InvalidNumber, TypeError, ValueError):
        return False
    for listed in getattr(values, 'sip_trunk_did_list', ()) or ():
        try:
            if normalize_number(listed, country=country) == own:
                return True
        except (InvalidNumber, TypeError, ValueError):
            continue
    return False


def note_partner_engine(engine, peer_id, own_engine, said_at, *, now=None):
    """Keep what a partner said, in a statement it signed, about whether its own Faxbot fax engine answers its
    number (append-only; the newest statement counts). None (not said) is not kept."""
    if own_engine is None:
        return False
    table = _table(engine, ENGINES)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id=uuid.uuid4().hex, peer_id=str(peer_id)[:40],
                                                 own_engine=1 if own_engine else 0, said_at=said_at,
                                                 created_at=now or datetime.utcnow()))
    return True


def partner_own_engine(engine, number) -> bool:
    """Whether ``number`` belongs to an enrolled, verified partner whose newest signed statement says its own
    Faxbot fax engine answers it. A partner that never said so does not qualify."""
    import sqlalchemy as sa
    from ..direct.store import DirectStore
    peer = DirectStore(engine).verified_peer_for(_number(number))
    if peer is None:
        return False
    table = _table(engine, ENGINES)
    with engine.connect() as connection:
        row = connection.execute(sa.select(table.c.own_engine).where(table.c.peer_id == peer['id']).order_by(
            table.c.said_at.desc(), table.c.created_at.desc()).limit(1)).first()
    return bool(row is not None and row[0])


# One call -----------------------------------------------------------------------------------------------------

def decide(*, setting=True, choice=None, learned=None, partner=False) -> CallTuning:
    """The tuning one call asks for (pure): your setting, your choice for the number, what Faxbot learned from its
    refusals since that choice, and whether it is a partner whose own Faxbot answers it."""
    choice = choice or {'tune': None, 'tune_jbig': False}
    learned = learned or {}
    if not setting:
        return CallTuning(mr=False, jbig='never', reasons=(SETTING_OFF,))
    if choice.get('tune') is False:
        return CallTuning(mr=False, jbig='never', reasons=(NUMBER_OFF,))
    reasons = []
    mr = 'MR' not in learned
    if not mr:
        reasons.append(learned['MR'])
    if 'JBIG' in learned:
        return CallTuning(mr=mr, jbig='never', reasons=tuple(reasons + [learned['JBIG']]))
    if choice.get('tune_jbig'):
        return CallTuning(mr=mr, jbig='always', reasons=tuple(reasons + [JBIG_ON]))
    if partner:
        return CallTuning(mr=mr, jbig='always', reasons=tuple(reasons + [PARTNER]))
    return CallTuning(mr=mr, jbig='sslfax', reasons=tuple(reasons))


def for_call(values, engine, number) -> CallTuning:
    """``decide`` from the installation's records for a call to ``number``. Records that cannot be read (before
    migration 0063) give the default: the engine's own defaults, the same as the setting on."""
    import sqlalchemy as sa
    from ..routing.database import DeliveryStoreError
    setting = bool(getattr(values, SETTING, True))
    if engine is None or not setting:
        return decide(setting=setting)
    try:
        choice = recipient_choice(engine, number)
        learned = learned_plain(engine, number, since=choice['saved_at'])
        try:
            partner = partner_own_engine(engine, number)
        except (sa.exc.SQLAlchemyError, DeliveryStoreError):
            partner = False
    except (sa.exc.SQLAlchemyError, DeliveryStoreError):
        logging.getLogger(__name__).warning('The tuning choices for this number could not be read; the engine uses '
                                            'its defaults.', exc_info=True)
        return decide(setting=setting)
    return decide(setting=setting, choice=choice, learned=learned, partner=partner)


def recipient_view(values, engine, number):
    """Recipients, Details: your choice for the number and what is in force, one sentence each."""
    choice = recipient_choice(engine, number)
    tuning = for_call(values, engine, number)
    if tuning.jbig == 'always':
        jbig = 'Smallest page format: on for this number.'
    elif tuning.jbig == 'sslfax':
        jbig = SSLFAX
    else:
        jbig = 'Smallest page format: off for this number.'
    return {'number': _number(number), 'tune': choice['tune'], 'tune_jbig': choice['tune_jbig'],
            'setting': bool(getattr(values, SETTING, True)), 'mr': tuning.mr, 'jbig': tuning.jbig,
            'reasons': list(tuning.reasons), 'jbig_sentence': jbig, 'warning': JBIG_WARNING}


# The Sent detail ----------------------------------------------------------------------------------------------

def sent_suffix(rows, coding):
    """", tuned" (JBIG) or ", tuned schedule" (MR) when the engine reported tuning the coding the call used."""
    row = (rows or {}).get(coding)
    if not row or not row.get('tuned_pages'):
        return ''
    return ', tuned' if coding == 'JBIG' else ', tuned schedule'


def sent_sentence(rows):
    """One sentence about what tuning did on the call, or None: "Tuned JBIG sent 5,629 bytes where plain JBIG
    would have sent 29,003." / the refusal."""
    for coding in ('JBIG', 'MR'):
        row = (rows or {}).get(coding)
        if not row:
            continue
        if row.get('refused'):
            return REFUSED[coding]
        sent, plain = row.get('tuned_bytes'), row.get('plain_bytes')
        if row.get('tuned_pages') and sent and plain and sent < plain:
            name = 'JBIG' if coding == 'JBIG' else 'MR'
            return (f'Tuned {name} sent {sent:,} bytes where plain {name} would have sent {plain:,}, the same '
                    'pixels either way.')
    return None
