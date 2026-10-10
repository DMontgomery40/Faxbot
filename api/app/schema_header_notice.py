"""Frozen 0073 the header notice; registration and validation belong to ``schema``.

A confidentiality notice line Faxbot prints at the top of every page it sends,
under the header line the fax engine prints, and a cover sheet whose notice
travels in that line instead (``header_notice.py``). This revision adds three
tables and changes no stored row:

- ``header_notices``: append-only. One row per change to the notice of the
  organization (``scope`` ``organization``, ``mailbox_id`` NULL) or of one
  mailbox (``scope`` ``mailbox``). ``notice`` NULL removes it. The newest row
  per scope counts; older rows stay as history.
- ``fax_header_notices``: one row per accepted fax that carried a notice
  (``id`` is the fax's own ID): the notice printed, whose it was, what
  happened to its cover (``cover``: ``none`` when the sender did not mark
  one, ``dropped`` when the cover's notice went in the header and the page
  was not sent, ``kept`` when the recipient needs a cover sheet), and the
  page counts before and after.
- ``recipient_cover_changes``: append-only. One row per change to whether a
  recipient number needs a cover sheet; the newest row per number counts.

The downgrade drops the tables and refuses while any of them holds a row.
Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_dialing import frozen_metadata as previous_metadata


REVISION = '0073_header_notice'
ORDER = ('header_notices', 'fax_header_notices', 'recipient_cover_changes')
TABLES = frozenset(ORDER)
SCOPES = ('organization', 'mailbox')
COVERS = ('none', 'dropped', 'kept')
INDEXES = (
    ('ix_header_notices_scope', 'header_notices', ('scope', 'mailbox_id', 'created_at'), False),
    ('ix_recipient_cover_changes_number', 'recipient_cover_changes', ('phone_number', 'created_at'), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _who():
    return (sa.Column('actor_principal_id', sa.String(40), nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True))


def _definitions():
    return {
        'header_notices': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('scope', sa.String(16), nullable=False),
            sa.Column('mailbox_id', sa.String(40), nullable=True),
            sa.Column('notice', sa.String(200), nullable=True),
            *_who(),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_header_notices'),
            sa.CheckConstraint(_choice('scope', SCOPES), name='ck_header_notices_scope'),
            sa.CheckConstraint("(scope = 'organization' AND mailbox_id IS NULL) OR "
                               "(scope = 'mailbox' AND mailbox_id IS NOT NULL)", name='ck_header_notices_mailbox'),
        ),
        'fax_header_notices': (
            # The fax's own ID.
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('notice', sa.String(200), nullable=False),
            sa.Column('scope', sa.String(16), nullable=False),
            sa.Column('mailbox_id', sa.String(40), nullable=True),
            sa.Column('cover', sa.String(16), nullable=False),
            sa.Column('original_pages', sa.Integer(), nullable=False),
            sa.Column('sent_pages', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_header_notices'),
            sa.CheckConstraint(_choice('scope', SCOPES), name='ck_fax_header_notices_scope'),
            sa.CheckConstraint(_choice('cover', COVERS), name='ck_fax_header_notices_cover'),
            sa.CheckConstraint('sent_pages >= 1 AND original_pages >= sent_pages',
                               name='ck_fax_header_notices_pages'),
            sa.ForeignKeyConstraint(['id'], ['fax_jobs.id'], name='fk_fax_header_notices_job', ondelete='CASCADE'),
        ),
        'recipient_cover_changes': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('needs_cover', sa.Integer(), nullable=False),
            *_who(),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_recipient_cover_changes'),
            sa.CheckConstraint('needs_cover = 0 OR needs_cover = 1', name='ck_recipient_cover_changes_flag'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for name, columns in _definitions().items():
        sa.Table(name, metadata, *columns)
    for index, name, columns, unique in INDEXES:
        sa.Index(index, *(metadata.tables[name].c[column] for column in columns), unique=unique)
    return metadata


def upgrade_header_notice(connection, operations):
    from .schema import SchemaUpgradeError
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Header notice tables already exist before their migration.')
    for name, columns in _definitions().items():
        operations.create_table(name, *columns)
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_header_notice(connection, operations):
    from .schema import SchemaUpgradeError
    for name in ORDER:
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            raise SchemaUpgradeError('Header notices, the faxes that carried one, or recipients that need a cover '
                                     'sheet are recorded; this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
