"""Sending rules in the database: revisions, heads and drafts per scope, and each fax's decisions.

Revisions are immutable: this module inserts them and never updates or deletes
one. Publishing is one transaction that checks the expected head and draft
version, inserts the next revision, moves the head, removes the draft and writes
the audit row. A fax's decisions are inserted, never rewritten; the highest
sequence is current.

Writes run inside the access store's transaction when one is given, so the
audit row commits with the change and publishes are serialized; without one
(tests, the CLI's offline reads) they use the delivery tables' write lock.
"""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import uuid

import sqlalchemy as sa

from . import model
from .compile import compile_document, compiled_revision, document_problems
from ..routing.database import DeliveryStoreError, reflect, read_connection, utcnow, write_transaction


TABLES = ('routing_rule_revisions', 'routing_rule_state', 'routing_rule_drafts', 'fax_job_rule_decisions')
OPERATIONS = {'publish': 'routing.rules_published', 'restore': 'routing.rules_restored',
              'discard': 'routing.rules_draft_discarded'}


class RulesConflict(RuntimeError):
    """Someone else changed the rules first; the sentence says what to do."""


class RulesInputError(ValueError):
    """The request cannot be done as asked; ``problems`` lists what is wrong, when there is a document."""

    def __init__(self, message, problems=()):
        super().__init__(message)
        self.problems = tuple(problems)


