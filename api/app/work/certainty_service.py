"""Access-checked operations on uncertain sent faxes, for the console and the command line.

Each operation runs in one access transaction, so the visibility check, the
permission check and the change are one decision. An item is visible when the
person may read its fax (``fax:read``); lists and counts apply that before
filters and limits. An invisible item is "not found"; a visible item with a
denied action is "not permitted". The owner may settle, assign back and query
their own item; anyone else needs ``fax:reconcile`` on the fax. Every change
needs the version the person last saw.

Sending anything to the recipient (the receipt query page, or the fax again)
needs the person's request and their own ``fax:send``: the caller passes
``send``, ``routing/submit.accept_generated_fax`` bound to the person and the
active configuration, so the fax goes through the same acceptance as any other
(the sending rules decide its route, and may hold it). Its fax ID is derived
from the item, so a repeated request returns the fax already queued.

A fax whose call broke part way may instead get only its remaining pages
(``routing/continuation.py``): settling it as not delivered with
``continue_from`` sends pages k+1 to n as a new fax, whose ID comes from the
broken attempt, so Sent's own action and this one give the same fax.
"""
from io import BytesIO
import json
from pathlib import Path

import sqlalchemy as sa

from .. import people_time
from ..access.types import ResourceRef
from ..routing.database import utcnow
from . import certainty_checks as checks
from .certainty import (CHANGED, FORBIDDEN, MAX_SETTLE_HOURS, NOT_FOUND, OUTCOMES, SETTLE_PERMISSION,
                        CertaintyConflict, CertaintyForbidden, CertaintyInputError, CertaintyNotFound, _Changed,
                        may_see_on, query_id, resend_id)


VIEWS = ('all', 'mine', 'unassigned', 'overdue')
STATES = ('open', 'settled')
MAX_REASON = 400
CATEGORY_TEXT = {
    'transport_ambiguous': 'The fax service did not confirm it accepted the fax.',
    'response_unusable': "The fax service's answer could not be read.",
    'submission_cancelled': 'Sending was interrupted after the fax went to the fax service.',
    'worker_lost': 'Faxbot stopped while the fax was being sent.',
    'pages_unconfirmed': 'The call ended without confirming which pages arrived.',
    'partly_sent': 'The call failed part way through, so some pages may have arrived.',
}
OWNER_SOURCE_TEXT = {
    'sender': 'the person who sent it',
    'mailbox': 'the backup person of the mailbox it was sent from',
    'fallback': 'the person you chose to settle uncertain faxes',
    'person': 'chosen by a person',
}
OUTCOME_TEXT = {'delivered': 'delivered', 'not_delivered': 'not delivered', 'unknown': "can't tell"}


def _identifier(value):
    return type(value) is str and 0 < len(value) <= 40 and all(32 <= ord(c) < 127 for c in value)


def _pages(number):
    return f"{number} page{'' if number == 1 else 's'}"


def mask(number):
    if not number or len(number) < 4:
        return '****'
    return '*' * (len(number) - 4) + number[-4:]


def category_text(category):
    return CATEGORY_TEXT.get(category, 'Faxbot does not know whether this fax arrived.')


def state_key(item, now):
    if item['state'] == 'settled':
        return 'settled'
    if item['due_at'] is not None and now > item['due_at']:
        return 'overdue'
    return 'assigned' if item['owner_principal_id'] else 'waiting'


def _sent_pages(pages):
    """'Pages 8–20 were sent as a new fax.' for 'pages 8–20'; 'Page 20 was sent ...' for one page."""
    return f"{pages[0].upper()}{pages[1:]} {'was' if pages.startswith('page ') else 'were'} sent as a new fax."


def state_text(item, names, now, continued=None):
    """``continued``: the pages text of the continuation the item was settled with, if it was."""
    key = state_key(item, now)
    owner = names.get(item['owner_principal_id']) or 'someone who is no longer listed'
    if key == 'settled':
        if item['settled_by'] is None and not item['settled_by_name'] and item['settled_reason']:
            return item['settled_reason']  # closed by itself when the fax was delivered: the sentence says how
        who = item['settled_by_name'] or names.get(item['settled_by']) or 'a person'
        text = f"Settled as {OUTCOME_TEXT.get(item['outcome'], 'settled')} by {who}."
        if continued:
            return f'{text} {_sent_pages(continued)}'
        return text + (' The fax was sent again as a new fax.' if item['resend_job_id'] else '')
    if key == 'overdue':
        return f'Overdue; assigned to {owner}.' if item['owner_principal_id'] else 'Overdue; waiting for an owner.'
    if key == 'assigned':
        if item['due_at'] is not None:
            return f"Assigned to {owner}; settle by {people_time.short(item['due_at'])}."
        return f'Assigned to {owner}.'
    return 'Waiting for an owner.'


