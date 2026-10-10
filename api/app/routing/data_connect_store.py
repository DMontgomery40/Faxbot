"""NTT Data Connect in route choice: which numbers take it, which trunks place it, how each call went (brief 99, 2).

A fax to a number whose machine takes Data Connect, over an NTT Hikari Denwa trunk with Data Connect on, goes as a
Data Connect call: Faxbot's built-in engine offers only T.38 in the first INVITE (asterisk patch 0010), NTT prices
the session by its bandwidth (``data_connect.card``, ¥1.1 per started 30 seconds) instead of the voice price, and
route choice prices it so. One predicate decides it everywhere (``places``): the predictor's facts that rank and
price routes, the call Faxbot places (``ami.originate_fields_for`` through ``hylafax_engine.CallSettings``) and the
attempt's recorded estimate, which uses the Data Connect price only for a call that really went as Data Connect.

- **The recipient's fact** (``destination_data_connect``, migration 0081): you set it on Recipients → Details or with
  ``faxbot recipients set NUMBER --data-connect on|off``; Faxbot learns it from calls, both ways. A call that went as
  Data Connect confirms it; a 488 refusal (the machine or NTT's network does not take the offer) turns it off, with
  the date, and the same attempt calls again as an ordinary fax call before any page, so nothing is ever sent twice.
- **The trunk** (``data_connect_trunks``): on by default for an NTT Hikari Denwa trunk, because Data Connect has no
  monthly fee (NTT's service page, read 10 October 2026); you can turn it off.
- **Each call** (``data_connect_calls``): 'data_connect' or 'refused', with NTT's Warning text on a refusal.

NTT's restrictions (``data_connect.ADVICE``) are shown wherever you turn it on. Not yet run against NTT.
"""
from __future__ import annotations

from datetime import datetime, timezone
import base64
import logging
import uuid

import sqlalchemy as sa

from . import data_connect


PRESET = 'ntt-hikari'
REFUSED_SENTENCE = ("The recipient's machine doesn't take Data Connect; Faxbot called again as an ordinary fax "
                    'call.')
NOT_RUN = 'Faxbot has not yet run Data Connect against NTT.'
log = logging.getLogger(__name__)


