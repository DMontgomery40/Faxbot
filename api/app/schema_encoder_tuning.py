"""Frozen 0063 encoder tuning; registration and validation belong to ``schema``.

Two additive tables; no stored row changes:

- ``recipient_coding_tuning``: your per-number choices for lossless tuning
  (``pages.tuning``), append-only (the newest row for a number is in force).
  ``tune`` is NULL for "as set for all faxes" or 0 for off; ``tune_jbig`` is 1
  when you asked for tuned JBIG for that number too, after its warning.
- ``coding_tuning_calls``: what the SSL Fax engine reported about tuning on
  one call (hylafax/bin/negotiation, from the session log's "LOSSLESS TUNING"
  lines; hylafax/patches/0003): the coding it tuned (JBIG or MR), pages tuned
  and coded, the bytes sent and what plain coding would have sent, the JBIG
  settings, the SHA-256 of each coded raster, and whether the receiving machine
  refused a tuned page (RTN, error correction that could not get a page through,
  or a failed call after one). Once per call; never rewritten. A refusal makes
  that number's later calls use plain settings for that coding.
- ``partner_fax_engines``: what an enrolled partner said, in a statement it
  signed, about whether its own Faxbot fax engine answers its number (its
  decoder then reads tuned JBIG). Append-only; the newest statement counts.

The downgrade refuses while either table has a row, because they are your
choices and evidence. Runtime code reflects these tables; it never imports this
metadata.
"""
import sqlalchemy as sa

from .schema_routing_learning import frozen_metadata as previous_metadata


REVISION = '0063_encoder_tuning'
ORDER = ('recipient_coding_tuning', 'coding_tuning_calls', 'partner_fax_engines')
TABLES = frozenset(ORDER)
CODINGS = ('JBIG', 'MR')
INDEXES = (
    ('ix_recipient_coding_tuning_number', 'recipient_coding_tuning', ('number', 'created_at'), False),
    ('ix_coding_tuning_calls_number', 'coding_tuning_calls', ('number', 'created_at'), False),
    ('uq_coding_tuning_calls_call', 'coding_tuning_calls', ('call_key', 'coding'), True),
    ('ix_partner_fax_engines_peer', 'partner_fax_engines', ('peer_id', 'said_at'), False),
)


def _choice(column, values):
    # No grouping parentheses (PostgreSQL reflection round trip).
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    return {
        'recipient_coding_tuning': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('tune', sa.Integer(), nullable=True),
            sa.Column('tune_jbig', sa.Integer(), nullable=False),
            sa.Column('recorded_by', sa.String(40), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_recipient_coding_tuning'),
            sa.CheckConstraint('tune IS NULL OR tune = 0', name='ck_recipient_coding_tuning_tune'),
            sa.CheckConstraint('tune_jbig >= 0 AND tune_jbig <= 1', name='ck_recipient_coding_tuning_jbig'),
        ),
        'coding_tuning_calls': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('call_key', sa.String(64), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('number', sa.String(32), nullable=True),
            sa.Column('coding', sa.String(8), nullable=False),
            sa.Column('pages', sa.Integer(), nullable=False),
            sa.Column('tuned_pages', sa.Integer(), nullable=False),
            sa.Column('tuned_bytes', sa.Integer(), nullable=True),
            sa.Column('plain_bytes', sa.Integer(), nullable=True),
            sa.Column('settings', sa.String(200), nullable=True),
            sa.Column('digests', sa.Text(), nullable=True),
            sa.Column('sslfax', sa.Integer(), nullable=False),
            sa.Column('refused', sa.Integer(), nullable=False),
            sa.Column('reason', sa.String(300), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_coding_tuning_calls'),
            sa.CheckConstraint(_choice('coding', CODINGS), name='ck_coding_tuning_calls_coding'),
            sa.CheckConstraint('sslfax >= 0 AND sslfax <= 1', name='ck_coding_tuning_calls_sslfax'),
            sa.CheckConstraint('refused >= 0 AND refused <= 1', name='ck_coding_tuning_calls_refused'),
        ),
        'partner_fax_engines': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('own_engine', sa.Integer(), nullable=False),
            sa.Column('said_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_partner_fax_engines'),
            sa.CheckConstraint('own_engine >= 0 AND own_engine <= 1', name='ck_partner_fax_engines_own'),
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


def upgrade_encoder_tuning(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse an unexpected shape before any DDL;
    # never adopt an existing object.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A coding tuning table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_encoder_tuning(connection, operations):
    # Your per-number choices and what the engine reported are records; never drop them silently.
    for name in ORDER:
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            _refuse('Faxbot has coding tuning records; this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