def event_text(kind, details):
    """One time-free sentence for a history row; names are those stored when it happened."""
    actor = details.get('actor_name') or 'Faxbot'
    if kind == 'opened':
        why = category_text(details.get('category'))
        owner = details.get('owner_name')
        if owner:
            how = OWNER_SOURCE_TEXT.get(details.get('owner_source'), '')
            return f'{why} Faxbot gave it to {owner}' + (f', {how}.' if how else '.')
        return f'{why} Nobody could be given it automatically.'
    if kind == 'assigned':
        return f"{actor} assigned it to {details.get('to_name') or 'someone'}."
    if kind == 'reassigned':
        return f"{actor} moved it from {details.get('from_name') or 'someone'} to {details.get('to_name') or 'someone'}."
    if kind == 'probe':
        if details.get('result') == 'answered':
            held, total = details.get('pages_held'), details.get('total_pages')
            if details.get('status') != 'found' or not held:
                return 'The partner signed that it holds no page of this call.'
            return f'The partner signed that it holds {held} of {total} pages of this call.'
        if details.get('result') == 'no_answer':
            return 'Faxbot asked the partner; it did not answer.'
        return details.get('text') or 'A check found something new.'
    if kind == 'drafted':
        return f'{actor} opened the receipt query page.'
    if kind == 'query_sent':
        return f'{actor} sent the one-page receipt query.'
    if kind == 'settled' and details.get('automatic'):
        return details.get('reason') or 'Closed: the fax was delivered.'
    if kind == 'settled':
        outcome = OUTCOME_TEXT.get(details.get('outcome'), 'settled')
        reason = (details.get('reason') or '').strip()
        text = f'{actor} settled it as {outcome}' + (f': {reason}' if reason else '')
        text = text if text.endswith(('.', '!', '?')) else text + '.'
        if details.get('continuation'):
            return f"{text} {_sent_pages(str(details['continuation']))}"
        return text + (' The fax was sent again as a new fax.' if details.get('resend_job_id') else '')
    if kind == 'escalated':
        if details.get('to_name'):
            return f"Not settled in time; moved to {details['to_name']}."
        if details.get('fallback_name'):
            return (f"Not settled in time; {details['fallback_name']} is the fallback person but cannot see this "
                    'fax, so the owner did not change.')
        return 'Not settled in time; no fallback person is set, so the owner did not change.'
    return kind.replace('_', ' ').capitalize() + '.'


def moved_on(delivery, item):
    """What the fax's own delivery record has said since the item was made, when that changes what to do; or None.

    The item stays open for a person either way; this only stops "send it again" from doubling a fax that is
    already delivered or on its way again.
    """
    if delivery is None or item['state'] != 'open':
        return None
    state, same = delivery['state'], delivery['attempt_id'] == item['attempt_id']
    if state == 'success':
        return {'kind': 'delivered', 'text': 'The fax service has since reported this fax delivered.'}
    if state == 'cancelled':
        return {'kind': 'cancelled', 'text': 'This fax was cancelled since.'}
    if same and state in ('reconciliation_required', 'failed'):
        return None
    if same and state == 'in_progress':
        return {'kind': 'following', 'text': "The fax service's fax ID is recorded, and Faxbot is following this "
                                             'fax with the fax service.'}
    if state == 'failed':
        return {'kind': 'failed', 'text': 'A later attempt to send this fax failed; see its delivery attempts.'}
    return {'kind': 'resent', 'text': 'Faxbot is already sending this fax again by another route, so it is not '
                                      'sent again from here.'}