class DataConnectError(ValueError):
    """The change cannot be made as asked; one plain sentence."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _table(engine, name):
    return sa.Table(name, sa.MetaData(), autoload_with=engine)


def _has_tables(engine):
    with engine.connect() as connection:
        return sa.inspect(connection).has_table('destination_data_connect')


# -- the trunk ---------------------------------------------------------------------------------------------------

def account_preset(values, account_key):
    """The carrier preset of a trunk account: the first trunk's setting, or a later trunk's own; None otherwise.
    Read from the account documents as the predictor reads them (``predict_facts.facts_for``)."""
    from ..sip_trunk import PRIMARY
    if values is None:
        return None
    if account_key == PRIMARY:
        return getattr(values, 'sip_trunk_preset', '') or None
    document = (getattr(values, 'provider_accounts', None) or {}).get(account_key) or {}
    if document.get('provider') != 'sip':
        return None
    return (document.get('settings') or {}).get('preset') or None


def ntt_trunks(values):
    """[(account key, label)] of the trunks on NTT Hikari Denwa."""
    from ..accounts import all_accounts
    found = []
    for account in all_accounts(values):
        if account.provider == 'sip' and account_preset(values, account.key) == PRESET:
            found.append((account.key, account.label))
    return found


def trunk_state(engine, account_key):
    """``(enabled, reason, row)``: on unless you turned it off, or Faxbot did and said why (newest row counts)."""
    table = _table(engine, 'data_connect_trunks')
    with engine.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.account_key == account_key).order_by(
            table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().one_or_none()
    if row is None:
        return True, None, None
    return bool(row['enabled']), row['reason'], dict(row)


def set_trunk(engine, values, account_key, enabled, *, actor_id=None, actor_name=None, reason=None, now=None):
    if account_preset(values, account_key) != PRESET:
        raise DataConnectError('Data Connect is only for a trunk on NTT Hikari Denwa.')
    row = {'id': uuid.uuid4().hex, 'account_key': account_key[:64], 'enabled': int(bool(enabled)),
           'reason': (reason or '')[:500] or None, 'created_at': now or utcnow(),
           'created_by': (str(actor_id)[:40] if actor_id else None),
           'created_by_name': (str(actor_name)[:200] if actor_name else None)}
    with engine.begin() as connection:
        connection.execute(_table(engine, 'data_connect_trunks').insert().values(**row))
    return row


# -- the recipient's fact ----------------------------------------------------------------------------------------

def fact(engine, destination):
    """The newest fact about ``destination`` (a dict), or None: never set and never learned."""
    table = _table(engine, 'destination_data_connect')
    with engine.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.destination == destination).order_by(
            table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().one_or_none()
    return dict(row) if row is not None else None


def set_fact(engine, destination, capable, *, source='admin', attempt_id=None, note=None, actor_id=None,
             actor_name=None, now=None):
    row = {'id': uuid.uuid4().hex, 'destination': destination[:32], 'capable': int(bool(capable)),
           'source': source, 'attempt_id': attempt_id, 'note': (note or '')[:500] or None,
           'created_at': now or utcnow(), 'created_by': (str(actor_id)[:40] if actor_id else None),
           'created_by_name': (str(actor_name)[:200] if actor_name else None)}
    with engine.begin() as connection:
        connection.execute(_table(engine, 'destination_data_connect').insert().values(**row))
    return row


def takes_data_connect(engine, destination) -> bool:
    found = fact(engine, destination)
    return bool(found and found['capable'])


def capable_numbers(engine) -> set:
    """The numbers whose newest fact says their machine takes Data Connect calls (Savings & optimization →
    Capabilities); none before the tables exist."""
    if not _has_tables(engine):
        return set()
    table = _table(engine, 'destination_data_connect')
    newest = {}
    with engine.connect() as connection:
        for row in connection.execute(sa.select(table.c.destination, table.c.capable).order_by(
                table.c.created_at, table.c.id)):
            newest[row.destination] = bool(row.capable)
    return {number for number, capable in newest.items() if capable}


# -- the one predicate -------------------------------------------------------------------------------------------

def _t38_on(values, account_key):
    """Whether the trunk offers T.38 at all (its fax setting, and no encrypted calls, which carry audio only)."""
    from .. import sip_trunk
    from ..ami import trunk_values
    try:
        own, _ = trunk_values(values, None if account_key == sip_trunk.PRIMARY else account_key)
        return bool(sip_trunk.effective_trunk(own).t38)
    except ValueError:
        # An unknown trunk or one that cannot be rendered places no call at all, Data Connect or not.
        return False


def trunks_here(engine, values):
    """[(account key, label, T.38 on, Data Connect on)] of the NTT Hikari Denwa trunks that send faxes."""
    from ..accounts import all_accounts
    sending = {account.key for account in all_accounts(values) if account.sends and account.enabled}
    found = []
    for key, label in ntt_trunks(values):
        if key in sending:
            enabled = trunk_state(engine, key)[0] if _has_tables(engine) else True
            found.append((key, label, _t38_on(values, key), enabled))
    return found


def places(engine, values, account_key, destination) -> bool:
    """Whether a fax to ``destination`` by ``account_key`` goes as a Data Connect call.

    All of: the account is a trunk on NTT Hikari Denwa with Data Connect on, the number's newest fact says its
    machine takes Data Connect, no keys are pressed after it answers (they need a voice call) and it is not set up
    for sending together (a shared call). The built-in engine places every such call (``outbound_transport``); a
    call that goes to an enrolled partner inside the tunnel instead is not a Data Connect call and is not priced
    as one."""
    if engine is None or values is None or not destination or account_preset(values, account_key) != PRESET:
        return False
    if not _t38_on(values, account_key) or not _has_tables(engine):
        return False
    if not trunk_state(engine, account_key)[0] or not takes_data_connect(engine, destination):
        return False
    from .after_answer import AfterAnswerUnavailable, for_number
    try:
        if for_number(engine, destination):
            return False
    except AfterAnswerUnavailable:
        return False
    from ..batching.store import BatchingSettings
    from .database import DeliveryStoreError
    try:
        if BatchingSettings(engine).get(destination)['enabled']:
            return False
    except DeliveryStoreError:
        return False
    return True


# -- how each call went ------------------------------------------------------------------------------------------

def outcome(engine, attempt_id):
    """'data_connect', 'refused' or None (not a Data Connect call, or not known yet) for one attempt."""
    try:
        table = _table(engine, 'data_connect_calls')
    except sa.exc.NoSuchTableError:
        return None
    with engine.connect() as connection:
        return connection.execute(sa.select(table.c.outcome).where(table.c.attempt_id == attempt_id)).scalar()


def _attempt(engine, attempt_id):
    """(account key, destination) the attempt was decided for, from its route decision."""
    from .store import RouteStore
    found = RouteStore(engine).decision(attempt_id)
    if found is None:
        return None, None
    return found.get('route') or found.get('provider_id'), found.get('destination')


def record_call(engine, job_id, attempt_id, call_outcome, *, warning=None, now=None):
    """Keep how one attempt's call went, once, and learn the recipient's fact from it (both ways)."""
    now = now or utcnow()
    account_key, destination = _attempt(engine, attempt_id)
    table = _table(engine, 'data_connect_calls')
    with engine.begin() as connection:
        if connection.execute(sa.select(table.c.id).where(table.c.attempt_id == attempt_id)).first():
            return False
        connection.execute(table.insert().values(
            id=uuid.uuid4().hex, attempt_id=attempt_id, job_id=job_id, account_key=(account_key or 'sip')[:64],
            destination=(destination or '')[:32], outcome=call_outcome, warning=(warning or '')[:200] or None,
            kbps=data_connect.SESSION_KBPS if call_outcome == 'data_connect' else None, created_at=now))
    if destination:
        current = fact(engine, destination)
        capable = call_outcome == 'data_connect'
        if current is None or bool(current['capable']) != capable:
            note = ('A Data Connect call to this number went through.' if capable else
                    f"This number's machine or NTT's network refused a Data Connect call"
                    f"{': ' + warning if warning else ''}.")
            set_fact(engine, destination, capable, source='learned', attempt_id=attempt_id, note=note, now=now)
    return True


def _decode(value):
    try:
        return base64.b64decode(str(value or ''), validate=True).decode('utf-8', 'replace').strip()[:200] or None
    except (ValueError, TypeError):
        return None


def attach(ami_client, engine):
    """Record Data Connect calls from this AMI client's events into ``engine``; safe to call on every start."""
    def refused(event):
        fields = {str(key).lower(): value for key, value in event.items()}
        if str(fields.get('outcome', '')).lower() != 'refused':
            return
        try:
            record_call(engine, fields.get('jobid'), fields.get('attemptid'), 'refused',
                        warning=_decode(fields.get('warning64')))
        except sa.exc.SQLAlchemyError:
            log.warning('A refused Data Connect call could not be recorded.', exc_info=True)

    def result(event):
        fields = {str(key).lower(): value for key, value in event.items()}
        if str(fields.get('dataconnect', '')).lower() != 'connected':
            return
        try:
            record_call(engine, fields.get('jobid'), fields.get('attemptid'), 'data_connect')
        except sa.exc.SQLAlchemyError:
            log.warning('A Data Connect call could not be recorded.', exc_info=True)
    ami_client.on_data_connect(refused)
    ami_client.on_fax_result(result)


