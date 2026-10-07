"""Receiving with the experimental payload codec: find payload pages, decode them, keep both.

A received fax is checked when it is first delivered or opened: page one is
probed for the payload pattern (cheap, so every fax can be checked). A payload
fax is decoded, its SHA-256 checked, a PDF original validated, and the
original kept beside the received fax, whose image is never changed. A fax
whose pages cannot be decoded is delivered as received, with one sentence
saying why. The result is written once in ``codec_receipts``.
"""
import logging
import os
from pathlib import Path
import tempfile

from .store import CodecSettings, CodecStoreError, receipt_for, record_receipt

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
    if existing is not None:
        return existing
    try:
        images = codec.read_images(data)
    except codec.CodecError:
        return None
    if not images or not any(codec.looks_like_payload(image) for image in images[:1]):
        return None
    try:
        secrets = CodecSettings(engine, seal).secrets(from_number)
    except Exception:
        secrets = []
    try:
        document, report = codec.decode_images(images, secrets=secrets)
    except codec.CodecError as error:
        return record_receipt(engine, inbound_fax_id, {'state': 'failed', 'reason': _reason(error)})
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
                                             'delivered as received.'})
        finally:
            Path(temporary).unlink(missing_ok=True)
    target = Path(folder) / f'{inbound_fax_id}-decoded-{document.sha256[:12]}{EXTENSIONS[document.content_type]}'
    descriptor, temporary = tempfile.mkstemp(prefix='.codec-', dir=folder)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(document.data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return record_receipt(engine, inbound_fax_id, {
        'state': 'decoded', 'layout': report.get('layout'), 'pages_encoded': report.get('pages_expected'),
        'document_sha256': document.sha256, 'content_type': document.content_type,
        'document_name': (document.name or None) and document.name[:200], 'size_bytes': len(document.data),
        'document_path': str(target)})


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
        return None, None


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
