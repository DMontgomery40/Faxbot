"""Expected faxes (CE3): hold an expectation before anything arrives, match arrivals, flag what is missing.

Every fax queue starts after something arrives. An expectation is recorded
first ("PO 483 needs the supplier's signed acknowledgement by Friday") and
Faxbot reports it when nothing that answers it has come in time. It sits beside
the work items for received documents (``store.py``) and keeps their rules.

Rules this module keeps:

- **A mailbox owns each expectation.** Who may see or change it is decided as
  for a fax filed in that mailbox that has not arrived yet
  (``AccessControl.authorize_child_on``); an owner, when set, must be able to see
  every document in that mailbox.
- **Only a stable business reference or a person closes one.** A strong match is
  the exact reference carried in the fax's subaddress (T.33 SUB, recorded in
  ``inbound_fax_routing``), a partner's registered form field
  (``form_deliveries``), a Direct message's identifier, or the admin's email
  subject pattern. A known sender's number, partner or Direct address within the
  expectation's window, or text from an approved extraction, only proposes a
  match; a proposal waits for a person.
- **A generic fax never satisfies a specific requirement.** An expectation with
  required parts (a signature, a full packet) is never closed automatically; an
  expectation for a specific revision closes only when the arrival states that
  revision, and a different revision is shown as a proposal that names it.
- **One arrival, two open expectations with the same key:** both get proposals.
- **The received document is linked, never copied** (``work_expectation_links``).
- **Every change is a compare-and-set on ``version``** together with its event,
  which records the actor, the source of the change, the time and the evidence.
- **Overdue is escalated once** (``escalate``, called from
  ``WorkStore.escalate``): one event with a dedupe key per due time, and the
  mailbox's backup person becomes the owner when they can see the mailbox.
- **The due time is an operational target**, set once when the expectation is
  made: the time the import or the person gave, else the import source's hours,
  else the mailbox's acknowledgement hours, else the installation's. Restarts
  and replays never move it.
"""
from datetime import timedelta
import json
import re
import secrets
from uuid import uuid4

import sqlalchemy as sa

from ..routing.database import DeliveryStoreError, read_connection, reflect, utcnow, write_transaction


TABLES = ('work_expectations', 'work_expectation_events', 'work_expectation_links', 'work_expectation_arrivals',
          'work_expectation_sources', 'work_expectation_imports', 'work_outages', 'work_outage_actions',
          'work_outage_reconciliations', 'work_items', 'inbound_faxes', 'inbound_imports', 'access_resources',
          'mailboxes', 'access_principals', 'work_mailbox_settings')
# Still waiting for something to arrive or for a person to decide.
OPEN_STATES = ('open', 'proposed_match', 'overdue')
CLOSED_STATES = ('matched', 'cancelled', 'completed_elsewhere', 'replaced')
ENTERED = 'entered'  # source_key of an expectation a person added
STRONG_SIGNALS = ('subaddress', 'form_field', 'digital_message', 'email_subject')
WEAK_SIGNALS = ('counterparty_number', 'partner', 'direct_address', 'extraction')
# A strong reference may have arrived shortly before the export that created the expectation ran.
STRONG_LOOKBACK = timedelta(days=30)
# A known sender within the expectation's window, up to a week after its due time, may be the answer.
WEAK_GRACE = timedelta(days=7)
MAX_DETAILS = 8000
# Short codes people read out and type: no 0/O, 1/I, 2/Z, 5/S, 8/B (as for uncertain sent faxes).
CODE_LETTERS = 'ACDEFGHJKMNPQRTUVWXY34679'
CODE_LENGTH = 6
_REVISION_AFTER = re.compile(r'\s*[-,:;/()\]]?\s*(?:rev(?:ision)?|version|ver|v)\.?\s*[:#-]?\s*([a-z0-9][a-z0-9.\-]{0,19})',
                             re.IGNORECASE)
_FORM = 'form'  # direct/crypto.FORM: a partner's registered form


