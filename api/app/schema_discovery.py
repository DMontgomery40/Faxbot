"""Frozen 0045 partner discovery; registration and validation belong to ``schema``.

Faxbot finds recipients that run Faxbot from calls it already makes, from a
partner's introduction and from directories the administrator trusts
(``direct/discovery.py``, M16 and D13). The challenge fax stays the only
authentication. This revision adds eight tables and changes no stored row:

- ``direct_discovery_settings``: append-only; the newest row is the current
  setting. ``well_known`` (answer ``/.well-known/faxbot-direct``) and
  ``from_calls`` (look up the far end's address from calls) are 1 or 0; no row
  means both on. ``directories`` lists the trusted directory domains, one per
  line ('' means none). ``recorded_by`` and ``recorded_by_name`` are the person
  who saved the row, as they were then.
- ``direct_discovery_hints``: one row per call whose far end gave an SSL Fax
  address (its CSA on a sent call, its TSA on a received one), keyed by where
  it was read (``frames``: the built-in engine's ``fax_call_frames`` row;
  ``engine``: the SSL Fax engine's attempt). Only the host and port are kept,
  never the passcode the address carries. ``lookup_id`` is set once: the
  lookup that answered the hint, or ``skipped`` when none was needed (its
  number is already a partner's, or not a number Faxbot can read).
- ``direct_discovery_lookups``: append-only, one row per lookup of a host
  (``call``), a partner's introduction (``introduction``) or a directory
  record (``directory``): what was asked, the outcome, the verified card when
  there was one, and until when the answer is reused (``expires_at``).
  Rate limits and the cache are read from these rows. ``certificate_sha256``
  is the SHA-256 of the certificate a host on a private network presented
  (accepted without a trusted authority only there, and only when the
  administrator allows private partners); NULL for every other host.
- ``direct_discovery_suggestions``: a recipient that runs Faxbot, offered to
  the administrator for enrollment. ``dismissed_*`` and ``enrolled_*`` are each
  set once. ``introduction`` keeps an introducing partner's signed statement
  exactly as received. ``certificate_sha256`` carries the lookup's
  certificate fingerprint for a recipient on a private network.
- ``direct_certificate_pins``: append-only; the certificate fingerprint a
  partner enrolled from a suggestion on a private network presented then, by
  partner and host. A later lookup of that host that sees another certificate
  is recorded as ``certificate_changed``.
- ``direct_introduction_consents``: append-only; whether a partner may be
  introduced to this installation's other partners. The newest row for a
  partner is current; no row means no. A consent counts only while the
  partner is verified and only when recorded at or after its verification.
- ``direct_introductions``: append-only, one row per introduction this
  installation made between two of its partners, with what each was told.
- ``direct_dns_publications``: a fax number published in a directory's DNS as
  reaching this Faxbot: the record name and signed value an administrator adds
  to their zone, until when it is valid, and who published it. Withdrawal sets
  ``withdrawn_*`` once.

No foreign key ties these rows to partners, faxes or people; a partner may be
removed while what was discovered stays. The downgrade drops the tables and
refuses while any publication, consent or introduction is recorded, because
those are what people decided. Runtime code reflects these tables; it never
imports this metadata.
"""
import sqlalchemy as sa

from .schema_engine_learning import frozen_metadata as previous_metadata


REVISION = '0045_discovery'
ORDER = ('direct_discovery_settings', 'direct_discovery_hints', 'direct_discovery_lookups',
         'direct_discovery_suggestions', 'direct_certificate_pins', 'direct_introduction_consents',
         'direct_introductions', 'direct_dns_publications')
