"""Frozen 0042 fax-friendly pages; registration and validation belong to ``schema``.

Faxbot can leave light shading out of a page and remove specks before the page
goes on the line (``pages/friendly.py``): a pixel whose gray is 25% gray or
lighter becomes white and every darker pixel stays as it was. Whether it runs
is decided for each attempt (the setting for your documents, the recipient's
own choice, the route's billing and the receiving machine). This revision adds
two tables:

- ``fax_friendly_pages``: append-only, one row for each attempt whose pages
  were lightened (``attempt_id``; NULL only for an image made outside an
  attempt). ``scope`` is 'documents' (your documents) or 'drawn' (a page
  Faxbot drew itself). ``pages`` is the pages in the document and
  ``pages_changed`` how many of them changed; ``bits_before`` and
  ``bits_after`` are the measured MMR (Group 4) bits of every page before and
  after, and ``seconds_saved`` the estimate of the time saved on the line at
  14,400 bit/s (NULL when not estimated).
- ``fax_friendly_recipients``: one row per number a person chose for:
  ``shading`` 'always' or 'never'. No row follows the setting for all faxes.

Rows of ``fax_friendly_pages`` are never rewritten; the recipients table holds
a person's current choice. The downgrade drops both tables. Runtime code
reflects them; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_dense_pages import frozen_metadata as previous_metadata


REVISION = '0042_fax_friendly_pages'
ORDER = ('fax_friendly_pages', 'fax_friendly_recipients')
TABLES = frozenset(ORDER)
SCOPES = ('drawn', 'documents')
SHADING = ('always', 'never')
INDEXES = (
    ('ix_fax_friendly_pages_job', 'fax_friendly_pages', ('job_id', 'created_at'), False),
    ('ix_fax_friendly_pages_created_at', 'fax_friendly_pages', ('created_at', 'id'), False),
    ('ix_fax_friendly_pages_attempt', 'fax_friendly_pages', ('attempt_id',), False),
    ('uq_fax_friendly_recipients_number', 'fax_friendly_recipients', ('number',), True),
)


def _choice(column, values):
    # No grouping parentheses (PostgreSQL reflection round trip).
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    return {
        'fax_friendly_pages': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('attempt_id', sa.String(40), nullable=True),
            sa.Column('scope', sa.String(16), nullable=False),
            sa.Column('pages', sa.Integer(), nullable=False),
            sa.Column('pages_changed', sa.Integer(), nullable=False),
            sa.Column('bits_before', sa.Integer(), nullable=False),
            sa.Column('bits_after', sa.Integer(), nullable=False),
            sa.Column('seconds_saved', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_friendly_pages'),
            sa.CheckConstraint(_choice('scope', SCOPES), name='ck_fax_friendly_pages_scope'),
            sa.CheckConstraint('pages >= 1 AND pages_changed >= 1 AND pages_changed <= pages AND bits_before >= 0 '
                               'AND bits_after >= 0 AND seconds_saved >= 0', name='ck_fax_friendly_pages_counts'),
        ),
        'fax_friendly_recipients': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('shading', sa.String(16), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('updated_by', sa.String(100), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_fax_friendly_recipients'),
            sa.CheckConstraint(_choice('shading', SHADING), name='ck_fax_friendly_recipients_shading'),
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


def upgrade_friendly_pages(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view before any DDL; never adopt an existing object.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('A fax-friendly pages table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_friendly_pages(connection, operations):
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
