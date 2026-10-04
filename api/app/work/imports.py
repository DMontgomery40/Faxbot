"""Generic import: another system hands Faxbot one PDF and a small manifest.

``POST /imports`` (``work:import``) takes a multipart ``file`` and a ``manifest``
JSON string. The PDF is validated before anything is recorded, then acquired
through the inbound acquisition store under source ``import`` and account
``import:<principal id>``, so the document lands in a mailbox by its
``to_number`` exactly like a received fax and reaches the work queue.

The import identity is that account plus ``operation_id`` and ``revision``. A
replay with the same identity and the same bytes returns the existing import
as ``duplicate``; the same identity with different bytes is a conflict (409)
that the acquisition store records without touching the stored document. A new
``revision`` is a related, separate document. ``source_system`` is recorded in
the acquisition report; give each source system its own API key, or keep
operation ids unique across the systems that share one.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import tempfile
import uuid

from ..routing.numbers import stored_number


MANIFEST_FIELDS = frozenset({'source_system', 'operation_id', 'revision', 'source_received_at', 'to_number',
                             'from_number', 'pages'})
_RFC3339 = re.compile(r'\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?([Zz]|[+-]\d{2}:\d{2})')


class ImportInputError(ValueError):
    """One plain sentence about what to fix."""


class ImportConflict(RuntimeError):
    """The same import identity arrived with different bytes."""


class ImportUnavailable(RuntimeError):
    """Imports cannot be recorded on this installation yet."""


@dataclass(frozen=True)
class ImportManifest:
    source_system: str
    operation_id: str
    revision: str
    source_received_at: datetime | None
    to_number: str | None
    from_number: str | None
    pages: int | None

    def report(self):
        """The bounded, sanitized record kept with the acquisition."""
        return {'source_system': self.source_system, 'operation_id': self.operation_id,
                'revision': self.revision or None,
                'source_received_at': (self.source_received_at.isoformat(timespec='seconds') + 'Z'
                                       if self.source_received_at else None),
                'to_number': self.to_number, 'from_number': self.from_number, 'pages': self.pages}


def _text(value, name, limit, *, required=False):
    if value is None or value == '':
        if required:
            raise ImportInputError(f'The manifest needs {name}.')
        return None
    if type(value) is not str or len(value.strip()) == 0:
        raise ImportInputError(f'{name} must be text.')
    value = value.strip()
    if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ImportInputError(f'{name} can be up to {limit} characters of plain text.')
    return value


def parse_time(value):
    """An RFC 3339 time with its offset, as naive UTC."""
    if value is None or value == '':
        return None
    if type(value) is not str or not _RFC3339.fullmatch(value.strip()):
        raise ImportInputError('source_received_at must be a time like 2026-10-03T14:05:00Z, with its offset.')
    text = value.strip().replace('z', 'Z').replace('Z', '+00:00')
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ImportInputError('source_received_at must be a time like 2026-10-03T14:05:00Z, with its offset.') from None
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def parse_manifest(raw, *, country):
    if not isinstance(raw, str) or not raw.strip():
        raise ImportInputError('Add a manifest describing the document.')
    if len(raw) > 8192:
        raise ImportInputError('The manifest is too large.')
    try:
        data = json.loads(raw)
    except ValueError:
        raise ImportInputError('The manifest is not valid JSON.') from None
    if not isinstance(data, dict):
        raise ImportInputError('The manifest must be a JSON object.')
    unknown = sorted(set(data) - MANIFEST_FIELDS)
    if unknown:
        raise ImportInputError(f"The manifest has fields Faxbot does not use: {', '.join(unknown)}.")
    pages = data.get('pages')
    if pages is not None and (type(pages) is not int or not 1 <= pages <= 10000):
        raise ImportInputError('pages must be a whole number from 1 to 10000.')
    numbers = {}
    for field in ('to_number', 'from_number'):
        value = _text(data.get(field), field, 64)
        numbers[field] = stored_number(value, country=country) if value else None
    return ImportManifest(
        source_system=_text(data.get('source_system'), 'source_system', 64, required=True),
        operation_id=_text(data.get('operation_id'), 'operation_id', 100, required=True),
        revision=_text(data.get('revision'), 'revision', 40) or '',
        source_received_at=parse_time(data.get('source_received_at')),
        to_number=numbers['to_number'], from_number=numbers['from_number'], pages=pages)


async def spool_upload(upload, *, max_bytes, directory):
    """Copy the upload to a private temporary file; return (path, sha256, size)."""
    if upload is None:
        raise ImportInputError('Attach the document as a PDF file.')
    os.makedirs(directory, exist_ok=True)
    handle, path = tempfile.mkstemp(prefix='import-', suffix='.pdf', dir=directory)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(handle, 'wb') as output:
            while chunk := await upload.read(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ImportInputError(f'The file is larger than the {max_bytes // (1024 * 1024)} MB limit.')
                digest.update(chunk)
                output.write(chunk)
        if size == 0:
            raise ImportInputError('The file is empty.')
    except BaseException:
        discard(path)
        raise
    return path, digest.hexdigest(), size


def validate_document(path):
    """The page count of a valid PDF; anything else is refused before it is recorded."""
    from ..conversion import DocumentConversionError, validate_pdf
    try:
        with open(path, 'rb') as handle:
            if handle.read(5) != b'%PDF-':
                raise ImportInputError('The file is not a PDF.')
        return validate_pdf(path)
    except DocumentConversionError:
        raise ImportInputError('The file is not a valid PDF.') from None


def discard(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def store_document(path, directory):
    """Move the validated file into storage; return its location (a path or storage URI)."""
    from ..storage import get_storage
    name = f'{uuid.uuid4().hex}.pdf'
    final = os.path.join(directory, name)
    os.replace(path, final)
    location = get_storage().put_pdf(final, name)
    if location.startswith('s3://'):
        discard(final)
    return location
