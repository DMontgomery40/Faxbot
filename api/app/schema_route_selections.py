"""Frozen 0067: the account and pages each attempt was bound to after its accounts were compared (brief 84, JO).

``fax_route_selections`` holds one row per attempt whose route choice measured every account the fax could use
and bound the cheapest account with its own best pages (``routing.joint``). It is written once, before the
attempt's durable submission marker, and never rewritten:

- ``account_key`` and ``provider_id``: the account bound (``sip-pages``) and its provider (``sip``); ``number``
  the number it calls.
- ``rendering``, ``layout``, ``coding``, ``original_pages`` and ``sent_pages``: the pages the attempt sends.
- ``expected_micros`` and ``currency``: their expected bill on that account (NULL: unknown, never zero), over the
  call's duration spread; ``plan_units`` and ``plan_unit`` the pages, minutes or faxes of a monthly plan it uses.
- ``runner_*``: the cheapest other account compared, with its own best pages and price, for the sentence that
  says why this account (NULL when no other account had a known price).
- ``compared``: how many accounts were measured; ``measured`` 1 when the pages were measured, 0 for a page count.
- ``artifact_sha256`` (the file the call sends, NULL when the fax's own file goes), ``pixels_sha256`` (the pages'
  pixels), ``source_sha256`` (the fax's own PDF) and ``tariff_sha256`` (the account's tariff as priced) tie the
  price to exactly what was sent.
- ``slot_at``: the sending time chosen when a later time was found cheaper (NULL: sent when ready).
- ``candidates``: every candidate compared, as JSON (account, rendering, layout, coding, pages, micros, currency,
  seconds), for review.

Runtime code reflects the table; it never imports this metadata. The downgrade refuses while rows exist.
"""
import sqlalchemy as sa

from .schema_header_notice import frozen_metadata as previous_metadata

REVISION = '0067_route_selections'
ORDER = ('fax_route_selections',)
TABLES = frozenset(ORDER)
LAYOUTS = ('normal', 'dense', 'codec')
RENDERINGS = ('as_is', 'screened', 'whitened')
PLAN_UNITS = ('pages', 'minutes', 'faxes')
INDEXES = (
    ('uq_fax_route_selections_attempt', 'fax_route_selections', ('attempt_id',), True),
    ('ix_fax_route_selections_job', 'fax_route_selections', ('job_id', 'created_at'), False),
)


def _choice(column, values):
    # NULL passes (unknown); no grouping parentheses (PostgreSQL reflection round trip).
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    return {
        'fax_route_selections': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('attempt_id', sa.String(40), nullable=False),
            sa.Column('account_key', sa.String(64), nullable=False),
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('number', sa.String(32), nullable=True),
            sa.Column('rendering', sa.String(16), nullable=False),
            sa.Column('layout', sa.String(16), nullable=False),
            sa.Column('coding', sa.String(8), nullable=True),
            sa.Column('original_pages', sa.Integer(), nullable=False),
            sa.Column('sent_pages', sa.Integer(), nullable=False),
            sa.Column('expected_micros', sa.Integer(), nullable=True),
            sa.Column('currency', sa.String(3), nullable=True),
            sa.Column('plan_units', sa.Integer(), nullable=True),
            sa.Column('plan_unit', sa.String(8), nullable=True),
            sa.Column('measured', sa.Integer(), nullable=False),
            sa.Column('compared', sa.Integer(), nullable=False),
            sa.Column('runner_key', sa.String(64), nullable=True),
            sa.Column('runner_layout', sa.String(16), nullable=True),
            sa.Column('runner_pages', sa.Integer(), nullable=True),
            sa.Column('runner_micros', sa.Integer(), nullable=True),
            sa.Column('runner_currency', sa.String(3), nullable=True),
            sa.Column('artifact_sha256', sa.String(64), nullable=True),
            sa.Column('pixels_sha256', sa.String(64), nullable=True),
            sa.Column('source_sha256', sa.String(64), nullable=True),
            sa.Column('tariff_sha256', sa.String(64), nullable=False),
            sa.Column('slot_at', sa.DateTime(), nullable=True),
            sa.Column('candidates', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_route_selections'),
            sa.CheckConstraint(_choice('layout', LAYOUTS), name='ck_fax_route_selections_layout'),
            sa.CheckConstraint(_choice('rendering', RENDERINGS), name='ck_fax_route_selections_rendering'),
            sa.CheckConstraint(_choice('runner_layout', LAYOUTS), name='ck_fax_route_selections_runner_layout'),
            sa.CheckConstraint(_choice('plan_unit', PLAN_UNITS), name='ck_fax_route_selections_plan_unit'),
            sa.CheckConstraint('original_pages >= 1 AND sent_pages >= 1 AND compared >= 1 AND measured >= 0 '
                               'AND measured <= 1 AND expected_micros >= 0 AND runner_micros >= 0 '
                               'AND runner_pages >= 1 AND plan_units >= 0',
                               name='ck_fax_route_selections_values'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for name, columns in _definitions().items():
        sa.Table(name, metadata, *columns)
    for index, name, columns, unique in INDEXES:
        sa.Index(index, *(metadata.tables[name].c[column] for column in columns), unique=unique)
    return metadata


def upgrade_route_selections(connection, operations):
    from .schema import SchemaUpgradeError
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Route selection tables already exist before their migration.')
    for name, columns in _definitions().items():
        operations.create_table(name, *columns)
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_route_selections(connection, operations):
    from .schema import SchemaUpgradeError
    rows = connection.execute(sa.select(sa.func.count()).select_from(sa.table('fax_route_selections'))).scalar()
    if rows:
        raise SchemaUpgradeError('Faxes sent after comparing accounts keep their route records; this revision '
                                 'cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
