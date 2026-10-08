"""Frozen 0046 notice fax and repair; registration and validation belong to ``schema``.

Four direct delivery capabilities between enrolled partners share this
revision (``direct/notice.py``, ``direct/transfer.py``, ``direct/repair.py``):

- ``direct_notices``: one row per notice fax on each side. Some intakes need a
  fax event, so the original goes by direct delivery and one opaque page goes
  by fax. ``notice_id`` is the random 20-digit ID printed on that page (as a
  barcode and as digits) and sent as the T.33 subaddress where the engine can.
  ``link_statement`` is the sender's signed statement binding the notice ID to
  the original (message ID and SHA-256), kept exactly as signed with
  ``link_signature``. The sender keeps its original fax (``job_id``) and the
  notice fax it queued (``notice_job_id``); the receiver keeps the received fax
  it paired (``inbound_id``), how (``matched_by``: ``sub``, ``barcode`` or
  ``person``) and its own signed pairing statement, which the sender keeps too.
  ``state``: ``announced``, ``queued``, ``paired`` or ``cancelled`` on the
  sender; ``waiting``, ``paired`` or ``cancelled`` on the receiver.
- ``direct_notice_scans``: append-only, one row per received fax the receiver
  examined for a notice, with the ID it read (or none), so each fax is read once.
- ``direct_transfers``: a resumable transfer of one sealed document in pieces
  (byte ranges of the encrypted document, each with a sequence number and a
  SHA-256), opened by a signed preflight (``offer``) and committed by the
  application only when every piece and the whole match. ``state``: ``open``,
  ``committed``, ``refused`` or ``abandoned``. ``confirmed`` is how many
  pieces the receiver holds (as it last said, on the sender).
- ``direct_transfer_pieces``: the pieces a receiver holds; one per transfer
  and sequence, so a repeated piece is never stored twice.
- ``direct_call_repairs``: a fax call to an enrolled partner that broke part
  way. The partner's signed statement (``statement``) says how many leading
  pages of that call it holds intact (``pages_held``); only the rest then go
  directly as fax image pages, and the receiver files one document made of the
  call's pages and the rest (``assembled_path``), once.
- ``direct_peers.notice_fax``: 1 when every original sent to the partner is
  paired with a notice fax (NULL means no). ``certificate_changed_sha256`` and
  ``certificate_changed_at``: the certificate a partner with a pinned
  certificate presented when it was not the pinned one (NULL means none seen).

No stored row is changed. The downgrade drops the tables and columns, and
refuses while any notice, transfer or repair is recorded, because those are
what two organizations signed. Runtime code reflects these tables; it never
imports this metadata.
"""
import sqlalchemy as sa

from .schema_partner_relay import frozen_metadata as previous_metadata


REVISION = '0046_notice_repair'
NOTICES, SCANS, TRANSFERS, PIECES, REPAIRS = ('direct_notices', 'direct_notice_scans', 'direct_transfers',
                                              'direct_transfer_pieces', 'direct_call_repairs')