# -- views ------------------------------------------------------------------------------------------------------

def sent_sentence(engine, job_id):
    """One sentence for Sent about the newest Data Connect call of this fax, or None."""
    try:
        table = _table(engine, 'data_connect_calls')
    except sa.exc.NoSuchTableError:
        return None
    with engine.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.job_id == job_id).order_by(
            table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().one_or_none()
    if row is None:
        return None
    if row['outcome'] == 'refused':
        return REFUSED_SENTENCE
    found = data_connect.tier(row['kbps'] or data_connect.SESSION_KBPS)
    return (f'Sent as a Data Connect call over NTT Hikari Denwa, priced by its bandwidth: ¥{found.yen} per started '
            f'{found.seconds} seconds.')


def recipient_view(engine, values, destination):
    """Recipients → Details → Data Connect: the fact, where it came from, the trunks and NTT's restrictions."""
    found = fact(engine, destination) if _has_tables(engine) else None
    trunks = []
    for key, label in ntt_trunks(values):
        enabled, reason, _ = trunk_state(engine, key)
        trunks.append({'account': key, 'label': label, 'enabled': enabled, 'reason': reason})
    if found is None:
        sentence = ('Faxbot does not know whether this number\'s fax machine takes Data Connect calls; it calls it '
                    'as an ordinary fax call.')
    elif found['source'] == 'learned':
        sentence = found['note'] or ('Faxbot learned this from a call.')
    elif found['capable']:
        sentence = 'You recorded that this number\'s fax machine takes Data Connect calls.'
    else:
        sentence = 'You recorded that this number\'s fax machine does not take Data Connect calls.'
    if found is not None and found['capable'] and not any(item['enabled'] for item in trunks):
        sentence += ' No NTT Hikari Denwa trunk places Data Connect calls, so faxes to it go as ordinary calls.'
    return {'number': destination, 'capable': bool(found and found['capable']),
            'known': found is not None, 'source': found['source'] if found else None,
            'learned_on': found['created_at'].date().isoformat() if found and found['source'] == 'learned' else None,
            'sentence': sentence, 'trunks': trunks, 'advice': list(data_connect.ADVICE),
            'price': data_connect.price_sentence(), 'not_run': NOT_RUN}
