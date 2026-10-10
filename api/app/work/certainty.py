"""Uncertain sent faxes become owned items with checks ranked by cost (D19, M19).

Rules this module keeps:

- **One item per uncertain attempt.** An attempt is uncertain when its fax
  waits for reconciliation with the attempt's outcome unknown, or when the fax
  failed part way through its call (``partly_sent``: some pages may have
  arrived). Imported faxes with no delivery record, held test faxes, the
  receipt queries this module sends and the direct path's notice faxes get
  none.
- **An owner who can see the fax.** The person who sent it; else the backup
  person of the mailbox it was sent from (Work settings); else the
  installation's fallback person (``certainty_settings``); else nobody until a
  person assigns it. Assignment never grants access: the owner must already
  hold ``fax:read`` on the fax.
- **A deadline, set once** when the item is made, from the installation's
  hours (24 when never set; 0 means none). It counts from when Faxbot made the
  item, so faxes that were uncertain before this update do not all arrive
  overdue. An open item past its deadline is escalated once, to the fallback
  person when they can see the fax.
- **Only a person settles it**, unless the fax's own delivery record comes to
  say delivered by any path (a partner completing a broken call, a late result
  from the fax service, a confirmed receipt): then the item closes by itself
  with that sentence ("Closed: the fax was delivered."). Otherwise a person
  decides: delivered, not delivered (optionally sending
  the same document again as a new fax, linked both ways), or can't tell, with
  who decided, why, and what the checks showed then. The fax's own delivery
  record is never changed here; confirming receipt with the provider's fax ID
  stays a separate step.
- **Nothing reaches the recipient without a person.** The automatic checks
  only read stored records, or ask an enrolled partner once about a call
  (``PartnerQuestion``); the receipt query page and a new fax go only from a
  person's request (``send_query``, ``settle``), each under a fax ID derived
  from the item, so a repeated click cannot send twice.
- The item's owner may settle and query their own item; anyone else needs
  ``fax:reconcile`` on the fax. Every change is a compare-and-set on
  ``version`` together with its event.
"""
from datetime import timedelta
import hashlib
import json
import secrets
from uuid import uuid4

import sqlalchemy as sa

from ..routing.database import DeliveryStoreError, read_connection, reflect, utcnow, write_transaction
from . import certainty_checks as checks
from .store import _target


TABLES = ('certainty_items', 'certainty_events', 'certainty_settings', 'outbound_deliveries', 'outbound_attempts',
          'fax_jobs', 'access_resources', 'access_principals', 'access_users', 'mailboxes', 'work_mailbox_settings')
OUTCOMES = ('delivered', 'not_delivered', 'unknown')
DEFAULT_SETTLE_HOURS = 24
MAX_SETTLE_HOURS = 720
MAX_DETAILS = 4000
# A fax that failed part way through its call: pages may have arrived, so it waits for a person too.
PARTLY_SENT = 'partly_sent'
# A person or a voice line answered (sip_calls.PERSON_ANSWERED): nothing arrived, and nobody calls the number again
# until a person checks it with the recipient (research N9).
PERSON_ANSWERED = 'person_answered'
# The number answered as another fax machine and the station check refused it before any page (routing/stations.py).
WRONG_STATION = 'wrong_station'
# Short codes people read out and type: no 0/O, 1/I, 2/Z, 5/S, 8/B.
REFERENCE_LETTERS = 'ACDEFGHJKMNPQRTUVWXY34679'
REFERENCE_LENGTH = 6
NOT_FOUND = 'This uncertain fax was not found.'
CHANGED = 'This item changed; reload and try again.'
FORBIDDEN = 'You do not have permission to do this.'
SETTLE_PERMISSION = 'fax:reconcile'


class CertaintyError(Exception):
    status = 400

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class CertaintyInputError(CertaintyError):
    status = 400


class CertaintyForbidden(CertaintyError):
    status = 403


class CertaintyNotFound(CertaintyError):
    status = 404


class CertaintyConflict(CertaintyError):
    status = 409


class _Changed(RuntimeError):
    """The item changed between reading and writing; nothing was written."""


def details_json(details):
    """Bounded JSON of plain values for one event; never secrets or document content."""
    text = json.dumps({key: value for key, value in details.items() if value not in (None, '')},
                      ensure_ascii=True, separators=(',', ':'), sort_keys=True, default=str)
    if len(text) > MAX_DETAILS:
        raise ValueError('event details are too large')
    return text