ORDER = (NOTICES, SCANS, TRANSFERS, PIECES, REPAIRS)
TABLES = frozenset(ORDER)
# Rows two organizations signed; the downgrade refuses while any is kept.
SIGNED = (NOTICES, TRANSFERS, REPAIRS)
PEER_COLUMNS = (
    ('notice_fax', sa.Integer),
    ('certificate_changed_sha256', lambda: sa.String(64)),
    ('certificate_changed_at', sa.DateTime),
)
INDEXES = (
    ('uq_direct_notices_message', NOTICES, ('role', 'message_id'), True),
    ('ix_direct_notices_state', NOTICES, ('role', 'state', 'created_at'), False),
    ('ix_direct_notices_notice', NOTICES, ('role', 'notice_id'), False),
    ('ix_direct_notices_job', NOTICES, ('notice_job_id',), False),
    ('ix_direct_notices_inbound', NOTICES, ('inbound_id',), False),
    ('uq_direct_notice_scans_inbound', SCANS, ('inbound_id',), True),
    ('uq_direct_transfers_message', TRANSFERS, ('role', 'message_id'), True),
    ('uq_direct_transfer_pieces_sequence', PIECES, ('transfer_id', 'sequence'), True),
    ('ix_direct_transfers_state', TRANSFERS, ('role', 'state', 'updated_at'), False),
    ('uq_direct_call_repairs_repair', REPAIRS, ('role', 'repair_id'), True),
    ('ix_direct_call_repairs_attempt', REPAIRS, ('attempt_id',), False),
    ('ix_direct_call_repairs_message', REPAIRS, ('message_id',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        NOTICES: (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('role', sa.String(8), nullable=False),
            sa.Column('notice_id', sa.String(20), nullable=False),
            sa.Column('message_id', sa.String(32), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('document_sha256', sa.String(64), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('link_statement', sa.Text(), nullable=False),
            sa.Column('link_signature', sa.String(128), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('notice_job_id', sa.String(40), nullable=True),
            sa.Column('inbound_id', sa.String(40), nullable=True),
            sa.Column('matched_by', sa.String(8), nullable=True),
            sa.Column('paired_by', sa.String(40), nullable=True),
            sa.Column('paired_by_name', sa.String(200), nullable=True),
            sa.Column('paired_at', sa.DateTime(), nullable=True),
            sa.Column('pairing_statement', sa.Text(), nullable=True),
            sa.Column('pairing_signature', sa.String(128), nullable=True),
            sa.Column('told_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_notices'),
            sa.CheckConstraint("role = 'sender' OR role = 'receiver'", name='ck_direct_notices_role'),
            sa.CheckConstraint(_choice('state', ('announced', 'queued', 'waiting', 'paired', 'cancelled')),
                               name='ck_direct_notices_state'),
            sa.CheckConstraint(_choice('matched_by', ('sub', 'barcode', 'person')), name='ck_direct_notices_matched'),
            sa.CheckConstraint('paired_at IS NOT NULL OR matched_by IS NULL', name='ck_direct_notices_paired'),
            sa.ForeignKeyConstraint(['peer_id'], ['direct_peers.id'], name='fk_direct_notices_peer'),
        ),
        SCANS: (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('inbound_id', sa.String(40), nullable=False),
            sa.Column('found', sa.String(20), nullable=True),
            sa.Column('method', sa.String(8), nullable=True),
            sa.Column('notice_row_id', sa.String(40), nullable=True),
            sa.Column('scanned_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_notice_scans'),
            sa.CheckConstraint("method = 'sub' OR method = 'barcode'", name='ck_direct_notice_scans_method'),
        ),
        TRANSFERS: (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('role', sa.String(8), nullable=False),
            sa.Column('message_id', sa.String(32), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('manifest', sa.Text(), nullable=False),
            sa.Column('signature', sa.String(128), nullable=False),
            sa.Column('offer', sa.Text(), nullable=False),
            sa.Column('offer_signature', sa.String(128), nullable=False),
            sa.Column('size', sa.Integer(), nullable=False),
            sa.Column('piece_size', sa.Integer(), nullable=False),
            sa.Column('pieces', sa.Integer(), nullable=False),
            sa.Column('confirmed', sa.Integer(), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('detail', sa.String(300), nullable=True),
            sa.Column('folder', sa.String(500), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_direct_transfers'),
            sa.CheckConstraint("role = 'sender' OR role = 'receiver'", name='ck_direct_transfers_role'),
            sa.CheckConstraint(_choice('state', ('open', 'committed', 'refused', 'abandoned')),
                               name='ck_direct_transfers_state'),
            sa.CheckConstraint('size >= 1 AND piece_size >= 1 AND pieces >= 1 AND confirmed >= 0',
                               name='ck_direct_transfers_sizes'),
            sa.ForeignKeyConstraint(['peer_id'], ['direct_peers.id'], name='fk_direct_transfers_peer'),
        ),
        PIECES: (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('transfer_id', sa.String(40), nullable=False),
            sa.Column('sequence', sa.Integer(), nullable=False),
            sa.Column('sha256', sa.String(64), nullable=False),
            sa.Column('size', sa.Integer(), nullable=False),
            sa.Column('received_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_transfer_pieces'),
            sa.CheckConstraint('sequence >= 0 AND size >= 1', name='ck_direct_transfer_pieces_sizes'),
            sa.ForeignKeyConstraint(['transfer_id'], ['direct_transfers.id'], name='fk_direct_transfer_pieces_transfer'),
        ),
        REPAIRS: (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('role', sa.String(8), nullable=False),
            sa.Column('repair_id', sa.String(32), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('attempt_id', sa.String(40), nullable=True),
            sa.Column('message_id', sa.String(32), nullable=True),
            sa.Column('inbound_id', sa.String(40), nullable=True),
            sa.Column('caller', sa.String(32), nullable=True),
            sa.Column('call_started_at', sa.DateTime(), nullable=True),
            sa.Column('total_pages', sa.Integer(), nullable=False),
            sa.Column('pages_held', sa.Integer(), nullable=False),
            sa.Column('ecm', sa.Integer(), nullable=True),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('statement', sa.Text(), nullable=False),
            sa.Column('signature', sa.String(128), nullable=False),
            sa.Column('assembled_path', sa.String(500), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_call_repairs'),
            sa.CheckConstraint("role = 'sender' OR role = 'receiver'", name='ck_direct_call_repairs_role'),
            sa.CheckConstraint(_choice('state', ('confirmed', 'sent', 'completed', 'offered', 'expired')),
                               name='ck_direct_call_repairs_state'),
            sa.CheckConstraint('total_pages >= 1 AND pages_held >= 0 AND pages_held <= total_pages',
                               name='ck_direct_call_repairs_pages'),
            sa.CheckConstraint('ecm = 0 OR ecm = 1', name='ck_direct_call_repairs_ecm'),
            sa.ForeignKeyConstraint(['peer_id'], ['direct_peers.id'], name='fk_direct_call_repairs_peer'),
        ),
    }


def _column(name, kind):
    return sa.Column(name, kind(), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for name, kind in PEER_COLUMNS:
        metadata.tables['direct_peers'].append_column(_column(name, kind))
    definitions = _definitions()
    tables = {name: sa.Table(name, metadata, *definitions[name]) for name in ORDER}
    for index, table, columns, unique in INDEXES:
        sa.Index(index, *(tables[table].c[column] for column in columns), unique=unique)
    return metadata


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_notice_repair(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A notice fax or transfer table already exists before its migration.')
    if not inspector.has_table('direct_peers'):
        _refuse('The direct delivery partner table is missing before the notice fax migration.')
    present = {column['name'] for column in inspector.get_columns('direct_peers')}
    if any(name in present for name, _ in PEER_COLUMNS):
        _refuse('Direct delivery tables are not in the expected state for their migration.')
    for name, kind in PEER_COLUMNS:
        operations.add_column('direct_peers', _column(name, kind))
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_notice_repair(connection, operations):
    # What two organizations signed is never dropped silently.
    if any(connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar() for name in SIGNED):
        _refuse('Notice faxes, transfers or repaired calls with partners are recorded; this revision cannot be '
                'undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
    # No constraint or index names these columns, so SQLite drops them too.
    for name, _ in reversed(PEER_COLUMNS):
        operations.drop_column('direct_peers', name)
