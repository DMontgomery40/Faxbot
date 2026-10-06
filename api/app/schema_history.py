"""Frozen 0022 history; registration and validation belong to ``schema``.

Earlier code rewrote or never kept four facts. This revision keeps them, and
changes no stored record except to take Faxbot's own eFax deletion state back
out of the provider's report:

- ``inbound_import_failures``: append-only, one row each time an import that
  had stopped (``failed``) was set going again: how many attempts it had made,
  its last plain-sentence error, when it stopped, when and by what it was
  resumed (``person``, with the person's ID and name as they were then, or
  ``notification``, a provider notice that arrived again). Never a document,
  a URL or a credential.
- ``inbound_provider_deletions``: one row per received fax whose copy Faxbot
  is deleting at the provider (eFax), keyed by the import's ID: ``pending``
  (retried at ``next_at``), ``stopped`` (gave up; delete it at the provider)
  or ``deleted``. This is the retry state that earlier code kept inside
  ``inbound_imports.report`` under ``efax_delete``.
- ``intake_items.delivered_to``: the addresses the email server accepted for
  a delivered email, as a JSON list. NULL means not recorded (delivered before
  this revision).
- ``carrier_call_checks.unpriced_records``: how many of the carrier records
  matched to a call had no price at its last check. A call still unpriced at
  the give-up time settles with that count kept, so its cost reads as
  incomplete, never as the priced part alone. NULL means none known. Unpriced
  carrier records are never stored, so this cannot be worked out later.

Moving the eFax marks: for each ``efax`` import whose report is a JSON object
holding a mark Faxbot wrote (``{"state": "pending" | "stopped", "attempts",
"since", "next_at"}``), the upgrade inserts the mark as a deletion row and
writes the report again without that key, in the encoding the report was first
stored in (sorted keys, no spaces, ASCII). Faxbot added the key to that
encoding, so the report then reads byte for byte as first written. A report
that is not JSON, not an object, or holds anything else under that key is left
as it is. The downgrade writes each pending or stopped deletion back into its
report the same way, then drops both tables and both columns; recorded
failures and email recipients are lost by a downgrade, so back up first.

Runtime code reflects these tables; it never imports this metadata.
"""
from datetime import datetime
import json

import sqlalchemy as sa

from .schema_capacity import frozen_metadata as previous_metadata