TABLES = frozenset(ORDER)
# Rows people decided; the downgrade refuses while any is kept.
DECIDED = ('direct_introduction_consents', 'direct_introductions', 'direct_dns_publications')
HINT_SOURCES = ('frames', 'engine')
LOOKUP_KINDS = ('call', 'introduction', 'directory', 'certificate')
SUGGESTION_SOURCES = ('call', 'introduction', 'directory')
INDEXES = (
    ('ix_direct_discovery_settings_created', 'direct_discovery_settings', ('created_at',), False),
    ('ix_direct_discovery_hints_source', 'direct_discovery_hints', ('source', 'source_ref'), True),
    ('ix_direct_discovery_hints_pending', 'direct_discovery_hints', ('lookup_id', 'created_at'), False),
    ('ix_direct_discovery_lookups_host', 'direct_discovery_lookups', ('host', 'started_at'), False),
    ('ix_direct_discovery_lookups_number', 'direct_discovery_lookups', ('number', 'started_at'), False),
    ('ix_direct_discovery_suggestions_number', 'direct_discovery_suggestions', ('number', 'created_at'), False),
    ('ix_direct_discovery_suggestions_key', 'direct_discovery_suggestions', ('signing_key',), False),
    ('ix_direct_certificate_pins_host', 'direct_certificate_pins', ('host', 'created_at'), False),
    ('ix_direct_introduction_consents_peer', 'direct_introduction_consents', ('peer_id', 'created_at'), False),
    ('ix_direct_introductions_created', 'direct_introductions', ('created_at',), False),
    ('ix_direct_dns_publications_number', 'direct_dns_publications', ('number', 'created_at'), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'direct_discovery_settings': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('well_known', sa.Integer(), nullable=False),
            sa.Column('from_calls', sa.Integer(), nullable=False),
            sa.Column('directories', sa.Text(), nullable=False),
            sa.Column('recorded_by', sa.String(40), nullable=True),
            sa.Column('recorded_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_discovery_settings'),
            sa.CheckConstraint('well_known = 0 OR well_known = 1', name='ck_direct_discovery_settings_well_known'),
            sa.CheckConstraint('from_calls = 0 OR from_calls = 1', name='ck_direct_discovery_settings_from_calls'),
        ),
        'direct_discovery_hints': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('source_ref', sa.String(80), nullable=False),
            sa.Column('direction', sa.String(8), nullable=False),
            sa.Column('number', sa.String(32), nullable=True),
            sa.Column('host', sa.String(253), nullable=False),
            sa.Column('port', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('lookup_id', sa.String(40), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_direct_discovery_hints'),
            sa.CheckConstraint(_choice('source', HINT_SOURCES), name='ck_direct_discovery_hints_source'),
            sa.CheckConstraint("direction = 'out' OR direction = 'in'", name='ck_direct_discovery_hints_direction'),
            sa.CheckConstraint('port >= 1 AND port <= 65535', name='ck_direct_discovery_hints_port'),
        ),
        'direct_discovery_lookups': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('kind', sa.String(16), nullable=False),
            sa.Column('host', sa.String(253), nullable=False),
            sa.Column('url', sa.String(600), nullable=False),
            sa.Column('number', sa.String(32), nullable=True),
            sa.Column('outcome', sa.String(32), nullable=False),
            sa.Column('signing_key', sa.String(64), nullable=True),
            sa.Column('organization', sa.String(200), nullable=True),
            sa.Column('fax_number', sa.String(32), nullable=True),
            sa.Column('endpoint', sa.String(512), nullable=True),
            sa.Column('card', sa.Text(), nullable=True),
            sa.Column('record', sa.Text(), nullable=True),
            sa.Column('certificate_sha256', sa.String(64), nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_discovery_lookups'),
            sa.CheckConstraint(_choice('kind', LOOKUP_KINDS), name='ck_direct_discovery_lookups_kind'),
        ),
        'direct_discovery_suggestions': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('lookup_id', sa.String(40), nullable=True),
            sa.Column('introduced_by', sa.String(40), nullable=True),
            sa.Column('introduction', sa.Text(), nullable=True),
            sa.Column('directory', sa.String(253), nullable=True),
            sa.Column('organization', sa.String(200), nullable=False),
            sa.Column('signing_key', sa.String(64), nullable=False),
            sa.Column('endpoint', sa.String(512), nullable=False),
            sa.Column('card', sa.Text(), nullable=True),
            sa.Column('certificate_sha256', sa.String(64), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('dismissed_at', sa.DateTime(), nullable=True),
            sa.Column('dismissed_by', sa.String(40), nullable=True),
            sa.Column('dismissed_by_name', sa.String(200), nullable=True),
            sa.Column('enrolled_at', sa.DateTime(), nullable=True),
            sa.Column('enrolled_peer_id', sa.String(40), nullable=True),
            sa.Column('enrolled_by', sa.String(40), nullable=True),
            sa.Column('enrolled_by_name', sa.String(200), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_direct_discovery_suggestions'),
            sa.CheckConstraint(_choice('source', SUGGESTION_SOURCES), name='ck_direct_discovery_suggestions_source'),
            sa.CheckConstraint('dismissed_by IS NULL OR dismissed_at IS NOT NULL',
                               name='ck_direct_discovery_suggestions_dismissed'),
            sa.CheckConstraint('enrolled_peer_id IS NULL OR enrolled_at IS NOT NULL',
                               name='ck_direct_discovery_suggestions_enrolled'),
        ),
        'direct_certificate_pins': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('host', sa.String(253), nullable=False),
            sa.Column('certificate_sha256', sa.String(64), nullable=False),
            sa.Column('suggestion_id', sa.String(40), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_certificate_pins'),
        ),
        'direct_introduction_consents': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('allowed', sa.Integer(), nullable=False),
            sa.Column('recorded_by', sa.String(40), nullable=True),
            sa.Column('recorded_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_introduction_consents'),
            sa.CheckConstraint('allowed = 0 OR allowed = 1', name='ck_direct_introduction_consents_allowed'),
        ),
        'direct_introductions': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('first_peer_id', sa.String(40), nullable=False),
            sa.Column('second_peer_id', sa.String(40), nullable=False),
            sa.Column('first_outcome', sa.String(32), nullable=False),
            sa.Column('second_outcome', sa.String(32), nullable=False),
            sa.Column('created_by', sa.String(40), nullable=True),
            sa.Column('created_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_introductions'),
        ),
        'direct_dns_publications': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('directory', sa.String(253), nullable=False),
            sa.Column('record_name', sa.String(300), nullable=False),
            sa.Column('record_value', sa.Text(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('created_by', sa.String(40), nullable=True),
            sa.Column('created_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('withdrawn_at', sa.DateTime(), nullable=True),
            sa.Column('withdrawn_by', sa.String(40), nullable=True),
            sa.Column('withdrawn_by_name', sa.String(200), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_direct_dns_publications'),
            sa.CheckConstraint('withdrawn_by IS NULL OR withdrawn_at IS NOT NULL',
                               name='ck_direct_dns_publications_withdrawn'),
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


def upgrade_discovery(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A partner discovery table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_discovery(connection, operations):
    # Publications, consents and introductions are what people decided; never dropped silently.
    if any(connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar() for name in DECIDED):
        _refuse('Partner publications, introductions or consents are recorded; this revision cannot be undone '
                'without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
