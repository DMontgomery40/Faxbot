"""Frozen 0053 setup plans; registration and validation belong to ``schema``.

Guided setup (``setup_plan/``) compiles what Faxbot already knows about an
installation into suggested packs of sending rules and settings. An
administrator previews a plan and applies it in one step. This revision keeps
both, so a plan can be applied later (from the console or ``faxbot``) and so
anyone can see afterwards what was suggested, from which facts, and what was
applied:

- ``setup_plans``: one row per preview, never changed. ``number`` is the
  plan's short number, which people type to apply it from the command line.
  ``context`` is what the administrator said about the organization (its
  name, and the country each mailbox works in), as JSON. ``plan`` is the
  compiled plan as JSON: its packs and items, each with its sources and
  explanation, the missing list, each mailbox's effective choices, and the
  exact rules documents and settings change that applying it would make.
  ``basis`` is a digest of the configuration revision and the rules
  revisions the plan was built on; applying is refused once they changed.
  ``digest`` is the SHA-256 of ``plan``.
- ``setup_plan_applications``: one row per apply, never changed. ``outcome``
  is ``applied`` (every chosen step is done), ``partial`` (some steps are
  done and others were refused) or ``refused`` (nothing changed). ``items``
  lists the plan items that were applied and ``steps`` says, for the
  settings and for each rules scope, what happened.

The downgrade drops both tables, and refuses while any plan is recorded,
because they are the record of what was suggested and applied. Runtime code
reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_send_once import frozen_metadata as previous_metadata


REVISION = '0053_setup_plans'
PLANS, APPLICATIONS = 'setup_plans', 'setup_plan_applications'
ORDER = (PLANS, APPLICATIONS)
TABLES = frozenset(ORDER)
OUTCOMES = ('applied', 'partial', 'refused')
INDEXES = (
    ('uq_setup_plans_number', PLANS, ('number',), True),
    ('ix_setup_plans_created', PLANS, ('created_at',), False),
    ('ix_setup_plan_applications_plan', APPLICATIONS, ('plan_id', 'created_at'), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        PLANS: (
            sa.Column('id', sa.String(32), nullable=False),
            sa.Column('number', sa.Integer(), nullable=False),
            sa.Column('basis', sa.String(64), nullable=False),
            sa.Column('context', sa.Text(), nullable=False),
            sa.Column('plan', sa.Text(), nullable=False),
            sa.Column('digest', sa.String(64), nullable=False),
            sa.Column('actor_principal_id', sa.String(40), nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_setup_plans'),
            sa.CheckConstraint('number >= 1', name='ck_setup_plans_number'),
        ),
        APPLICATIONS: (
            sa.Column('id', sa.String(32), nullable=False),
            sa.Column('plan_id', sa.String(32), nullable=False),
            sa.Column('outcome', sa.String(12), nullable=False),
            sa.Column('items', sa.Text(), nullable=False),
            sa.Column('steps', sa.Text(), nullable=False),
            sa.Column('restart_required', sa.Integer(), nullable=False),
            sa.Column('actor_principal_id', sa.String(40), nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_setup_plan_applications'),
            sa.CheckConstraint(_choice('outcome', OUTCOMES), name='ck_setup_plan_applications_outcome'),
            sa.CheckConstraint('restart_required = 0 OR restart_required = 1',
                               name='ck_setup_plan_applications_restart'),
            sa.ForeignKeyConstraint(['plan_id'], ['setup_plans.id'], name='fk_setup_plan_applications_plan'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    definitions = _definitions()
    tables = {name: sa.Table(name, metadata, *definitions[name]) for name in ORDER}
    for index, table, columns, unique in INDEXES:
        sa.Index(index, *(tables[table].c[column] for column in columns), unique=unique)
    return metadata


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_setup_plans(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A setup plan table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_setup_plans(connection, operations):
    # What was suggested and applied is the record of a configuration change; never dropped silently.
    if any(connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar() for name in ORDER):
        _refuse('Setup plans are recorded; this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
