"""File one received document through the generic import contract, once.

Every connector imports under its own account, ``import:connector:<id>``, with
the identity it chose (a message and attachment position, or a file's
SHA-256) as operation id and revision, so the acquisition store returns the
existing import for a document seen before. The connector's item compares the
digest of what the source supplied: a converted TIFF is not byte-identical
between runs, so only a PDF passed through unchanged is compared by the
acquisition store as well.
"""
from dataclasses import dataclass
from datetime import datetime

from ...work.imports import ImportConflict, ImportManifest, record_import
from . import documents, text


ACCOUNT_PREFIX = 'connector:'


def account(source_id):
    from ...inbound.acquisition import account_identity
    return account_identity('import', ACCOUNT_PREFIX + source_id)


@dataclass
class Arrival:
    operation_id: str
    part: str
    name: str
    kind: str
    data: bytes
    digest: str
    to_number: str | None
    from_number: str | None = None
    received_at: datetime | None = None
    reference: str | None = None
    subject: str | None = None
    sender: str | None = None
    pages: int | None = None
    report: dict | None = None
    mailbox_label: str | None = None


def file_document(access, values, store, source, arrival):
    """Returns (item, outcome) with outcome 'imported', 'duplicate', 'conflict' or 'failed'."""
    fields = dict(reference=arrival.reference, subject=arrival.subject or arrival.name, sender=arrival.sender,
                  source_received_at=arrival.received_at, to_number=arrival.to_number,
                  document_digest=arrival.digest)
    existing = store.item(source.id, arrival.operation_id, arrival.part)
    if existing is not None:
        if existing['document_digest'] and existing['document_digest'] != arrival.digest:
            store.mark_conflict(existing, text.CONFLICT)
            return existing, 'conflict'
        store.seen_again(existing)
        return existing, 'duplicate'
    if not arrival.to_number:
        item, created = store.record(source.id, 'receive', arrival.operation_id, arrival.part, state='failed',
                                     reason=text.NO_MAILBOX, **fields)
        return item, 'failed' if created else 'duplicate'
    try:
        path, pages, pdf_digest, converted = documents.to_pdf(arrival.data, arrival.kind, values.fax_data_dir)
    except documents.Unreadable:
        item, created = store.record(source.id, 'receive', arrival.operation_id, arrival.part, state='failed',
                                     reason=text.UNREADABLE_RECEIVE.format(name=arrival.name), **fields)
        return item, 'failed' if created else 'duplicate'
    manifest = ImportManifest(source_system=source.kind, operation_id=arrival.operation_id, revision=arrival.part,
                              source_received_at=arrival.received_at, to_number=arrival.to_number,
                              from_number=arrival.from_number, pages=arrival.pages)
    report = {'source_system': source.kind, 'connector': source.name, 'operation_id': arrival.operation_id,
              'revision': arrival.part or None, 'file_name': arrival.name[:200], 'source_sha256': arrival.digest,
              'message_id': arrival.reference, 'sender': arrival.sender, 'to_number': arrival.to_number,
              'source_received_at': (arrival.received_at.isoformat(timespec='seconds') + 'Z'
                                     if arrival.received_at else None), **(arrival.report or {})}
    try:
        status, import_id, inbound_id = record_import(access, values, account=account(source.id), manifest=manifest,
                                                      path=path, digest=pdf_digest, report=report,
                                                      compare=not converted)
    except ImportConflict:
        item, created = store.record(source.id, 'receive', arrival.operation_id, arrival.part, state='conflict',
                                     reason=text.CONFLICT, **fields)
        if not created:
            store.seen_again(item)
        return item, 'conflict'
    finally:
        documents.discard(path)
    where = (text.FILED.format(mailbox=arrival.mailbox_label) if arrival.mailbox_label
             else text.FILED_NUMBER.format(number=arrival.to_number))
    item, created = store.record(source.id, 'receive', arrival.operation_id, arrival.part, state='imported',
                                 reason=where, import_id=import_id, inbound_fax_id=inbound_id, **fields)
    if status == 'duplicate' or not created:
        # Filed before this connector's own record was written (a crash in between): a replay, not a new document.
        store.seen_again(item)
        return item, 'duplicate'
    return item, 'imported'
