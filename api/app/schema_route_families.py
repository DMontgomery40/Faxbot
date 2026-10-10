"""Frozen 0072: route families, shared upstreams, receive owners and the UPS Faxbot reads (brief 92, RF).

Faxbot learns per number, but many failures belong to something bigger than one number: a trunk after a
settings change, a carrier's upstream, the office's power, or whether anyone can receive at all. These tables
keep what Faxbot concluded about those bigger things. Every row is written once; a later fact is a new row.

- ``route_family_incidents``: many destinations failing on one route family after a common change. A family is
  the account (``account_key``), the transport (``t38``, ``audio``, or ``service`` for a fax service's own
  calls), the phase the calls failed in (``connect``, ``media``, ``answer``, ``t30``, ``service``) and the
  settings generation (``generation``: the trunk's learning epoch, or the provider profile a fax service's
  attempts were bound to). ``change_at`` is the change point and ``change_cause`` whether the settings changed
  there (``settings``) or something outside Faxbot did (``outside``). ``destinations``, ``failures``,
  ``calls`` and ``corroborated`` (failed numbers that went through before the change or by another family)
  are the evidence at opening. One incident per family and change point.
- ``route_family_closures``: when and why an incident ended (``went_through``, ``settings_changed``,
  ``quiet``, ``person``), who closed it, and the per-number lessons it retired (``retired``, ``retired_ids``).
- ``route_family_tests`` and ``route_family_test_sends``: a 2-by-2 test, two of your sending accounts against
  two of your own receiving numbers, and each test fax a person sent for one of its four cells (``a1`` ...
  ``b2``), once per cell. Nothing is sent without that person's action.
- ``route_upstreams``: which carrier a provider is known to use upstream, with the published source and its
  date. The newest row per provider counts; ``upstream`` NULL means unknown.
- ``receive_owner_claims``: which endpoint accepts a number's faxes. ``generation`` counts up per number, so
  two claims made at once cannot both win; ``owner`` NULL releases the number.
- ``power_sources``: the UPS Faxbot reads through NUT (``host``, ``port``, ``ups_name``) and the reserve it
  keeps (``reserve_seconds``). The newest row counts; ``host`` NULL turns the UPS check off.

Runtime code reflects the tables; it never imports this metadata. The downgrade refuses while rows exist.
"""
import sqlalchemy as sa

from .schema_station_check import frozen_metadata as previous_metadata

REVISION = '0072_route_families'
ORDER = ('route_family_incidents', 'route_family_closures', 'route_family_tests', 'route_family_test_sends',
         'route_upstreams', 'receive_owner_claims', 'power_sources')
TABLES = frozenset(ORDER)
TRANSPORTS = ('t38', 'audio', 'service')
PHASES = ('connect', 'media', 'answer', 't30', 'service')
CAUSES = ('settings', 'outside')
CLOSED = ('went_through', 'settings_changed', 'quiet', 'person')
CELLS = ('a1', 'a2', 'b1', 'b2')
INDEXES = (
    ('uq_route_family_incidents_family', 'route_family_incidents',
     ('account_key', 'transport', 'phase', 'generation', 'change_at'), True),
    ('ix_route_family_incidents_opened', 'route_family_incidents', ('opened_at',), False),
    ('uq_route_family_closures_incident', 'route_family_closures', ('incident_id',), True),
    ('ix_route_family_tests_created', 'route_family_tests', ('created_at',), False),
    ('uq_route_family_test_sends_cell', 'route_family_test_sends', ('test_id', 'cell'), True),
    ('ix_route_upstreams_provider', 'route_upstreams', ('provider', 'created_at'), False),
    ('uq_receive_owner_claims_generation', 'receive_owner_claims', ('number', 'generation'), True),
    ('ix_power_sources_created', 'power_sources', ('created_at',), False),
)


def _choice(column, values):
    # NULL passes (unknown); no grouping parentheses (PostgreSQL reflection round trip).
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _by(prefix):
    return (sa.Column(f'{prefix}_by', sa.String(40), nullable=True),
            sa.Column(f'{prefix}_by_name', sa.String(200), nullable=True))


