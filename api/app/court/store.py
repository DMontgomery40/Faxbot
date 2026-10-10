"""Where the court profile applies (mailboxes and routing rules) and the faxes sent under it (migration 0083)."""
from __future__ import annotations

import json
import uuid

import sqlalchemy as sa

from ..routing.database import read_connection, utcnow, write_transaction

KINDS = ('mailbox', 'rule')


class CourtStoreError(ValueError):
    """A change the court profile store refuses, in one sentence."""


def _tables(bind):
    metadata = sa.MetaData()
    return (sa.Table('court_profile_attachments', metadata, autoload_with=bind),
            sa.Table('court_sends', metadata, autoload_with=bind))


def attachments(engine):
    """The mailboxes and rules the profile applies to now: [{'kind', 'target', 'attached_at', 'by'}]."""
    table, _ = _tables(engine)
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(table).order_by(table.c.created_at, table.c.id)).mappings().all()
    newest = {}
    for row in rows:
        newest[(row['kind'], row['target'])] = row
    return [{'kind': row['kind'], 'target': row['target'], 'attached_at': row['created_at'],
             'by': row['recorded_by_name']} for row in newest.values() if row['state'] == 'active']


def attached_on(connection, kind, target):
    """Whether the profile applies to this mailbox or rule now, read on ``connection``."""
    if not target:
        return False
    table = sa.table('court_profile_attachments', sa.column('kind'), sa.column('target'), sa.column('state'),
                     sa.column('created_at'), sa.column('id'))
    row = connection.execute(sa.select(table.c.state).where(table.c.kind == kind, table.c.target == str(target))
                             .order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)).first()
    return row is not None and row[0] == 'active'


def attached(engine, kind, target):
    with read_connection(engine) as connection:
        return attached_on(connection, kind, target)


def set_attached(engine, kind, target, on, *, actor=None, now=None):
    """Attach the profile to a mailbox or a rule, or take it off; True when anything changed."""
    if kind not in KINDS:
        raise CourtStoreError('Attach the court profile to a mailbox or a routing rule.')
    target = str(target or '').strip()
    if not target or len(target) > 100:
        raise CourtStoreError('Name the mailbox or the routing rule.')
    actor = actor or {}
    table, _ = _tables(engine)
    with write_transaction(engine) as connection:
        if attached_on(connection, kind, target) == bool(on):
            return False
        connection.execute(table.insert().values(
            id=uuid.uuid4().hex, kind=kind, target=target, state='active' if on else 'removed',
            recorded_by=(actor.get('id') or None), recorded_by_name=(actor.get('name') or None),
            created_at=now or utcnow()))
    return True


def recorder(job_id, *, purpose, source, details, cover_pages, by_fax, original_pages, sent_pages):
    """The acceptance-transaction step that keeps the fax's court details, once and unchanged."""
    def record(connection, now):
        _, sends = _tables(connection)
        connection.execute(sends.insert().values(
            id=job_id, purpose=purpose, source=source, details=json.dumps(details, sort_keys=True),
            cover_pages=cover_pages, by_fax=by_fax, original_pages=original_pages, sent_pages=sent_pages,
            created_at=now))
    return record


def send_on(connection, job_id):
    """The fax's court row as a dict (``details`` parsed), or None when it was not sent under the profile."""
    table = sa.table('court_sends', sa.column('id'), sa.column('purpose'), sa.column('source'),
                     sa.column('details'), sa.column('cover_pages'), sa.column('by_fax'),
                     sa.column('original_pages'), sa.column('sent_pages'), sa.column('created_at'))
    row = connection.execute(sa.select(table).where(table.c.id == job_id)).mappings().first()
    if row is None:
        return None
    found = dict(row)
    found['details'] = json.loads(found['details'])
    return found


def send_for(engine, job_id):
    if engine is None or not job_id:
        return None
    with read_connection(engine) as connection:
        return send_on(connection, job_id)
