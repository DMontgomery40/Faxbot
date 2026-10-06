"""The per-number setting, its append-only history, and one row per fax held to go together.

Writes to the per-fax rows happen inside the delivery store's installation
transaction (acceptance, claims, recovery), so a fax is never both waiting and
claimed. The per-number setting is ordinary settings with a version; every
change also appends a ``batching_changes`` row that is never updated.
"""
from dataclasses import dataclass
from datetime import timedelta
import re
from uuid import uuid4

import sqlalchemy as sa

from ..routing.database import DeliveryStoreError, read_connection, utcnow, write_transaction
from . import policy


TABLE_NAMES = ('batching_numbers', 'batching_changes', 'outbound_batch_members', 'outbound_deliveries',
               'fax_job_bindings', 'case_documents')
_REFERENCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9.:_ -]{0,99}', re.ASCII)
_TABLES = {}


class BatchingInputError(ValueError):
    """One plain sentence for an operator."""


class BatchingConflict(RuntimeError):
    """The setting changed; reload before saving again."""


def tables(engine, connection=None):
    """Reflected tables for one engine, cached; runtime never imports frozen metadata.

    Inside an open transaction pass its ``connection``: reflection then reads
    through it instead of opening another connection.
    """
    key = id(engine)
    cached = _TABLES.get(key)
    if cached is not None and cached[0] is engine:
        return cached[1]
    metadata = sa.MetaData()
    try:
        metadata.reflect(connection if connection is not None else engine, only=list(TABLE_NAMES))
    except sa.exc.SQLAlchemyError:
        raise DeliveryStoreError('Sending-together storage is unavailable.') from None
    result = {name: metadata.tables[name] for name in TABLE_NAMES}
    if len(_TABLES) > 16:
        _TABLES.clear()
    _TABLES[key] = (engine, result)
    return result


@dataclass(frozen=True)
class HoldPlan:
    """What acceptance records for a fax that waits to go with others."""
    phone_number: str
    sender_scope: str
    sender_name: str | None
    pages: int
    urgent: bool
    wait_seconds: int


# Settings -------------------------------------------------------------------------

def _setting_view(row, number):
    if row is None:
        return {'phone_number': number, 'enabled': False, 'max_wait_seconds': policy.DEFAULT_WAIT_SECONDS,
                'max_pages': policy.DEFAULT_MAX_PAGES, 'mixed_senders': False, 'version': 0}
    return {'phone_number': row['phone_number'], 'enabled': bool(row['enabled']),
            'max_wait_seconds': row['max_wait_seconds'], 'max_pages': row['max_pages'],
            'mixed_senders': bool(row['mixed_senders']), 'version': row['version']}


def setting_on(connection, t, number):
    row = connection.execute(sa.select(t['batching_numbers']).where(
        t['batching_numbers'].c.phone_number == number)).mappings().one_or_none()
    return _setting_view(row, number)


