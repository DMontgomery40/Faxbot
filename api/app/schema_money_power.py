"""Frozen 0081 money and power: dated exchange rates, NTT Data Connect, and faxes held while on battery (brief 99).

Registration and validation belong to ``schema``. This revision adds append-only tables and changes no stored row:

- ``power_hold_events``: a fax held, unsent, while the office's UPS was on battery and its runtime could not cover
  the call (``power.py``), and its release by itself when mains power returned or the runtime grew enough. The
  ``held`` row keeps what the call needed (``needed_seconds``, ``reserve_seconds``) and the runtime then; the
  ``released`` row says why (``mains``, ``runtime``, ``ups_off``). The hold itself stays an ``outbound_holds`` row.
- ``exchange_rates``: one dated rate per row, ``rate`` units of ``quote`` for one ``base``, as a decimal string.
  ``source`` ``ecb`` is the European Central Bank's euro reference rate (base EUR); ``own`` is a rate you set, such
  as the one on your invoice. The newest ``own`` row per pair counts; ``state`` ``cleared`` withdraws it.
- ``exchange_rate_fetches``: each time Faxbot read (or could not read) the reference rates, with the date they were
  for and the reason in one sentence when it failed.
- ``exchange_rate_settings``: whether Faxbot reads the reference rates (on unless you turned it off); newest counts.
- ``exchange_rate_uses``: how an attempt's routes were compared across currencies: each route's own amount and
  currency, the amount it was compared as, and the rate (with its date and source) that converted it. Stored
  amounts elsewhere stay in their own currency; this only explains the comparison.
- ``destination_data_connect``: whether a number's fax machine takes NTT Data Connect calls, set by you
  (``admin``) or learned from a call (``learned``, with the attempt). The newest row per number counts.
- ``data_connect_trunks``: whether an NTT Hikari Denwa trunk places Data Connect calls (on unless you turned it
  off, or Faxbot turned it off itself and said why in ``reason``). The newest row per account counts.
- ``data_connect_calls``: how one attempt's call went: as a Data Connect call, or refused by the recipient's
  network or machine (488) so the same attempt called again as an ordinary fax call.

Rows that belong to a fax go with it (CASCADE). Runtime code reflects these tables; it never imports this metadata.
The downgrade refuses while any row is stored, because they are what people set and what happened to faxes.
"""
import sqlalchemy as sa

from .schema_pots_quote import frozen_metadata as previous_metadata


REVISION = '0081_money_power'
ORDER = ('power_hold_events', 'exchange_rates', 'exchange_rate_fetches', 'exchange_rate_settings',
         'exchange_rate_uses', 'destination_data_connect', 'data_connect_trunks', 'data_connect_calls')
TABLES = frozenset(ORDER)
POWER_EVENTS = ('held', 'released')
POWER_WHY = ('battery', 'mains', 'runtime', 'ups_off')
RATE_SOURCES = ('ecb', 'own')
RATE_STATES = ('active', 'cleared')
FETCH_OUTCOMES = ('stored', 'unchanged', 'failed')
FACT_SOURCES = ('admin', 'learned')
CALL_OUTCOMES = ('data_connect', 'refused')
INDEXES = (
    ('ix_power_hold_events_job', 'power_hold_events', ('job_id', 'created_at'), False),
    ('ix_power_hold_events_hold', 'power_hold_events', ('hold_id',), False),
    ('ix_exchange_rates_pair', 'exchange_rates', ('base', 'quote', 'created_at'), False),
    ('ix_exchange_rates_dated', 'exchange_rates', ('source', 'rate_date'), False),
    ('ix_exchange_rate_fetches_attempted', 'exchange_rate_fetches', ('source', 'attempted_at'), False),
    ('ix_exchange_rate_settings_created', 'exchange_rate_settings', ('created_at',), False),
    ('ix_exchange_rate_uses_attempt', 'exchange_rate_uses', ('attempt_id',), False),
    ('ix_exchange_rate_uses_job', 'exchange_rate_uses', ('job_id', 'created_at'), False),
    ('ix_destination_data_connect_number', 'destination_data_connect', ('destination', 'created_at'), False),
    ('ix_data_connect_trunks_account', 'data_connect_trunks', ('account_key', 'created_at'), False),
    ('uq_data_connect_calls_attempt', 'data_connect_calls', ('attempt_id',), True),
    ('ix_data_connect_calls_job', 'data_connect_calls', ('job_id',), False),
)


