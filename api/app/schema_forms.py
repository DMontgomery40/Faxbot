"""Frozen 0038 registered forms; registration and validation belong to ``schema``.

Three new tables, and no change to any existing one:

- ``forms``: a named form. ``origin`` is ``local`` (imported here) or
  ``partner`` (fetched from an enrolled direct delivery partner, whose
  ``peer_id`` is kept).
- ``form_versions``: immutable versions. Nothing updates or deletes a row:
  a changed template or field is a new version with the next ``number``.
  ``address`` is the SHA-256 of the canonical form content (pages and
  fields; ``forms/model.py``) and is unique. ``content`` is that canonical
  JSON; ``backgrounds`` is the bilevel page rasters, zlib-compressed and
  base64-encoded; ``template`` keeps the imported file (base64) when it was
  imported here.
- ``form_deliveries``: each form sent or received. ``route`` is ``direct``
  (values and page hashes to a partner, who renders and compares) or ``fax``
  (the rendered pages as an ordinary fax). Outbound states: ``sending``,
  ``delivered`` (the partner's pages matched and were filed), ``mismatch``
  (they did not; nothing was filed), ``refused`` (a signed refusal for
  another reason), ``not_sent`` (the partner could not be reached),
  ``uncertain`` (the answer was lost; Faxbot asks the partner),
  ``not_received`` (the partner signed that it never had it) and ``faxed``.
  Inbound states: ``matched`` and ``mismatch``. ``fax_job_id`` names the
  fax a person chose to send after a direct delivery did not go through;
  the outcome that led to it is kept. ``field_values`` holds the canonical
  values (NULL for an inbound mismatch, which files nothing).

Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_case_ledger import frozen_metadata as previous_metadata


REVISION = '0038_registered_forms'
ORDER = ('forms', 'form_versions', 'form_deliveries')
TABLES = frozenset(ORDER)
ORIGINS = ('local', 'partner')
SOURCES = ('pdf_acroform', 'pdf_positions', 'svg_positions', 'partner')
DIRECTIONS = ('outbound', 'inbound')
ROUTES = ('direct', 'fax')
STATES = ('sending', 'delivered', 'mismatch', 'refused', 'not_sent', 'uncertain', 'not_received', 'faxed', 'matched')
INDEXES = (
    ('ix_forms_name', 'forms', ('name',), False),
    ('ix_form_versions_number', 'form_versions', ('form_id', 'number'), True),
    ('ix_form_versions_address', 'form_versions', ('address',), True),
    ('ix_form_deliveries_message', 'form_deliveries', ('direction', 'message_id'), True),
    ('ix_form_deliveries_state', 'form_deliveries', ('direction', 'state', 'updated_at'), False),
    ('ix_form_deliveries_created', 'form_deliveries', ('created_at',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'forms': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('origin', sa.String(16), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_forms'),
            sa.CheckConstraint(_choice('origin', ORIGINS), name='ck_forms_origin'),
        ),
        'form_versions': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('form_id', sa.String(40), nullable=False),
            sa.Column('number', sa.Integer(), nullable=False),
            sa.Column('address', sa.String(64), nullable=False),
            sa.Column('title', sa.String(200), nullable=False),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('backgrounds', sa.Text(), nullable=False),
            sa.Column('template', sa.Text(), nullable=True),
            sa.Column('template_media_type', sa.String(40), nullable=True),
            sa.Column('template_sha256', sa.String(64), nullable=True),
            sa.Column('page_count', sa.Integer(), nullable=False),
            sa.Column('field_count', sa.Integer(), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=True),
            sa.Column('created_by', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_form_versions'),
            sa.CheckConstraint(_choice('source', SOURCES), name='ck_form_versions_source'),
            sa.CheckConstraint('number >= 1', name='ck_form_versions_number'),
            sa.CheckConstraint('page_count >= 1', name='ck_form_versions_pages'),
            sa.ForeignKeyConstraint(['form_id'], ['forms.id'], name='fk_form_versions_form', ondelete='RESTRICT'),
        ),
        'form_deliveries': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('direction', sa.String(8), nullable=False),
            sa.Column('route', sa.String(8), nullable=False),
            sa.Column('message_id', sa.String(32), nullable=True),
            sa.Column('peer_id', sa.String(40), nullable=True),
            sa.Column('partner', sa.String(200), nullable=True),
            sa.Column('form_version_id', sa.String(40), nullable=True),
            sa.Column('form_address', sa.String(64), nullable=False),
            sa.Column('renderer', sa.String(40), nullable=False),
            sa.Column('resolution', sa.String(16), nullable=False),
            sa.Column('fax_number', sa.String(32), nullable=True),
            sa.Column('field_values', sa.Text(), nullable=True),
            sa.Column('page_hashes', sa.Text(), nullable=False),
            sa.Column('pages', sa.Integer(), nullable=False),
            sa.Column('digest', sa.String(64), nullable=True),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('detail', sa.String(300), nullable=True),
            sa.Column('fax_job_id', sa.String(40), nullable=True),
            sa.Column('principal_id', sa.String(40), nullable=True),
            sa.Column('principal_name', sa.String(200), nullable=True),
            sa.Column('decided_by', sa.String(200), nullable=True),
            sa.Column('decided_at', sa.DateTime(), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_form_deliveries'),
            sa.CheckConstraint(_choice('direction', DIRECTIONS), name='ck_form_deliveries_direction'),
            sa.CheckConstraint(_choice('route', ROUTES), name='ck_form_deliveries_route'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_form_deliveries_state'),
            sa.CheckConstraint('pages >= 1', name='ck_form_deliveries_pages'),
            sa.ForeignKeyConstraint(['form_version_id'], ['form_versions.id'],
                                    name='fk_form_deliveries_version', ondelete='RESTRICT'),
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


def upgrade_forms(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('A registered form table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_forms(connection, operations):
    # Registered forms and their send records are lost by a downgrade; back up first.
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