def _definitions():
    return {
        'route_family_incidents': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('account_key', sa.String(64), nullable=False),
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('transport', sa.String(8), nullable=False),
            sa.Column('phase', sa.String(16), nullable=False),
            sa.Column('generation', sa.String(64), nullable=False),
            sa.Column('change_at', sa.DateTime(), nullable=False),
            sa.Column('change_cause', sa.String(16), nullable=False),
            sa.Column('first_failure_at', sa.DateTime(), nullable=False),
            sa.Column('last_failure_at', sa.DateTime(), nullable=False),
            sa.Column('destinations', sa.Integer(), nullable=False),
            sa.Column('failures', sa.Integer(), nullable=False),
            sa.Column('calls', sa.Integer(), nullable=False),
            sa.Column('corroborated', sa.Integer(), nullable=False),
            sa.Column('opened_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_route_family_incidents'),
            sa.CheckConstraint(_choice('transport', TRANSPORTS), name='ck_route_family_incidents_transport'),
            sa.CheckConstraint(_choice('phase', PHASES), name='ck_route_family_incidents_phase'),
            sa.CheckConstraint(_choice('change_cause', CAUSES), name='ck_route_family_incidents_cause'),
            sa.CheckConstraint('destinations >= 1 AND failures >= destinations AND calls >= failures '
                               'AND corroborated >= 0 AND corroborated <= destinations',
                               name='ck_route_family_incidents_counts'),
        ),
        'route_family_closures': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('incident_id', sa.String(40), nullable=False),
            sa.Column('closed_at', sa.DateTime(), nullable=False),
            sa.Column('reason', sa.String(24), nullable=False),
            *_by('closed'),
            sa.Column('retired', sa.Integer(), nullable=False),
            sa.Column('retired_ids', sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_route_family_closures'),
            sa.ForeignKeyConstraint(['incident_id'], ['route_family_incidents.id'], ondelete='RESTRICT',
                                    name='fk_route_family_closures_incident'),
            sa.CheckConstraint(_choice('reason', CLOSED), name='ck_route_family_closures_reason'),
            sa.CheckConstraint('retired >= 0', name='ck_route_family_closures_retired'),
        ),
        'route_family_tests': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('route_a', sa.String(64), nullable=False),
            sa.Column('route_b', sa.String(64), nullable=False),
            sa.Column('number_a', sa.String(32), nullable=False),
            sa.Column('number_b', sa.String(32), nullable=False),
            sa.Column('incident_id', sa.String(40), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            *_by('created'),
            sa.PrimaryKeyConstraint('id', name='pk_route_family_tests'),
            sa.ForeignKeyConstraint(['incident_id'], ['route_family_incidents.id'], ondelete='RESTRICT',
                                    name='fk_route_family_tests_incident'),
            sa.CheckConstraint('route_a <> route_b AND number_a <> number_b', name='ck_route_family_tests_pairs'),
        ),
        'route_family_test_sends': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('test_id', sa.String(40), nullable=False),
            sa.Column('cell', sa.String(2), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('sent_at', sa.DateTime(), nullable=False),
            *_by('sent'),
            sa.PrimaryKeyConstraint('id', name='pk_route_family_test_sends'),
            sa.ForeignKeyConstraint(['test_id'], ['route_family_tests.id'], ondelete='RESTRICT',
                                    name='fk_route_family_test_sends_test'),
            sa.CheckConstraint(_choice('cell', CELLS), name='ck_route_family_test_sends_cell'),
        ),
        'route_upstreams': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('provider', sa.String(64), nullable=False),
            sa.Column('upstream', sa.String(120), nullable=True),
            sa.Column('source_url', sa.String(500), nullable=True),
            sa.Column('source_date', sa.String(10), nullable=True),  # the day the source was read, YYYY-MM-DD
            sa.Column('note', sa.String(300), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            *_by('created'),
            sa.PrimaryKeyConstraint('id', name='pk_route_upstreams'),
            sa.CheckConstraint('upstream IS NULL OR source_url IS NOT NULL', name='ck_route_upstreams_source'),
        ),
        'receive_owner_claims': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('generation', sa.Integer(), nullable=False),
            sa.Column('owner', sa.String(64), nullable=True),
            sa.Column('owner_label', sa.String(120), nullable=True),
            sa.Column('note', sa.String(300), nullable=True),
            sa.Column('claimed_at', sa.DateTime(), nullable=False),
            *_by('claimed'),
            sa.PrimaryKeyConstraint('id', name='pk_receive_owner_claims'),
            sa.CheckConstraint('generation >= 1', name='ck_receive_owner_claims_generation'),
        ),
        'power_sources': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('host', sa.String(255), nullable=True),
            sa.Column('port', sa.Integer(), nullable=False),
            sa.Column('ups_name', sa.String(64), nullable=True),
            sa.Column('reserve_seconds', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            *_by('created'),
            sa.PrimaryKeyConstraint('id', name='pk_power_sources'),
            sa.CheckConstraint('port >= 1 AND port <= 65535 AND reserve_seconds >= 0',
                               name='ck_power_sources_values'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for name, columns in _definitions().items():
        sa.Table(name, metadata, *columns)
    for index, name, columns, unique in INDEXES:
        sa.Index(index, *(metadata.tables[name].c[column] for column in columns), unique=unique)
    return metadata


def upgrade_route_families(connection, operations):
    from .schema import SchemaUpgradeError
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Route family tables already exist before their migration.')
    for name, columns in _definitions().items():
        operations.create_table(name, *columns)
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_route_families(connection, operations):
    from .schema import SchemaUpgradeError
    for name in ORDER:
        rows = connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar()
        if rows:
            raise SchemaUpgradeError('Faxbot keeps its route problems, tests, receive owners and power settings; '
                                     'this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
