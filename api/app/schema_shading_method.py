"""Frozen 0058 shading method; registration and validation belong to ``schema``.

Faxbot keeps a document's shaded areas with a fax-friendly pattern by default
(``pages/screens.py``) and makes light areas white only when the administrator
opts in (``pages/friendly.py``). Each attempt whose pages changed has one
``fax_friendly_pages`` row (0042); this revision adds one nullable column and
changes no stored row:

- ``fax_friendly_pages.method``: 'screened' (shaded areas kept with a pattern;
  nothing removed) or 'whitened' (light areas made white and specks removed).
  NULL marks a row written before this revision; Faxbot then only ever made
  light areas white, so those rows read as 'whitened'.

Like the nullable columns of 0021 and 0032, the column has no CHECK
constraint (adding one to an existing table needs a table rebuild on SQLite);
the only writer, ``friendly.record_send``, refuses any other value. The
downgrade drops the column; it refuses while any row records a method, because
that is how an attempt's pages were sent. Runtime code reflects this table; it
never imports this metadata.
"""
import sqlalchemy as sa

from .schema_measured_codec import frozen_metadata as previous_metadata


REVISION = '0058_shading_method'
# This revision adds no table.
TABLES = frozenset()
COLUMN = ('fax_friendly_pages', 'method')
METHODS = ('screened', 'whitened')


def _column():
    return sa.Column(COLUMN[1], sa.String(16), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    metadata.tables[COLUMN[0]].append_column(_column())
    return metadata


def upgrade_shading_method(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    from .schema import SchemaUpgradeError
    table, column = COLUMN
    inspector = sa.inspect(connection)
    if not inspector.has_table(table) or column in {item['name'] for item in inspector.get_columns(table)}:
        raise SchemaUpgradeError('The fax-friendly pages table is not in the expected state for its migration.')
    operations.add_column(table, _column())


def downgrade_shading_method(connection, operations):
    # How each attempt's pages were sent is history; never drop it silently.
    from .schema import SchemaUpgradeError
    table, column = COLUMN
    values = sa.table(table, sa.column(column))
    if connection.execute(sa.select(sa.func.count()).select_from(values)
                          .where(values.c[column].is_not(None))).scalar():
        raise SchemaUpgradeError('Sent faxes record how their shaded areas were sent; this revision cannot be '
                                 'undone without losing that.')
    # No constraint or index names this column, so SQLite drops it too.
    operations.drop_column(table, column)
