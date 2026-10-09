"""Received fax expiration and decoded-original publication share one parent lock."""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import re

import sqlalchemy as sa


class InboundRetentionError(RuntimeError):
    """A received document change could not be completed safely."""


_REFLECTED = {}


def _tables(engine):
    key = id(engine)
    found = _REFLECTED.get(key)
    if found is None or found[0] is not engine:
        metadata = sa.MetaData()
        found = (engine, {
            name: sa.Table(name, metadata, autoload_with=engine, resolve_fks=False)
            for name in ('inbound_faxes', 'codec_receipts')})
        _REFLECTED[key] = found
    return found[1]


@contextmanager
def locked_document(engine, inbound_fax_id):
    """Yield (connection, current parent mapping or None) in a write transaction.

    Decoding happens outside this guard. Publication must check the source is
    still available while holding the same guard as expiration. PostgreSQL
    locks this fax's row; SQLite serializes writers. Commit failures propagate.
    """
    try:
        faxes = _tables(engine)['inbound_faxes']
        with engine.connect() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            else:
                connection.begin()
            try:
                query = sa.select(faxes).where(faxes.c.id == inbound_fax_id)
                if connection.dialect.name != 'sqlite':
                    query = query.with_for_update()
                row = connection.execute(query).mappings().first()
                yield connection, row
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except sa.exc.SQLAlchemyError:
        raise InboundRetentionError('Received document storage could not complete the change.') from None


def _local_path(value, data_dir):
    # The same boundary used when an authenticated fax engine hands over its
    # image: an exact recorded path inside the data folder, with no links below it.
    from .worker import inside_directory
    path = Path(inside_directory(str(value), str(data_dir)))
    if path.exists() and not path.is_file():
        raise InboundRetentionError('The received document is not a regular file.')
    return path


def _decoded_path(receipt, data_dir):
    identity, digest = receipt['inbound_fax_id'], receipt['document_sha256']
    suffix = {'application/pdf': '.pdf', 'text/plain': '.txt'}.get(receipt['content_type'])
    if (not isinstance(identity, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,40}', identity) is None
            or not isinstance(digest, str) or re.fullmatch(r'[a-f0-9]{64}', digest) is None or suffix is None):
        raise InboundRetentionError('The decoded original does not have an owned filename.')
    path = _local_path(receipt['document_path'], data_dir)
    if (path.parent != Path(data_dir).resolve()
            or path.name != f'{identity}-decoded-{digest[:12]}{suffix}'):
        raise InboundRetentionError('The decoded original is outside its owned file location.')
    return path


def _pdf_location(fax, storage, data_dir):
    """Validate the exact recorded local file or object in the selected storage."""
    identity = fax['id']
    if not isinstance(identity, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,40}', identity) is None:
        raise InboundRetentionError('The received document does not have an owned filename.')
    value = str(fax['pdf_path'])
    if value.startswith('s3://'):
        prefix = f"s3://{getattr(storage, 'bucket', '')}/{getattr(storage, 'prefix', '') or ''}"
        if not getattr(storage, 'bucket', None) or not value.startswith(prefix):
            raise InboundRetentionError('The received document is outside its configured storage.')
        name = value[len(prefix):]
    else:
        path = _local_path(value, data_dir)
        if path.parent != Path(data_dir).resolve():
            raise InboundRetentionError('The received document is outside its owned file location.')
        name = path.name
    # Current acquisition names include a content digest; earlier files used only the fax ID.
    if re.fullmatch(re.escape(identity) + r'(?:-[a-f0-9]{12})?\.pdf', name) is None:
        raise InboundRetentionError('The received document does not have an owned filename.')
    return value


def remove_expired_documents(engine, storage, data_dir, *, now=None):
    """Return (removed IDs, IDs needing attention); never enumerate/delete a folder.

    Null or future deadlines keep every copy. Delete failures retain database
    references for retry. Decoded local copies share the received PDF's policy,
    including when that PDF is kept in object storage.
    """
    now = now or datetime.utcnow()
    tables = _tables(engine)
    faxes, receipts = tables['inbound_faxes'], tables['codec_receipts']
    with engine.connect() as connection:
        identities = connection.execute(sa.select(faxes.c.id).select_from(
            faxes.outerjoin(receipts, receipts.c.inbound_fax_id == faxes.c.id)).where(
                faxes.c.retention_until <= now,
                sa.or_(faxes.c.pdf_path.is_not(None), faxes.c.tiff_path.is_not(None),
                       receipts.c.document_path.is_not(None)))).scalars().all()
    removed, attention = [], []
    for identity in identities:
        try:
            with locked_document(engine, identity) as (connection, fax):
                if fax is None or fax['retention_until'] is None or fax['retention_until'] > now:
                    continue
                receipt = connection.execute(sa.select(receipts).where(
                    receipts.c.inbound_fax_id == identity)).mappings().first()
                decoded = _decoded_path(receipt, data_dir) if receipt and receipt['document_path'] else None
                pdf = _pdf_location(fax, storage, data_dir) if fax['pdf_path'] else None
                tiff = _local_path(fax['tiff_path'], data_dir) if fax['tiff_path'] else None
                if tiff is not None and tiff.suffix.lower() not in ('.tif', '.tiff'):
                    raise InboundRetentionError('The received image does not have an image filename.')
                # Validate every location before deleting any of this fax's files.
                if decoded is not None:
                    decoded.unlink(missing_ok=True)
                if pdf is not None:
                    storage.delete(pdf)
                if tiff is not None:
                    tiff.unlink(missing_ok=True)
                if decoded is not None:
                    connection.execute(receipts.update().where(receipts.c.inbound_fax_id == identity).values(
                        document_path=None))
                connection.execute(faxes.update().where(faxes.c.id == identity).values(
                    pdf_path=None, tiff_path=None, updated_at=now))
            removed.append(identity)
        except Exception:
            attention.append(identity)
    return removed, attention