class ExpectationChanged(RuntimeError):
    """The expectation changed between reading and writing; nothing was written."""


def new_code():
    return ''.join(secrets.choice(CODE_LETTERS) for _ in range(CODE_LENGTH))


def reference_key(value):
    """A reference as Faxbot compares it: whitespace collapsed, case folded."""
    return ' '.join(str(value or '').split()).casefold()


def details_json(details, limit=MAX_DETAILS):
    """Bounded JSON of plain values for one event or link; never secrets or document content."""
    text = json.dumps({key: value for key, value in details.items() if value not in (None, '', [], {})},
                      ensure_ascii=True, separators=(',', ':'), sort_keys=True, default=str)
    if len(text) > limit:
        raise ValueError('details are too large')
    return text


def loads(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _phrase(key):
    """A pattern for ``key`` as a whole phrase: any spacing, any case, not inside a longer word or number."""
    tokens = reference_key(key).split(' ')
    body = r'\s+'.join(re.escape(token) for token in tokens)
    return re.compile((r'(?<![^\W_])' if tokens[0][:1].isalnum() else '') + body
                      + (r'(?![^\W_])' if tokens[-1][-1:].isalnum() else ''), re.IGNORECASE)


def subject_match(subject, key):
    """Where ``key`` appears in ``subject`` as a whole phrase, or None."""
    if not subject or not reference_key(key):
        return None
    return _phrase(key).search(subject)


def revision_after(subject, key):
    """The revision a subject states right after the reference, such as "PO 483 rev B", or None."""
    found = subject_match(subject, key)
    if found is None:
        return None
    match = _REVISION_AFTER.match(subject, found.end())
    return match.group(1) if match else None


def same_revision(left, right):
    return reference_key(left) == reference_key(right)


def mailbox_coverage(control, connection, actor, permission, now, store):
    """{mailbox id: resource id} for every mailbox where ``permission`` holds for a fax that has not arrived yet."""
    from ..access.types import ResourceRef
    rows = connection.execute(sa.select(store.resources.c.id, store.resources.c.mailbox_id).where(
        store.resources.c.kind == 'mailbox')).all()
    return {row.mailbox_id: row.id for row in rows
            if control.authorize_child_on(connection, actor, permission, ResourceRef(row.id), now=now)}


def may_back_up_mailbox(control, connection, principal_id, resource_id):
    """Whether this user can see every document in the mailbox, as a work backup must."""
    from .store import may_back_up_on
    return may_back_up_on(control, connection, principal_id, resource_id)


class ExpectationStore:
    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, TABLES)
        self.expectations, self.events = tables['work_expectations'], tables['work_expectation_events']
        self.links, self.arrivals = tables['work_expectation_links'], tables['work_expectation_arrivals']
        self.sources, self.imports = tables['work_expectation_sources'], tables['work_expectation_imports']
        self.outages, self.actions = tables['work_outages'], tables['work_outage_actions']
        self.reconciliations = tables['work_outage_reconciliations']
        self.items, self.inbound, self.inbound_imports = (tables['work_items'], tables['inbound_faxes'],
                                                          tables['inbound_imports'])
        self.resources, self.mailboxes = tables['access_resources'], tables['mailboxes']
        self.principals, self.mailbox_settings = tables['access_principals'], tables['work_mailbox_settings']
        from ..access.receiving_rules import tables as receiving_tables
        receiving = receiving_tables(engine)
        self.routing = receiving['routing'] if receiving is not None else None
        try:
            self.forms = reflect(engine, ('form_deliveries',))['form_deliveries']
        except DeliveryStoreError:
            self.forms = None

    # -- reading ---------------------------------------------------------------------
    def expectation_on(self, connection, identity):
        row = connection.execute(sa.select(self.expectations).where(self.expectations.c.id == identity)
                                 ).mappings().one_or_none()
        return dict(row) if row is not None else None

    def names_on(self, connection, ids):
        ids = sorted({value for value in ids if value})
        if not ids:
            return {}
        return dict(connection.execute(sa.select(self.principals.c.id, self.principals.c.display_name)
                                       .where(self.principals.c.id.in_(ids))).all())

    def mailbox_resource_on(self, connection, mailbox_id):
        return connection.execute(sa.select(self.resources.c.id).where(
            self.resources.c.kind == 'mailbox', self.resources.c.mailbox_id == mailbox_id)).scalar_one_or_none()

    def mailbox_setting_on(self, connection, mailbox_id):
        row = connection.execute(sa.select(self.mailbox_settings).where(
            self.mailbox_settings.c.mailbox_id == mailbox_id)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def unique_code_on(self, connection, table):
        for _ in range(20):
            code = new_code()
            if connection.execute(sa.select(table.c.id).where(table.c.code == code)).first() is None:
                return code
        raise DeliveryStoreError('No free code could be found.')

    # -- writing ---------------------------------------------------------------------
    def event_on(self, connection, expectation_id, kind, *, source, now, actor_id=None, actor_name=None,
                 details=None, evidence=None, occurred_at=None, dedupe_key=None):
        connection.execute(self.events.insert().values(
            id=uuid4().hex, expectation_id=expectation_id, kind=kind, source=source, actor_principal_id=actor_id,
            actor_name=actor_name, evidence=details_json(evidence) if evidence else None,
            details=details_json(details or {}), dedupe_key=dedupe_key, occurred_at=occurred_at or now,
            created_at=now))

    def change_on(self, connection, row, values, *, kind, source, now, actor_id=None, actor_name=None,
                  details=None, evidence=None, dedupe_key=None):
        """Compare-and-set one expectation and append its event; raise ExpectationChanged when it moved."""
        result = connection.execute(self.expectations.update().where(
            self.expectations.c.id == row['id'], self.expectations.c.version == row['version']).values(
            version=row['version'] + 1, updated_at=now, **values))
        if result.rowcount != 1:
            raise ExpectationChanged()
        self.event_on(connection, row['id'], kind, source=source, now=now, actor_id=actor_id,
                      actor_name=actor_name, details=details, evidence=evidence, dedupe_key=dedupe_key)
        row.update(values, version=row['version'] + 1, updated_at=now)
        return row

    def create_on(self, connection, values, *, source, now, actor_id=None, actor_name=None, details=None,
                  evidence=None):
        """Insert one expectation (``values`` hold its columns) and its ``created`` event; return the row."""
        identity = uuid4().hex
        row = dict(values, id=identity, code=self.unique_code_on(connection, self.expectations), version=1,
                   created_at=now, updated_at=now, created_by=actor_id, created_by_name=actor_name,
                   reference_key=reference_key(values['reference']))
        row.setdefault('state', 'open')
        connection.execute(self.expectations.insert().values(**row))
        self.event_on(connection, identity, 'created', source=source, now=now, actor_id=actor_id,
                      actor_name=actor_name, details=details, evidence=evidence)
        return self.expectation_on(connection, identity)

    def due_for(self, connection, mailbox_id, *, start, due_at=None, due_hours=None, due_source=None,
                installation_hours=0):
        """(due_at, due_hours, due_source): the time given, else the source's hours, else the targets."""
        from .store import MAX_HOURS
        if due_at is not None:
            return due_at, None, due_source or 'entered'
        if due_hours:
            return start + timedelta(hours=due_hours), due_hours, due_source or 'source'
        setting = self.mailbox_setting_on(connection, mailbox_id)
        if setting is not None and setting['acknowledge_hours'] is not None:
            hours = setting['acknowledge_hours']
            return (start + timedelta(hours=hours), hours, 'mailbox') if hours > 0 else (None, None, None)
        if installation_hours and installation_hours > 0:
            hours = min(int(installation_hours), MAX_HOURS)
            return start + timedelta(hours=hours), hours, 'installation'
        return None, None, None

    # -- arrivals --------------------------------------------------------------------
    def signals_on(self, connection, inbound_id):
        """The reference facts a received document carried, for matching and for the arrivals report."""
        inbound = connection.execute(sa.select(self.inbound.c.from_number, self.inbound.c.to_number).where(
            self.inbound.c.id == inbound_id)).first()
        signals = {'from_number': inbound.from_number if inbound is not None else None}
        if self.routing is not None and 'subaddress' in self.routing.c:
            signals['subaddress'] = connection.execute(sa.select(self.routing.c.subaddress).where(
                self.routing.c.id == inbound_id)).scalar_one_or_none()
        records = connection.execute(sa.select(self.inbound_imports.c.source, self.inbound_imports.c.account,
                                               self.inbound_imports.c.report).where(
            self.inbound_imports.c.inbound_fax_id == inbound_id)).all()
        for record in records:
            report = loads(record.report, {})
            report = report if isinstance(report, dict) else {}
            account = record.account or ''
            text = lambda name, limit=512: (report.get(name)[:limit] if isinstance(report.get(name), str)
                                            and report.get(name) else None)
            if record.source == 'local' and account.startswith('direct:'):
                signals['partner'] = account[len('direct:'):] or None
                signals['partner_message_id'] = text('message_id')
                if report.get('kind') == _FORM and self.forms is not None and signals['partner_message_id']:
                    values = connection.execute(sa.select(self.forms.c.field_values).where(
                        self.forms.c.direction == 'inbound', self.forms.c.message_id == signals['partner_message_id'],
                        self.forms.c.state == 'matched')).scalar_one_or_none()
                    values = loads(values, {})
                    if isinstance(values, dict):
                        signals['form_values'] = {str(name)[:100]: str(value)[:200] for name, value in values.items()
                                                  if isinstance(value, (str, int))}
            elif account.startswith('import:digital:'):
                signals['digital_message_id'] = text('message_id')
                signals['direct_sender'] = text('sender', 320)
                # The requests it answers (In-Reply-To, References), as Faxbot's Direct receiver recorded them.
                answers = report.get('replies_to')
                if isinstance(answers, list):
                    signals['digital_replies_to'] = [value[:512] for value in answers[:20] if isinstance(value, str)]
            elif account.startswith('import:connector:'):
                signals['email_subject'] = text('subject', 200)
                signals['email_sender'] = text('sender', 320)
                signals['email_message_id'] = text('message_id')
        return {key: value for key, value in signals.items() if value not in (None, '', {})}

    @staticmethod
    def referenced(signals):
        """Whether the sender stated a reference: a subaddress or a registered form's values."""
        return bool(signals.get('subaddress') or signals.get('form_values'))

    # -- matching --------------------------------------------------------------------
    @staticmethod
    def strong_hits(expectation, signals):
        """[(signal, evidence, stated revision)] for each stable reference this arrival carries for it."""
        hits = []
        sub = signals.get('subaddress')
        if expectation['subaddress_key'] and sub and sub == expectation['subaddress_key']:
            hits.append(('subaddress', {'subaddress': sub}, None))
        values = signals.get('form_values') or {}
        field = expectation['form_field']
        if field and field in values and reference_key(values[field]) == expectation['reference_key']:
            stated = values.get(expectation['revision_field']) if expectation['revision_field'] else None
            hits.append(('form_field', {'field': field, 'value': values[field],
                                        'revision': stated, 'partner_message_id': signals.get('partner_message_id')},
                         stated))
        key = expectation['message_key']
        if key and key in (signals.get('digital_message_id'), signals.get('email_message_id'),
                           signals.get('partner_message_id')):
            hits.append(('digital_message', {'message_id': key}, None))
        elif key and key in (signals.get('digital_replies_to') or []):
            hits.append(('digital_message', {'replies_to': key}, None))
        subject = signals.get('email_subject')
        if expectation['subject_key'] and subject and subject_match(subject, expectation['subject_key']):
            stated = revision_after(subject, expectation['subject_key'])
            hits.append(('email_subject', {'subject': subject, 'revision': stated}, stated))
        return hits

    @staticmethod
    def weak_hits(expectation, signals, available_at):
        """[(signal, evidence)] for a known sender within the expectation's window."""
        end = (expectation['due_at'] or available_at) + WEAK_GRACE
        if not expectation['window_start'] <= available_at <= end:
            return []
        hits = []
        numbers = loads(expectation['counterparty_numbers'], []) or []
        if signals.get('from_number') and signals['from_number'] in numbers:
            hits.append(('counterparty_number', {'from_number': signals['from_number']}))
        if expectation['partner_id'] and signals.get('partner') == expectation['partner_id']:
            hits.append(('partner', {'partner': signals['partner']}))
        address = (expectation['direct_address'] or '').casefold()
        if address and address in ((signals.get('direct_sender') or '').casefold(),
                                   (signals.get('email_sender') or '').casefold()):
            hits.append(('direct_address', {'sender': expectation['direct_address']}))
        return hits

    @classmethod
    def decide(cls, expectation, signals, available_at, *, ambiguous):
        """(link state, strength, signal, evidence, reason) for one expectation and one arrival, or None.

        ``automatic`` closes the expectation; ``proposed`` waits for a person;
        ``also_arrived`` notes a second document for one already matched.
        """
        strong = cls.strong_hits(expectation, signals)
        if strong and available_at >= expectation['window_start'] - STRONG_LOOKBACK:
            signal, evidence, stated = strong[0]
            if expectation['state'] == 'matched':
                return 'also_arrived', 'strong', signal, evidence, 'also_arrived'
            if expectation['state'] not in OPEN_STATES:
                return None
            required = expectation['required_revision']
            if required:
                stated = next((hit[2] for hit in strong if hit[2]), None)
                if stated is None:
                    return 'proposed', 'strong', signal, evidence, 'revision_unstated'
                if not same_revision(stated, required):
                    return 'proposed', 'strong', signal, dict(evidence, revision=stated), 'wrong_revision'
            if loads(expectation['required_parts'], []):
                return 'proposed', 'strong', signal, evidence, 'parts'
            if ambiguous:
                return 'proposed', 'strong', signal, evidence, 'ambiguous'
            return 'automatic', 'strong', signal, evidence, None
        if expectation['state'] not in OPEN_STATES:
            return None
        weak = cls.weak_hits(expectation, signals, available_at)
        if weak:
            signal, evidence = weak[0]
            return 'proposed', 'weak', signal, evidence, 'weak'
        return None

    def candidates_on(self, connection):
        """Every expectation still waiting, plus matched ones (for a second arrival), as plain dicts."""
        columns = [self.expectations.c[name] for name in (
            'id', 'state', 'reference_key', 'subaddress_key', 'subject_key', 'message_key', 'form_field',
            'revision_field', 'required_revision', 'required_parts', 'counterparty_numbers', 'partner_id',
            'direct_address', 'window_start', 'due_at', 'version', 'created_at')]
        rows = connection.execute(sa.select(*columns).where(
            self.expectations.c.state.in_(OPEN_STATES + ('matched',)))).mappings().all()
        return [dict(row) for row in rows]

    def decisions(self, candidates, signals, available_at):
        """{expectation id: decision} for one arrival against the candidates."""
        strong = [candidate for candidate in candidates if candidate['state'] in OPEN_STATES
                  and self.strong_hits(candidate, signals)
                  and available_at >= candidate['window_start'] - STRONG_LOOKBACK]
        ambiguous = len(strong) > 1
        found = {}
        for candidate in candidates:
            decision = self.decide(candidate, signals, available_at, ambiguous=ambiguous)
            if decision is not None:
                found[candidate['id']] = decision
        return found

    def link_on(self, connection, expectation, inbound_id, work_item_id, decision, *, now, actor_id=None,
                actor_name=None):
        """Record one decision for one arrival; returns True when it changed the expectation."""
        state, strength, signal, evidence, reason = decision
        if connection.execute(sa.select(self.links.c.id).where(
                self.links.c.expectation_id == expectation['id'],
                self.links.c.inbound_fax_id == inbound_id)).first() is not None:
            return False
        link_id = uuid4().hex
        connection.execute(self.links.insert().values(
            id=link_id, expectation_id=expectation['id'], inbound_fax_id=inbound_id, work_item_id=work_item_id,
            strength=strength, signal=signal, state=state, evidence=details_json(evidence, 4000), reason=reason,
            version=1, created_at=now, updated_at=now))
        reference = {'inbound_fax_id': inbound_id, 'work_item_id': work_item_id, 'link_id': link_id}
        current = self.expectation_on(connection, expectation['id'])
        if state == 'also_arrived':
            self.event_on(connection, current['id'], 'also_arrived', source='arrival', now=now,
                          details={'signal': signal}, evidence=reference)
            return False
        if state == 'automatic':
            self.change_on(connection, current, {
                'state': 'matched', 'matched_at': now, 'matched_inbound_fax_id': inbound_id,
                'matched_link_id': link_id, 'matched_by': None, 'closed_at': now},
                kind='matched', source='arrival', now=now, details={'signal': signal, **evidence},
                evidence=reference)
            self.settle_proposals_on(connection, current['id'], now=now, keep=link_id)
            return True
        self.change_on(connection, current, {'state': 'proposed_match'}, kind='proposed', source='arrival',
                       now=now, actor_id=actor_id, actor_name=actor_name,
                       details={'signal': signal, 'reason': reason, **evidence}, evidence=reference)
        return True

    def settle_proposals_on(self, connection, expectation_id, *, now, keep):
        """Once an expectation is closed, its other proposals are no longer waiting for a person."""
        query = self.links.update().where(self.links.c.expectation_id == expectation_id,
                                          self.links.c.state == 'proposed')
        if keep is not None:
            query = query.where(self.links.c.id != keep)
        connection.execute(query.values(state='rejected', note='Closed by another document.', decided_at=now,
                                             updated_at=now, version=self.links.c.version + 1))

    def examine(self, *, now=None, limit=100, item_ids=None):
        """Examine received documents with a work item not examined yet; return how many were examined.

        ``WorkStore.feed`` calls this with the items it has just made, so a
        received document is matched as soon as it becomes work and an idle
        installation runs no matching at all. Without ``item_ids`` this is the
        slow sweep for any item a crash left between the two.
        """
        now = now or utcnow()
        items, arrivals = self.items, self.arrivals
        if item_ids is not None and not item_ids:
            return 0
        query = (sa.select(items.c.id, items.c.inbound_fax_id, items.c.mailbox_id, items.c.available_at)
                 .select_from(items.outerjoin(arrivals, arrivals.c.id == items.c.inbound_fax_id))
                 .where(arrivals.c.id.is_(None)).order_by(items.c.created_at, items.c.id).limit(limit))
        if item_ids is not None:
            query = query.where(items.c.id.in_(sorted(item_ids)))
        with read_connection(self.engine) as connection:
            rows = connection.execute(query).all()
            candidates = self.candidates_on(connection) if rows else []
        examined = 0
        for row in rows:
            linked = False
            try:
                with write_transaction(self.engine) as connection:
                    if connection.execute(sa.select(arrivals.c.id).where(arrivals.c.id == row.inbound_fax_id)).first():
                        continue
                    signals = self.signals_on(connection, row.inbound_fax_id)
                    connection.execute(arrivals.insert().values(
                        id=row.inbound_fax_id, work_item_id=row.id, mailbox_id=row.mailbox_id,
                        available_at=row.available_at, signals=details_json(signals, 8000),
                        referenced=1 if self.referenced(signals) else 0, examined_at=now))
                    for identity, decision in self.decisions(candidates, signals, row.available_at).items():
                        # Decide again on the stored row: a person or another arrival may have changed it.
                        current = self.expectation_on(connection, identity)
                        fresh = current and self.decide(current, signals, row.available_at,
                                                        ambiguous=decision[4] == 'ambiguous')
                        if fresh:
                            self.link_on(connection, current, row.inbound_fax_id, row.id, fresh, now=now)
                            linked = True
                    examined += 1
            except (ExpectationChanged, DeliveryStoreError):
                continue  # Another worker examined it first; the unique key decides.
            if linked:  # the next arrival in this step sees what this one changed
                with read_connection(self.engine) as connection:
                    candidates = self.candidates_on(connection)
        return examined

    def waiting_to_look_back(self):
        """The watermark check: whether any expectation has not been looked back over yet (one indexed query)."""
        with read_connection(self.engine) as connection:
            return connection.execute(sa.select(self.expectations.c.id).where(
                self.expectations.c.examined_at.is_(None)).limit(1)).first() is not None

    def look_back(self, *, now=None, limit=100):
        """Check each new expectation once against documents that arrived before it; return how many."""
        now = now or utcnow()
        expectations, arrivals = self.expectations, self.arrivals
        with read_connection(self.engine) as connection:
            fresh = connection.execute(sa.select(expectations.c.id, expectations.c.window_start).where(
                expectations.c.examined_at.is_(None)).order_by(expectations.c.created_at, expectations.c.id)
                .limit(limit)).all()
            if not fresh:
                return 0
            since = min(row.window_start for row in fresh) - STRONG_LOOKBACK
            earlier = connection.execute(sa.select(arrivals).where(arrivals.c.available_at >= since)
                                         .order_by(arrivals.c.available_at, arrivals.c.id)).mappings().all()
            candidates = self.candidates_on(connection)
        parsed = [(row, loads(row['signals'], {})) for row in earlier]
        done = 0
        for entry in fresh:
            try:
                with write_transaction(self.engine) as connection:
                    current = self.expectation_on(connection, entry.id)
                    if current is None or current['examined_at'] is not None:
                        continue
                    connection.execute(expectations.update().where(expectations.c.id == entry.id).values(
                        examined_at=now))
                    for arrival, signals in parsed:
                        current = self.expectation_on(connection, entry.id)
                        if current['state'] not in OPEN_STATES:
                            break
                        strong = self.strong_hits(current, signals)
                        if not (strong or self.weak_hits(current, signals, arrival['available_at'])):
                            continue
                        # The same reference also fits another expectation still waiting: a person decides.
                        ambiguous = bool(strong) and any(
                            other['id'] != entry.id and other['state'] in OPEN_STATES
                            and self.strong_hits(other, signals)
                            and arrival['available_at'] >= other['window_start'] - STRONG_LOOKBACK
                            for other in candidates)
                        decision = self.decide(current, signals, arrival['available_at'], ambiguous=ambiguous)
                        if decision is not None:
                            self.link_on(connection, current, arrival['id'], arrival['work_item_id'], decision,
                                         now=now)
                    done += 1
            except (ExpectationChanged, DeliveryStoreError):
                continue
        return done

    # -- escalation ------------------------------------------------------------------
    def escalate(self, control, *, now=None, limit=100):
        """Escalate expectations past their due time once each; return how many changed."""
        now = now or utcnow()
        expectations = self.expectations
        due = (sa.select(expectations.c.id).where(
            expectations.c.state.in_(('open', 'proposed_match')), expectations.c.due_at.is_not(None),
            expectations.c.due_at <= now, expectations.c.escalated_at.is_(None))
            .order_by(expectations.c.due_at, expectations.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            identities = connection.execute(due).scalars().all()
        changed = 0
        for identity in identities:
            try:
                with write_transaction(self.engine) as connection:
                    row = self.expectation_on(connection, identity)
                    if (row is None or row['state'] not in ('open', 'proposed_match')
                            or row['escalated_at'] is not None or row['due_at'] is None or row['due_at'] > now):
                        continue
                    setting = self.mailbox_setting_on(connection, row['mailbox_id']) or {}
                    backup, owner = setting.get('backup_principal_id'), row['owner_principal_id']
                    names = self.names_on(connection, (owner, backup))
                    values = {'escalated_at': now, 'state': 'overdue' if row['state'] == 'open' else row['state']}
                    details = {'from_name': names.get(owner), 'due_at': row['due_at'].isoformat(timespec='seconds')}
                    resource = self.mailbox_resource_on(connection, row['mailbox_id'])
                    if backup and backup == owner:
                        details['to_name'] = names.get(backup)
                    elif backup and may_back_up_mailbox(control, connection, backup, resource):
                        values['owner_principal_id'] = backup
                        details['to_name'] = names.get(backup)
                    elif backup:
                        details['backup_name'] = names.get(backup)
                    self.change_on(connection, row, values, kind='overdue', source='faxbot', now=now,
                                   details=details,
                                   dedupe_key='escalated:' + row['due_at'].isoformat(timespec='seconds'))
                    changed += 1
            except (ExpectationChanged, DeliveryStoreError):
                continue  # A person changed it first, or another worker escalated it.
        return changed

    # -- controlled processing ---------------------------------------------------------
    def propose_from_extraction(self, inbound_id, reference, *, extraction, now=None):
        """Text an approved extraction read from a received document may only propose a match.

        ``extraction`` must say which approved tool and version read it, under which
        policy revision, from which pages, and how sure it was (the plan's
        ``ExtractionService`` rule). No extraction service exists in Faxbot yet;
        this is the one entry point such a service will call. It never closes an
        expectation. Returns how many proposals it made.
        """
        required = ('tool', 'version', 'policy_revision', 'pages', 'confidence')
        if not isinstance(extraction, dict) or any(extraction.get(name) in (None, '') for name in required):
            raise ValueError('An extraction must name its tool, version, policy revision, pages and confidence.')
        key = reference_key(reference)
        if not key:
            return 0
        now = now or utcnow()
        made = 0
        with write_transaction(self.engine) as connection:
            item = connection.execute(sa.select(self.items.c.id).where(
                self.items.c.inbound_fax_id == inbound_id)).scalar_one_or_none()
            rows = connection.execute(sa.select(self.expectations).where(
                self.expectations.c.reference_key == key,
                self.expectations.c.state.in_(OPEN_STATES))).mappings().all()
            evidence = {'reference': str(reference)[:200], **{name: extraction[name] for name in required}}
            for row in rows:
                if self.link_on(connection, dict(row), inbound_id, item,
                                ('proposed', 'weak', 'extraction', evidence, 'extraction'), now=now):
                    made += 1
        return made


class ExpectationWorker:
    """Background matching that costs nothing while nothing changes.

    New arrivals are matched when ``WorkStore.feed`` makes their work items.
    Each step makes one indexed check for expectations not looked back over yet
    and looks back only when there are some; every ``SWEEP`` it also examines
    any arrival a crash left unexamined. Overdue escalation runs from
    ``WorkStore.escalate`` at most once every ``SWEEP`` per process.
    """
    SWEEP = timedelta(minutes=5)

    def __init__(self, store):
        self.store = store
        self.next_sweep = None

    def step(self, *, now=None):
        now = now or utcnow()
        looked = self.store.look_back(now=now) if self.store.waiting_to_look_back() else 0
        swept = 0
        if self.next_sweep is None or now >= self.next_sweep:
            swept = self.store.examine(now=now)
            self.next_sweep = now + self.SWEEP if swept < 100 else now  # a full batch: keep sweeping
        return looked >= 100 or swept >= 100
