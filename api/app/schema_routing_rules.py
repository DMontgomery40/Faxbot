"""Frozen 0029 sending rules; registration and validation belong to ``schema``.

The organization, each mailbox and each workflow may have one rules document.
Publishing a document creates an immutable revision, numbered 1, 2, 3 and so
on per scope. A fax accepted under rules keeps the decision Faxbot made for it
(its route envelope), each attempt keeps the account it was given, and holds
(approval, a time window, or no allowed route) keep a fax from being claimed:

- ``routing_rule_revisions``: one row per published revision of one scope's
  document. ``scope_kind`` is ``organization``, ``mailbox`` or ``workflow``;
  ``scope_id`` is ``''`` for the organization, the mailbox's ID, or the
  workflow's key. ``number`` is unique within the scope. Rows are never
  updated: there is no update path, and a revision is restored by publishing
  it again as a new revision.
- ``routing_rule_state``: the head of each scope (its active revision, or none
  for no rules) and a generation that every publish increments, so two
  administrators cannot overwrite each other.
- ``routing_rule_drafts``: one shared draft per scope, with an optimistic
  version and the result of its last check.
- ``fax_job_rule_decisions``: the decisions for one fax. Sequence 1 is written
  in the transaction that accepts the fax; re-deciding a waiting fax inserts the
  next sequence and never changes an earlier one, and the highest sequence is
  the fax's current decision. ``revisions`` maps each scope to the revision ID
  it was decided under (JSON, because the scopes vary); ``facts`` is the
  snapshot the rules read (never the document's content); ``decision`` is the
  route envelope with its trace.
- ``delivery_rule_choices``: one row per attempt, keyed by the attempt's ID:
  the account it was given, its place in the envelope and what was skipped.
- ``outbound_holds``: approval, time-window and no-route holds on a fax.

Every row under a sent fax goes with the fax (CASCADE), so cleanup of sent
faxes is unchanged. The downgrade drops these tables; it refuses while any
revision, decision or hold is stored, because they are the evidence of how
faxes were routed. Runtime code reflects these tables; it never imports this
metadata.
"""
import sqlalchemy as sa

from .schema_negotiation import frozen_metadata as previous_metadata


REVISION = '0029_routing_rules'
ORDER = ('routing_rule_revisions', 'routing_rule_state', 'routing_rule_drafts', 'fax_job_rule_decisions',
         'delivery_rule_choices', 'outbound_holds')
TABLES = frozenset(ORDER)
# Literals belong to this revision; rules/model.py keeps equal constants and a test compares them.
SCOPE_KINDS = ('organization', 'mailbox', 'workflow')
OUTCOMES = ('route', 'held', 'blocked')
MODES = ('one', 'ordered', 'cheapest', 'automatic')
PAGE_LAYOUTS = ('as_receiver_allows', 'one_per_sheet')
HOLD_KINDS = ('approval', 'window', 'no_route')
HOLD_STATES = ('open', 'released', 'refused')
INDEXES = (
    ('uq_routing_rule_revisions_number', 'routing_rule_revisions', ('scope_kind', 'scope_id', 'number'), True),
    ('ix_routing_rule_revisions_parent', 'routing_rule_revisions', ('parent_id',), False),
    ('uq_routing_rule_state_scope', 'routing_rule_state', ('scope_kind', 'scope_id'), True),
    ('ix_routing_rule_state_active', 'routing_rule_state', ('active_revision_id',), False),
    ('uq_routing_rule_drafts_scope', 'routing_rule_drafts', ('scope_kind', 'scope_id'), True),
    ('ix_routing_rule_drafts_base', 'routing_rule_drafts', ('base_revision_id',), False),
    ('uq_fax_job_rule_decisions_sequence', 'fax_job_rule_decisions', ('job_id', 'sequence'), True),
    ('ix_fax_job_rule_decisions_created', 'fax_job_rule_decisions', ('created_at', 'id'), False),
    ('ix_delivery_rule_choices_job', 'delivery_rule_choices', ('job_id', 'created_at'), False),
    ('ix_delivery_rule_choices_decision', 'delivery_rule_choices', ('decision_id',), False),
    ('ix_outbound_holds_state_job', 'outbound_holds', ('state', 'job_id'), False),
    ('ix_outbound_holds_due', 'outbound_holds', ('state', 'release_at'), False),
    ('ix_outbound_holds_job', 'outbound_holds', ('job_id', 'requested_at'), False),
    ('ix_outbound_holds_decision', 'outbound_holds', ('decision_id',), False),
)


