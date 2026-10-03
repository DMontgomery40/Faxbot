"""Frozen revision0004 outbound delivery schema; independent of runtime models."""
from datetime import datetime, timezone
from itertools import islice
import json
import uuid

import sqlalchemy as sa

from .schema_configuration import frozen_metadata as configuration_metadata

REVISION = '0004_outbound_delivery'
TABLES = frozenset({'outbound_attempts', 'outbound_deliveries', 'outbound_events'})


def frozen_metadata(*, dialect='sqlite'):
    metadata = configuration_metadata(dialect=dialect)
    attempts = sa.Table('outbound_attempts', metadata,
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('job_id', sa.String(40), sa.ForeignKey('fax_jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sequence', sa.Integer, nullable=False),
        sa.Column('profile_id', sa.String(40), sa.ForeignKey('provider_profiles.id', ondelete='RESTRICT')),
        sa.Column('phase', sa.String(32), nullable=False),
        sa.Column('provider_sid', sa.String(100)),
        sa.Column('error_category', sa.String(64)),
        sa.Column('created_at', sa.DateTime, nullable=False),
        sa.Column('submitted_at', sa.DateTime),
        sa.Column('completed_at', sa.DateTime),
    )
    sa.Index('uq_outbound_attempts_job_sequence', attempts.c.job_id, attempts.c.sequence, unique=True)
    sa.Index('ix_outbound_attempts_job_id', attempts.c.job_id)
    deliveries = sa.Table('outbound_deliveries', metadata,
        sa.Column('id', sa.String(40), sa.ForeignKey('fax_jobs.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('dispatch_mode', sa.String(16), nullable=False),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('version', sa.Integer, nullable=False),
        sa.Column('attempt_id', sa.String(40), sa.ForeignKey('outbound_attempts.id', ondelete='SET NULL')),
        sa.Column('claim_owner', sa.String(40)),
        sa.Column('claim_token', sa.String(64)),
        sa.Column('claim_expires_at', sa.DateTime),
        sa.Column('next_poll_at', sa.DateTime),
        sa.Column('request_fingerprint', sa.String(64)),
        sa.Column('principal_scope', sa.String(100)),
        sa.Column('idempotency_digest', sa.String(64)),
        sa.Column('legacy_status', sa.String(32)),
        sa.Column('created_at', sa.DateTime, nullable=False),
        sa.Column('updated_at', sa.DateTime, nullable=False),
    )
    sa.Index('uq_outbound_deliveries_principal_idempotency', deliveries.c.principal_scope,
             deliveries.c.idempotency_digest, unique=True)
    sa.Index('ix_outbound_deliveries_state_claim_expires', deliveries.c.state, deliveries.c.claim_expires_at)
    sa.Index('ix_outbound_deliveries_next_poll_at', deliveries.c.next_poll_at)
    events = sa.Table('outbound_events', metadata,
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('job_id', sa.String(40), sa.ForeignKey('fax_jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('attempt_id', sa.String(40), sa.ForeignKey('outbound_attempts.id', ondelete='SET NULL')),
        sa.Column('kind', sa.String(40), nullable=False),
        sa.Column('dedupe_key', sa.String(64)),
        sa.Column('details', sa.Text, nullable=False),
        sa.Column('created_at', sa.DateTime, nullable=False),
    )
    sa.Index('uq_outbound_events_job_dedupe', events.c.job_id, events.c.dedupe_key, unique=True)
    sa.Index('ix_outbound_events_job_created_at', events.c.job_id, events.c.created_at)
    return metadata


def upgrade_outbound(connection, operations):
    metadata = frozen_metadata(dialect=connection.dialect.name)
    for table in metadata.sorted_tables:
        if table.name in TABLES:
            table.create(connection)
    migrated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    jobs = metadata.tables['fax_jobs']
    deliveries = metadata.tables['outbound_deliveries']
    events = metadata.tables['outbound_events']
    # Iterate historical rows without retaining their contents in application
    # memory. A PostgreSQL named cursor remains in the guarded transaction.
    rows = connection.execute(sa.select(jobs.c.id, jobs.c.status).execution_options(stream_results=True))
    try:
        for job_id, status in rows:
            state = 'success' if status.lower() in {'success', 'completed', 'completed_ok'} else 'reconciliation_required'
            connection.execute(deliveries.insert().values(
                id=job_id, dispatch_mode='legacy', state=state, version=1,
                legacy_status=status, created_at=migrated_at, updated_at=migrated_at,
            ))
            # The column retains exact evidence. The bounded event strips control
            # characters and never copies paths, provider errors or fax contents.
            safe_status = ''.join(islice((character for character in status if character.isprintable()), 128))
            details = json.dumps({'legacy_status': safe_status}, ensure_ascii=True, separators=(',', ':'))
            connection.execute(events.insert().values(
                id=uuid.uuid4().hex, job_id=job_id, kind='legacy_migrated',
                dedupe_key='legacy_migration', details=details, created_at=migrated_at,
            ))
    finally:
        rows.close()
