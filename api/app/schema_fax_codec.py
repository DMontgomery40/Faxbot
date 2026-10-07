"""Frozen 0031 fax payload codec (experimental); registration and validation belong to ``schema``.

A recipient can agree to receive a document as encoded payload pages that
their own Faxbot decodes. This revision adds:

- ``codec_numbers``: the current setting for one fax number: on or off, the
  page style ('dense' or 'picture'), the error-correction level, and an
  optional shared key, sealed with the installation key (only its fingerprint
  is ever shown). Mutable settings.
- ``codec_number_changes``: append-only history of every change to that
  setting, with who made it, when and whether they recorded that the
  recipient agreed. Rows are never updated.
- ``codec_sends``: one row per fax sent as payload pages, written when it is
  accepted: original and encoded page counts, the predicted line seconds and
  cost of each (NULL when unknown, never 0), the layout and the document's
  fingerprint. Rows are never updated; delivery comes from the fax's record.
- ``codec_receipts``: one row per received fax that carried payload pages:
  decoded or failed, the one-sentence reason for a failure, and where the
  decoded original is kept. The received fax image is never changed. Rows
  are never updated.

Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_negotiation import frozen_metadata as previous_metadata


REVISION = '0031_fax_codec'
ORDER = ('codec_numbers', 'codec_number_changes', 'codec_sends', 'codec_receipts')
TABLES = frozenset(ORDER)
STYLES = ('dense', 'picture')
LEVELS = ('low', 'medium', 'high')
CHANGE_ACTIONS = ('on', 'changed', 'off')
RECEIPT_STATES = ('decoded', 'failed')
INDEXES = (
    ('uq_codec_numbers_number', 'codec_numbers', ('phone_number',), True),
    ('ix_codec_number_changes_number', 'codec_number_changes', ('phone_number', 'created_at'), False),
    ('ix_codec_sends_created_at', 'codec_sends', ('created_at',), False),
    ('uq_codec_receipts_inbound_fax', 'codec_receipts', ('inbound_fax_id',), True),
)


def _id(name='id', nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'codec_numbers': (
            _id(),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('enabled', sa.Integer(), nullable=False),
            sa.Column('style', sa.String(16), nullable=False),
            sa.Column('fec', sa.String(8), nullable=False),
            sa.Column('secret_envelope', sa.Text(), nullable=True),
            sa.Column('key_fingerprint', sa.String(16), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_codec_numbers'),
            sa.CheckConstraint('enabled >= 0 AND enabled <= 1 AND version >= 1', name='ck_codec_numbers_values'),
            sa.CheckConstraint(_choice('style', STYLES), name='ck_codec_numbers_style'),
            sa.CheckConstraint(_choice('fec', LEVELS), name='ck_codec_numbers_fec'),
        ),
        'codec_number_changes': (
            _id(),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('action', sa.String(16), nullable=False),
            # The person or key that made the change (a replay scope such as principal:<id>) and its name then.
            sa.Column('actor', sa.String(100), nullable=False),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('recipient_agreed', sa.Integer(), nullable=False),
            sa.Column('style', sa.String(16), nullable=False),
            sa.Column('fec', sa.String(8), nullable=False),
            sa.Column('key_fingerprint', sa.String(16), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_codec_number_changes'),
            sa.CheckConstraint(_choice('action', CHANGE_ACTIONS), name='ck_codec_number_changes_action'),
            sa.CheckConstraint('recipient_agreed >= 0 AND recipient_agreed <= 1',
                               name='ck_codec_number_changes_agreed'),
        ),
        'codec_sends': (
            # One row per fax: the primary key is the fax job identity.
            _id(),
            sa.Column('phone_number', sa.String(32), nullable=False),
            # The route the encoded pages were made for; another route sends the original pages.
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('layout', sa.String(16), nullable=False),
            sa.Column('resolution', sa.String(16), nullable=False),
            sa.Column('fec', sa.String(8), nullable=False),
            sa.Column('pages_original', sa.Integer(), nullable=False),
            sa.Column('pages_encoded', sa.Integer(), nullable=False),
            sa.Column('seconds_original', sa.Integer(), nullable=True),
            sa.Column('seconds_encoded', sa.Integer(), nullable=True),
            sa.Column('cost_original_micros', sa.Integer(), nullable=True),
            sa.Column('cost_encoded_micros', sa.Integer(), nullable=True),
            sa.Column('currency', sa.String(3), nullable=True),
            sa.Column('basis', sa.String(300), nullable=True),
            sa.Column('document_sha256', sa.String(64), nullable=False),
            sa.Column('encrypted', sa.Integer(), nullable=False),
            sa.Column('format_version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_codec_sends'),
            sa.CheckConstraint('pages_original >= 1 AND pages_encoded >= 1 AND encrypted >= 0 AND encrypted <= 1 '
                               'AND format_version >= 1', name='ck_codec_sends_values'),
            sa.ForeignKeyConstraint(['id'], ['fax_jobs.id'], name='fk_codec_sends_job', ondelete='CASCADE'),
        ),
        'codec_receipts': (
            _id(),
            _id('inbound_fax_id'),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('reason', sa.String(300), nullable=True),
            sa.Column('layout', sa.String(16), nullable=True),
            sa.Column('pages_encoded', sa.Integer(), nullable=True),
            sa.Column('document_sha256', sa.String(64), nullable=True),
            sa.Column('content_type', sa.String(40), nullable=True),
            sa.Column('document_name', sa.String(200), nullable=True),
            sa.Column('size_bytes', sa.Integer(), nullable=True),
            sa.Column('document_path', sa.String(512), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_codec_receipts'),
            sa.CheckConstraint(_choice('state', RECEIPT_STATES), name='ck_codec_receipts_state'),
            sa.ForeignKeyConstraint(['inbound_fax_id'], ['inbound_faxes.id'], name='fk_codec_receipts_inbound_fax',
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


def upgrade_fax_codec(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view before any DDL; never adopt an existing object.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Fax payload codec tables already exist before their migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_fax_codec(connection, operations):
    # Like 0022, the downgrade removes what this revision added; decoded documents stay on disk.
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
