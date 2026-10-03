"""Frozen schema descriptions for revisions 0001/0002, derived from dd8bd991.

Historical migration code must not import mutable ORM models. Extend later heads
in new modules/revisions; do not change these definitions to match future models.
"""
import sqlalchemy as sa

CORE_TABLES = frozenset({"fax_jobs", "api_keys", "inbound_faxes", "mailboxes", "inbound_rules", "inbound_events"})
HYBRIDS = {"fax_jobs": "outbound_backend", "inbound_faxes": "inbound_backend"}
UNIQUE_IDENTITIES = {
    "api_keys": ("key_id",),
    "mailboxes": ("label",),
    "inbound_events": ("provider_sid", "event_type"),
}


def frozen_metadata(*, hybrids=True):
    metadata = sa.MetaData()
    for name, definition in DEFINITIONS.items():
        columns = []
        for field in definition["columns"]:
            if not hybrids and field["name"] == HYBRIDS.get(name):
                continue
            kind = getattr(sa, field["type"])
            args = (field["length"],) if field.get("length") else ()
            columns.append(sa.Column(field["name"], kind(*args),
                                     primary_key=field.get("primary_key", False),
                                     nullable=field.get("nullable", not field.get("primary_key", False))))
        table = sa.Table(name, metadata, *columns)
        for field in definition["columns"]:
            if field.get("index"):
                sa.Index(f"ix_{name}_{field['name']}", table.c[field["name"]], unique=field.get("unique", False))
        if name == "mailboxes":
            sa.UniqueConstraint(table.c.label, name="uq_mailboxes_label")
        elif name == "inbound_events":
            sa.UniqueConstraint(table.c.provider_sid, table.c.event_type, name="uix_inbound_events_sid_type")
    return metadata


def adopt_initial(connection, operations):
    """Called only after the locked runner validates the whole legacy schema."""
    existing = set(sa.inspect(connection).get_table_names())
    for name, table in frozen_metadata(hybrids=False).tables.items():
        if name not in existing:
            table.create(connection)
            continue
        columns = {column["name"] for column in sa.inspect(connection).get_columns(name)}
        for column in table.columns:
            if column.name not in columns:
                # Original fax_jobs predates providers. Its historical jobs were SIP.
                default = sa.text("'sip'") if name == "fax_jobs" and column.name == "backend" else None
                operations.add_column(name, sa.Column(column.name, column.type,
                                                      nullable=column.nullable, server_default=default))


def normalize_foundation(connection, operations):
    for table, name in HYBRIDS.items():
        columns = {column["name"] for column in sa.inspect(connection).get_columns(table)}
        if name not in columns:
            operations.add_column(table, sa.Column(name, sa.String(20), nullable=True))
        connection.execute(sa.text(f"UPDATE {table} SET {name} = backend WHERE {name} IS NULL"))
    for name, table in frozen_metadata().tables.items():
        inspector = sa.inspect(connection)
        indexes = inspector.get_indexes(name)
        for index in table.indexes:
            columns = tuple(column.name for column in index.columns)
            if index.unique and has_unique_identity(inspector, name, columns):
                continue
            if not any(tuple(item["column_names"]) == columns and bool(item["unique"]) == index.unique
                       for item in indexes):
                index.create(connection)
        unique = UNIQUE_IDENTITIES.get(name)
        if unique and not has_unique_identity(sa.inspect(connection), name, unique):
            operations.create_index(f"uq_{name}_identity", name, list(unique), unique=True)


def has_unique_identity(inspector, table, columns):
    return any(set(item["column_names"]) == set(columns) and len(item["column_names"]) == len(columns) and item.get("unique")
               and not any(key.endswith("_where") and value is not None
                           for key, value in item.get("dialect_options", {}).items())
               for item in inspector.get_indexes(table)) or any(
        set(item["column_names"]) == set(columns) and len(item["column_names"]) == len(columns)
        for item in inspector.get_unique_constraints(table))

