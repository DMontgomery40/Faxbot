"""The per-number setting, its append-only history, and one row per fax held to go together.

Writes to the per-fax rows happen inside the delivery store's installation
transaction (acceptance, claims, recovery), so a fax is never both waiting and
claimed. The per-number setting is ordinary settings with a version; every
change also appends a ``batching_changes`` row that is never updated.

A shared call's layout (a separator page before each document, one index
page listing every document's pages, or a line at the top of every page) is
fixed when the call is formed, from the number's setting then, and stored on
each fax's row; everything after (the call image, outcomes, charge shares)
reads that stored layout.
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
                'max_pages': policy.DEFAULT_MAX_PAGES, 'mixed_senders': False,
                'boundaries': policy.LAYOUT_SEPARATORS, 'version': 0}
    return {'phone_number': row['phone_number'], 'enabled': bool(row['enabled']),
            'max_wait_seconds': row['max_wait_seconds'], 'max_pages': row['max_pages'],
            'mixed_senders': bool(row['mixed_senders']), 'boundaries': layout_of(row), 'version': row['version']}


def layout_of(setting):
    """How a call formed now under ``setting`` (a ``batching_numbers`` row or its view) marks documents."""
    value = setting['boundaries']
    return value if value in policy.LAYOUTS else policy.LAYOUT_SEPARATORS


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

    def boundaries_agreement(self, number, boundaries):
        """The change that recorded the recipient's agreement to ``boundaries``, or None for separators.

        Changing how documents are marked ends that agreement, and choosing it again needs a new one, so
        while ``boundaries`` is in use, the latest agreement recorded for it is the one in force.
        """
        if boundaries not in policy.AGREEMENTS:
            return None
        changes = self.t['batching_changes']
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(changes).where(
                changes.c.phone_number == number, changes.c.boundaries_agreed == 1,
                changes.c.boundaries == boundaries).order_by(
                changes.c.created_at.desc(), changes.c.id.desc()).limit(1)).mappings().one_or_none()
            return dict(row) if row is not None else None

    def save(self, number, *, enabled, actor, actor_name=None, recipient_agreed=False, max_wait_seconds=None,
             max_pages=None, mixed_senders=None, boundaries=None, boundaries_agreed=False, expected_version=None,
             now=None):
        """Turn sending together on, change it, or turn it off; returns the setting and the action.

        ``boundaries`` (None keeps it) chooses how a shared call marks each document: 'separators',
        'index_page' or 'page_headers'. Choosing anything but separators needs ``boundaries_agreed``
        (the recipient agreed to that convention). Turning sending together off goes back to separators.
        """
        if type(enabled) is not bool or type(recipient_agreed) is not bool:
            raise BatchingInputError('Choose whether faxes to this number are sent together.')
        if (boundaries is not None and boundaries not in policy.LAYOUTS) or type(boundaries_agreed) is not bool:
            raise BatchingInputError('Choose a separator page, one index page or marks at the top of every page.')
        if boundaries in policy.AGREEMENTS and not enabled:
            raise BatchingInputError('Turn on sending together before choosing how documents are marked.')
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
            marks = (current['boundaries'] if boundaries is None else boundaries) if enabled else policy.LAYOUT_SEPARATORS
            agreed_now = marks in policy.AGREEMENTS and marks != current['boundaries']
            if agreed_now and not boundaries_agreed:
                raise BatchingInputError(policy.REFUSALS[marks])
            values = {'enabled': int(enabled), 'max_wait_seconds': wait, 'max_pages': pages,
                      'mixed_senders': int(mixed), 'boundaries': marks}
            unchanged = (current['version'] and current['enabled'] == enabled and current['max_wait_seconds'] == wait
                         and current['max_pages'] == pages and current['mixed_senders'] == mixed
                         and current['boundaries'] == marks)
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
                # Only a save that itself records the recipient's agreement says so; a later change does not.
                actor_name=(actor_name or '')[:200] or None, recipient_agreed=int(bool(enabled and recipient_agreed)),
                max_wait_seconds=wait, max_pages=pages, mixed_senders=int(mixed), boundaries=marks,
                # Only the save that chooses an index page or page marks records the recipient's agreement to it.
                boundaries_agreed=int(agreed_now), created_at=now))
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


def due_group_on(connection, t, now, values=None):
    """The first group of waiting faxes that should go now, in call order, or None.

    A group shares a number, an accepted account and configuration, and a
    sender (unless the number allows mixed senders). It takes faxes in the
    order they were accepted while they fit the page cap, and is due when its
    oldest fax's wait has ended, any fax in it is marked "Send now", or the
    next waiting fax would not fit. With separators each fax counts its pages
    plus its separator page; with one index page the call counts that page
    once and lists at most ``policy.INDEX_PAGE_DOCUMENTS`` faxes; with marks
    at the top of every page each fax counts only its own pages. Marks at the
    top of every page are used only while the active settings (``values``)
    make every page's header show the sender and the sending number
    (``policy.header_identifies_sender``); otherwise the call uses separators.
    Each row returned carries the call's ``layout``. A number whose setting
    was turned off releases its waiting faxes one at a time. A group with a
    fax its recipient's schedule holds (``capacity.Capacity.held``) is passed
    over for now, so it never keeps another number's group waiting.
    """
    held = _held_by_schedule(connection, now)
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
            if row['id'] in held:
                continue
            return [dict(row)]
        sender = '*' if setting['mixed_senders'] else row['sender_scope']
        groups.setdefault((row['phone_number'], row['profile_id'], row['revision_id'], sender), []).append(row)
    for key, group in groups.items():
        if any(row['id'] in held for row in group):
            continue
        setting = settings[key[0]]
        cap, layout = setting['max_pages'], layout_of(setting)
        if layout == policy.LAYOUT_PAGE_HEADERS and not policy.header_identifies_sender(values):
            layout = policy.LAYOUT_SEPARATORS  # 47 CFR 68.318(d): each page's header must name the sender
        index = layout == policy.LAYOUT_INDEX_PAGE
        chosen, used = [], 1 if index else 0
        for row in group:
            need = row['pages'] + (1 if layout == policy.LAYOUT_SEPARATORS else 0)
            if chosen and (used + need > cap or (index and len(chosen) >= policy.INDEX_PAGE_DOCUMENTS)):
                break
            chosen.append(row)
            used += need
        due = (len(chosen) < len(group) or any(row['urgent'] for row in group)
               or min(row['hold_until'] for row in chosen) <= now)
        if due:
            return [{**row, 'layout': layout} for row in chosen]
    return None


def _held_by_schedule(connection, now):
    """Faxes the claim found held by their recipient's schedule (routing/schedule.py), read through ``connection``."""
    try:
        from ..capacity import for_engine
        return set(for_engine(connection.engine, connection)._held(now))
    except Exception:
        return set()


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
    """Record each fax's place, pages and the call's layout in the one call ``claims[0]`` places.

    With separators a fax's ``first_page`` is its separator page; with one index page (page 1 of
    the call) or marks at the top of every page, ``first_page`` is the fax's own first page.
    """
    members = t['outbound_batch_members']
    layout = rows[0].get('layout') or policy.LAYOUT_SEPARATORS
    separators = layout == policy.LAYOUT_SEPARATORS
    batch, first = claims[0].attempt_id, 2 if layout == policy.LAYOUT_INDEX_PAGE else 1
    for number, (claim, row) in enumerate(zip(claims, rows), start=1):
        # Separators: the separator page, then the fax's own pages. Otherwise: the fax's own pages only.
        last = first + row['pages'] if separators else first + row['pages'] - 1
        connection.execute(members.update().where(members.c.id == claim.job_id).values(
            state='together', batch_id=batch, attempt_id=claim.attempt_id, document_number=number,
            documents=len(claims), first_page=first, last_page=last, layout=layout,
            reference=reference_on(connection, t, claim.job_id), updated_at=now))
        first = last + 1