class BatchingSettings:
    def __init__(self, engine):
        self.engine = engine
        self.t = tables(engine)

    def get(self, number):
        with read_connection(self.engine) as connection:
            return setting_on(connection, self.t, number)

    def history(self, number, *, limit=20):
        changes = self.t['batching_changes']
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(changes).where(
                changes.c.phone_number == number).order_by(changes.c.created_at.desc(), changes.c.id.desc())
                .limit(limit)).mappings()]

    def save(self, number, *, enabled, actor, actor_name=None, recipient_agreed=False, max_wait_seconds=None,
             max_pages=None, mixed_senders=None, expected_version=None, now=None):
        """Turn sending together on, change it, or turn it off; returns the setting and the action."""
        if type(enabled) is not bool or type(recipient_agreed) is not bool:
            raise BatchingInputError('Choose whether faxes to this number are sent together.')
        if not isinstance(actor, str) or not actor or len(actor) > 100:
            raise BatchingInputError('Faxbot could not tell who made this change.')
        now = now or utcnow()
        numbers, changes = self.t['batching_numbers'], self.t['batching_changes']
        with write_transaction(self.engine) as connection:
            current = setting_on(connection, self.t, number)
            if expected_version is not None and expected_version != current['version']:
                raise BatchingConflict('This number changed; reload and try again.')
            wait = current['max_wait_seconds'] if max_wait_seconds is None else max_wait_seconds
            pages = current['max_pages'] if max_pages is None else max_pages
            mixed = current['mixed_senders'] if mixed_senders is None else mixed_senders
            if type(wait) is not int or not policy.MIN_WAIT_SECONDS <= wait <= policy.MAX_WAIT_SECONDS:
                raise BatchingInputError('A fax can wait between 1 and 60 minutes.')
            if type(pages) is not int or not policy.MIN_PAGES <= pages <= policy.MAX_PAGES:
                raise BatchingInputError('One call can carry between 2 and 200 pages.')
            if type(mixed) is not bool:
                raise BatchingInputError('Choose whether faxes from different senders may share a call.')
            if enabled and not current['enabled'] and not recipient_agreed:
                raise BatchingInputError('Record that the recipient agreed before turning this on.')
            values = {'enabled': int(enabled), 'max_wait_seconds': wait, 'max_pages': pages,
                      'mixed_senders': int(mixed)}
            unchanged = (current['version'] and current['enabled'] == enabled and current['max_wait_seconds'] == wait
                         and current['max_pages'] == pages and current['mixed_senders'] == mixed)
            if unchanged:
                return current, None
            action = 'off' if not enabled else ('on' if not current['enabled'] else 'changed')
            if current['version'] == 0:
                connection.execute(numbers.insert().values(id=uuid4().hex, phone_number=number, version=1,
                                                           created_at=now, updated_at=now, **values))
            else:
                updated = connection.execute(numbers.update().where(
                    numbers.c.phone_number == number, numbers.c.version == current['version']).values(
                        version=current['version'] + 1, updated_at=now, **values))
                if updated.rowcount != 1:
                    raise BatchingConflict('This number changed; reload and try again.')
            connection.execute(changes.insert().values(
                id=uuid4().hex, phone_number=number, action=action, actor=actor,
                actor_name=(actor_name or '')[:200] or None, recipient_agreed=int(enabled),
                max_wait_seconds=wait, max_pages=pages, mixed_senders=int(mixed), created_at=now))
            return setting_on(connection, self.t, number), action


# Holding --------------------------------------------------------------------------

def hold_on(connection, t, job_id, plan, now):
    """Record, inside the acceptance transaction, that this fax waits to go with others."""
    connection.execute(t['outbound_batch_members'].insert().values(
        id=job_id, phone_number=plan.phone_number, sender_scope=plan.sender_scope[:100],
        sender_name=(plan.sender_name or '')[:200] or None, pages=plan.pages, urgent=int(plan.urgent),
        hold_until=now if plan.urgent else now + timedelta(seconds=plan.wait_seconds), state='waiting',
        created_at=now, updated_at=now))


def urge_on(connection, t, job_id, now):
    """Mark a waiting fax "Send now"; its group goes at the next claim. False when it is not waiting."""
    members = t['outbound_batch_members']
    updated = connection.execute(members.update().where(members.c.id == job_id, members.c.state == 'waiting')
                                 .values(urgent=1, hold_until=now, updated_at=now))
    return updated.rowcount == 1


def waiting_ids(t):
    members = t['outbound_batch_members']
    return sa.select(members.c.id).where(members.c.state == 'waiting')


def due_group_on(connection, t, now):
    """The first group of waiting faxes that should go now, in call order, or None.

    A group shares a number, an accepted account and configuration, and a
    sender (unless the number allows mixed senders). It takes faxes in the
    order they were accepted while each fax plus its separator page fits the
    page cap, and is due when its oldest fax's wait has ended, any fax in it is
    marked "Send now", or the next waiting fax would not fit. A number whose
    setting was turned off releases its waiting faxes one at a time.
    """
    members, deliveries, bindings = t['outbound_batch_members'], t['outbound_deliveries'], t['fax_job_bindings']
    rows = connection.execute(
        sa.select(members, deliveries.c.created_at.label('accepted_at'), bindings.c.profile_id,
                  bindings.c.revision_id)
        .select_from(members.join(deliveries, deliveries.c.id == members.c.id)
                     .join(bindings, bindings.c.id == members.c.id))
        .where(members.c.state == 'waiting', deliveries.c.state == 'ready', deliveries.c.dispatch_mode == 'normal')
        .order_by(deliveries.c.created_at, members.c.id).limit(2000)).mappings().all()
    if not rows:
        return None
    numbers = t['batching_numbers']
    settings = {row['phone_number']: row for row in connection.execute(sa.select(numbers).where(
        numbers.c.phone_number.in_({row['phone_number'] for row in rows}))).mappings()}
    groups = {}
    for row in rows:
        setting = settings.get(row['phone_number'])
        if setting is None or not setting['enabled']:
            return [dict(row)]
        sender = '*' if setting['mixed_senders'] else row['sender_scope']
        groups.setdefault((row['phone_number'], row['profile_id'], row['revision_id'], sender), []).append(row)
    for key, group in groups.items():
        cap = settings[key[0]]['max_pages']
        chosen, used = [], 0
        for row in group:
            if chosen and used + row['pages'] + 1 > cap:
                break
            chosen.append(row)
            used += row['pages'] + 1
        due = (len(chosen) < len(group) or any(row['urgent'] for row in group)
               or min(row['hold_until'] for row in chosen) <= now)
        if due:
            return [dict(row) for row in chosen]
    return None


