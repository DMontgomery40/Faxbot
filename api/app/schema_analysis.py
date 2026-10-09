"""Frozen 0066: a singleton scheduler lease and persisted operational analysis runs."""
import sqlalchemy as sa
from .schema_polled_transmit import frozen_metadata as previous_metadata

REVISION = '0066_analysis'
ORDER = ('analysis_state', 'analysis_runs')
TABLES = frozenset(ORDER)
INDEXES = (('ix_analysis_runs_finished', 'analysis_runs', ('finished_at',), False),)


def _definitions():
    return {
        'analysis_state': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('current_run_id', sa.String(40), nullable=True),
            sa.Column('last_success_id', sa.String(40), nullable=True),
            sa.Column('lease_until', sa.DateTime(), nullable=True),
            sa.Column('next_run_at', sa.DateTime(), nullable=True),
            sa.Column('message', sa.String(300), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_analysis_state'),
            sa.CheckConstraint("id = 'installation'", name='ck_analysis_state_singleton'),
            sa.CheckConstraint("state = 'idle' OR state = 'queued' OR state = 'running' OR state = 'succeeded' OR state = 'failed'", name='ck_analysis_state_status'),
        ),
        'analysis_runs': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('started_at', sa.DateTime(), nullable=False),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.Column('provider', sa.String(32), nullable=False),
            sa.Column('model', sa.String(200), nullable=False),
            sa.Column('config_signature', sa.String(64), nullable=False),
            sa.Column('summary', sa.Text(), nullable=True),
            sa.Column('evidence', sa.Text(), nullable=False),
            sa.Column('usage', sa.Text(), nullable=False),
            sa.Column('error', sa.String(300), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_analysis_runs'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for name, columns in _definitions().items():
        sa.Table(name, metadata, *columns)
    for index, name, columns, unique in INDEXES:
        sa.Index(index, *(metadata.tables[name].c[column] for column in columns), unique=unique)
    return metadata


def upgrade_analysis(connection, operations):
    from .schema import SchemaUpgradeError
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Analysis tables already exist before their migration.')
    for name, columns in _definitions().items():
        operations.create_table(name, *columns)
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_analysis(connection, operations):
    from .schema import SchemaUpgradeError
    runs = connection.execute(sa.select(sa.func.count()).select_from(sa.table('analysis_runs'))).scalar()
    state = sa.table('analysis_state', sa.column('state'))
    pending = connection.execute(sa.select(sa.func.count()).select_from(state).where(
        state.c.state.in_(('queued', 'running')))).scalar()
    if runs or pending:
        raise SchemaUpgradeError('Saved analysis or pending analysis requests exist; this revision cannot be undone '
                                 'without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
