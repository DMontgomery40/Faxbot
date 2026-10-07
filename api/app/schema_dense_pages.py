"""Frozen 0028 dense pages; registration and validation belong to ``schema``.

Faxbot can stack several original pages onto one long fax page, up to the
longest page the receiving machine says it accepts (T.30 DIS bits 19-20: A4
297 mm, B4 364 mm or unlimited), and cut where that limit falls; and for a
machine without error correction it can leave out the blank bottom of pages
it rendered itself (``pages``). This revision adds five tables:

- ``page_capability_observations``: append-only, one row each time a sent
  call's session log showed the other machine's DIS: its longest page
  (``max_length``: 'a4', 'b4' or 'unlimited'), widest page (``max_width``:
  'a4', 'b4' or 'a3'), whether it takes fine resolution (``fine``), whether
  it has error correction (``ecm``) and its minimum time per scan line at
  fine resolution (``scan_ms``); and how long that call took from the end of
  one page to the start of the next (``boundary_ms``, averaged over
  ``boundaries``). ``source`` is the engine call that showed it (the
  attempt), so a result reported twice adds one row. The newest row for a
  number is what Faxbot knows; until there is one, Faxbot assumes A4 and
  never trims.
- ``recipient_page_settings``: one row per number a person set something
  for: ``packing`` 'allow' (as the receiving machine allows) or 'never', and
  ``trim_blank`` 1 or 0 (leave out blank page bottoms for a machine without
  error correction). NULL is the default (allow; the installation's trim
  setting).
- ``route_page_settings``: one row per delivery route a person set something
  for: ``long_pages`` 1 or 0 and ``trim_blank`` 1 or 0. NULL is the route's
  default: long pages on for Faxbot's own fax engines and off for cloud
  providers until someone checks the provider sends them unchanged; trimming
  on. The SIP trunk's row holds the installation's trim setting.
- ``fax_page_changes``: one row per sent attempt whose pages Faxbot changed:
  original pages and ``sent_pages``, the pages that attempt actually sent
  (an attempt without a row sent ``fax_jobs.pages``), the layout the chooser
  kept (``layout``: 'dense' or 'codec'; NULL: the pages' own layout) and its
  one-sentence ``reason``, the receiving machine's limit (NULL when
  not packed) and when it was learned (NULL: the A4 default), the route's
  billing (``per_page``, ``per_minute``, ``plan`` or ``unpriced``; NULL when
  not packed), pages saved, pages whose blank bottom was left out and how
  many rows, ``resolution`` 'standard' when a document that was really
  standard resolution went at standard, and the estimated seconds saved
  (NULL when not estimated).
- ``inbound_page_splits``: one row per received fax whose long pages Faxbot
  split back into the original pages: pages received and pages delivered.
  The received image itself is never changed.

NULL means not known, not reported or the default, never a guess. Rows are
never rewritten except the two settings tables, which hold a person's
current choice. The downgrade drops the five tables. Runtime code reflects
these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_negotiation import frozen_metadata as previous_metadata


REVISION = '0028_dense_pages'
ORDER = ('page_capability_observations', 'recipient_page_settings', 'route_page_settings', 'fax_page_changes',
         'inbound_page_splits')
TABLES = frozenset(ORDER)
LENGTHS = ('a4', 'b4', 'unlimited')
WIDTHS = ('a4', 'b4', 'a3')
ENGINES = ('builtin', 'hylafax')
PACKING = ('allow', 'never')
BILLING = ('per_page', 'per_minute', 'plan', 'unpriced')
INDEXES = (
    ('ix_page_capability_observations_number', 'page_capability_observations', ('number', 'observed_at'), False),
    ('uq_page_capability_observations_source', 'page_capability_observations', ('number', 'source'), True),
    ('uq_recipient_page_settings_number', 'recipient_page_settings', ('number',), True),
    ('uq_route_page_settings_route', 'route_page_settings', ('route',), True),
    ('uq_fax_page_changes_attempt', 'fax_page_changes', ('attempt_id',), True),
    ('ix_fax_page_changes_job', 'fax_page_changes', ('job_id', 'created_at'), False),
    ('ix_fax_page_changes_created_at', 'fax_page_changes', ('created_at', 'id'), False),
    ('uq_inbound_page_splits_inbound_fax', 'inbound_page_splits', ('inbound_fax_id',), True),
)


def _choice(column, values):
    # NULL passes (unknown); no grouping parentheses (PostgreSQL reflection round trip).
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'page_capability_observations': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('max_length', sa.String(16), nullable=False),
            sa.Column('max_width', sa.String(16), nullable=True),
            sa.Column('fine', sa.Integer(), nullable=True),
            sa.Column('ecm', sa.Integer(), nullable=True),
            sa.Column('scan_ms', sa.Integer(), nullable=True),
            sa.Column('boundary_ms', sa.Integer(), nullable=True),
            sa.Column('boundaries', sa.Integer(), nullable=True),
            sa.Column('engine', sa.String(16), nullable=False),
            # The engine call that showed it (the attempt), so a repeated report adds nothing.
            sa.Column('source', sa.String(100), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('observed_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_page_capability_observations'),
            sa.CheckConstraint(_choice('max_length', LENGTHS), name='ck_page_capability_observations_length'),
            sa.CheckConstraint(_choice('max_width', WIDTHS), name='ck_page_capability_observations_width'),
            sa.CheckConstraint(_choice('engine', ENGINES), name='ck_page_capability_observations_engine'),
            sa.CheckConstraint('fine >= 0 AND fine <= 1 AND ecm >= 0 AND ecm <= 1 AND scan_ms >= 0 '
                               'AND boundary_ms >= 0 AND boundaries >= 1',
                               name='ck_page_capability_observations_values'),
        ),
        'recipient_page_settings': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('packing', sa.String(16), nullable=True),
            sa.Column('trim_blank', sa.Integer(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('updated_by', sa.String(100), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_recipient_page_settings'),
            sa.CheckConstraint(_choice('packing', PACKING), name='ck_recipient_page_settings_packing'),
            sa.CheckConstraint('trim_blank >= 0 AND trim_blank <= 1', name='ck_recipient_page_settings_trim'),
        ),
        'route_page_settings': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('route', sa.String(64), nullable=False),
            sa.Column('long_pages', sa.Integer(), nullable=True),
            sa.Column('trim_blank', sa.Integer(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('updated_by', sa.String(100), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_route_page_settings'),
            sa.CheckConstraint('long_pages >= 0 AND long_pages <= 1 AND trim_blank >= 0 AND trim_blank <= 1',
                               name='ck_route_page_settings_values'),
        ),
        'fax_page_changes': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('attempt_id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=True),
            sa.Column('route', sa.String(64), nullable=False),
            sa.Column('original_pages', sa.Integer(), nullable=False),
            sa.Column('sent_pages', sa.Integer(), nullable=False),
            # The layout the chooser kept ('dense' or 'codec'; NULL: the pages' own layout) and its sentence.
            sa.Column('layout', sa.String(16), nullable=True),
            sa.Column('reason', sa.String(300), nullable=True),
            sa.Column('page_limit', sa.String(16), nullable=True),
            sa.Column('limit_learned_at', sa.DateTime(), nullable=True),
            sa.Column('billing', sa.String(16), nullable=True),
            sa.Column('pages_saved', sa.Integer(), nullable=False),
            sa.Column('trimmed_pages', sa.Integer(), nullable=True),
            sa.Column('trimmed_rows', sa.Integer(), nullable=True),
            # 'standard' when a document that was really standard resolution went at standard; else NULL.
            sa.Column('resolution', sa.String(16), nullable=True),
            sa.Column('seconds_saved', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_page_changes'),
            sa.CheckConstraint(_choice('page_limit', LENGTHS), name='ck_fax_page_changes_limit'),
            sa.CheckConstraint(_choice('billing', BILLING), name='ck_fax_page_changes_billing'),
            sa.CheckConstraint("resolution = 'standard'", name='ck_fax_page_changes_resolution'),
            sa.CheckConstraint("layout = 'dense' OR layout = 'codec'", name='ck_fax_page_changes_layout'),
            sa.CheckConstraint('original_pages >= 1 AND sent_pages >= 1 AND trimmed_pages >= 1 AND trimmed_rows >= 1 '
                               'AND seconds_saved >= 0', name='ck_fax_page_changes_pages'),
        ),
        'inbound_page_splits': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('inbound_fax_id', sa.String(40), nullable=False),
            sa.Column('received_pages', sa.Integer(), nullable=False),
            sa.Column('original_pages', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_inbound_page_splits'),
            sa.CheckConstraint('received_pages >= 1 AND original_pages >= 2', name='ck_inbound_page_splits_pages'),
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


def upgrade_dense_pages(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view before any DDL; never adopt an existing object.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('A dense pages table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_dense_pages(connection, operations):
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