def separate_on(connection, t, job_id, now):
    members = t['outbound_batch_members']
    connection.execute(members.update().where(members.c.id == job_id).values(
        state='separate', batch_id=None, attempt_id=None, document_number=None, documents=None,
        first_page=None, last_page=None, layout=None, updated_at=now))


def return_to_waiting_on(connection, t, job_id, attempt_id, now):
    """An unsent call's fax waits again (its wait has usually ended, so it goes at the next claim)."""
    members = t['outbound_batch_members']
    connection.execute(members.update().where(
        members.c.id == job_id, members.c.state == 'together', members.c.attempt_id == attempt_id).values(
            state='waiting', batch_id=None, attempt_id=None, document_number=None, documents=None,
            first_page=None, last_page=None, layout=None, updated_at=now))


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
    """{call attempt: [faxes in call order]} for calls to ``number`` that carried several faxes since ``since``.

    Each fax's row also carries its ``delivery_state``.
    """
    t = tables(engine)
    members, deliveries = t['outbound_batch_members'], t['outbound_deliveries']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(members, deliveries.c.state.label('delivery_state')).select_from(
            members.outerjoin(deliveries, deliveries.c.id == members.c.id)).where(
            members.c.phone_number == number, members.c.state == 'together', members.c.batch_id.is_not(None),
            members.c.updated_at >= since).order_by(members.c.batch_id, members.c.document_number)).mappings().all()
    calls = {}
    for row in rows:
        calls.setdefault(row['batch_id'], []).append(dict(row))
    return calls
