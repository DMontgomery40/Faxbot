"""Frozen 0068 where Faxbot may dial; registration and validation belong to ``schema``.

The dialing guard (``routing/guard.py``) holds a fax whose number is in a class
of numbers Faxbot may not call: premium-rate, special-service and satellite
numbers unless an administrator allows them, and countries this installation
has never sent to unless a sending rule, a saved recipient or an administrator
allows them. This revision adds three tables and changes no stored row:

- ``dialing_class_changes``: append-only. One row per change to a class of
  numbers: ``class_key`` names the class (``national_geographic``,
  ``national_toll_free``, ``national_mobile``, ``premium``,
  ``special_service``, ``satellite``, or ``country:GB``); ``state`` is
  ``allowed``, ``blocked`` or ``default`` (back to Faxbot's own default);
  ``reason`` is ``administrator`` (a person changed it) or ``delivered`` (the
  country already had successful faxes when Faxbot started checking, with the
  first one's time in ``first_delivered_at``). ``ceiling_micros`` and
  ``currency`` are the highest per-minute price a call to the class may cost,
  NULL for none. The newest row per class counts; older rows stay as history.
- ``dialing_guard_state``: one row, written once, when Faxbot recorded the
  countries this installation had already delivered to.
- ``dialing_guard_holds``: one row per fax the guard held, beside its row in
  ``outbound_holds`` (the same ``id``): the class, the number dialed, why (``why``), and the
  per-minute price and ceiling compared when a ceiling held it.

The downgrade drops the tables and refuses while an administrator's change or
a guard hold is recorded. Runtime code reflects these tables; it never imports
this metadata.
"""
import sqlalchemy as sa

from .schema_analysis import frozen_metadata as previous_metadata


REVISION = '0068_dialing_guard'
ORDER = ('dialing_class_changes', 'dialing_guard_state', 'dialing_guard_holds')
TABLES = frozenset(ORDER)
STATES = ('allowed', 'blocked', 'default')
REASONS = ('administrator', 'delivered')
WHY = ('blocked', 'not_allowed', 'fenced', 'over_ceiling', 'unknown_rate')
INDEXES = (
    ('ix_dialing_class_changes_class', 'dialing_class_changes', ('class_key', 'created_at'), False),
    ('ix_dialing_guard_holds_job', 'dialing_guard_holds', ('job_id',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    return {
        'dialing_class_changes': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('class_key', sa.String(32), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('reason', sa.String(16), nullable=False),
            sa.Column('ceiling_micros', sa.Integer(), nullable=True),
            sa.Column('currency', sa.String(3), nullable=True),
            sa.Column('first_delivered_at', sa.DateTime(), nullable=True),
            sa.Column('actor_principal_id', sa.String(40), nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_dialing_class_changes'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_dialing_class_changes_state'),
            sa.CheckConstraint(_choice('reason', REASONS), name='ck_dialing_class_changes_reason'),
            sa.CheckConstraint('(ceiling_micros IS NULL AND currency IS NULL) OR '
                               '(ceiling_micros >= 0 AND currency IS NOT NULL)',
                               name='ck_dialing_class_changes_ceiling'),
        ),
        'dialing_guard_state': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('history_checked_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_dialing_guard_state'),
            sa.CheckConstraint("id = 'installation'", name='ck_dialing_guard_state_singleton'),
        ),
        'dialing_guard_holds': (
            # The hold's own identity in ``outbound_holds``.
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('class_key', sa.String(32), nullable=False),
            sa.Column('dialed_number', sa.String(32), nullable=False),
            sa.Column('why', sa.String(16), nullable=False),
            sa.Column('rate_micros', sa.Integer(), nullable=True),
            sa.Column('ceiling_micros', sa.Integer(), nullable=True),
            sa.Column('currency', sa.String(3), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_dialing_guard_holds'),
            sa.CheckConstraint(_choice('why', WHY), name='ck_dialing_guard_holds_why'),
            sa.ForeignKeyConstraint(['id'], ['outbound_holds.id'], name='fk_dialing_guard_holds_hold',
                                    ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_dialing_guard_holds_job',
                                    ondelete='CASCADE'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for name, columns in _definitions().items():
        sa.Table(name, metadata, *columns)
    for index, name, columns, unique in INDEXES:
        sa.Index(index, *(metadata.tables[name].c[column] for column in columns), unique=unique)
    return metadata


def upgrade_dialing(connection, operations):
    from .schema import SchemaUpgradeError
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Dialing guard tables already exist before their migration.')
    for name, columns in _definitions().items():
        operations.create_table(name, *columns)
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_dialing(connection, operations):
    from .schema import SchemaUpgradeError
    changes = sa.table('dialing_class_changes', sa.column('reason'))
    chosen = connection.execute(sa.select(sa.func.count()).select_from(changes).where(
        changes.c.reason == 'administrator')).scalar()
    held = connection.execute(sa.select(sa.func.count()).select_from(sa.table('dialing_guard_holds'))).scalar()
    if chosen or held:
        raise SchemaUpgradeError('Choices about where Faxbot may dial, or faxes it held for them, are recorded; '
                                 'this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
