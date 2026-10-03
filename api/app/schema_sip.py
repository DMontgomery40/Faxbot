"""Frozen 0009 SIP call records; registration and validation belong to ``schema``.

One row per telephone call Faxbot's own fax engine places or answers over the
carrier trunk: the evidence a carrier bills from (start, answer, end, connected
seconds, disposition) next to what the fax engine observed (T.38 or audio,
pages, remote station). Rows are independent of jobs so call history outlives
job cleanup. Runtime code reflects the table; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_delivery import frozen_metadata as previous_metadata


REVISION = '0009_sip_call_records'
TABLES = frozenset({'sip_call_records'})
DISPOSITIONS = ('answered', 'busy', 'congestion', 'failed', 'no_answer', 'ambiguous')
INDEXES = (
    ('uq_sip_call_records_call', ('direction', 'call_id'), True),
    ('ix_sip_call_records_attempt', ('attempt_id',), False),
    ('ix_sip_call_records_started', ('started_at', 'id'), False),
    ('ix_sip_call_records_did', ('did', 'started_at'), False),
)


def _definition():
    dispositions = ' OR '.join(f"disposition = '{value}'" for value in DISPOSITIONS)
    return (
        sa.Column('id', sa.String(40), nullable=False),
        sa.Column('direction', sa.String(16), nullable=False),
        sa.Column('call_id', sa.String(100), nullable=False),
        sa.Column('job_id', sa.String(40), nullable=True),
        sa.Column('attempt_id', sa.String(40), nullable=True),
        sa.Column('trunk_preset', sa.String(32), nullable=True),
        sa.Column('did', sa.String(32), nullable=True),
        sa.Column('caller', sa.String(32), nullable=True),
        sa.Column('called', sa.String(32), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('answered_at', sa.DateTime(), nullable=True),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.Column('disposition', sa.String(16), nullable=False),
        sa.Column('connected_seconds', sa.Integer(), nullable=True),
        sa.Column('t38', sa.String(8), nullable=False),
        sa.Column('pages', sa.Integer(), nullable=True),
        sa.Column('fax_status', sa.String(16), nullable=True),
        sa.Column('remote_station_id', sa.String(40), nullable=True),
        sa.Column('error_cause', sa.String(64), nullable=True),
        sa.Column('fax_preference', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_sip_call_records'),
        sa.CheckConstraint("direction = 'outbound' OR direction = 'inbound'",
                           name='ck_sip_call_records_direction'),
        sa.CheckConstraint(dispositions, name='ck_sip_call_records_disposition'),
        sa.CheckConstraint("t38 = 'yes' OR t38 = 'no' OR t38 = 'unknown'", name='ck_sip_call_records_t38'),
        # NULL counts pass (a CHECK only rejects false); no grouping parentheses,
        # which PostgreSQL reflection does not round-trip for this grammar.
        sa.CheckConstraint('connected_seconds >= 0 AND pages >= 0 AND fax_preference >= 0 AND fax_preference <= 1',
                           name='ck_sip_call_records_counts'),
    )


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    table = sa.Table('sip_call_records', metadata, *_definition())
    for name, columns, unique in INDEXES:
        sa.Index(name, *(table.c[column] for column in columns), unique=unique)
    return metadata


def upgrade_sip(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view before any DDL; never adopt an existing object.
    if sa.inspect(connection).has_table('sip_call_records'):
        from .schema import SchemaUpgradeError
        raise SchemaUpgradeError('SIP call record table already exists before its migration.')
    operations.create_table('sip_call_records', *_definition())
    for name, columns, unique in INDEXES:
        operations.create_index(name, 'sip_call_records', list(columns), unique=unique)
