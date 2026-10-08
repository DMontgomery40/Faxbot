"""Frozen 0064 expected faxes; registration and validation belong to ``schema``.

An expectation is held before anything arrives: "PO 483 needs the supplier's
signed acknowledgement by Friday". It sits beside the work items for received
documents (``work_items``, 0011) and uses the same rules: a mailbox decides who
may see it, every change is a compare-and-set on ``version`` together with an
append-only event, and an escalation has one effect through a unique
``dedupe_key``. This revision adds nine tables and changes no stored row:

- ``work_expectations``: one row per expectation and source revision. The
  import identity is ``(source_key, operation_id, revision)``, unique;
  ``source_key`` is the saved import source's id, or ``entered`` for one a
  person added. ``code`` is a short handle people read out and type.
  ``state`` is one of ``STATES``. ``replaces_id`` names the earlier revision a
  new revision replaced. ``missing_since`` is set when a later full export no
  longer lists it; nothing cancels it on that account. The match keys
  (``subaddress_key``, ``subject_key``, ``message_key``, ``form_field``,
  ``revision_field``) say where a business reference may appear in what
  arrives.
- ``work_expectation_events``: append-only history with the actor, the source
  of the change (``person``, ``import``, ``arrival``, ``faxbot``) and an
  evidence reference.
- ``work_expectation_links``: each received document linked to an
  expectation, as a strong automatic match or a proposal waiting for a person.
  The document is linked by its id, never copied.
- ``work_expectation_arrivals``: each received document Faxbot examined once,
  with the reference facts it carried, so unmatched arrivals can be reported
  apart from unmatched expectations.
- ``work_expectation_sources``: a saved import source: its format and the
  column mapping the administrator saves once.
- ``work_expectation_imports``: each import run; a run is identified by its
  source, file digest and whether it was a full export, so a replay resumes it.
- ``work_outages``, ``work_outage_actions`` and
  ``work_outage_reconciliations``: a declared outage of a source system, the
  actions completed by fax or email during it against their original IDs, and
  each reconciliation list produced after it, kept unchanged.

No foreign key ties these rows to faxes, mailboxes or people; history stays
when a fax or person goes. The downgrade drops the tables and refuses while any
expectation, import run or outage is recorded. Runtime code reflects these
tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_digital_routes import frozen_metadata as previous_metadata


REVISION = '0064_expected_faxes'
ORDER = ('work_expectation_sources', 'work_expectation_imports', 'work_expectations', 'work_expectation_events',
         'work_expectation_links', 'work_expectation_arrivals', 'work_outages', 'work_outage_actions',
         'work_outage_reconciliations')
TABLES = frozenset(ORDER)
STATES = ('open', 'proposed_match', 'matched', 'overdue', 'cancelled', 'completed_elsewhere', 'replaced')
EVENT_KINDS = ('created', 'revised', 'replaced', 'conflict', 'conflict_kept', 'conflict_applied', 'proposed',
               'proposal_rejected', 'matched', 'confirmed', 'also_arrived', 'overdue', 'cancelled',
               'completed_elsewhere', 'missing_from_export', 'back_in_export', 'outage_action', 'exported')
EVENT_SOURCES = ('person', 'import', 'arrival', 'faxbot')
LINK_STATES = ('automatic', 'proposed', 'confirmed', 'rejected', 'also_arrived')
STRENGTHS = ('strong', 'weak')
SIGNALS = ('subaddress', 'form_field', 'digital_message', 'email_subject', 'counterparty_number', 'partner',
           'direct_address', 'extraction', 'person')
DUE_SOURCES = ('entered', 'import', 'source', 'mailbox', 'installation')
FORMATS = ('csv', 'json')
RUN_STATES = ('running', 'complete')
CHANNELS = ('fax', 'email', 'phone', 'other')
OUTCOMES = ('done', 'uncertain')
INDEXES = (
    ('uq_work_expectation_sources_name', 'work_expectation_sources', ('normalized_name',), True),
    ('uq_work_expectation_imports_run', 'work_expectation_imports', ('source_id', 'file_digest', 'full_export'), True),
    ('ix_work_expectation_imports_created', 'work_expectation_imports', ('source_id', 'created_at'), False),
    ('uq_work_expectations_identity', 'work_expectations', ('source_key', 'operation_id', 'revision'), True),
    ('uq_work_expectations_code', 'work_expectations', ('code',), True),
    ('ix_work_expectations_state_due', 'work_expectations', ('state', 'due_at'), False),
    ('ix_work_expectations_mailbox', 'work_expectations', ('mailbox_id', 'state'), False),
    ('ix_work_expectations_subaddress', 'work_expectations', ('subaddress_key',), False),
    ('ix_work_expectations_reference', 'work_expectations', ('reference_key',), False),
    # The matcher's watermark: expectations not yet looked back over (examined_at IS NULL).
    ('ix_work_expectations_examined', 'work_expectations', ('examined_at',), False),
    ('uq_work_expectation_events_dedupe', 'work_expectation_events', ('expectation_id', 'dedupe_key'), True),
    ('ix_work_expectation_events_time', 'work_expectation_events', ('expectation_id', 'occurred_at'), False),
    ('uq_work_expectation_links_pair', 'work_expectation_links', ('expectation_id', 'inbound_fax_id'), True),
    ('ix_work_expectation_links_fax', 'work_expectation_links', ('inbound_fax_id',), False),
    ('ix_work_expectation_links_state', 'work_expectation_links', ('state', 'expectation_id'), False),
    ('ix_work_expectation_arrivals_time', 'work_expectation_arrivals', ('available_at',), False),
    ('ix_work_outages_source', 'work_outages', ('source_id', 'started_at'), False),
    ('ix_work_outage_actions_outage', 'work_outage_actions', ('outage_id', 'operation_id'), False),
    ('ix_work_outage_reconciliations_outage', 'work_outage_reconciliations', ('outage_id', 'created_at'), False),
)


def _id(name='id', nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _text(name, length, nullable=True):
    return sa.Column(name, sa.String(length), nullable=nullable)


def _time(name, nullable=True):
    return sa.Column(name, sa.DateTime(), nullable=nullable)


def _count(name):
    return sa.Column(name, sa.Integer(), nullable=False)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'work_expectation_sources': (
            _id(),
            _text('name', 100, False),
            _text('normalized_name', 100, False),
            _text('format', 8, False),
            # JSON object: Faxbot's field name -> the column (CSV) or key (JSON) it is read from.
            sa.Column('mapping', sa.Text(), nullable=False),
            _id('mailbox_id', True),
            sa.Column('due_hours', sa.Integer(), nullable=True),
            _text('subject_template', 200),
            _text('subaddress_template', 40),
            _text('form_field', 100),
            _text('revision_field', 100),
            _count('version'),
            _id('created_by', True),
            _text('created_by_name', 200),
            _time('created_at', False),
            _time('updated_at', False),
            sa.PrimaryKeyConstraint('id', name='pk_work_expectation_sources'),
            sa.CheckConstraint(_choice('format', FORMATS), name='ck_work_expectation_sources_format'),
            sa.CheckConstraint('version >= 1 AND (due_hours IS NULL OR (due_hours >= 0 AND due_hours <= 8760))',
                               name='ck_work_expectation_sources_values'),
        ),
        'work_expectation_imports': (
            _id(),
            _id('source_id'),
            _text('file_digest', 64, False),
            _text('file_name', 200),
            sa.Column('full_export', sa.Integer(), nullable=False),
            _text('state', 16, False),
            _count('rows_total'),
            _count('created_count'),
            _count('unchanged_count'),
            _count('revised_count'),
            _count('conflict_count'),
            _count('missing_count'),
            _count('problem_count'),
            # JSON: the row problems and the expectations missing from a full export.
            sa.Column('summary', sa.Text(), nullable=True),
            # JSON: each row's identity, digest and mailbox, for reconciliation after an outage.
            sa.Column('rows', sa.Text(), nullable=True),
            _id('created_by', True),
            _text('created_by_name', 200),
            _time('created_at', False),
            _time('completed_at'),
            sa.PrimaryKeyConstraint('id', name='pk_work_expectation_imports'),
            sa.CheckConstraint(_choice('state', RUN_STATES), name='ck_work_expectation_imports_state'),
            sa.CheckConstraint('(full_export = 0 OR full_export = 1) AND rows_total >= 0 AND created_count >= 0 '
                               'AND unchanged_count >= 0 AND revised_count >= 0 AND conflict_count >= 0 '
                               'AND missing_count >= 0 AND problem_count >= 0',
                               name='ck_work_expectation_imports_counts'),
        ),
        'work_expectations': (
            _id(),
            _text('code', 8, False),
            _text('reference', 200, False),
            _text('reference_key', 200, False),
            _text('kind', 100, False),
            _text('description', 500),
            sa.Column('required_parts', sa.Text(), nullable=True),
            _text('required_revision', 40),
            _text('counterparty', 200),
            sa.Column('counterparty_numbers', sa.Text(), nullable=True),
            _id('partner_id', True),
            _text('direct_address', 320),
            _text('fhir_source', 512),
            _id('mailbox_id'),
            _id('owner_principal_id', True),
            _text('subaddress_key', 20),
            _text('subject_key', 200),
            _text('message_key', 512),
            _text('form_field', 100),
            _text('revision_field', 100),
            _text('source_key', 40, False),
            _text('operation_id', 100, False),
            _text('revision', 40, False),
            _text('row_digest', 64),
            _id('replaces_id', True),
            _id('last_import_id', True),
            _time('missing_since'),
            _time('conflict_at'),
            _text('state', 20, False),
            _time('window_start', False),
            _time('due_at'),
            sa.Column('due_hours', sa.Integer(), nullable=True),
            _text('due_source', 16),
            _time('escalated_at'),
            _time('matched_at'),
            _id('matched_inbound_fax_id', True),
            _id('matched_link_id', True),
            _id('matched_by', True),
            _time('closed_at'),
            _id('closed_by', True),
            _text('closed_note', 300),
            _time('examined_at'),
            _id('created_by', True),
            _text('created_by_name', 200),
            _count('version'),
            _time('created_at', False),
            _time('updated_at', False),
            sa.PrimaryKeyConstraint('id', name='pk_work_expectations'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_work_expectations_state'),
            sa.CheckConstraint('due_source IS NULL OR ' + _choice('due_source', DUE_SOURCES),
                               name='ck_work_expectations_due_source'),
            sa.CheckConstraint('version >= 1 AND (due_hours IS NULL OR due_hours >= 1)',
                               name='ck_work_expectations_counts'),
        ),
        'work_expectation_events': (
            _id(),
            _id('expectation_id'),
            _text('kind', 24, False),
            _text('source', 16, False),
            # History keeps the acting person's id and their name at the time.
            _id('actor_principal_id', True),
            _text('actor_name', 200),
            # JSON: what the change rests on (a received fax, a link, an import run, an outage action).
            sa.Column('evidence', sa.Text(), nullable=True),
            sa.Column('details', sa.Text(), nullable=False),
            _text('dedupe_key', 64),
            _time('occurred_at', False),
            _time('created_at', False),
            sa.PrimaryKeyConstraint('id', name='pk_work_expectation_events'),
            sa.CheckConstraint(_choice('kind', EVENT_KINDS), name='ck_work_expectation_events_kind'),
            sa.CheckConstraint(_choice('source', EVENT_SOURCES), name='ck_work_expectation_events_source'),
        ),
        'work_expectation_links': (
            _id(),
            _id('expectation_id'),
            _id('inbound_fax_id'),
            _id('work_item_id', True),
            _text('strength', 8, False),
            _text('signal', 24, False),
            _text('state', 16, False),
            # JSON: the fact that linked them (the subaddress, the form field, the subject, an extraction record).
            sa.Column('evidence', sa.Text(), nullable=False),
            _text('reason', 300),
            _id('decided_by', True),
            _text('decided_by_name', 200),
            _time('decided_at'),
            _text('note', 300),
            _count('version'),
            _time('created_at', False),
            _time('updated_at', False),
            sa.PrimaryKeyConstraint('id', name='pk_work_expectation_links'),
            sa.CheckConstraint(_choice('state', LINK_STATES), name='ck_work_expectation_links_state'),
            sa.CheckConstraint(_choice('strength', STRENGTHS), name='ck_work_expectation_links_strength'),
            sa.CheckConstraint(_choice('signal', SIGNALS), name='ck_work_expectation_links_signal'),
            sa.CheckConstraint('version >= 1', name='ck_work_expectation_links_version'),
        ),
        'work_expectation_arrivals': (
            # The received fax's own id: each is examined once.
            _id(),
            _id('work_item_id', True),
            _id('mailbox_id', True),
            _time('available_at', False),
            # JSON: the reference facts it carried (subaddress, subject, message id, form values, sender).
            sa.Column('signals', sa.Text(), nullable=False),
            sa.Column('referenced', sa.Integer(), nullable=False),
            _time('examined_at', False),
            sa.PrimaryKeyConstraint('id', name='pk_work_expectation_arrivals'),
            sa.CheckConstraint('referenced = 0 OR referenced = 1', name='ck_work_expectation_arrivals_referenced'),
        ),
        'work_outages': (
            _id(),
            _text('code', 8, False),
            _id('source_id'),
            _time('started_at', False),
            _time('ended_at'),
            _text('note', 300),
            _id('declared_by', True),
            _text('declared_by_name', 200),
            _id('ended_by', True),
            _text('ended_by_name', 200),
            _count('version'),
            _time('created_at', False),
            _time('updated_at', False),
            sa.PrimaryKeyConstraint('id', name='pk_work_outages'),
            sa.CheckConstraint('version >= 1', name='ck_work_outages_version'),
        ),
        'work_outage_actions': (
            _id(),
            _id('outage_id'),
            _text('operation_id', 100, False),
            _text('revision', 40, False),
            _text('reference', 200),
            _text('action', 300, False),
            _text('channel', 8, False),
            _text('outcome', 16, False),
            _id('fax_job_id', True),
            _text('evidence_note', 300),
            _time('occurred_at', False),
            _id('recorded_by', True),
            _text('recorded_by_name', 200),
            _time('created_at', False),
            sa.PrimaryKeyConstraint('id', name='pk_work_outage_actions'),
            sa.CheckConstraint(_choice('channel', CHANNELS), name='ck_work_outage_actions_channel'),
            sa.CheckConstraint(_choice('outcome', OUTCOMES), name='ck_work_outage_actions_outcome'),
        ),
        'work_outage_reconciliations': (
            _id(),
            _id('outage_id'),
            _id('import_id'),
            # JSON lists: already done (record, do not submit again), new (submit normally), unresolved (a person).
            sa.Column('already_done', sa.Text(), nullable=False),
            sa.Column('new_items', sa.Text(), nullable=False),
            sa.Column('unresolved', sa.Text(), nullable=False),
            _count('already_done_count'),
            _count('new_count'),
            _count('unresolved_count'),
            _id('created_by', True),
            _text('created_by_name', 200),
            _time('created_at', False),
            sa.PrimaryKeyConstraint('id', name='pk_work_outage_reconciliations'),
            sa.CheckConstraint('already_done_count >= 0 AND new_count >= 0 AND unresolved_count >= 0',
                               name='ck_work_outage_reconciliations_counts'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    definitions = _definitions()
    for name in ORDER:
        sa.Table(name, metadata, *definitions[name])
    for index, name, columns, unique in INDEXES:
        table = metadata.tables[name]
        sa.Index(index, *(table.c[column] for column in columns), unique=unique)
    return metadata


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_expected_faxes(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('An expected-fax table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_expected_faxes(connection, operations):
    # Expectations, import runs and outages are what people recorded; never dropped silently.
    recorded = sum(connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar()
                   for name in ('work_expectations', 'work_expectation_imports', 'work_outages'))
    if recorded:
        _refuse('Expected faxes, their imports or outages are recorded; this revision cannot be undone without '
                'losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