def _choice(column, values, *, nullable=False):
    expression = ' OR '.join(f"{column} = '{value}'" for value in values)
    return f'{column} IS NULL OR {expression}' if nullable else expression


def _id(name='id', nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _scope():
    return (sa.Column('scope_kind', sa.String(16), nullable=False),
            sa.Column('scope_id', sa.String(100), nullable=False))


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'routing_rule_revisions': (
            _id(),
            *_scope(),
            sa.Column('number', sa.Integer(), nullable=False),
            _id('parent_id', nullable=True),
            sa.Column('format_version', sa.Integer(), nullable=False),
            sa.Column('document', sa.Text(), nullable=False),
            sa.Column('digest', sa.String(64), nullable=False),
            sa.Column('note', sa.String(200), nullable=True),
            _id('actor_principal_id', nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_routing_rule_revisions'),
            sa.CheckConstraint(_choice('scope_kind', SCOPE_KINDS), name='ck_routing_rule_revisions_scope'),
            sa.CheckConstraint('number >= 1 AND format_version >= 1', name='ck_routing_rule_revisions_counts'),
            sa.ForeignKeyConstraint(['parent_id'], ['routing_rule_revisions.id'],
                                    name='fk_routing_rule_revisions_parent', ondelete='RESTRICT'),
        ),
        'routing_rule_state': (
            _id(),
            *_scope(),
            _id('active_revision_id', nullable=True),
            sa.Column('generation', sa.Integer(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_routing_rule_state'),
            sa.CheckConstraint(_choice('scope_kind', SCOPE_KINDS), name='ck_routing_rule_state_scope'),
            sa.CheckConstraint('generation >= 0', name='ck_routing_rule_state_generation'),
            sa.ForeignKeyConstraint(['active_revision_id'], ['routing_rule_revisions.id'],
                                    name='fk_routing_rule_state_active', ondelete='RESTRICT'),
        ),
        'routing_rule_drafts': (
            _id(),
            *_scope(),
            _id('base_revision_id', nullable=True),
            sa.Column('document', sa.Text(), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            _id('actor_principal_id', nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            # The last check (errors, warnings and the replay summary) and the draft version it covered.
            sa.Column('check_result', sa.Text(), nullable=True),
            sa.Column('checked_version', sa.Integer(), nullable=True),
            sa.Column('checked_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_routing_rule_drafts'),
            sa.CheckConstraint(_choice('scope_kind', SCOPE_KINDS), name='ck_routing_rule_drafts_scope'),
            sa.CheckConstraint('version >= 1 AND checked_version >= 1', name='ck_routing_rule_drafts_versions'),
            sa.ForeignKeyConstraint(['base_revision_id'], ['routing_rule_revisions.id'],
                                    name='fk_routing_rule_drafts_base', ondelete='RESTRICT'),
        ),
        'fax_job_rule_decisions': (
            _id(),
            _id('job_id'),
            sa.Column('sequence', sa.Integer(), nullable=False),
            sa.Column('revisions', sa.Text(), nullable=False),
            sa.Column('facts', sa.Text(), nullable=False),
            sa.Column('facts_digest', sa.String(64), nullable=False),
            sa.Column('decision', sa.Text(), nullable=False),
            sa.Column('dial_number', sa.String(32), nullable=True),
            _id('approval_id', nullable=True),
            sa.Column('page_layout', sa.String(32), nullable=True),
            sa.Column('outcome', sa.String(16), nullable=False),
            # Why a later sequence was written ("apply the current rules to waiting faxes"); NULL at acceptance.
            sa.Column('reason', sa.String(200), nullable=True),
            _id('actor_principal_id', nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_job_rule_decisions'),
            sa.CheckConstraint(_choice('outcome', OUTCOMES), name='ck_fax_job_rule_decisions_outcome'),
            sa.CheckConstraint(_choice('page_layout', PAGE_LAYOUTS, nullable=True),
                               name='ck_fax_job_rule_decisions_layout'),
            sa.CheckConstraint('sequence >= 1', name='ck_fax_job_rule_decisions_sequence'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'],
                                    name='fk_fax_job_rule_decisions_job', ondelete='CASCADE'),
        ),
        'delivery_rule_choices': (
            # The primary key is the outbound attempt identity: one row per attempt.
            _id(),
            _id('job_id'),
            _id('decision_id', nullable=True),
            sa.Column('scope_kind', sa.String(16), nullable=True),
            sa.Column('rule_id', sa.String(64), nullable=True),
            sa.Column('account_key', sa.String(64), nullable=False),
            sa.Column('mode', sa.String(16), nullable=False),
            sa.Column('place', sa.Integer(), nullable=False),
            sa.Column('dialed_number', sa.String(32), nullable=True),
            sa.Column('skipped', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_delivery_rule_choices'),
            sa.CheckConstraint(_choice('scope_kind', SCOPE_KINDS, nullable=True), name='ck_delivery_rule_choices_scope'),
            sa.CheckConstraint(_choice('mode', MODES), name='ck_delivery_rule_choices_mode'),
            sa.CheckConstraint('place >= 0', name='ck_delivery_rule_choices_place'),
            sa.ForeignKeyConstraint(['id'], ['outbound_attempts.id'],
                                    name='fk_delivery_rule_choices_attempt', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'],
                                    name='fk_delivery_rule_choices_job', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['decision_id'], ['fax_job_rule_decisions.id'],
                                    name='fk_delivery_rule_choices_decision', ondelete='CASCADE'),
        ),
        'outbound_holds': (
            _id(),
            _id('job_id'),
            sa.Column('kind', sa.String(16), nullable=False),
            _id('decision_id', nullable=True),
            sa.Column('rule_id', sa.String(64), nullable=True),
            # Time-window holds: when the window opens (naive UTC), worked out at acceptance.
            sa.Column('release_at', sa.DateTime(), nullable=True),
            sa.Column('state', sa.String(16), nullable=False),
            # Approval holds: the approver must not be the sender, who is recorded here.
            sa.Column('separate_approver', sa.Integer(), nullable=False),
            _id('requested_by_principal_id', nullable=True),
            # What an approval binds to (document, destination, dialed number, revisions); a change voids it.
            sa.Column('bound_digest', sa.String(64), nullable=True),
            sa.Column('requested_at', sa.DateTime(), nullable=False),
            _id('decided_by_principal_id', nullable=True),
            sa.Column('decided_by_name', sa.String(200), nullable=True),
            sa.Column('decided_at', sa.DateTime(), nullable=True),
            sa.Column('reason', sa.String(500), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_outbound_holds'),
            sa.CheckConstraint(_choice('kind', HOLD_KINDS), name='ck_outbound_holds_kind'),
            sa.CheckConstraint(_choice('state', HOLD_STATES), name='ck_outbound_holds_state'),
            sa.CheckConstraint('(separate_approver = 0 OR separate_approver = 1) AND version >= 1',
                               name='ck_outbound_holds_flags'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_outbound_holds_job', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['decision_id'], ['fax_job_rule_decisions.id'],
                                    name='fk_outbound_holds_decision', ondelete='CASCADE'),
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


def upgrade_routing_rules(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view before any DDL; never adopt an existing object.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('Sending rule table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_routing_rules(connection, operations):
    # Revisions, decisions and holds are the record of how faxes were routed; never drop them silently.
    for name in ('routing_rule_revisions', 'fax_job_rule_decisions', 'outbound_holds'):
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            _refuse('Sending rules or routing decisions are recorded; this revision cannot be undone without '
                    'losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
