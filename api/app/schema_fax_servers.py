"""Frozen 0086 fax servers that send through Faxbot (store and forward for RightFax and other fax servers).

Registration and validation belong to ``schema``. This revision adds three append-only tables and changes no stored
row:

- ``fax_servers``: each fax server you enrolled (OpenText RightFax with its Brooktrout SR140, or any fax server or
  PBX that sends faxes to a SIP gateway), newest row per ``server_id`` counts. A fax server never registers: it is
  recognised by its one source ``address`` on your network and proves itself with SIP digest authentication
  (``username`` and pjsip's ``md5_cred``; the password is shown once and never stored). ``countries`` (JSON ISO
  codes) narrows where it may send, under Faxbot's own dialing guard; ``per_hour`` and ``per_day`` cap its answered
  calls; ``dial_prefix`` is an outside-line prefix it puts before every number; ``contact_email`` and
  ``contact_webhook`` get a notice when one of its faxes is not delivered. Its own sending key is sealed with the
  installation key (``secret_envelope``), as a copier extension's is, so its faxes show in Sent under its name.
  ``state`` ``removed`` takes it away.
- ``fax_server_calls``: one row per call from a fax server, written when Faxbot decides whether to answer it, unique
  per server and SIP Call-ID (``call_key`` is the Call-ID's SHA-256), so a repeated call is never a second fax.
  ``answered`` is 0 for a call Faxbot turned away before answering; only answered calls count toward the caps.
- ``fax_server_events``: what happened to each call, one row per step (each step once per call, written under
  Faxbot's write lock): ``admitted``,
  ``turned_away``, ``not_received`` (the call ended before every page arrived, so nothing was sent), ``accepted``
  (queued as an outbound fax, ``job_id``), ``refused`` (received, but Faxbot could not queue it), then ``delivered``,
  ``failed`` or ``uncertain``; and the failure notice to the server's contact (``notice_sent``, ``notice_failed``,
  ``notice_uncertain``, one row per try; an uncertain notice is never sent again).

The downgrade drops the tables and refuses while a fax server or one of its calls is recorded.
Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_copier_extensions import frozen_metadata as previous_metadata


REVISION = '0086_fax_servers'
ORDER = ('fax_servers', 'fax_server_calls', 'fax_server_events')
TABLES = frozenset(ORDER)
STATES = ('active', 'removed')
KINDS = ('admitted', 'turned_away', 'not_received', 'accepted', 'refused', 'delivered', 'failed', 'uncertain',
         'notice_sent', 'notice_failed', 'notice_uncertain')
INDEXES = (
    ('ix_fax_servers_server', 'fax_servers', ('server_id', 'created_at'), False),
    ('uq_fax_server_calls_call', 'fax_server_calls', ('server_id', 'call_key'), True),
    ('ix_fax_server_calls_server', 'fax_server_calls', ('server_id', 'created_at'), False),
    ('ix_fax_server_calls_uniqueid', 'fax_server_calls', ('uniqueid',), False),
    ('ix_fax_server_events_call', 'fax_server_events', ('call_id', 'created_at'), False),
    ('ix_fax_server_events_job', 'fax_server_events', ('job_id',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'fax_servers': (
            sa.Column('id', sa.String(32), nullable=False),
            sa.Column('server_id', sa.String(32), nullable=False),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('address', sa.String(45), nullable=False),
            sa.Column('username', sa.String(64), nullable=False),
            sa.Column('md5_cred', sa.String(32), nullable=False),
            sa.Column('mailbox_id', sa.String(40), nullable=True),
            sa.Column('countries', sa.Text(), nullable=False),
            sa.Column('per_hour', sa.Integer(), nullable=False),
            sa.Column('per_day', sa.Integer(), nullable=False),
            sa.Column('dial_prefix', sa.String(8), nullable=True),
            sa.Column('contact_email', sa.String(254), nullable=True),
            sa.Column('contact_webhook', sa.String(512), nullable=True),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('secret_envelope', sa.Text(), nullable=True),
            sa.Column('key_binding_id', sa.String(40), nullable=True),
            sa.Column('key_principal_id', sa.String(40), nullable=True),
            sa.Column('recorded_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_servers'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_fax_servers_state'),
            sa.CheckConstraint('per_hour >= 1', name='ck_fax_servers_per_hour'),
            sa.CheckConstraint('per_day >= 1', name='ck_fax_servers_per_day'),
        ),
        'fax_server_calls': (
            sa.Column('id', sa.String(32), nullable=False),
            sa.Column('server_id', sa.String(32), nullable=False),
            sa.Column('call_key', sa.String(64), nullable=False),
            sa.Column('uniqueid', sa.String(40), nullable=True),
            sa.Column('dialled', sa.String(40), nullable=True),
            sa.Column('destination', sa.String(32), nullable=True),
            sa.Column('caller', sa.String(32), nullable=True),
            sa.Column('answered', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_server_calls'),
            sa.CheckConstraint('answered = 0 OR answered = 1', name='ck_fax_server_calls_answered'),
        ),
        'fax_server_events': (
            sa.Column('id', sa.String(32), nullable=False),
            sa.Column('call_id', sa.String(32), nullable=False),
            sa.Column('kind', sa.String(20), nullable=False),
            sa.Column('detail', sa.String(300), nullable=True),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('pages', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_server_events'),
            sa.CheckConstraint(_choice('kind', KINDS), name='ck_fax_server_events_kind'),
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


def upgrade_fax_servers(connection, operations):
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        _refuse('A fax server table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_fax_servers(connection, operations):
    for name in ('fax_servers', 'fax_server_calls'):
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            _refuse('Fax servers that send through Faxbot, or their calls, are recorded; this revision cannot be '
                    'undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
