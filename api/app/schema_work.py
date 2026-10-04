"""Frozen 0011 work items; registration and validation belong to ``schema``.

A work item is the accountable owner's record for one received document: who
owns it, when it must be acknowledged, and whether it is done. Transport,
email delivery and acknowledgement stay separate records; this revision adds
only the work layer above received documents. ``work_events`` is append-only
history; ``dedupe_key`` gives repeated escalation one effect.

The revision also adds the four work permissions to the frozen access
catalogue and to the built-in roles, because the access store validates the
catalogue exactly at startup. Only rows are added: existing roles, assignments,
the policy version and the audit trail are untouched, and the revision number
itself records the change. Runtime code reflects these tables; it never
imports this metadata.
"""
import sqlalchemy as sa

from .schema_access import _identity
from .schema_inbound import frozen_metadata as previous_metadata


REVISION = '0011_work_items'
# Creation order respects foreign keys.
ORDER = ('work_items', 'work_events', 'work_mailbox_settings')
TABLES = frozenset(ORDER)
STATES = ('open', 'acknowledged', 'done')
EVENT_KINDS = ('received', 'assigned', 'acknowledged', 'escalated', 'reassigned', 'done', 'exported', 'reopened')
DUE_SOURCES = ('mailbox', 'installation')
INDEXES = (
    ('uq_work_items_inbound_fax', 'work_items', ('inbound_fax_id',), True),
    ('ix_work_items_state_due', 'work_items', ('state', 'due_at'), False),
    ('ix_work_items_owner_state', 'work_items', ('owner_principal_id', 'state'), False),
    ('ix_work_items_mailbox', 'work_items', ('mailbox_id',), False),
    ('ix_work_items_digest', 'work_items', ('content_digest',), False),
    ('uq_work_events_dedupe', 'work_events', ('work_item_id', 'dedupe_key'), True),
    ('ix_work_events_item_time', 'work_events', ('work_item_id', 'occurred_at'), False),
    ('uq_work_mailbox_settings_mailbox', 'work_mailbox_settings', ('mailbox_id',), True),
)
# Literals belong to this revision, never the mutable runtime catalogue.
PERMISSIONS = {
    'work:read': 'Read the work queue for received documents',
    'work:manage': 'Assign, complete and reopen work on received documents',
    'work:export': 'Export evidence for received documents',
    'work:import': 'Import documents from another system',
}
ROLE_PERMISSIONS = (
    ('role_owner', ('work:read', 'work:manage', 'work:export', 'work:import')),
    ('role_administrator', ('work:read', 'work:manage', 'work:export', 'work:import')),
    ('role_fax_operator', ('work:read', 'work:manage')),
    ('role_fax_viewer', ('work:read',)),
    ('role_auditor', ('work:read', 'work:export')),
)


def _id(name='id', nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'work_items': (
            _id(),
            _id('inbound_fax_id'),
            _id('mailbox_id', True),
            sa.Column('state', sa.String(16), nullable=False),
            _id('owner_principal_id', True),
            _id('backup_principal_id', True),
            # When the document became available: the acknowledgement clock starts here.
            sa.Column('available_at', sa.DateTime(), nullable=False),
            sa.Column('due_at', sa.DateTime(), nullable=True),
            sa.Column('due_hours', sa.Integer(), nullable=True),
            sa.Column('due_source', sa.String(16), nullable=True),
            sa.Column('assigned_at', sa.DateTime(), nullable=True),
            sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
            _id('acknowledged_by', True),
            sa.Column('escalated_at', sa.DateTime(), nullable=True),
            sa.Column('done_at', sa.DateTime(), nullable=True),
            _id('done_by', True),
            sa.Column('done_note', sa.String(200), nullable=True),
            sa.Column('content_digest', sa.String(64), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_work_items'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_work_items_state'),
            sa.CheckConstraint('due_source IS NULL OR ' + _choice('due_source', DUE_SOURCES),
                               name='ck_work_items_due_source'),
            # NULL hours pass (a CHECK only rejects false).
            sa.CheckConstraint('due_hours >= 1 AND version >= 1', name='ck_work_items_counts'),
            sa.ForeignKeyConstraint(['inbound_fax_id'], ['inbound_faxes.id'],
                                    name='fk_work_items_inbound_fax', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['mailbox_id'], ['mailboxes.id'],
                                    name='fk_work_items_mailbox', ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['owner_principal_id'], ['access_principals.id'],
                                    name='fk_work_items_owner', ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['backup_principal_id'], ['access_principals.id'],
                                    name='fk_work_items_backup', ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['acknowledged_by'], ['access_principals.id'],
                                    name='fk_work_items_acknowledged_by', ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['done_by'], ['access_principals.id'],
                                    name='fk_work_items_done_by', ondelete='SET NULL'),
        ),
        'work_events': (
            _id(),
            _id('work_item_id'),
            sa.Column('kind', sa.String(24), nullable=False),
            # History keeps the acting principal's id and, in details, its name at the time.
            _id('actor_principal_id', True),
            sa.Column('occurred_at', sa.DateTime(), nullable=False),
            sa.Column('details', sa.Text(), nullable=False),
            sa.Column('dedupe_key', sa.String(64), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_work_events'),
            sa.CheckConstraint(_choice('kind', EVENT_KINDS), name='ck_work_events_kind'),
            sa.ForeignKeyConstraint(['work_item_id'], ['work_items.id'],
                                    name='fk_work_events_item', ondelete='CASCADE'),
        ),
        'work_mailbox_settings': (
            _id(),
            _id('mailbox_id'),
            # NULL uses the installation target; 0 means no target for this mailbox.
            sa.Column('acknowledge_hours', sa.Integer(), nullable=True),
            _id('backup_principal_id', True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_work_mailbox_settings'),
            sa.CheckConstraint('acknowledge_hours >= 0 AND acknowledge_hours <= 8760 AND version >= 1',
                               name='ck_work_mailbox_settings_values'),
            sa.ForeignKeyConstraint(['mailbox_id'], ['mailboxes.id'],
                                    name='fk_work_mailbox_settings_mailbox', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['backup_principal_id'], ['access_principals.id'],
                                    name='fk_work_mailbox_settings_backup', ondelete='SET NULL'),
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


def upgrade_work(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view, or an already-present permission, before any DDL.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Work tables already exist before their migration.')
    metadata = frozen_metadata(dialect=connection.dialect.name)
    permissions = metadata.tables['access_permissions']
    if connection.execute(sa.select(permissions.c.id).where(permissions.c.id.in_(sorted(PERMISSIONS)))).first():
        raise SchemaUpgradeError('Work permissions already exist before their migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
        for index, table, columns, unique in INDEXES:
            if table == name:
                operations.create_index(index, name, list(columns), unique=unique)
    for permission, description in PERMISSIONS.items():
        connection.execute(permissions.insert().values(id=permission, description=description))
    members = metadata.tables['access_role_permissions']
    for role_id, granted in ROLE_PERMISSIONS:
        for permission in granted:
            connection.execute(members.insert().values(id=_identity('role_permission', role_id, permission),
                                                       role_id=role_id, permission_id=permission))