def _choice(column, values):
    # NULL passes (unknown); no grouping parentheses (PostgreSQL reflection round trip).
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _by(prefix):
    return (sa.Column(f'{prefix}_by', sa.String(40), nullable=True),
            sa.Column(f'{prefix}_by_name', sa.String(200), nullable=True))


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'power_hold_events': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('hold_id', sa.String(40), nullable=True),
            sa.Column('event', sa.String(16), nullable=False),
            sa.Column('why', sa.String(16), nullable=False),
            sa.Column('needed_seconds', sa.Integer(), nullable=True),
            sa.Column('reserve_seconds', sa.Integer(), nullable=True),
            sa.Column('runtime_seconds', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_power_hold_events'),
            sa.CheckConstraint(_choice('event', POWER_EVENTS), name='ck_power_hold_events_event'),
            sa.CheckConstraint(_choice('why', POWER_WHY), name='ck_power_hold_events_why'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_power_hold_events_job', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['hold_id'], ['outbound_holds.id'], name='fk_power_hold_events_hold',
                                    ondelete='CASCADE'),
        ),
        'exchange_rates': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('base', sa.String(3), nullable=False),
            sa.Column('quote', sa.String(3), nullable=False),
            sa.Column('rate', sa.String(32), nullable=True),
            sa.Column('rate_date', sa.DateTime(), nullable=True),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('source_url', sa.String(512), nullable=True),
            sa.Column('note', sa.String(500), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            *_by('created'),
            sa.PrimaryKeyConstraint('id', name='pk_exchange_rates'),
            sa.CheckConstraint(_choice('source', RATE_SOURCES), name='ck_exchange_rates_source'),
            sa.CheckConstraint(_choice('state', RATE_STATES), name='ck_exchange_rates_state'),
        ),
        'exchange_rate_fetches': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('attempted_at', sa.DateTime(), nullable=False),
            sa.Column('outcome', sa.String(16), nullable=False),
            sa.Column('rate_date', sa.DateTime(), nullable=True),
            sa.Column('rates', sa.Integer(), nullable=True),
            sa.Column('error', sa.String(500), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_exchange_rate_fetches'),
            sa.CheckConstraint(_choice('source', RATE_SOURCES), name='ck_exchange_rate_fetches_source'),
            sa.CheckConstraint(_choice('outcome', FETCH_OUTCOMES), name='ck_exchange_rate_fetches_outcome'),
        ),
        'exchange_rate_settings': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('reference_enabled', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            *_by('created'),
            sa.PrimaryKeyConstraint('id', name='pk_exchange_rate_settings'),
            sa.CheckConstraint('reference_enabled = 0 OR reference_enabled = 1',
                               name='ck_exchange_rate_settings_enabled'),
        ),
        'exchange_rate_uses': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('attempt_id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('route_key', sa.String(64), nullable=False),
            sa.Column('amount', sa.String(32), nullable=False),
            sa.Column('currency', sa.String(3), nullable=False),
            sa.Column('compared_amount', sa.String(32), nullable=False),
            sa.Column('compared_in', sa.String(3), nullable=False),
            sa.Column('rate', sa.String(32), nullable=True),
            sa.Column('rate_date', sa.DateTime(), nullable=True),
            sa.Column('source', sa.String(16), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_exchange_rate_uses'),
            sa.CheckConstraint(_choice('source', RATE_SOURCES), name='ck_exchange_rate_uses_source'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_exchange_rate_uses_job',
                                    ondelete='CASCADE'),
        ),
        'destination_data_connect': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('destination', sa.String(32), nullable=False),
            sa.Column('capable', sa.Integer(), nullable=False),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('attempt_id', sa.String(40), nullable=True),
            sa.Column('note', sa.String(500), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            *_by('created'),
            sa.PrimaryKeyConstraint('id', name='pk_destination_data_connect'),
            sa.CheckConstraint('capable = 0 OR capable = 1', name='ck_destination_data_connect_capable'),
            sa.CheckConstraint(_choice('source', FACT_SOURCES), name='ck_destination_data_connect_source'),
        ),
        'data_connect_trunks': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('account_key', sa.String(64), nullable=False),
            sa.Column('enabled', sa.Integer(), nullable=False),
            sa.Column('reason', sa.String(500), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            *_by('created'),
            sa.PrimaryKeyConstraint('id', name='pk_data_connect_trunks'),
            sa.CheckConstraint('enabled = 0 OR enabled = 1', name='ck_data_connect_trunks_enabled'),
        ),
        'data_connect_calls': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('attempt_id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('account_key', sa.String(64), nullable=False),
            sa.Column('destination', sa.String(32), nullable=False),
            sa.Column('outcome', sa.String(16), nullable=False),
            sa.Column('warning', sa.String(200), nullable=True),
            sa.Column('kbps', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_data_connect_calls'),
            sa.CheckConstraint(_choice('outcome', CALL_OUTCOMES), name='ck_data_connect_calls_outcome'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_data_connect_calls_job',
                                    ondelete='CASCADE'),
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


def upgrade_money_power(connection, operations):
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        _refuse('An exchange-rate, Data Connect or power-hold table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_money_power(connection, operations):
    for name in ORDER:
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            _refuse('Exchange rates, Data Connect facts or held-fax records are stored; this revision cannot be '
                    'undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
