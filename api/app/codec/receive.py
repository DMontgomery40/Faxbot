"""Receiving with the experimental payload codec: find payload pages, decode them, keep both.

A received fax is checked when it is first delivered or opened: page one is
probed for the payload pattern (cheap, so every fax can be checked). A payload
fax is decoded, its SHA-256 checked, a PDF original validated, and the
original kept beside the received fax, whose image is never changed. A fax
whose pages cannot be decoded is delivered as received, with one sentence
saying why. Successful results are kept in ``codec_receipts``; a failed
attempt is checked again after shared-key settings change.
"""
import logging
import os
from pathlib import Path
import tempfile

from .store import CodecSettings, CodecStoreError, receipt_for, record_receipt, utcnow

log = logging.getLogger(__name__)
EXTENSIONS = {'application/pdf': '.pdf', 'text/plain': '.txt'}


def _reason(error):
    text = str(error).strip().rstrip('.') or 'The encoded pages could not be read'
    return f'{text}, so the fax is delivered as received.'[:300]


def check_document(engine, inbound_fax_id, data, *, from_number=None, folder, seal=None):
    """The decode result for a received fax's document bytes, or None when it carries no payload pages."""
    from .. import codec
    try:
        existing = receipt_for(engine, inbound_fax_id)
    except CodecStoreError:
        return None
    settings = CodecSettings(engine, seal)
    if existing is not None:
        if existing['state'] == 'decoded':
            return existing
        try:
            if not settings.keys_changed_since(existing['created_at']):
                return existing
        except CodecStoreError:
            return existing
    # Start before reading keys: a key changed during decoding still permits a later retry.
    attempted_at = utcnow()
    # Probe page one only; an ordinary fax costs one page's image and one scan for the pattern.
    probe = codec.first_page(data)
    if probe is None or not codec.looks_like_payload(probe):
        return None
    try:
        secrets = settings.secrets(from_number)
    except Exception:
        secrets = []
    try:
        document, report = codec.decode_images(codec.read_images(data), secrets=secrets)
    except codec.CodecError as error:
        return record_receipt(engine, inbound_fax_id, {'state': 'failed', 'reason': _reason(error)}, now=attempted_at)
    except Exception:
        log.warning('Decoding a received payload fax failed; it is delivered as received.')
        return record_receipt(engine, inbound_fax_id, {
            'state': 'failed', 'reason': 'The encoded pages could not be read, so the fax is delivered as received.'},
            now=attempted_at)
    if document.content_type == 'application/pdf':
        from ..conversion import DocumentConversionError, validate_pdf
        descriptor, temporary = tempfile.mkstemp(prefix='.codec-', suffix='.pdf', dir=folder)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(document.data)
            validate_pdf(temporary)
        except DocumentConversionError:
            return record_receipt(engine, inbound_fax_id, {
                'state': 'failed', 'reason': 'The decoded document is not a PDF Faxbot can open, so the fax is '
                                             'delivered as received.'}, now=attempted_at)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return _publish_document(engine, inbound_fax_id, document, report, folder, attempted_at)


def _publish_document(engine, inbound_fax_id, document, report, folder, attempted_at):
    """Publish only while the received source is retained, sharing its cleanup lock."""
    from ..inbound.retention import locked_document
    temporary = published = None
    try:
        with locked_document(engine, inbound_fax_id) as (connection, inbound):
            if inbound is None or inbound['status'] != 'received' or not inbound['pdf_path']:
                return None
            # Another checker may have completed while this one was decoding.
            existing = receipt_for(engine, inbound_fax_id, connection=connection)
            if existing is not None and existing['state'] == 'decoded':
                return existing
            target = Path(folder) / f'{inbound_fax_id}-decoded-{document.sha256[:12]}{EXTENSIONS[document.content_type]}'
            descriptor, temporary = tempfile.mkstemp(prefix='.codec-', dir=folder)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(document.data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            published = target
            temporary = None
            return record_receipt(engine, inbound_fax_id, {
                'state': 'decoded', 'layout': report.get('layout'), 'pages_encoded': report.get('pages_expected'),
                'document_sha256': document.sha256, 'content_type': document.content_type,
                'document_name': (document.name or None) and document.name[:200], 'size_bytes': len(document.data),
                'document_path': str(target)}, now=attempted_at, connection=connection)
    except Exception:
        if published is not None:
            try:
                # Context exit can fail at commit. Recheck under the lock so an
                # uncertain commit or another successful checker never loses its file.
                with locked_document(engine, inbound_fax_id) as (connection, _):
                    stored = receipt_for(engine, inbound_fax_id, connection=connection)
                    if not stored or stored['state'] != 'decoded' or stored.get('document_path') != str(published):
                        published.unlink(missing_ok=True)
            except Exception:
                log.warning('A decoded original could not be published or safely removed; check received-document storage.')
        raise
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def sentence(receipt):
    """One line for the received fax's detail, or None for an ordinary fax."""
    if not receipt:
        return None
    if receipt['state'] == 'decoded':
        count = receipt.get('pages_encoded') or 1
        return (f'Carried an encoded document on {count} page{"s" if count != 1 else ""}; Faxbot decoded it and '
                'checked its fingerprint (experimental).')
    return receipt.get('reason') or 'The encoded pages could not be decoded, so the fax is delivered as received.'


def email_extras(engine, item, document, *, folder, seal=None):
    """(attachment or None, note) for a received fax's email; never raises."""
    try:
        if not item.get('inbound_fax_id'):
            return None, None
        receipt = check_document(engine, item['inbound_fax_id'], document, from_number=item.get('from_number'),
                                 folder=folder, seal=seal)
        if receipt is None:
            return None, None
        if receipt['state'] != 'decoded':
            return None, sentence(receipt)
        data = Path(receipt['document_path']).read_bytes()
        name = receipt.get('document_name') or 'decoded' + EXTENSIONS.get(receipt['content_type'], '.pdf')
        return (data, receipt['content_type'], 'decoded-' + name), (
            'This fax carried an encoded document. Faxbot decoded it and attached the original; '
            'the fax as received is attached too (experimental).')
    except Exception:
        log.warning('Checking a received fax for encoded pages failed; it is delivered as received.')
        return None, ('Faxbot could not check this fax for an encoded original, so the fax is delivered as '
                      'received.')


def attach(message, attachment, note):
    """Add the decoded original and its note to an email built for the received fax."""
    if note:
        body = message.get_body(preferencelist=('plain',))
        if body is not None:
            body.set_content(body.get_content().rstrip('\n') + '\n\n' + note + '\n')
    if attachment is not None:
        data, content_type, filename = attachment
        maintype, subtype = content_type.split('/', 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message
