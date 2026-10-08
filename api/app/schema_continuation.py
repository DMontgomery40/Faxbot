"""Frozen 0050 continuation; registration and validation belong to ``schema``.

A long fax that breaks part way (``partly_sent``) can be finished by sending
only the pages the receiving machine did not confirm, as a new fax a person
chooses (``routing/continuation.py``). This revision adds two tables and
changes no stored row:

- ``fax_page_reports``: what a call's fax engine or a cloud fax service said
  about the pages it confirmed, beyond the call record's page count. One row
  per attempt and ``source``, written once when the result arrives and never
  changed. ``source`` is ``hylafax`` (the SSL Fax engine's session log),
  ``sinch``, ``documo`` or ``humblefax``. For the engine, ``clean_pages`` is
  how many pages the receiving machine answered "received fine" (T.30 MCF)
  before any other answer, and ``flagged_page`` the first page it answered
  with damaged lines (RTP: kept but poor; RTN: not received well), NULL when
  none was. For a cloud service, ``pages_sent`` is its own count of pages
  sent successfully and ``total_pages`` its count of pages in the fax. NULL
  always means not reported, never 0.
- ``fax_continuations``: one row per broken attempt whose remaining pages a
  person chose to send: the fax (``job_id``) and attempt that broke, the new
  fax (``continuation_job_id``, unique), the pages it carries
  (``first_page`` to ``last_page``, the last page of the original), whose
  report confirmed the earlier pages (``confirmed_by``), the uncertain-fax
  item it settled when there was one, who chose it (their ID and the name
  they had then) and their reason. Written once; it is the link both ways.

The downgrade drops the tables, and refuses while any continuation is kept,
because it is the record of who sent which pages again. Runtime code reflects
these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_certainty import frozen_metadata as previous_metadata


REVISION = '0050_continuation'
REPORTS, CONTINUATIONS = 'fax_page_reports', 'fax_continuations'
ORDER = (REPORTS, CONTINUATIONS)
TABLES = frozenset(ORDER)
SOURCES = ('hylafax', 'sinch', 'documo', 'humblefax')
CONFIRMED_BY = ('builtin', 'hylafax', 'sinch', 'documo', 'humblefax')
INDEXES = (
    ('uq_fax_page_reports_attempt', REPORTS, ('attempt_id', 'source'), True),
    ('ix_fax_page_reports_job', REPORTS, ('job_id',), False),
    ('uq_fax_continuations_attempt', CONTINUATIONS, ('attempt_id',), True),
    ('uq_fax_continuations_new_job', CONTINUATIONS, ('continuation_job_id',), True),
    ('ix_fax_continuations_job', CONTINUATIONS, ('job_id',), False),
)


def _id(name='id', nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        REPORTS: (
            _id(),
            _id('job_id'),
            _id('attempt_id'),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('clean_pages', sa.Integer(), nullable=True),
            sa.Column('flagged_page', sa.Integer(), nullable=True),
            sa.Column('pages_sent', sa.Integer(), nullable=True),
            sa.Column('total_pages', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_page_reports'),
            sa.CheckConstraint(_choice('source', SOURCES), name='ck_fax_page_reports_source'),
            # NULL counts pass (a CHECK only rejects false); no grouping parentheses.
            sa.CheckConstraint('clean_pages >= 0 AND flagged_page >= 1 AND pages_sent >= 0 AND total_pages >= 0',
                               name='ck_fax_page_reports_counts'),
        ),
        CONTINUATIONS: (
            _id(),
            _id('job_id'),
            _id('attempt_id'),
            _id('continuation_job_id'),
            sa.Column('first_page', sa.Integer(), nullable=False),
            sa.Column('last_page', sa.Integer(), nullable=False),
            sa.Column('confirmed_by', sa.String(16), nullable=False),
            _id('item_id', True),
            _id('requested_by', True),
            sa.Column('requested_by_name', sa.String(200), nullable=True),
            sa.Column('reason', sa.String(400), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_continuations'),
            sa.CheckConstraint(_choice('confirmed_by', CONFIRMED_BY), name='ck_fax_continuations_confirmed_by'),
            sa.CheckConstraint('first_page >= 2 AND last_page >= first_page', name='ck_fax_continuations_pages'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    definitions = _definitions()
    tables = {name: sa.Table(name, metadata, *definitions[name]) for name in ORDER}
    for index, table, columns, unique in INDEXES:
        sa.Index(index, *(tables[table].c[column] for column in columns), unique=unique)
    return metadata


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_continuation(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A table for fax continuations already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
        for index, table, columns, unique in INDEXES:
            if table == name:
                operations.create_index(index, name, list(columns), unique=unique)


def downgrade_continuation(connection, operations):
    # Who sent which pages again is never dropped silently.
    if connection.execute(sa.select(sa.func.count()).select_from(sa.table(CONTINUATIONS))).scalar():
        _refuse('Continuations of broken faxes are recorded; this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
