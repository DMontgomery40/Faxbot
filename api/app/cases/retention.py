"""Kept case originals follow the installation's artifact retention setting.

An original is removed once the retention period has passed since it was last
added to its case or last sent in a packet: its record is deleted, a removal
row keeps only its case, SHA-256, source, version and the time, and its file
in ``cases/`` is deleted once no other original uses the same bytes. Only
files in ``cases/`` whose names Faxbot wrote are ever touched: ``<sha256>.pdf``
and the ``.<sha256>.<id>.part`` names used while writing one.
"""
from datetime import datetime
from pathlib import Path
import re
from uuid import uuid4

import sqlalchemy as sa

from ..routing.database import reflect, utcnow, write_transaction


KEPT_FILE = re.compile(r'([a-f0-9]{64})\.pdf')
PARTIAL_FILE = re.compile(r'\.[a-f0-9]{64}\.[a-f0-9]{32}\.part')
TABLES = ('case_originals', 'case_entries', 'case_entry_sends', 'case_original_removals')


def _old(path, cutoff):
    try:
        return datetime.utcfromtimestamp(path.lstat().st_mtime) < cutoff
    except FileNotFoundError:
        return False


def remove_expired_originals(engine, data_dir, cutoff, *, now=None):
    """Remove originals last added or sent before ``cutoff``; returns how many records were removed."""
    now = now or utcnow()
    t = reflect(engine, TABLES)
    o, c, s, r = t['case_originals'], t['case_entries'], t['case_entry_sends'], t['case_original_removals']
    # When each original was last sent: the newest packet that carried its bytes in its case.
    sent = (sa.select(c.c.case_id, c.c.digest, sa.func.max(s.c.created_at).label('at'))
            .join(s, s.c.entry_id == c.c.id).group_by(c.c.case_id, c.c.digest).subquery())
    query = (sa.select(o.c.id, o.c.case_id, o.c.digest, o.c.source, o.c.version, o.c.created_at, sent.c.at)
             .select_from(o.outerjoin(sent, sa.and_(sent.c.case_id == o.c.case_id, sent.c.digest == o.c.digest))))
    with write_transaction(engine) as connection:
        expired = [row for row in connection.execute(query)
                   if max(row.created_at, row.at or row.created_at) < cutoff]
        for row in expired:
            connection.execute(r.insert().values(
                id=uuid4().hex, case_id=row.case_id, digest=row.digest, source=row.source, version=row.version,
                reason='retention', removed_at=now, created_at=now))
            connection.execute(o.delete().where(o.c.id == row.id))
        in_use = set(connection.execute(sa.select(o.c.digest).distinct()).scalars())
    folder = Path(data_dir) / 'cases'
    if folder.is_dir() and not folder.is_symlink():
        for path in folder.iterdir():
            kept = KEPT_FILE.fullmatch(path.name)
            unused = kept is not None and kept.group(1) not in in_use
            stray = PARTIAL_FILE.fullmatch(path.name) is not None
            # A file still in use, or written within the period, stays; a write in progress is recent.
            if (unused or stray) and not path.is_symlink() and path.is_file() and _old(path, cutoff):
                path.unlink(missing_ok=True)
    return len(expired)


def removals(connection, table, case_id):
    """{sha256: when the retention cleanup last removed an original with those bytes from this case}."""
    found = {}
    for digest, removed_at in connection.execute(sa.select(table.c.digest, table.c.removed_at).where(
            table.c.case_id == case_id).order_by(table.c.removed_at)):
        found[digest] = removed_at
    return found