DEFINITIONS = {'fax_jobs': {'columns': [{'name': 'id',
                           'type': 'String',
                           'length': 40,
                           'primary_key': True,
                           'index': True},
                          {'name': 'to_number',
                           'type': 'String',
                           'length': 64,
                           'index': True,
                           'nullable': False},
                          {'name': 'file_name', 'type': 'String', 'length': 255, 'nullable': False},
                          {'name': 'tiff_path', 'type': 'String', 'length': 512, 'nullable': False},
                          {'name': 'status',
                           'type': 'String',
                           'length': 32,
                           'index': True,
                           'nullable': False},
                          {'name': 'error', 'type': 'Text', 'length': None, 'nullable': True},
                          {'name': 'pages', 'type': 'Integer', 'length': None, 'nullable': True},
                          {'name': 'backend', 'type': 'String', 'length': 20, 'nullable': False},
                          {'name': 'outbound_backend',
                           'type': 'String',
                           'length': 20,
                           'nullable': True},
                          {'name': 'provider_sid',
                           'type': 'String',
                           'length': 100,
                           'nullable': True},
                          {'name': 'pdf_url', 'type': 'String', 'length': 512, 'nullable': True},
                          {'name': 'pdf_token', 'type': 'String', 'length': 128, 'nullable': True},
                          {'name': 'pdf_token_expires_at',
                           'type': 'DateTime',
                           'length': None,
                           'nullable': True},
                          {'name': 'created_at',
                           'type': 'DateTime',
                           'length': None,
                           'nullable': False},
                          {'name': 'updated_at',
                           'type': 'DateTime',
                           'length': None,
                           'nullable': False}],
              'constraints': []},
 'api_keys': {'columns': [{'name': 'id',
                           'type': 'String',
                           'length': 40,
                           'primary_key': True,
                           'index': True},
                          {'name': 'key_id',
                           'type': 'String',
                           'length': 32,
                           'unique': True,
                           'index': True,
                           'nullable': False},
                          {'name': 'key_hash', 'type': 'String', 'length': 200, 'nullable': False},
                          {'name': 'name', 'type': 'String', 'length': 100, 'nullable': True},
                          {'name': 'owner', 'type': 'String', 'length': 100, 'nullable': True},
                          {'name': 'scopes', 'type': 'String', 'length': 200, 'nullable': True},
                          {'name': 'created_at',
                           'type': 'DateTime',
                           'length': None,
                           'nullable': False},
                          {'name': 'last_used_at',
                           'type': 'DateTime',
                           'length': None,
                           'nullable': True},
                          {'name': 'expires_at',
                           'type': 'DateTime',
                           'length': None,
                           'nullable': True},
                          {'name': 'revoked_at',
                           'type': 'DateTime',
                           'length': None,
                           'nullable': True},
                          {'name': 'note', 'type': 'Text', 'length': None, 'nullable': True}],
              'constraints': []},
 'inbound_faxes': {'columns': [{'name': 'id',
                                'type': 'String',
                                'length': 40,
                                'primary_key': True,
                                'index': True},
                               {'name': 'from_number',
                                'type': 'String',
                                'length': 64,
                                'index': True,
                                'nullable': True},
                               {'name': 'to_number',
                                'type': 'String',
                                'length': 64,
                                'index': True,
                                'nullable': True},
                               {'name': 'status',
                                'type': 'String',
                                'length': 32,
                                'index': True,
                                'nullable': False},
                               {'name': 'backend',
                                'type': 'String',
                                'length': 20,
                                'nullable': False},
                               {'name': 'inbound_backend',
                                'type': 'String',
                                'length': 20,
                                'nullable': True},
                               {'name': 'provider_sid',
                                'type': 'String',
                                'length': 100,
                                'nullable': True},
                               {'name': 'pages',
                                'type': 'Integer',
                                'length': None,
                                'nullable': True},
                               {'name': 'size_bytes',
                                'type': 'Integer',
                                'length': None,
                                'nullable': True},
                               {'name': 'sha256', 'type': 'String', 'length': 64, 'nullable': True},
                               {'name': 'pdf_path',
                                'type': 'String',
                                'length': 512,
                                'nullable': True},
                               {'name': 'tiff_path',
                                'type': 'String',
                                'length': 512,
                                'nullable': True},
                               {'name': 'mailbox_label',
                                'type': 'String',
                                'length': 100,
                                'nullable': True},
                               {'name': 'retention_until',
                                'type': 'DateTime',
                                'length': None,
                                'nullable': True},
                               {'name': 'pdf_token',
                                'type': 'String',
                                'length': 128,
                                'nullable': True},
                               {'name': 'pdf_token_expires_at',
                                'type': 'DateTime',
                                'length': None,
                                'nullable': True},
                               {'name': 'error', 'type': 'Text', 'length': None, 'nullable': True},
                               {'name': 'created_at',
                                'type': 'DateTime',
                                'length': None,
                                'nullable': False},
                               {'name': 'received_at',
                                'type': 'DateTime',
                                'length': None,
                                'nullable': False},
                               {'name': 'updated_at',
                                'type': 'DateTime',
                                'length': None,
                                'nullable': False}],
                   'constraints': []},
 'mailboxes': {'columns': [{'name': 'id',
                            'type': 'String',
                            'length': 40,
                            'primary_key': True,
                            'index': True},
                           {'name': 'label',
                            'type': 'String',
                            'length': 100,
                            'unique': True,
                            'nullable': False},
                           {'name': 'allowed_scopes',
                            'type': 'String',
                            'length': 200,
                            'nullable': True},
                           {'name': 'note', 'type': 'Text', 'length': None, 'nullable': True},
                           {'name': 'created_at',
                            'type': 'DateTime',
                            'length': None,
                            'nullable': False},
                           {'name': 'updated_at',
                            'type': 'DateTime',
                            'length': None,
                            'nullable': False}],
               'constraints': []},
 'inbound_rules': {'columns': [{'name': 'id',
                                'type': 'String',
                                'length': 40,
                                'primary_key': True,
                                'index': True},
                               {'name': 'to_number',
                                'type': 'String',
                                'length': 64,
                                'index': True,
                                'nullable': False},
                               {'name': 'mailbox_label',
                                'type': 'String',
                                'length': 100,
                                'nullable': False},
                               {'name': 'created_at',
                                'type': 'DateTime',
                                'length': None,
                                'nullable': False}],
                   'constraints': []},
 'inbound_events': {'columns': [{'name': 'id',
                                 'type': 'String',
                                 'length': 40,
                                 'primary_key': True,
                                 'index': True},
                                {'name': 'provider_sid',
                                 'type': 'String',
                                 'length': 100,
                                 'nullable': False},
                                {'name': 'event_type',
                                 'type': 'String',
                                 'length': 50,
                                 'nullable': False},
                                {'name': 'created_at',
                                 'type': 'DateTime',
                                 'length': None,
                                 'nullable': False}],
                    'constraints': [{'type': 'UniqueConstraint',
                                     'columns': ['provider_sid', 'event_type']}]}}
