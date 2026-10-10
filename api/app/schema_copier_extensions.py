"""Frozen 0085 copier extensions: copiers that register to Faxbot's Asterisk and fax through Faxbot (N22).

Registration and validation belong to ``schema``. This revision adds one append-only table and changes no stored
row:

- ``copier_extensions``: each copier's extension as you set it, newest row per ``copier_id`` counts: its name, the
  extension digits, the SIP user name, pjsip's MD5 digest of its password (``md5_cred``; the password itself is
  shown once and never stored), the mailbox its faxes are sent from, the numbers whose received faxes go on to it
  (JSON), whether those faxes go on to it or stay in intake (``receive``), and ``state`` (``removed`` takes the
  extension away).

The downgrade drops the table and refuses while a copier is recorded, because the copier's registration depends on
it. Runtime code reflects this table; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_partner_admission import frozen_metadata as previous_metadata


REVISION = '0085_copier_extensions'
ORDER = ('copier_extensions',)
TABLES = frozenset(ORDER)
STATES = ('active', 'removed')
RECEIVE = ('copier', 'intake')
INDEXES = (
    ('ix_copier_extensions_copier', 'copier_extensions', ('copier_id', 'created_at'), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    return {
        'copier_extensions': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('copier_id', sa.String(40), nullable=False),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('extension', sa.String(8), nullable=False),
            sa.Column('username', sa.String(40), nullable=False),
            sa.Column('md5_cred', sa.String(32), nullable=False),
            sa.Column('mailbox_id', sa.String(40), nullable=True),
            sa.Column('numbers', sa.Text(), nullable=True),
            sa.Column('receive', sa.String(16), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('recorded_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_copier_extensions'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_copier_extensions_state'),
            sa.CheckConstraint(_choice('receive', RECEIVE), name='ck_copier_extensions_receive'),
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


def upgrade_copier_extensions(connection, operations):
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        _refuse('A copier extension table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_copier_extensions(connection, operations):
    if connection.execute(sa.select(sa.func.count()).select_from(sa.table('copier_extensions'))).scalar():
        _refuse('Copier extensions are recorded; this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
