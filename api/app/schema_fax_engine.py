"""Frozen 0017 fax engine records; registration and validation belong to ``schema``.

Faxbot has two fax engines on the SIP trunk: the built-in one in Asterisk and
the SSL Fax engine (HylaFAX+). This revision adds:

- ``fax_engine_calls``: one row per trunk fax call, sent or received, naming
  the engine that handled it and, for the built-in engine, why (one sentence).
  The SSL Fax engine fills in what it saw: whether the pages went over SSL
  Fax, whether the other machine offered it, how long the pages took and the
  whole call took on the engine, the negotiated speed and compression, and
  the engine's own reference for the call (unique, so a result reported twice
  is recorded once). The telephone call itself (carrier Call-ID, answer and
  end) stays in ``sip_call_records``; ``call_key`` is that row's call id.
- ``sslfax_observations``: append-only, one row each time a call showed
  whether a fax number takes SSL Fax (sending or receiving). Whether a number
  "accepts SSL Fax" is its newest observation, with that date.
- ``recipient_fax_settings``: a person's own limits for one troublesome fax
  machine (highest speed, error correction), used on every call to it by both
  engines. Changed by people; the newest values apply.

Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_case_packets import frozen_metadata as previous_metadata


REVISION = '0017_fax_engine'
ORDER = ('fax_engine_calls', 'sslfax_observations', 'recipient_fax_settings')
TABLES = frozenset(ORDER)
INDEXES = (
    ('uq_fax_engine_calls_call', 'fax_engine_calls', ('direction', 'call_key'), True),
    ('uq_fax_engine_calls_engine_ref', 'fax_engine_calls', ('engine_ref',), True),
    ('ix_fax_engine_calls_created_at', 'fax_engine_calls', ('created_at', 'id'), False),
    ('ix_sslfax_observations_number', 'sslfax_observations', ('number', 'observed_at'), False),
    ('uq_sslfax_observations_source', 'sslfax_observations', ('number', 'source'), True),
    ('uq_recipient_fax_settings_number', 'recipient_fax_settings', ('number',), True),
)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'fax_engine_calls': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('direction', sa.String(16), nullable=False),
            # The outbound attempt id, or the received call's id in sip_call_records.
            sa.Column('call_key', sa.String(100), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('engine', sa.String(16), nullable=False),
            sa.Column('reason', sa.String(200), nullable=True),
            sa.Column('engine_ref', sa.String(100), nullable=True),
            sa.Column('number', sa.String(32), nullable=True),
            sa.Column('sslfax', sa.Integer(), nullable=True),
            sa.Column('sslfax_offered', sa.Integer(), nullable=True),
            sa.Column('transfer_seconds', sa.Integer(), nullable=True),
            sa.Column('session_seconds', sa.Integer(), nullable=True),
            sa.Column('signal_rate', sa.String(32), nullable=True),
            sa.Column('data_format', sa.String(32), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_engine_calls'),
            sa.CheckConstraint("direction = 'outbound' OR direction = 'inbound'",
                               name='ck_fax_engine_calls_direction'),
            sa.CheckConstraint("engine = 'builtin' OR engine = 'hylafax'", name='ck_fax_engine_calls_engine'),
            # NULL passes (unknown); no grouping parentheses (PostgreSQL reflection round trip).
            sa.CheckConstraint('sslfax >= 0 AND sslfax <= 1 AND sslfax_offered >= 0 AND sslfax_offered <= 1 '
                               'AND transfer_seconds >= 0 AND session_seconds >= 0',
                               name='ck_fax_engine_calls_values'),
        ),
        'sslfax_observations': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('direction', sa.String(16), nullable=False),
            sa.Column('accepts', sa.Integer(), nullable=False),
            # The engine call that showed it (engine reference), so a repeated report adds nothing.
            sa.Column('source', sa.String(100), nullable=False),
            sa.Column('observed_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_sslfax_observations'),
            sa.CheckConstraint("direction = 'outbound' OR direction = 'inbound'",
                               name='ck_sslfax_observations_direction'),
            sa.CheckConstraint('accepts >= 0 AND accepts <= 1', name='ck_sslfax_observations_accepts'),
        ),
        'recipient_fax_settings': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('max_rate', sa.Integer(), nullable=True),
            sa.Column('ecm', sa.Integer(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('updated_by', sa.String(100), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_recipient_fax_settings'),
            sa.CheckConstraint('max_rate = 14400 OR max_rate = 9600 OR max_rate = 7200 OR max_rate = 4800',
                               name='ck_recipient_fax_settings_rate'),
            sa.CheckConstraint('ecm >= 0 AND ecm <= 1', name='ck_recipient_fax_settings_ecm'),
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


def upgrade_fax_engine(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view before any DDL; never adopt an existing object.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Fax engine record table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)