def receipt_query_page(*, organization, sent_on, pages, reference, reply_number):
    """The one-page receipt query: no part of the document, only the date, page count and reference."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    pdf.setTitle('Did you receive our fax?')
    sender = organization or 'We'
    size = f', {_pages(pages)}' if type(pages) is int and pages > 0 else ''
    back = f'fax this page back to {reply_number}' if reply_number else 'fax this page back to us'
    lines = [
        ('Helvetica-Bold', 20, 'Did you receive our fax?'),
        ('Helvetica', 12, f'{sender} sent a fax to this number on {sent_on}{size}, reference {reference}.'),
        ('Helvetica', 12, f'Please tick one box and {back}.'),
        ('Helvetica', 14, ''),
        ('Helvetica', 14, f'[   ]  Yes, we received all of it{size}.'),
        ('Helvetica', 14, '[   ]  We received only some pages:  ______________________'),
        ('Helvetica', 14, '[   ]  No, we did not receive it.'),
        ('Helvetica', 14, ''),
        ('Helvetica', 12, 'Name: ________________________     Date: ______________'),
        ('Helvetica', 12, ''),
        ('Helvetica-Bold', 28, f'Reference {reference}'),
    ]
    y = 700
    for font, size_points, text in lines:
        pdf.setFont(font, size_points)
        if text:
            pdf.drawString(72, y, text)
        y -= size_points + 16
    pdf.showPage()
    pdf.save()
    return output.getvalue()


class CertaintyService:
    def __init__(self, store, access, *, values, clock=None):
        """``access`` carries the AccessStore (``store``) and AccessControl (``control``)."""
        self.store = store
        self.access_store, self.control = access.store, access.control
        self.values = values
        self.clock = clock or utcnow

    # -- shared ----------------------------------------------------------------------------------
    def _rows(self):
        items, jobs, resources, attempts = (self.store.items, self.store.jobs, self.store.resources,
                                            self.store.attempts)
        mailboxes = self.store.mailboxes
        source = (items.join(jobs, jobs.c.id == items.c.job_id)
                  .join(resources, sa.and_(resources.c.kind == 'outbound', resources.c.fax_job_id == items.c.job_id))
                  .join(attempts, attempts.c.id == items.c.attempt_id)
                  .outerjoin(mailboxes, mailboxes.c.id == items.c.mailbox_id))
        return (sa.select(*items.c, jobs.c.to_number, jobs.c.pages, jobs.c.tiff_path, jobs.c.file_name,
                          jobs.c.created_at.label('fax_created_at'), attempts.c.submitted_at, attempts.c.profile_id,
                          mailboxes.c.label.label('mailbox'), resources.c.id.label('resource_id'))
                .select_from(source))

    def _visible(self, connection, actor, now):
        return self.control.visible_resource_ids_on(connection, actor, 'fax:read', 'outbound', now=now)

    def _among(self, connection, actor, resource_ids, now):
        """Which of these faxes the person may settle for anyone (``fax:reconcile``), in one query."""
        if not resource_ids:
            return set()
        allowed = self.control.visible_resource_ids_on(connection, actor, SETTLE_PERMISSION, 'outbound',
                                                       now=now).subquery()
        return set(connection.execute(sa.select(allowed.c.id).where(allowed.c.id.in_(sorted(resource_ids))))
                   .scalars())

    def _allowed(self, connection, actor, permission, resource_id, now):
        return self.control.authorize_on(connection, actor, permission, ResourceRef(resource_id), now=now).allowed

    def _require(self, connection, actor, item_id, now, *, act=False, manage=False):
        """The visible item; not found unless the person may read its fax, forbidden if the action is denied."""
        self.control._current_source_on(connection, actor, now)
        if not _identifier(item_id):
            raise CertaintyNotFound(NOT_FOUND)
        row = connection.execute(self._rows().where(self.store.items.c.id == item_id)).mappings().one_or_none()
        if row is None or not self._allowed(connection, actor, 'fax:read', row['resource_id'], now):
            raise CertaintyNotFound(NOT_FOUND)
        row = dict(row)
        mine = row['owner_principal_id'] == actor.principal_id
        if manage and not self._allowed(connection, actor, SETTLE_PERMISSION, row['resource_id'], now):
            raise CertaintyForbidden(FORBIDDEN)
        if act and not mine and not self._allowed(connection, actor, SETTLE_PERMISSION, row['resource_id'], now):
            raise CertaintyForbidden('Only the owner, or someone who may confirm receipt of this fax, can do this.')
        return row

    @staticmethod
    def _expect(row, version):
        if type(version) is not int or version < 1:
            raise CertaintyInputError('Send the version of the item you are changing.')
        if version != row['version']:
            raise CertaintyConflict(CHANGED)

    def _delivery(self, connection, job_id):
        row = connection.execute(sa.select(self.store.deliveries.c.state, self.store.deliveries.c.attempt_id).where(
            self.store.deliveries.c.id == job_id)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def _organization(self):
        return (getattr(self.values(), 'direct_organization', '') or '').strip()

    def _sent_at(self, row):
        return row['submitted_at'] or row['fax_created_at']

    def _checks(self, connection, row, now, *, number):
        sources = self.store.sources
        job = {'to_number': row['to_number'], 'pages': row['pages'], 'tiff_path': row['tiff_path']}
        answer, _ = self.store.partner_answer_on(connection, row['id'])
        query_job = self.store.job_on(connection, row['query_job_id']) if row['query_job_id'] else None
        if row['query_job_id']:
            sent = connection.execute(sa.select(self.store.events.c.created_at).where(
                self.store.events.c.item_id == row['id'], self.store.events.c.kind == 'query_sent')
                .order_by(self.store.events.c.created_at).limit(1)).scalar()
            row = {**row, 'query_sent_at': sent}
        when = people_time.short(self._sent_at(row))
        return [
            checks.partner_check(sources, connection, row, job, answer),
            checks.call_record_check(sources, connection, row, job, now=now),
            checks.receipt_query_check(row, query_job),
            checks.phone_check(row, job, organization=self._organization(), number=number or mask(row['to_number']),
                               when_text=when),
        ]

    # -- continuations (routing/continuation.py) -------------------------------------------------------
    def _continuations(self):
        """The continuation store, or None on an installation without its tables."""
        found = getattr(self, '_continuation_store', None)
        if found is None:
            try:
                from ..routing.continuation import ContinuationStore
                found = ContinuationStore(self.store.engine)
            except Exception:
                found = False
            self._continuation_store = found
        return found or None

    def _data_dir(self):
        return getattr(self.values(), 'fax_data_dir', '') or '.'

    def _continuation_view(self, connection, row, link, *, may_act):
        """What the item says about sending only the remaining pages: sent, offered, why not, or None."""
        from ..routing.continuation import offer_on, pages_text
        if link is not None:
            return {'state': 'sent', 'fax_id': link['continuation_job_id'], 'first_page': link['first_page'],
                    'last_page': link['last_page'], 'pages_text': pages_text(link['first_page'], link['last_page'])}
        store = self._continuations()
        if store is None or row['state'] != 'open':
            return None
        offer = offer_on(store, connection, row['job_id'], data_dir=self._data_dir())
        if offer is None or offer.attempt_id != row['attempt_id']:
            return None
        if not offer.available:
            return {'state': 'unavailable', 'reason': offer.reason} if row['category'] == 'partly_sent' else None
        first, last = offer.first_page, offer.last_page
        return {'state': 'offered', 'first_page': first, 'last_page': last, 'pages': last - first + 1,
                'pages_text': pages_text(first, last), 'action': f'Send {pages_text(first, last)}',
                'basis': offer.confirmed.sentence,
                'warning': f'Page {first} may already have arrived, so the recipient may get it twice.',
                'may_send': may_act, 'to_number': row['to_number'], 'total_pages': last}

    def _with_cost(self, actor, views):
        """Price each offered continuation on the account the rules would choose, outside the transaction."""
        from ..routing.continuation import cost
        for view in views:
            offer = view.get('continuation')
            if not offer or offer.get('state') != 'offered':
                continue
            number = offer.pop('to_number', None)
            sentence, account, money = cost(self.store.engine, self.values(), actor, to_number=number,
                                            pages=offer['pages'], total=offer['total_pages'])
            offer.update({'cost_text': sentence, 'cost_account': account, 'cost': money})
        return views

    def _views(self, connection, actor, rows, now, *, detail=False):
        rows = [dict(row) for row in rows]
        names = self.store.names_on(connection, [row[field] for row in rows for field in
                                                 ('owner_principal_id', 'settled_by')])
        manage = self._among(connection, actor, {row['resource_id'] for row in rows}, now)
        continuations = self._continuations()
        links = continuations.links_for(connection, [row['resend_job_id'] for row in rows]) if continuations else {}
        views = []
        for row in rows:
            mine = row['owner_principal_id'] == actor.principal_id
            may_act = mine or row['resource_id'] in manage
            actions = []
            if row['state'] == 'open' and row['resource_id'] in manage:
                actions.append('assign')
            if row['state'] == 'open' and may_act:
                actions.append('settle')
                if not row['query_job_id']:
                    actions.append('send_query')
            person = lambda identity: {'id': identity, 'name': names.get(identity)} if identity else None  # noqa: E731
            link = links.get(row['resend_job_id'])
            continued = None
            if link is not None:
                from ..routing.continuation import pages_text
                continued = pages_text(link['first_page'], link['last_page'])
            view = {
                'id': row['id'], 'fax_id': row['job_id'], 'reference': row['reference'], 'state': row['state'],
                'state_key': state_key(row, now), 'state_text': state_text(row, names, now, continued),
                'why': category_text(row['category']), 'category': row['category'],
                'to_number': mask(row['to_number']), 'pages': row['pages'], 'sent_at': self._sent_at(row),
                'mailbox': row['mailbox'], 'owner': person(row['owner_principal_id']),
                'owner_source_text': OWNER_SOURCE_TEXT.get(row['owner_source']),
                'due_at': row['due_at'], 'due_hours': row['due_hours'], 'escalated_at': row['escalated_at'],
                'overdue': row['state'] == 'open' and row['due_at'] is not None and now > row['due_at'],
                'outcome': row['outcome'], 'settled_by': row['settled_by_name'] or names.get(row['settled_by']),
                'settled_at': row['settled_at'], 'settled_reason': row['settled_reason'],
                'resend_fax_id': row['resend_job_id'], 'query_fax_id': row['query_job_id'],
                'is_mine': mine, 'version': row['version'], 'actions': actions,
            }
            moved = moved_on(self._delivery(connection, row['job_id']), row)
            view['moved_on'] = moved
            if detail:
                # The full number only for people who may act on the item: they need it to phone the recipient.
                found = self._checks(connection, row, now, number=row['to_number'] if may_act else None)
                view['checks'] = found
                view['suggestion'] = None if row['state'] != 'open' else (
                    checks.suggestion(found) if moved is None else ('delivered' if moved['kind'] == 'delivered'
                                                                     else None))
                if may_act:
                    view['number'] = row['to_number']
                view['continuation'] = self._continuation_view(connection, row, link, may_act=may_act)
                if (view['continuation'] or {}).get('state') == 'offered' and may_act:
                    actions.append('continue')
            elif link is not None:
                view['continuation'] = self._continuation_view(connection, row, link, may_act=may_act)
            views.append(view)
        return views

    # -- reads -----------------------------------------------------------------------------------
    def list(self, actor, *, view='all', state='open', limit=100, fax_id=None):
        if view not in VIEWS or (state is not None and state not in STATES) or type(limit) is not int \
                or not 1 <= limit <= 200 or (fax_id is not None and not _identifier(fax_id)):
            raise CertaintyInputError('Choose a valid filter.')
        items = self.store.items
        with self.access_store.transaction() as connection:
            now = self.clock()
            self.control._current_source_on(connection, actor, now)
            query = self._rows().where(self.store.resources.c.id.in_(self._visible(connection, actor, now)))
            if state is not None:
                query = query.where(items.c.state == state)
            if fax_id is not None:
                query = query.where(items.c.job_id == fax_id)
            if view == 'mine':
                query = query.where(items.c.owner_principal_id == actor.principal_id)
            elif view == 'unassigned':
                query = query.where(items.c.owner_principal_id.is_(None))
            elif view == 'overdue':
                query = query.where(items.c.state == 'open', items.c.due_at.is_not(None), items.c.due_at < now)
            query = query.order_by(sa.case((items.c.state == 'open', 0), else_=1), items.c.due_at.is_(None),
                                   items.c.due_at, items.c.created_at.desc(), items.c.id).limit(limit)
            return self._views(connection, actor, connection.execute(query).mappings().all(), now)

    def counts(self, actor):
        items = self.store.items
        with self.access_store.transaction() as connection:
            now = self.clock()
            self.control._current_source_on(connection, actor, now)
            base = self._rows().with_only_columns(sa.func.count()).where(
                self.store.resources.c.id.in_(self._visible(connection, actor, now)))

            def count(*conditions):
                return connection.execute(base.where(*conditions)).scalar_one()
            return {
                'open': count(items.c.state == 'open'),
                'mine': count(items.c.state == 'open', items.c.owner_principal_id == actor.principal_id),
                'unassigned': count(items.c.state == 'open', items.c.owner_principal_id.is_(None)),
                'overdue': count(items.c.state == 'open', items.c.due_at.is_not(None), items.c.due_at < now),
                'settled': count(items.c.state == 'settled'),
            }

    def _detail_on(self, connection, actor, item_id, now):
        row = self._require(connection, actor, item_id, now)
        return self._views(connection, actor, [row], now, detail=True)[0]

    def detail(self, actor, item_id):
        with self.access_store.transaction() as connection:
            view = self._detail_on(connection, actor, item_id, self.clock())
        return self._with_cost(actor, [view])[0]

    def for_fax(self, actor, fax_id):
        """Every item of one sent fax the person can see, newest first, each with its checks."""
        if not _identifier(fax_id):
            raise CertaintyNotFound(NOT_FOUND)
        items = self.store.items
        with self.access_store.transaction() as connection:
            now = self.clock()
            self.control._current_source_on(connection, actor, now)
            rows = connection.execute(self._rows().where(
                items.c.job_id == fax_id, self.store.resources.c.id.in_(self._visible(connection, actor, now)))
                .order_by(items.c.created_at.desc(), items.c.id)).mappings().all()
            # A fax sent again from an item, or a receipt query, points back to the fax it is about.
            origin = connection.execute(sa.select(items.c.job_id, items.c.resend_job_id, items.c.query_job_id).where(
                sa.or_(items.c.resend_job_id == fax_id, items.c.query_job_id == fax_id))).first()
            views = self._views(connection, actor, rows, now, detail=True)
            linked = None
            continuations = self._continuations()
            if continuations is not None and continuations.link_for_continuation(connection, fax_id) is not None:
                origin = None  # a continuation's link both ways is Sent's continuation section
            # Only a fax this person may read is named.
            if origin is not None and self._allowed(connection, actor, 'fax:read',
                                                    self.store.resource_of(connection, origin.job_id), now):
                linked = {'fax_id': origin.job_id,
                          'kind': 'resend' if origin.resend_job_id == fax_id else 'receipt_query'}
        return {'items': self._with_cost(actor, views), 'about': linked}

    def history(self, actor, item_id):
        events = self.store.events
        with self.access_store.transaction() as connection:
            self._require(connection, actor, item_id, self.clock())
            rows = connection.execute(sa.select(events).where(events.c.item_id == item_id)
                                      .order_by(events.c.created_at, events.c.id)).mappings().all()
        result = []
        for row in rows:
            try:
                details = json.loads(row['details'] or '{}')
            except ValueError:
                details = {}
            result.append({'kind': row['kind'], 'at': row['created_at'], 'actor': details.get('actor_name'),
                           'text': event_text(row['kind'], details)})
        return result

    def _people(self, connection, check):
        principals, users = self.store.principals, self.store.users
        rows = connection.execute(sa.select(principals.c.id, principals.c.display_name, users.c.login)
                                  .select_from(principals.join(users, users.c.id == principals.c.id))
                                  .where(principals.c.kind == 'user', principals.c.enabled == 1)
                                  .order_by(principals.c.display_name, principals.c.id).limit(500)).all()
        return [{'id': row.id, 'name': row.display_name, 'login': row.login} for row in rows if check(row.id)]

    def assignees(self, actor, item_id):
        with self.access_store.transaction() as connection:
            row = self._require(connection, actor, item_id, self.clock(), manage=True)
            return self._people(connection, lambda principal: may_see_on(self.control, connection, principal,
                                                                         row['resource_id']))

    # -- changes ---------------------------------------------------------------------------------
    def _change(self, connection, actor, row, values, *, kind, details, now):
        name = self.store.names_on(connection, [actor.principal_id]).get(actor.principal_id)
        try:
            self.store.change_on(connection, row, values, kind=kind, actor_id=actor.principal_id,
                                 details={'actor_name': name, **details}, now=now)
        except _Changed:
            raise CertaintyConflict(CHANGED) from None
        return self._detail_on(connection, actor, row['id'], now)

    def assign(self, actor, item_id, principal_id, *, version):
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, item_id, now, manage=True)
            self._expect(row, version)
            if row['state'] != 'open':
                raise CertaintyConflict('This fax is already settled.')
            if not _identifier(principal_id):
                raise CertaintyInputError('Choose a person to assign.')
            names = self.store.names_on(connection, [principal_id, row['owner_principal_id']])
            if not may_see_on(self.control, connection, principal_id, row['resource_id']):
                who = names.get(principal_id) or 'That person'
                raise CertaintyInputError(f'{who} cannot see this fax, so it cannot be assigned to them.')
            previous = row['owner_principal_id']
            if previous == principal_id:
                return self._detail_on(connection, actor, item_id, now)
            return self._change(connection, actor, row, {'owner_principal_id': principal_id, 'owner_source': 'person'},
                                kind='assigned' if previous is None else 'reassigned',
                                details={'from_name': names.get(previous), 'to_name': names.get(principal_id)},
                                now=now)

    def _reply_number(self, row):
        try:
            from ..routing.reply_number import choose
            return choose(self.values(), engine=self.store.engine, mailbox_id=row['mailbox_id']).number
        except Exception:
            return None

    def draft(self, actor, item_id):
        """The receipt query page (a PDF) and its file name; viewing it sends nothing."""
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, item_id, now, act=True)
            sent_on = people_time.date_and_time(self._sent_at(row)).split(' at ')[0]
            document = receipt_query_page(organization=self._organization(), sent_on=sent_on, pages=row['pages'],
                                          reference=row['reference'], reply_number=self._reply_number(row))
            if not connection.execute(sa.select(self.store.events.c.id).where(
                    self.store.events.c.item_id == item_id, self.store.events.c.dedupe_key == 'drafted')).first():
                name = self.store.names_on(connection, [actor.principal_id]).get(actor.principal_id)
                self.store.event_on(connection, item_id, 'drafted', actor_id=actor.principal_id, now=now,
                                    details={'actor_name': name}, dedupe_key='drafted')
            return document, f"receipt-query-{row['reference']}.pdf", row

    def send_query(self, actor, item_id, *, version, send):
        """Send the receipt query page to the recipient, once, at this person's request."""
        with self.access_store.transaction() as connection:
            row = self._require(connection, actor, item_id, self.clock(), act=True)
            self._expect(row, version)
            if row['state'] != 'open':
                raise CertaintyConflict('This fax is already settled.')
            if row['query_job_id']:
                raise CertaintyConflict('The receipt query for this fax was already sent.')
        document, name, row = self.draft(actor, item_id)
        job_id = query_id(item_id)
        send(to_number=row['to_number'], document=document, file_name=name, pages=1, job_id=job_id)
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, item_id, now, act=True)
            if row['query_job_id'] == job_id:
                return self._detail_on(connection, actor, item_id, now)
            if self.store.job_on(connection, job_id) is None:
                raise CertaintyConflict('The receipt query is already being sent; reload in a moment.')
            return self._change(connection, actor, row, {'query_job_id': job_id}, kind='query_sent',
                                details={'fax_id': job_id}, now=now)

    def settle(self, actor, item_id, *, outcome, reason, version, send_again=False, send=None, continue_from=None):
        """A person decides what happened: delivered, not delivered (and maybe send again), or can't tell.

        ``continue_from``: with not delivered, send only pages ``continue_from`` to the end as a new fax
        (``routing/continuation.py``); the page the person saw, so a changed offer is refused, never guessed.
        """
        if outcome not in OUTCOMES:
            raise CertaintyInputError("Choose delivered, not delivered or can't tell.")
        reason = ' '.join(str(reason or '').split())
        if len(reason) < 3:
            raise CertaintyInputError('Say in a few words how you know, for example who you spoke to.')
        if len(reason) > MAX_REASON:
            raise CertaintyInputError(f'Keep the reason to {MAX_REASON} characters.')
        if send_again and outcome != 'not_delivered':
            raise CertaintyInputError('Only a fax that did not arrive is sent again.')
        if continue_from is not None and (send_again or outcome != 'not_delivered'):
            raise CertaintyInputError('Send either the whole fax again or only its remaining pages, with not '
                                      'delivered.')
        resend = prepared = None
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, item_id, now, act=True)
            self._expect(row, version)
            if row['state'] != 'open':
                raise CertaintyConflict('This fax is already settled.')
        if send_again:
            if send is None:
                raise CertaintyConflict('Sending again is not available here.')
            with self.access_store.transaction() as connection:
                moved = moved_on(self._delivery(connection, row['job_id']), row)
            if moved is not None:
                raise CertaintyConflict(moved['text'])
            folder = Path(getattr(self.values(), 'fax_data_dir', '') or '.')
            source = folder / f"{row['job_id']}.pdf"
            if source.is_symlink() or not source.is_file():
                raise CertaintyConflict('The document of this fax is no longer kept, so it cannot be sent again '
                                        'from here; send it from Send a fax.')
            resend = resend_id(item_id)
            send(to_number=row['to_number'], document=source.read_bytes(), file_name=row['file_name'] or 'fax.pdf',
                 pages=row['pages'], job_id=resend)
        if continue_from is not None:
            prepared = self._prepare_continuation(row, continue_from, send)
            resend = prepared.job_id
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, item_id, now, act=True)
            if row['state'] != 'open':
                raise CertaintyConflict('This fax is already settled.')
            if resend is not None and self.store.job_on(connection, resend) is None:
                raise CertaintyConflict('The new fax is already being sent; reload in a moment.')
            found = self._checks(connection, row, now, number=None)
            name = self.store.names_on(connection, [actor.principal_id]).get(actor.principal_id)
            values = {'state': 'settled', 'outcome': outcome, 'settled_by': actor.principal_id,
                      'settled_by_name': (name or '')[:200] or None, 'settled_at': now, 'settled_reason': reason}
            if resend is not None:
                values['resend_job_id'] = resend
            details = {'outcome': outcome, 'reason': reason, 'resend_job_id': resend,
                       'evidence': checks.snapshot(found)}
            if prepared is not None:
                from ..routing.continuation import pages_text
                details['continuation'] = pages_text(prepared.first_page, prepared.last_page)
                self._continuations().record_on(connection, prepared, actor_id=actor.principal_id, actor_name=name,
                                                item_id=item_id, reason=reason, now=now)
            view = self._change(connection, actor, row, values, kind='settled', now=now, details=details)
        return self._with_cost(actor, [view])[0]

    def _prepare_continuation(self, row, first_page, send):
        """Check, build and send the remaining pages of this item's broken call; the ``Prepared`` continuation."""
        from ..routing.continuation import ContinuationError, offer_on, prepare
        store = self._continuations()
        if send is None or store is None:
            raise CertaintyConflict('Sending only the remaining pages is not available here.')
        with self.access_store.transaction() as connection:
            offer = offer_on(store, connection, row['job_id'], data_dir=self._data_dir())
        if offer is None or offer.attempt_id != row['attempt_id']:
            raise CertaintyConflict('This fax did not break part way through a call, so there are no remaining pages '
                                    'to send.')
        if not offer.available:
            raise CertaintyConflict(offer.reason)
        if type(first_page) is not int or offer.first_page != first_page:
            raise CertaintyConflict('The pages to send changed; reload and check them again.')
        try:
            prepared = prepare(offer, data_dir=self._data_dir())
        except ContinuationError as error:
            raise CertaintyConflict(error.message) from None
        send(to_number=prepared.to_number, document=prepared.document, file_name=prepared.file_name,
             pages=prepared.pages, job_id=prepared.job_id)
        return prepared

    # -- settings --------------------------------------------------------------------------------
    def _fallback_people(self, connection):
        context_check = self.control

        def covers(principal):
            from .store import _target
            context, source = _target(context_check, principal)
            return connection.execute(context_check._coverage_query(context, source, 'fax:read', 'installation')
                                      ).first() is not None
        return self._people(connection, covers)

    def settings(self, actor):
        with self.access_store.transaction() as connection:
            self.control._current_source_on(connection, actor, self.clock())
            current = self.store.settings_on(connection)
            names = self.store.names_on(connection, [current['fallback_principal_id']])
            fallback = current['fallback_principal_id']
            return {'settle_hours': current['settle_hours'], 'version': current['version'],
                    'fallback': {'id': fallback, 'name': names.get(fallback)} if fallback else None,
                    'people': self._fallback_people(connection)}

    def update_settings(self, actor, *, fallback_principal_id, settle_hours, version):
        if type(settle_hours) is not int or not 0 <= settle_hours <= MAX_SETTLE_HOURS:
            raise CertaintyInputError(f'Choose from 0 to {MAX_SETTLE_HOURS} hours; 0 means no deadline.')
        if fallback_principal_id is not None and not _identifier(fallback_principal_id):
            raise CertaintyInputError('Choose a person.')
        with self.access_store.transaction() as connection:
            now = self.clock()
            self.control._current_source_on(connection, actor, now)
            if fallback_principal_id is not None and fallback_principal_id not in {
                    person['id'] for person in self._fallback_people(connection)}:
                raise CertaintyInputError('That person cannot see every sent fax, so they cannot settle them all.')
            try:
                self.store.save_settings_on(connection, fallback_principal_id=fallback_principal_id,
                                            settle_hours=settle_hours, version=version, now=now)
            except _Changed:
                raise CertaintyConflict('These settings changed; reload and try again.') from None
        return self.settings(actor)