def reference_on(connection, t, job_id):
    """The reference printed on the fax's separator page and shown in Job Details.

    The sender's own case reference when the fax was sent for one case;
    otherwise "Faxbot" and the first eight characters of the fax's ID.
    """
    documents = t['case_documents']
    cases = connection.execute(sa.select(documents.c.case_id).where(
        documents.c.source_job_id == job_id).distinct().limit(2)).scalars().all()
    if len(cases) == 1 and isinstance(cases[0], str) and _REFERENCE.fullmatch(cases[0]):
        return cases[0]
    return 'Faxbot ' + job_id[:8]


def join_on(connection, t, claims, rows, now):
    """Record each fax's place and pages in the one call ``claims[0]`` places."""
    members = t['outbound_batch_members']
    batch, first = claims[0].attempt_id, 1
    for number, (claim, row) in enumerate(zip(claims, rows), start=1):
        last = first + row['pages']  # the separator page, then the fax's own pages
        connection.execute(members.update().where(members.c.id == claim.job_id).values(
            state='together', batch_id=batch, attempt_id=claim.attempt_id, document_number=number,
            documents=len(claims), first_page=first, last_page=last,
            reference=reference_on(connection, t, claim.job_id), updated_at=now))
        first = last + 1


def separate_on(connection, t, job_id, now):
    members = t['outbound_batch_members']
    connection.execute(members.update().where(members.c.id == job_id).values(
        state='separate', batch_id=None, attempt_id=None, document_number=None, documents=None,
        first_page=None, last_page=None, updated_at=now))


def return_to_waiting_on(connection, t, job_id, attempt_id, now):
    """An unsent call's fax waits again (its wait has usually ended, so it goes at the next claim)."""
    members = t['outbound_batch_members']
    connection.execute(members.update().where(
        members.c.id == job_id, members.c.state == 'together', members.c.attempt_id == attempt_id).values(
            state='waiting', batch_id=None, attempt_id=None, document_number=None, documents=None,
            first_page=None, last_page=None, updated_at=now))


# Reads ----------------------------------------------------------------------------

def member(engine, job_id):
    t = tables(engine)
    with read_connection(engine) as connection:
        row = connection.execute(sa.select(t['outbound_batch_members']).where(
            t['outbound_batch_members'].c.id == job_id)).mappings().one_or_none()
        return dict(row) if row is not None else None


def members_for(engine, job_ids):
    if not job_ids:
        return {}
    t = tables(engine)
    members = t['outbound_batch_members']
    with read_connection(engine) as connection:
        return {row['id']: dict(row) for row in connection.execute(
            sa.select(members).where(members.c.id.in_(list(job_ids)))).mappings()}


def call_members(engine, batch_id, connection=None):
    """Every fax the call placed by attempt ``batch_id`` carried, in call order; empty for an ordinary call."""
    t = tables(engine)
    members = t['outbound_batch_members']
    query = (sa.select(members).where(members.c.batch_id == batch_id, members.c.state == 'together')
             .order_by(members.c.document_number))
    if connection is not None:
        return [dict(row) for row in connection.execute(query).mappings()]
    with read_connection(engine) as conn:
        return [dict(row) for row in conn.execute(query).mappings()]


def calls_to(engine, number, since):
    """{call attempt: [faxes in call order]} for calls to ``number`` that carried several faxes since ``since``."""
    t = tables(engine)
    members = t['outbound_batch_members']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(members).where(
            members.c.phone_number == number, members.c.state == 'together', members.c.batch_id.is_not(None),
            members.c.updated_at >= since).order_by(members.c.batch_id, members.c.document_number)).mappings().all()
    calls = {}
    for row in rows:
        calls.setdefault(row['batch_id'], []).append(dict(row))
    return calls