def resend_id(item_id):
    """The new fax's ID for an item's "send again": the same on every click."""
    return hashlib.sha256(f'faxbot-resend|{item_id}'.encode('ascii')).hexdigest()[:32]


def query_id(item_id):
    """The receipt query fax's ID for an item: the same on every click."""
    return hashlib.sha256(f'faxbot-receipt-query|{item_id}'.encode('ascii')).hexdigest()[:32]


def new_reference():
    return ''.join(secrets.choice(REFERENCE_LETTERS) for _ in range(REFERENCE_LENGTH))


def may_see_on(control, connection, principal_id, resource_id):
    """Whether this user holds ``fax:read`` on this sent fax now; a temporary password does not count against them."""
    if not principal_id or not resource_id:
        return False
    context, source = _target(control, principal_id, grants_only=True)
    return connection.execute(control._allowed_query(context, source, 'fax:read', resource_id=resource_id)
                              ).first() is not None


class CertaintyStore:
    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, TABLES)
        self.items, self.events, self.settings = (tables['certainty_items'], tables['certainty_events'],
                                                  tables['certainty_settings'])
        self.deliveries, self.attempts, self.jobs = (tables['outbound_deliveries'], tables['outbound_attempts'],
                                                     tables['fax_jobs'])
        self.resources, self.principals, self.users = (tables['access_resources'], tables['access_principals'],
                                                       tables['access_users'])
        self.mailboxes, self.mailbox_settings = tables['mailboxes'], tables['work_mailbox_settings']
        self.sources = checks.CheckSources(engine)

    # -- settings ------------------------------------------------------------------------------------
    def settings_on(self, connection):
        row = connection.execute(sa.select(self.settings).where(self.settings.c.id == 'installation')
                                 ).mappings().one_or_none()
        if row is None:
            return {'fallback_principal_id': None, 'settle_hours': DEFAULT_SETTLE_HOURS, 'version': 0}
        return {'fallback_principal_id': row['fallback_principal_id'], 'settle_hours': row['settle_hours'],
                'version': row['version']}

    def save_settings_on(self, connection, *, fallback_principal_id, settle_hours, version, now):
        current = self.settings_on(connection)
        if version != current['version']:
            raise _Changed()
        values = {'fallback_principal_id': fallback_principal_id, 'settle_hours': settle_hours, 'updated_at': now}
        if current['version'] == 0:
            try:
                connection.execute(self.settings.insert().values(id='installation', version=1, created_at=now,
                                                                 **values))
            except sa.exc.IntegrityError:
                raise _Changed() from None
            return
        result = connection.execute(self.settings.update().where(
            self.settings.c.id == 'installation', self.settings.c.version == version).values(
            version=version + 1, **values))
        if result.rowcount != 1:
            raise _Changed()

    # -- reading -------------------------------------------------------------------------------------
    def item_on(self, connection, item_id):
        row = connection.execute(sa.select(self.items).where(self.items.c.id == item_id)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def job_on(self, connection, job_id):
        row = connection.execute(sa.select(self.jobs).where(self.jobs.c.id == job_id)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def attempt_on(self, connection, attempt_id):
        row = connection.execute(sa.select(self.attempts).where(self.attempts.c.id == attempt_id)
                                 ).mappings().one_or_none()
        return dict(row) if row is not None else None

    def resource_of(self, connection, job_id):
        return connection.execute(sa.select(self.resources.c.id).where(
            self.resources.c.kind == 'outbound', self.resources.c.fax_job_id == job_id)).scalar()

    def names_on(self, connection, ids):
        ids = sorted({value for value in ids if value})
        if not ids:
            return {}
        return dict(connection.execute(sa.select(self.principals.c.id, self.principals.c.display_name)
                                       .where(self.principals.c.id.in_(ids))).all())

    def partner_answer_on(self, connection, item_id):
        """The last answer a partner gave ``PartnerQuestion`` about this item's call, and when; or (None, None)."""
        row = connection.execute(sa.select(self.events.c.details, self.events.c.created_at).where(
            self.events.c.item_id == item_id, self.events.c.kind == 'probe',
            self.events.c.dedupe_key.like('partner:%')).order_by(self.events.c.created_at.desc()).limit(1)).first()
        if row is None:
            return None, None
        try:
            return json.loads(row.details), row.created_at
        except ValueError:
            return None, None

    # -- writing -------------------------------------------------------------------------------------
    def event_on(self, connection, item_id, kind, *, actor_id, details, now, dedupe_key=None):
        connection.execute(self.events.insert().values(
            id=uuid4().hex, item_id=item_id, kind=kind, actor_principal_id=actor_id,
            details=details_json(details), dedupe_key=dedupe_key, created_at=now))

    def change_on(self, connection, item, values, *, kind, actor_id, details, now, dedupe_key=None):
        """Compare-and-set one item and append its event; raise _Changed when it moved."""
        result = connection.execute(self.items.update().where(
            self.items.c.id == item['id'], self.items.c.version == item['version']).values(
            version=item['version'] + 1, updated_at=now, **values))
        if result.rowcount != 1:
            raise _Changed()
        self.event_on(connection, item['id'], kind, actor_id=actor_id, details=details, now=now,
                      dedupe_key=dedupe_key)

    def probe_on(self, connection, item_id, found, *, now, dedupe_key):
        """Record an automatic check's finding once (the same finding again has no effect)."""
        if connection.execute(sa.select(self.events.c.id).where(
                self.events.c.item_id == item_id, self.events.c.dedupe_key == dedupe_key)).first():
            return False
        self.event_on(connection, item_id, 'probe', actor_id=None, now=now, dedupe_key=dedupe_key,
                      details={key: found.get(key) for key in ('kind', 'result', 'text', 'status', 'pages_held',
                                                               'total_pages', 'statement', 'signature')})
        return True

    # -- the feed ------------------------------------------------------------------------------------
    def _uncertain(self):
        d, a = self.deliveries, self.attempts
        return sa.or_(sa.and_(d.c.state == 'reconciliation_required', a.c.phase == 'uncertain'),
                      sa.and_(d.c.state == 'failed',
                              a.c.error_category.in_((PARTLY_SENT, PERSON_ANSWERED, WRONG_STATION))))

    def _not_a_probe(self, job_id):
        """Faxes this module or the direct path send only to find out or to announce: no item of their own."""
        queries = sa.select(self.items.c.query_job_id).where(self.items.c.query_job_id.is_not(None))
        conditions = [job_id.not_in(queries)]
        notices = self.sources.table('direct_notices')
        if notices is not None:
            conditions.append(~sa.exists(sa.select(1).where(notices.c.role == 'sender',
                                                            notices.c.notice_job_id == job_id)))
        # A test fax to a public test line (public_test_lines.py): a person who answered or a station that differed there
        # is the test's result, not a fax for anyone to settle.
        tests = self.sources.table('test_line_sends')
        if tests is not None:
            conditions.append(~sa.exists(sa.select(1).where(tests.c.job_id == job_id)))
        return sa.and_(*conditions)

    def candidates(self, *, limit=100):
        d, a, items = self.deliveries, self.attempts, self.items
        j = self.jobs
        backend = sa.func.coalesce(j.c.outbound_backend, j.c.backend) if 'outbound_backend' in j.c else j.c.backend
        query = (sa.select(a.c.id.label('attempt_id'), a.c.job_id, a.c.error_category, a.c.profile_id,
                           backend.label('backend'))
                 .select_from(d.join(a, a.c.id == d.c.attempt_id).join(j, j.c.id == a.c.job_id)
                              .outerjoin(items, items.c.attempt_id == a.c.id))
                 .where(items.c.id.is_(None), d.c.dispatch_mode == 'normal', self._uncertain(),
                        self._not_a_probe(a.c.job_id))
                 .order_by(a.c.submitted_at, a.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def _sending_mailbox_on(self, connection, job_id):
        decisions = self.sources.table('fax_job_rule_decisions')
        if decisions is None:
            return None
        facts = connection.execute(sa.select(decisions.c.facts).where(decisions.c.job_id == job_id)
                                   .order_by(decisions.c.sequence.desc()).limit(1)).scalar()
        try:
            mailbox = json.loads(facts or '{}').get('mailbox_id')
        except (ValueError, AttributeError):
            return None
        if not isinstance(mailbox, str) or not mailbox:
            return None
        return connection.execute(sa.select(self.mailboxes.c.id).where(self.mailboxes.c.id == mailbox)).scalar()

    def sender_on(self, connection, job_id):
        """The person who sent the fax (its personal container's user), or None for a key or a relay."""
        r, parent = self.resources, self.resources.alias('parent')
        p = self.principals
        return connection.execute(
            sa.select(p.c.id).select_from(r.join(parent, parent.c.id == r.c.parent_id)
                                          .join(p, p.c.id == parent.c.principal_id))
            .where(r.c.kind == 'outbound', r.c.fax_job_id == job_id, parent.c.kind == 'personal',
                   p.c.kind == 'user', p.c.enabled == 1)).scalar()

    def owner_on(self, control, connection, job_id, resource_id, settings):
        """(owner, how chosen, sending mailbox): the sender, the mailbox's backup, the fallback, or nobody."""
        mailbox = self._sending_mailbox_on(connection, job_id)
        sender = self.sender_on(connection, job_id)
        if may_see_on(control, connection, sender, resource_id):
            return sender, 'sender', mailbox
        if mailbox is not None:
            backup = connection.execute(sa.select(self.mailbox_settings.c.backup_principal_id).where(
                self.mailbox_settings.c.mailbox_id == mailbox)).scalar()
            if may_see_on(control, connection, backup, resource_id):
                return backup, 'mailbox', mailbox
        fallback = settings['fallback_principal_id']
        if may_see_on(control, connection, fallback, resource_id):
            return fallback, 'fallback', mailbox
        return None, None, mailbox

    def feed(self, control, *, now=None, limit=100):
        """Make one open item per uncertain attempt that has none; return how many."""
        now = now or utcnow()
        created = 0
        for row in self.candidates(limit=limit):
            try:
                with write_transaction(self.engine) as connection:
                    if connection.execute(sa.select(self.items.c.id).where(
                            self.items.c.attempt_id == row['attempt_id'])).first():
                        continue
                    settings = self.settings_on(connection)
                    resource = self.resource_of(connection, row['job_id'])
                    owner, source, mailbox = self.owner_on(control, connection, row['job_id'], resource, settings)
                    hours = settings['settle_hours'] or None
                    route = checks.provider_of(self.sources, connection, row)
                    identity = uuid4().hex
                    for _ in range(5):
                        reference = new_reference()
                        if not connection.execute(sa.select(self.items.c.id).where(
                                self.items.c.reference == reference)).first():
                            break
                    connection.execute(self.items.insert().values(
                        id=identity, job_id=row['job_id'], attempt_id=row['attempt_id'],
                        category=row['error_category'] or 'transport_ambiguous', route=route, reference=reference,
                        state='open', owner_principal_id=owner, owner_source=source, mailbox_id=mailbox,
                        due_at=now + timedelta(hours=hours) if hours else None, due_hours=hours, version=1,
                        created_at=now, updated_at=now))
                    names = self.names_on(connection, [owner])
                    self.event_on(connection, identity, 'opened', actor_id=None, now=now, details={
                        'category': row['error_category'], 'owner_name': names.get(owner),
                        'owner_source': source, 'due_hours': hours})
                    created += 1
            except DeliveryStoreError:
                continue  # A concurrent feeder made it; the unique index decides.
        return created

    def escalate(self, control, *, now=None, limit=100):
        """Escalate open items past their deadline once each, to the fallback person; return how many."""
        now = now or utcnow()
        items = self.items
        due = (sa.select(items.c.id).where(items.c.state == 'open', items.c.due_at.is_not(None),
                                           items.c.due_at <= now, items.c.escalated_at.is_(None))
               .order_by(items.c.due_at, items.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            identities = connection.execute(due).scalars().all()
        changed = 0
        for identity in identities:
            try:
                with write_transaction(self.engine) as connection:
                    item = self.item_on(connection, identity)
                    if (item is None or item['state'] != 'open' or item['escalated_at'] is not None
                            or item['due_at'] is None or item['due_at'] > now):
                        continue
                    fallback = self.settings_on(connection)['fallback_principal_id']
                    owner = item['owner_principal_id']
                    names = self.names_on(connection, (owner, fallback))
                    values = {'escalated_at': now}
                    details = {'from_name': names.get(owner)}
                    resource = self.resource_of(connection, item['job_id'])
                    if fallback and fallback != owner and may_see_on(control, connection, fallback, resource):
                        values.update(owner_principal_id=fallback, owner_source='fallback')
                        details['to_name'] = names.get(fallback)
                    elif fallback and fallback != owner:
                        details['fallback_name'] = names.get(fallback)
                    self.change_on(connection, item, values, kind='escalated', actor_id=None, details=details,
                                   now=now, dedupe_key='escalated:' + item['due_at'].isoformat(timespec='seconds'))
                    changed += 1
            except (_Changed, DeliveryStoreError):
                continue
        return changed

    def delivered_sentence_on(self, connection, attempt_id):
        """Why a fax counts as delivered now, in one sentence: completed directly by its partner, or delivered."""
        repairs = self.sources.table('direct_call_repairs')
        if repairs is not None and attempt_id:
            # A repair's attempt carries the repair's ID (direct/repair.py, OutboundStore.complete_repair).
            peer = connection.execute(sa.select(repairs.c.peer_id).where(
                repairs.c.role == 'sender', repairs.c.repair_id == attempt_id)).scalar()
            if peer:
                partner = checks._organization(self.sources, connection, peer)
                return f'Closed: the fax was completed directly by {partner}.'
        return 'Closed: the fax was delivered.'

    def close_delivered(self, *, now=None, limit=100):
        """Close each open item whose fax is now delivered, by any path; return how many.

        A partner's repair, a late result from the fax service, or a confirmed receipt all end in the fax's own
        delivery record saying ``success``; that record is read here, so a restart misses nothing and a repeat
        changes nothing. The item records why, with no person's name.
        """
        now = now or utcnow()
        items, deliveries = self.items, self.deliveries
        due = (sa.select(items.c.id).select_from(items.join(deliveries, deliveries.c.id == items.c.job_id))
               .where(items.c.state == 'open', deliveries.c.state == 'success')
               .order_by(items.c.created_at, items.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            identities = connection.execute(due).scalars().all()
        closed = 0
        for identity in identities:
            try:
                with write_transaction(self.engine) as connection:
                    item = self.item_on(connection, identity)
                    delivery = connection.execute(sa.select(deliveries.c.state, deliveries.c.attempt_id).where(
                        deliveries.c.id == (item or {}).get('job_id'))).first()
                    if item is None or item['state'] != 'open' or delivery is None or delivery.state != 'success':
                        continue
                    sentence = self.delivered_sentence_on(connection, delivery.attempt_id)
                    self.change_on(connection, item, {'state': 'settled', 'outcome': 'delivered', 'settled_at': now,
                                                      'settled_reason': sentence},
                                   kind='settled', actor_id=None, now=now, dedupe_key='closed:delivered',
                                   details={'outcome': 'delivered', 'reason': sentence, 'automatic': True})
                    closed += 1
            except (_Changed, DeliveryStoreError):
                continue  # a person settled it first, or another worker closed it
        return closed

    def record_checks(self, *, now=None, limit=50):
        """Keep each new finding of the automatic checks on open items, once; return how many were new.

        The findings are worked out on a read connection; a write happens only for one not kept yet.
        """
        now = now or utcnow()
        new = []
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.items).where(self.items.c.state == 'open')
                                      .order_by(self.items.c.created_at).limit(limit)).mappings().all()
            for row in rows:
                item = dict(row)
                job = self.job_on(connection, item['job_id']) or {}
                answer, _ = self.partner_answer_on(connection, item['id'])
                for found in (checks.partner_check(self.sources, connection, item, job, answer),
                              checks.call_record_check(self.sources, connection, item, job, now=now)):
                    if found['result'] in ('unavailable', 'not_done', 'asking', 'unknown'):
                        continue
                    key = 'found:' + hashlib.sha256(f"{found['kind']}|{found['result']}|{found['text']}".encode()
                                                    ).hexdigest()[:48]
                    if connection.execute(sa.select(self.events.c.id).where(
                            self.events.c.item_id == item['id'], self.events.c.dedupe_key == key)).first() is None:
                        new.append((item['id'], found, key))
        recorded = 0
        for item_id, found, key in new:
            try:
                with write_transaction(self.engine) as connection:
                    recorded += self.probe_on(connection, item_id, found, now=now, dedupe_key=key)
            except (DeliveryStoreError, ValueError):
                continue
        return recorded


class CertaintyWorker:
    """Make items for new uncertain faxes, close those delivered since, keep what the checks find, escalate."""

    def __init__(self, store, *, control):
        self.store, self.control = store, control

    def step(self, *, now=None):
        now = now or utcnow()
        control = self.control()
        created = self.store.feed(control, now=now)
        # A fax delivered since, by any path (a partner's repair, a late result, a confirmed receipt), closes.
        self.store.close_delivered(now=now)
        self.store.record_checks(now=now)
        escalated = self.store.escalate(control, now=now)
        return created >= 100 or escalated >= 100


class PartnerQuestion:
    """Ask an enrolled partner, once and signed, which pages of an uncertain phone call it holds.

    Only for a call the installation's own fax line placed (its call record names the caller, the time and the
    pages) to a verified partner, when neither the direct path's delivery record nor its repair of a broken
    call already covers the attempt. It uses the direct path's signed call question (``direct/repair.py``,
    ``POST /direct/calls/pages``); without that module this installation does not ask. A partner that does
    not answer is asked again after ``checks.BACKOFF``. The answer is recorded as a probe event, signed
    statement included, and never changes the fax.
    """

    def __init__(self, store, service, *, asker=None):
        self.store, self.service = store, service
        self.asker = asker  # tests pass a stand-in for the direct path's question

    def _ask_function(self):
        if self.asker is not None:
            return self.asker
        try:
            from ..direct.repair import CallRepair
        except ImportError:
            return None
        sender = CallRepair(self.service)  # its signed call question (POST /direct/calls/pages)

        async def ask(peer, call, total_pages):
            from ..config_runtime import run_lifecycle_step
            identity = await run_lifecycle_step(self.service.identity)
            return await sender.ask(identity, peer, call, repair_id=uuid4().hex, message_id=uuid4().hex,
                                    total_pages=total_pages)
        return ask

    def due(self, *, now=None, limit=10):
        """(item, peer, call, total pages) for each open item a partner should be asked about now."""
        now = now or utcnow()
        sources = self.store.sources
        found = []
        with read_connection(self.store.engine) as connection:
            rows = connection.execute(sa.select(self.store.items).where(self.store.items.c.state == 'open')
                                      .order_by(self.store.items.c.created_at).limit(200)).mappings().all()
            for row in rows:
                item = dict(row)
                if checks._direct_delivery(sources, connection, item['attempt_id']) is not None:
                    continue  # the direct path asks about its own documents
                if checks.repair_for(sources, connection, item['attempt_id']) is not None:
                    continue  # the direct path's repair asked about this broken call
                job = self.store.job_on(connection, item['job_id']) or {}
                peer = checks.partner_for(sources, connection, job.get('to_number'))
                call = checks._call_record(sources, connection, item['attempt_id'])
                pages = job.get('pages')
                if peer is None or call is None or type(pages) is not int or pages < 1:
                    continue
                answer, answered_at = self.store.partner_answer_on(connection, item['id'])
                if answer and answer.get('result') != 'no_answer':
                    continue
                if not checks.answer_age_ok(answered_at, now, backoff=checks.BACKOFF):
                    continue
                found.append((item, peer, {'caller': call['caller'], 'called': call['called'],
                                           'started_at': call['started_at'], 'answered_at': call['answered_at'],
                                           'ended_at': call['ended_at'], 'pages_sent': call['pages'] or 0},
                              pages))
                if len(found) >= limit:
                    break
        return found

    async def step(self):
        from ..config_runtime import run_lifecycle_step
        ask = self._ask_function()
        if ask is None:
            return False
        for item, peer, call, pages in await run_lifecycle_step(self.due):
            try:
                answer = await ask(peer, call, pages)
            except Exception:
                answer = None
            await run_lifecycle_step(lambda: self.record(item, answer))
        return False

    def record(self, item, answer, *, now=None):
        now = now or utcnow()
        if answer is None:
            found = {'kind': 'partner', 'result': 'no_answer', 'text': 'The partner did not answer.'}
            key = f'partner:none:{now.isoformat(timespec="seconds")}'
        else:
            envelope = answer.get('envelope') or {}
            found = {'kind': 'partner', 'result': 'answered', 'status': answer.get('status'),
                     'pages_held': answer.get('pages_held'), 'total_pages': answer.get('total_pages'),
                     'statement': envelope.get('statement'), 'signature': envelope.get('signature')}
            key = 'partner:answer'
        with write_transaction(self.store.engine) as connection:
            return self.store.probe_on(connection, item['id'], found, now=now, dedupe_key=key)