REVISION = '0022_history'
ORDER = ('inbound_import_failures', 'inbound_provider_deletions')
TABLES = frozenset(ORDER)
RESUMERS = ('person', 'notification')
DELETION_STATES = ('pending', 'stopped', 'deleted')
# (table, column, type) added to tables created by earlier revisions; all nullable.
ADDED_COLUMNS = (
    ('intake_items', 'delivered_to', sa.Text()),
    ('carrier_call_checks', 'unpriced_records', sa.Integer()),
)
INDEXES = (
    ('ix_inbound_import_failures_import', 'inbound_import_failures', ('import_id', 'resumed_at'), False),
    ('ix_inbound_import_failures_inbound_fax', 'inbound_import_failures', ('inbound_fax_id',), False),
    ('ix_inbound_provider_deletions_due', 'inbound_provider_deletions', ('state', 'next_at'), False),
    ('ix_inbound_provider_deletions_inbound_fax', 'inbound_provider_deletions', ('inbound_fax_id',), False),
)
MARK = 'efax_delete'


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'inbound_import_failures': (
            sa.Column('id', sa.String(40), nullable=False),
            # No foreign key to inbound_imports: an SQLite rebuild of that table must not cascade here.
            sa.Column('import_id', sa.String(40), nullable=False),
            sa.Column('inbound_fax_id', sa.String(40), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False),
            sa.Column('last_error', sa.String(200), nullable=True),
            sa.Column('failed_at', sa.DateTime(), nullable=True),
            sa.Column('resumed_at', sa.DateTime(), nullable=False),
            sa.Column('resumed_by', sa.String(16), nullable=False),
            sa.Column('principal_id', sa.String(40), nullable=True),
            sa.Column('principal_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_inbound_import_failures'),
            sa.CheckConstraint(_choice('resumed_by', RESUMERS), name='ck_inbound_import_failures_resumed_by'),
            sa.CheckConstraint('attempts >= 0', name='ck_inbound_import_failures_attempts'),
            sa.ForeignKeyConstraint(['inbound_fax_id'], ['inbound_faxes.id'],
                                    name='fk_inbound_import_failures_inbound_fax', ondelete='CASCADE'),
        ),
        'inbound_provider_deletions': (
            # The primary key is the import's ID: one row per received fax's provider copy.
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('inbound_fax_id', sa.String(40), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False),
            sa.Column('since', sa.DateTime(), nullable=False),
            sa.Column('next_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_inbound_provider_deletions'),
            sa.CheckConstraint(_choice('state', DELETION_STATES), name='ck_inbound_provider_deletions_state'),
            sa.CheckConstraint('attempts >= 0', name='ck_inbound_provider_deletions_attempts'),
            sa.ForeignKeyConstraint(['inbound_fax_id'], ['inbound_faxes.id'],
                                    name='fk_inbound_provider_deletions_inbound_fax', ondelete='CASCADE'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, column, kind in ADDED_COLUMNS:
        metadata.tables[table].append_column(sa.Column(column, kind, nullable=True))
    definitions = _definitions()
    for name in ORDER:
        sa.Table(name, metadata, *definitions[name])
    for index, name, columns, unique in INDEXES:
        table = metadata.tables[name]
        sa.Index(index, *(table.c[column] for column in columns), unique=unique)
    return metadata


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _when(value):
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None) if isinstance(value, str) else None
    except ValueError:
        return None


def read_mark(report):
    """``(report without the mark, mark)`` for a report holding a mark Faxbot wrote; else None."""
    try:
        data = json.loads(report) if isinstance(report, str) else None
    except ValueError:
        return None
    mark = data.get(MARK) if isinstance(data, dict) else None
    if (not isinstance(mark, dict) or mark.get('state') not in ('pending', 'stopped')
            or _when(mark.get('since')) is None or type(mark.get('attempts', 0)) is not int):
        return None
    return {key: value for key, value in data.items() if key != MARK}, mark


def _imports():
    return sa.table('inbound_imports', sa.column('id', sa.String()), sa.column('source', sa.String()),
                    sa.column('report', sa.Text()), sa.column('inbound_fax_id', sa.String()),
                    sa.column('updated_at', sa.DateTime()))


def _deletions():
    return sa.table('inbound_provider_deletions', sa.column('id', sa.String()), sa.column('inbound_fax_id', sa.String()),
                    sa.column('state', sa.String()), sa.column('attempts', sa.Integer()),
                    *(sa.column(name, sa.DateTime()) for name in ('since', 'next_at', 'created_at', 'updated_at')))


def upgrade_history(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or an existing column before any DDL; never adopt an object.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('History table already exists before its migration.')
    for table, column, _ in ADDED_COLUMNS:
        if not inspector.has_table(table) or column in {c['name'] for c in inspector.get_columns(table)}:
            raise SchemaUpgradeError('Delivery and call check tables are not in the expected state for their migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)
    for table, column, kind in ADDED_COLUMNS:
        operations.add_column(table, sa.Column(column, kind, nullable=True))
    imports, deletions = _imports(), _deletions()
    rows = connection.execute(sa.select(imports.c.id, imports.c.report, imports.c.inbound_fax_id,
                                        imports.c.updated_at).where(imports.c.source == 'efax',
                                                                    imports.c.report.like('%' + MARK + '%'))
                              .order_by(imports.c.id)).all()
    for identity, report, inbound_fax_id, updated_at in rows:
        found = read_mark(report)
        if found is None:
            continue
        rest, mark = found
        stopped = mark['state'] == 'stopped'
        connection.execute(deletions.insert().values(
            id=identity, inbound_fax_id=inbound_fax_id, state=mark['state'], attempts=mark.get('attempts') or 0,
            since=_when(mark['since']), next_at=None if stopped else _when(mark.get('next_at')),
            created_at=updated_at, updated_at=updated_at))
        connection.execute(imports.update().where(imports.c.id == identity).values(report=_encode(rest)))


def downgrade_history(connection, operations):
    imports, deletions = _imports(), _deletions()
    open_rows = connection.execute(sa.select(deletions).where(deletions.c.state.in_(('pending', 'stopped')))
                                   .order_by(deletions.c.id)).mappings().all()
    for row in open_rows:
        report = connection.execute(sa.select(imports.c.report).where(imports.c.id == row['id'])).scalar_one_or_none()
        try:
            data = json.loads(report or '{}')
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        mark = {'state': row['state'], 'attempts': row['attempts'],
                'since': row['since'].isoformat(timespec='seconds')}
        if row['state'] == 'pending' and row['next_at'] is not None:
            mark['next_at'] = row['next_at'].isoformat(timespec='seconds')
        data[MARK] = mark
        connection.execute(imports.update().where(imports.c.id == row['id']).values(report=_encode(data)))
    for table, column, _ in reversed(ADDED_COLUMNS):
        operations.drop_column(table, column)
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