def encode(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(text):
    return hashlib.sha256(text.encode('ascii')).hexdigest()


def empty_document():
    return {'format': model.FORMAT, 'limits': [], 'routes': []}


class RuleStore:
    def __init__(self, engine, *, access_store=None):
        self.engine = engine
        self.access_store = access_store
        self.tables = reflect(engine, TABLES)
        self.revisions = self.tables['routing_rule_revisions']
        self.state = self.tables['routing_rule_state']
        self.drafts = self.tables['routing_rule_drafts']
        self.decisions = self.tables['fax_job_rule_decisions']

    # Transactions -----------------------------------------------------------------------------------------------

    @contextmanager
    def _write(self):
        """``(connection, policy version or None)`` in one serialized transaction."""
        if self.access_store is None:
            with write_transaction(self.engine) as connection:
                yield connection, None
            return
        try:
            with self.access_store.transaction() as connection:
                yield connection, self.access_store.require_lock_on(connection)
        except sa.exc.SQLAlchemyError:
            raise DeliveryStoreError('Sending rules could not be saved.') from None

    def _audit(self, connection, version, actor, operation, scope_name, details):
        if self.access_store is None or version is None:
            return
        credential = getattr(actor, 'credential', None)
        connection.execute(self.access_store.tables['access_audit'].insert().values(
            id=uuid.uuid4().hex, actor_principal_id=getattr(actor, 'principal_id', None),
            actor_key_binding_id=getattr(credential, 'binding_id', None),
            actor_session_id=getattr(credential, 'session_id', None), operation=OPERATIONS[operation],
            target_kind='installation', target_id=f'routing_rules:{scope_name}'[:100],
            policy_version_before=version, policy_version_after=version, outcome='allowed',
            details=encode({'scope': scope_name, **details}), created_at=utcnow()))

    # Reading ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _where(table, kind, scope_id):
        return sa.and_(table.c.scope_kind == kind, table.c.scope_id == scope_id)

    def _head_on(self, connection, kind, scope_id):
        row = connection.execute(sa.select(self.state).where(self._where(self.state, kind, scope_id))
                                 ).mappings().one_or_none()
        return dict(row) if row is not None else None

    def _revision_on(self, connection, revision_id):
        if revision_id is None:
            return None
        row = connection.execute(sa.select(self.revisions).where(self.revisions.c.id == revision_id)
                                 ).mappings().one_or_none()
        return dict(row) if row is not None else None

    def active(self, kind, scope_id=''):
        """The scope's active revision row (with ``document`` as text), or None for no rules."""
        with read_connection(self.engine) as connection:
            head = self._head_on(connection, kind, scope_id)
            return self._revision_on(connection, head['active_revision_id']) if head else None

    def revision(self, kind, scope_id, number):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.revisions).where(
                self._where(self.revisions, kind, scope_id), self.revisions.c.number == number)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def history(self, kind, scope_id=''):
        """Every revision of the scope, newest first, without documents."""
        columns = [column for column in self.revisions.c if column.name != 'document']
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(*columns).where(
                self._where(self.revisions, kind, scope_id)).order_by(self.revisions.c.number.desc())).mappings()]

    def draft(self, kind, scope_id=''):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.drafts).where(self._where(self.drafts, kind, scope_id))
                                     ).mappings().one_or_none()
            if row is None:
                return None
            found = dict(row)
            base = self._revision_on(connection, found['base_revision_id'])
            found['base_revision'] = base['number'] if base else None
            return found

    def active_scopes(self):
        """Every scope with an active revision: ``{scope name: revision row}``."""
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.revisions).join(
                self.state, self.state.c.active_revision_id == self.revisions.c.id)).mappings().all()
        return {model.scope_name(row['scope_kind'], row['scope_id']): dict(row) for row in rows}

    # Drafts -----------------------------------------------------------------------------------------------------

    def save_draft(self, kind, scope_id, document, *, expected_version, actor=None, actor_name=None):
        """Save the scope's one draft. ``expected_version`` 0 means "there is no draft yet"."""
        model.scope_name(kind, scope_id)
        text = encode(document)
        if len(text) > model.MAX_DOCUMENT_BYTES:
            raise RulesInputError('These rules are too large to save.')
        now = utcnow()
        with self._write() as (connection, _):
            current = connection.execute(sa.select(self.drafts).where(self._where(self.drafts, kind, scope_id))
                                         ).mappings().one_or_none()
            if (current['version'] if current else 0) != expected_version:
                raise RulesConflict('Someone else changed these rules since you opened them. Reload them and make '
                                    'your change again.')
            values = dict(document=text, actor_principal_id=getattr(actor, 'principal_id', None),
                          actor_name=actor_name, check_result=None, checked_version=None, checked_at=None,
                          updated_at=now)
            if current is None:
                head = self._head_on(connection, kind, scope_id)
                connection.execute(self.drafts.insert().values(
                    id=uuid.uuid4().hex, scope_kind=kind, scope_id=scope_id, version=1, created_at=now,
                    base_revision_id=head['active_revision_id'] if head else None, **values))
            else:
                connection.execute(self.drafts.update().where(self.drafts.c.id == current['id']).values(
                    version=current['version'] + 1, **values))
        return self.draft(kind, scope_id)

    def discard_draft(self, kind, scope_id, *, actor=None):
        with self._write() as (connection, version):
            removed = connection.execute(self.drafts.delete().where(self._where(self.drafts, kind, scope_id))).rowcount
            if removed:
                self._audit(connection, version, actor, 'discard', model.scope_name(kind, scope_id), {})
        return bool(removed)

    def record_check(self, kind, scope_id, draft_version, result):
        """Keep a check's result with the draft version it covered; a later edit clears it."""
        with self._write() as (connection, _):
            connection.execute(self.drafts.update().where(
                self._where(self.drafts, kind, scope_id), self.drafts.c.version == draft_version).values(
                check_result=encode(result), checked_version=draft_version, checked_at=utcnow()))

    def restore(self, kind, scope_id, number, *, actor=None, actor_name=None):
        """Make an earlier revision the draft. It is published again as a new revision, never reactivated."""
        found = self.revision(kind, scope_id, number)
        if found is None:
            raise RulesInputError(f'There is no version {number} of these rules.')
        current = self.draft(kind, scope_id)
        draft = self.save_draft(kind, scope_id, json.loads(found['document']),
                                expected_version=current['version'] if current else 0, actor=actor,
                                actor_name=actor_name)
        if self.access_store is not None:
            with self._write() as (connection, version):
                self._audit(connection, version, actor, 'restore', model.scope_name(kind, scope_id),
                            {'revision': number})
        return draft

    # Publishing -------------------------------------------------------------------------------------------------

    def publish(self, kind, scope_id, *, expected_active_revision, expected_draft_version, note=None, actor=None,
                actor_name=None, organization_definitions=None):
        """Publish the draft as the scope's next revision. Stale expectations raise ``RulesConflict``."""
        name = model.scope_name(kind, scope_id)
        if note is not None and (not isinstance(note, str) or len(note) > 200):
            raise RulesInputError('Keep the note to 200 characters.')
        now = utcnow()
        with self._write() as (connection, version):
            draft = connection.execute(sa.select(self.drafts).where(self._where(self.drafts, kind, scope_id))
                                       ).mappings().one_or_none()
            head = self._head_on(connection, kind, scope_id)
            active = self._revision_on(connection, head['active_revision_id']) if head else None
            if (active['number'] if active else None) != expected_active_revision:
                raise RulesConflict('Someone published these rules after you opened them. Reload them, check your '
                                    'change against the new version and publish again.')
            if draft is None or draft['version'] != expected_draft_version:
                raise RulesConflict('The draft changed after you checked it. Reload it and publish again.')
            document = json.loads(draft['document'])
            problems = [problem for problem in document_problems(kind, document) if problem.level == 'error']
            if problems:
                raise RulesInputError('Fix the problems in these rules before publishing them.', problems)
            number = connection.execute(sa.select(sa.func.coalesce(sa.func.max(self.revisions.c.number), 0)).where(
                self._where(self.revisions, kind, scope_id))).scalar_one() + 1
            identity = uuid.uuid4().hex
            connection.execute(self.revisions.insert().values(
                id=identity, scope_kind=kind, scope_id=scope_id, number=number,
                parent_id=active['id'] if active else None, format_version=model.FORMAT, document=draft['document'],
                digest=digest(draft['document']), note=(note or '').strip() or None,
                actor_principal_id=getattr(actor, 'principal_id', None), actor_name=actor_name, created_at=now))
            if head is None:
                connection.execute(self.state.insert().values(
                    id=uuid.uuid4().hex, scope_kind=kind, scope_id=scope_id, active_revision_id=identity,
                    generation=1, updated_at=now))
            else:
                moved = connection.execute(self.state.update().where(
                    self.state.c.id == head['id'], self.state.c.generation == head['generation']).values(
                    active_revision_id=identity, generation=head['generation'] + 1, updated_at=now)).rowcount
                if moved != 1:
                    raise RulesConflict('Someone published these rules at the same moment. Reload them and publish '
                                        'again.')
            connection.execute(self.drafts.delete().where(self.drafts.c.id == draft['id']))
            self._audit(connection, version, actor, 'publish', name, {
                'revision': number, 'previous': active['number'] if active else None,
                'note': (note or '').strip()[:200] or None,
                'rules': len(document.get('limits', [])) + len(document.get('routes', []))})
        return self.revision(kind, scope_id, number)

    # Compiled rules ---------------------------------------------------------------------------------------------

    def compiled_active(self):
        """``{scope name: Compiled}`` for every scope with active rules: the input of ``evaluate.decide``."""
        return compile_scopes(self.active_scopes())

    # Decisions --------------------------------------------------------------------------------------------------

    def record_decision_on(self, connection, *, job_id, facts, decision, sequence=1, actor_principal_id=None,
                           reason=None, now=None):
        """Insert one decision for a fax inside the caller's transaction (sequence 1 is the acceptance's own)."""
        if decision.facts_digest != facts.digest():
            raise ValueError('The decision was not made from these facts.')
        identity = uuid.uuid4().hex
        envelope = decision.envelope
        connection.execute(self.decisions.insert().values(
            id=identity, job_id=job_id, sequence=sequence, revisions=encode(decision.revision_ids),
            facts=facts.to_json(), facts_digest=decision.facts_digest, decision=decision.compact().to_json(),
            dial_number=envelope.dial.number if envelope.dial else None,
            approval_id=envelope.dial.approval_id if envelope.dial else None, page_layout=envelope.page_layout,
            outcome=decision.outcome, reason=reason, actor_principal_id=actor_principal_id,
            created_at=now or utcnow()))
        return identity

    def current_decision(self, job_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.decisions).where(self.decisions.c.job_id == job_id)
                                     .order_by(self.decisions.c.sequence.desc()).limit(1)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def recent_decisions(self, limit=200):
        """The latest acceptance decisions (sequence 1), newest first, with each fax's number."""
        jobs = sa.table('fax_jobs', sa.column('id'), sa.column('to_number'))
        with read_connection(self.engine) as connection:
            rows = connection.execute(
                sa.select(self.decisions, jobs.c.to_number).join(jobs, jobs.c.id == self.decisions.c.job_id)
                .where(self.decisions.c.sequence == 1)
                .order_by(self.decisions.c.created_at.desc(), self.decisions.c.id.desc()).limit(limit)).mappings()
            return [dict(row) for row in rows]


def compile_scopes(rows):
    """Compile ``{scope name: revision row}``; lower scopes read the organization revision's definitions."""
    compiled = {}
    organization = rows.get(model.ORGANIZATION)
    definitions, key = None, None
    if organization is not None:
        compiled[model.ORGANIZATION] = compiled_revision(_ref(organization), organization['document'])
        definitions, key = compiled[model.ORGANIZATION].definitions, organization['id']
    for name, row in rows.items():
        if name != model.ORGANIZATION:
            compiled[name] = compiled_revision(_ref(row), row['document'], definitions, definitions_key=key)
    return compiled


def _ref(row):
    return model.RevisionRef(row['scope_kind'], row['scope_id'], row['id'], row['number'])


def draft_ref(kind, scope_id):
    """The reference a draft is compiled under: no revision yet, number 0."""
    return model.RevisionRef(kind, scope_id, 'draft', 0)


def compile_draft(kind, scope_id, document, definitions=None):
    return compile_document(draft_ref(kind, scope_id), document, definitions)


def when(value):
    """Stored naive-UTC times as ISO text for the API."""
    return value.isoformat(timespec='seconds') if isinstance(value, datetime) else value
