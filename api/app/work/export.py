"""Evidence export for one work item: a zip with a manifest, the original and its history.

Access is checked when the export is created; it is never cached. The
original document is included only when the exporter may also read documents
(``inbound:document``) on that item; otherwise the manifest says it was
withheld. The manifest is ordered deterministically: the same item version
produces the same manifest apart from the export's own id and time. Earlier
exports are recorded as ``exported`` events but are not part of the item's
history in a manifest. Times in the files are UTC and say so.
"""
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4
import zipfile

import sqlalchemy as sa

from ..access.types import ResourceRef
from . import text


FORMAT = 'faxbot-work-evidence-1'
MAX_DOCUMENT_BYTES = 200 * 1024 * 1024
WITHHELD = 'The original document was withheld because you do not have permission to read documents.'
LIMITS = ('A digest shows whether a file changed after Faxbot stored it; it does not prove who sent the document, '
          'that it is complete, or when it was sent.',
          'Provider and email delivery records show what those systems reported to Faxbot; they do not show that '
          'a person read the document.')


def utc(value):
    return value.isoformat(timespec='seconds') + 'Z' if value is not None else None


def _report(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return value


def load_document(location, values):
    """The stored bytes from local storage or the configured S3 bucket; None when they cannot be read."""
    if not isinstance(location, str) or not location:
        return None
    try:
        if location.startswith('s3://'):
            from ..storage import S3Storage
            storage = S3Storage(bucket=values.s3_bucket, prefix=values.s3_prefix, region=values.s3_region,
                                endpoint=values.s3_endpoint_url, kms_key=values.s3_kms_key_id)
            stream, _ = storage.get_pdf_stream(location)
            with stream:
                data = stream.read(MAX_DOCUMENT_BYTES + 1)
        else:
            path = Path(location)
            if path.is_symlink() or not path.is_file():
                return None
            with path.open('rb') as handle:
                data = handle.read(MAX_DOCUMENT_BYTES + 1)
    except Exception:
        return None
    return data if len(data) <= MAX_DOCUMENT_BYTES else None


class EvidenceExport:
    def __init__(self, service, *, values):
        """``values`` returns the active configuration values (storage settings)."""
        self.service, self.store, self.values = service, service.store, values

    def _gather(self, actor, item_id):
        service, store = self.service, self.store
        with service.access_store.transaction() as connection:
            now = service.clock()
            row = service._require(connection, actor, item_id, 'work:export', now)
            documents = service.control.authorize_on(connection, actor, 'inbound:document',
                                                     ResourceRef(row['resource_id']), now=now).allowed
            inbound = dict(connection.execute(sa.select(store.inbound).where(
                store.inbound.c.id == row['inbound_fax_id'])).mappings().one())
            imports = [dict(item) for item in connection.execute(
                sa.select(store.imports).where(store.imports.c.inbound_fax_id == row['inbound_fax_id'])
                .order_by(store.imports.c.imported_at, store.imports.c.id)).mappings()]
            deliveries = [dict(item) for item in connection.execute(
                sa.select(store.intake).where(store.intake.c.inbound_fax_id == row['inbound_fax_id'])
                .order_by(store.intake.c.received_at, store.intake.c.id)).mappings()]
            connectors = {item.id: item for item in connection.execute(sa.select(
                store.connectors.c.id, store.connectors.c.name, store.connectors.c.settings))}
            events = [dict(item) for item in connection.execute(
                sa.select(store.events).where(store.events.c.work_item_id == item_id,
                                              store.events.c.kind != 'exported')
                .order_by(store.events.c.occurred_at, store.events.c.created_at, store.events.c.id)).mappings()]
            names = store.names_on(connection, [row[field] for field in ('owner_principal_id', 'backup_principal_id',
                                                                         'acknowledged_by', 'done_by')]
                                   + [actor.principal_id])
        return now, row, documents, inbound, imports, deliveries, connectors, events, names

    def create(self, actor, item_id):
        """Return (zip bytes, file name). Records an ``exported`` event with the manifest digest."""
        now, row, documents, inbound, imports, deliveries, connectors, events, names = self._gather(actor, item_id)
        missing = []
        original = None
        if not documents:
            missing.append(WITHHELD)
        elif not inbound['pdf_path']:
            missing.append('The original document is no longer stored.')
        else:
            original = load_document(inbound['pdf_path'], self.values())
            if original is None:
                missing.append('The original document could not be read from storage.')
        digest = hashlib.sha256(original).hexdigest() if original is not None else None
        if digest is not None and inbound['sha256'] and digest != inbound['sha256']:
            missing.append('The stored document does not match the digest recorded when it arrived.')
        if not any(item['report'] for item in imports):
            missing.append('No provider receipt was retained for this fax.')
        if not any(item['source_received_at'] for item in imports):
            missing.append('The source did not report when it received this document.')
        if not deliveries:
            missing.append('No email delivery was recorded for this document.')
        if row['acknowledged_at'] is None and not any(event['kind'] == 'acknowledged' for event in events):
            missing.append('No acknowledgement by an owner has been recorded.')

        def delivery(item):
            connector = connectors.get(item['connector_id'])
            recipients = []
            if connector is not None and item['state'] == 'delivered':
                try:
                    recipients = list(json.loads(connector.settings).get('recipients') or [])
                except ValueError:
                    recipients = []
            return {'state': item['state'], 'connector': connector.name if connector is not None else None,
                    'delivered_to': recipients, 'delivered_at': utc(item['delivered_at']),
                    'attempts': item['attempts'], 'problem': item['last_error'], 'queued_at': utc(item['received_at'])}

        history = []
        for event in events:
            details = json.loads(event['details'] or '{}')
            history.append({'occurred_at': utc(event['occurred_at']), 'kind': event['kind'],
                            'actor': details.get('actor_name'), 'text': text.event_text(event['kind'], details)})
        export_id = uuid4().hex
        manifest = {
            'export': {'id': export_id, 'created_at': utc(now), 'exported_by': names.get(actor.principal_id),
                       'format': FORMAT, 'snapshot_version': row['version']},
            'item': {
                'state': row['state'], 'mailbox': row['mailbox'],
                'owner': names.get(row['owner_principal_id']), 'backup': names.get(row['backup_principal_id']),
                'available_at': utc(row['available_at']), 'due_at': utc(row['due_at']),
                'due_rule': text.due_text(row), 'assigned_at': utc(row['assigned_at']),
                'acknowledged_at': utc(row['acknowledged_at']), 'acknowledged_by': names.get(row['acknowledged_by']),
                'escalated_at': utc(row['escalated_at']), 'done_at': utc(row['done_at']),
                'done_by': names.get(row['done_by']), 'done_note': row['done_note'],
                'created_at': utc(row['created_at']),
            },
            'document': {
                'from_number': inbound['from_number'], 'to_number': inbound['to_number'], 'pages': inbound['pages'],
                'size_bytes': inbound['size_bytes'], 'sha256': inbound['sha256'], 'status': inbound['status'],
                'received_at': utc(inbound['received_at']), 'provider': inbound['backend'],
                'provider_fax_id': inbound['provider_sid'],
                'file': 'original.pdf' if original is not None else None, 'file_sha256': digest,
            },
            'acquisition': [{
                'source': item['source'], 'account': item['account'], 'operation_id': item['operation_id'],
                'revision': item['revision'], 'state': item['state'],
                'source_received_at': utc(item['source_received_at']), 'imported_at': utc(item['imported_at']),
                'acquired_at': utc(item['acquired_at']), 'reported_pages': item['reported_pages'],
                'artifact_sha256': item['artifact_digest'], 'artifact_size': item['artifact_size'],
                'artifact_media_type': item['artifact_media_type'], 'provider_report': _report(item['report']),
            } for item in imports],
            'email_delivery': [delivery(item) for item in deliveries],
            'history': history,
            'missing': missing,
            'limits': list(LIMITS),
        }
        document = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False).encode('utf-8') + b'\n'
        manifest_digest = hashlib.sha256(document).hexdigest()
        lines = [f"Evidence for a document that arrived {text.time_text(row['available_at'])}.",
                 f"Exported by {names.get(actor.principal_id) or 'an unnamed account'} on {text.time_text(now)}.",
                 '']
        lines += [f"{text.time_text(event['occurred_at'])}  {text.event_text(event['kind'], json.loads(event['details'] or '{}'))}"
                  for event in events]
        if missing:
            lines += ['', 'Not included or not recorded:'] + [f'- {item}' for item in missing]
        lines += ['', *LIMITS]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', document)
            archive.writestr('history.txt', '\n'.join(lines) + '\n')
            if original is not None:
                archive.writestr('original.pdf', original)
        self._record(actor, item_id, export_id, manifest_digest)
        return buffer.getvalue(), f"faxbot-evidence-{row['available_at']:%Y%m%d-%H%M}.zip"

    def _record(self, actor, item_id, export_id, manifest_digest):
        """Append the export to the item's history; access is checked again so a revoked exporter gets nothing."""
        service = self.service
        with service.access_store.transaction() as connection:
            now = service.clock()
            service._require(connection, actor, item_id, 'work:export', now)
            name = self.store.names_on(connection, [actor.principal_id]).get(actor.principal_id)
            self.store.event_on(connection, item_id, 'exported', actor_id=actor.principal_id, now=now,
                                details={'actor_name': name, 'export_id': export_id,
                                         'manifest_sha256': manifest_digest})
