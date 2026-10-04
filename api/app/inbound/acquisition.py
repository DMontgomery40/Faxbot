"""Durable acquisition of received faxes and imported documents.

Every received document starts as one ``inbound_imports`` row next to the
``inbound_faxes`` row people see. The import names where the document comes
from, ``(source, account, operation_id, revision)``, and stays ``pending``
until the real document has been fetched, validated and stored. Only then does
the fax become ``received``; nothing else, and never a placeholder, is shown or
emailed as the fax. A repeated notification resumes the same import.

Account identity is the non-secret account a notification was authenticated
under, so the same provider fax ID under two accounts stays two records:

- Phaxio: ``phaxio:`` and the first 12 hex digits of SHA-256 of the API key.
- Sinch: ``sinch:`` and the project ID.
- eFax: ``efax:`` and the first 12 hex digits of SHA-256 of the app ID and user ID
  (``efax_service.account_key``); Faxbot finds eFax faxes by asking eFax's API.
- SIP trunk: ``sip:`` and the trunk user name, or ``sip:asterisk``.
- Generic import: ``import:`` and the importing principal's ID.
- Test fax: ``test:`` and the principal's ID.

Other sources (generic import, test fax) use two calls::

    store = ImportStore(access_runtime.inbound)
    begun = store.begin(source='import', account=account_identity('import', principal_id),
                        operation_id=client_id, backend='import', from_number=None, to_number=None,
                        report={'filename': 'scan.pdf'}, schedule=False)
    artifact = store_document(pdf_bytes, begun.inbound_fax_id, provider='The import')
    store.complete(begun.import_id, artifact_path=artifact.path, digest=artifact.digest,
                   size=artifact.size, pages=artifact.pages, media_type=artifact.media_type)

``begin`` returns ``Begun(import_id, inbound_fax_id, state, created, conflict)``;
``complete`` returns ``Completion(stored, state, inbound_fax_id, kept_path)``.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import secrets
import tempfile
from uuid import uuid4

import sqlalchemy as sa

from ..routing.numbers import DEFAULT_COUNTRY


SOURCES = ('phaxio', 'sinch', 'sip', 'import', 'test', 'efax')
FETCHABLE = ('phaxio', 'sinch', 'sip', 'efax')
SOURCE_NAMES = {'phaxio': 'Phaxio', 'sinch': 'Sinch', 'sip': 'the SIP trunk', 'import': 'the import',
                'test': 'Faxbot', 'efax': 'eFax'}
# Minutes to wait after each failed attempt: 1, 2, 4, 8, 16, 32, then hourly for 24 hours.
RETRY_MINUTES = (1, 2, 4, 8, 16, 32) + (60,) * 24
LEASE = timedelta(minutes=2)
# A notification that carried the document schedules a safety fetch this far
# out; storing the attached document cancels it.
DEFERRED_FETCH = timedelta(minutes=5)
REPORT_LIMIT = 8192
MEDIA_TYPE = 'application/pdf'
# Earlier versions stored these fixed stand-ins when a document could not be
# fetched (or for a test). They are read as "not received"; the stored rows are
# left exactly as they are.
PLACEHOLDER_DIGESTS = {
    '8410c67905922df0a298607ef7d52ce1af72d765eb1f3a7ed52a58c35b25ab1d':
        'Faxbot never received the document for this fax; ask the sender to send it again.',
    'de8d36b2410e1dd035fb78d977281e68d55839f4cd2bd10e80838581ef926bd9':
        'This older test fax has no document; add a new test fax instead.',
}
_PRIVATE_REPORT_KEYS = frozenset({'file', 'fileType', 'signature', 'authorization', 'password', 'secret',
                                  'token', 'api_key', 'api_secret', 'callback_token'})


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AcquisitionError(RuntimeError):
    """A plain sentence; never includes SQL, credentials, URLs or document bytes."""


class ImportNotFound(AcquisitionError):
    pass


class AlreadyReceived(AcquisitionError):
    pass


class InvalidDocument(AcquisitionError):
    pass


@dataclass(frozen=True)
class Begun:
    import_id: str
    inbound_fax_id: str
    state: str
    created: bool
    conflict: bool


@dataclass(frozen=True)
class Completion:
    stored: bool
    state: str
    inbound_fax_id: str
    kept_path: str | None


@dataclass(frozen=True)
class StoredArtifact:
    path: str
    digest: str
    size: int
    pages: int
    media_type: str = MEDIA_TYPE


def account_identity(source, value=None):
    """The documented non-secret account identity for a source (see module docstring)."""
    value = '' if value is None else str(value)
    if source in ('phaxio', 'efax'):
        return source + ':' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]
    if source == 'sip':
        return ('sip:' + (value.strip() or 'asterisk'))[:100]
    if source in ('sinch', 'import', 'test'):
        return (source + ':' + value.strip())[:100]
    raise ValueError('Unknown inbound source.')


def sanitize_report(report):
    """At most 8 KB of JSON: no files, signatures or credentials, longest values cut first."""
    if report is None:
        return None

    def clean(value, depth=0):
        if depth > 4:
            return None
        if isinstance(value, dict):
            return {str(key)[:64]: clean(item, depth + 1) for key, item in value.items()
                    if str(key) not in _PRIVATE_REPORT_KEYS and not str(key).lower().startswith('x-')}
        if isinstance(value, (list, tuple)):
            return [clean(item, depth + 1) for item in list(value)[:20]]
        if isinstance(value, (bytes, bytearray)):
            return None
        if isinstance(value, str):
            return value[:500]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:200]
    cleaned = clean(report)
    encoded = json.dumps(cleaned, sort_keys=True, separators=(',', ':'), ensure_ascii=True, default=str)
    if len(encoded) <= REPORT_LIMIT:
        return encoded
    shallow = {key: value for key, value in (cleaned.items() if isinstance(cleaned, dict) else [])
               if not isinstance(value, (dict, list)) and len(json.dumps(value, default=str)) <= 200}
    encoded = json.dumps({**shallow, 'shortened': True}, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True, default=str)
    return encoded if len(encoded) <= REPORT_LIMIT else json.dumps({'shortened': True})


def parse_source_time(value):
    """A provider-reported time as naive UTC, or None when there is none to read."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
        elif isinstance(value, str) and value.strip():
            text = value.strip()
            if text.isdigit():
                moment = datetime.fromtimestamp(int(text), tz=timezone.utc)
            else:
                moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    if not 2000 <= moment.year <= 2200:
        return None
    return moment.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _clock_text(moment):
    return moment.replace(tzinfo=timezone.utc).astimezone().strftime('%H:%M')


def describe(row, record, *, now=None):
    """The added InboundFaxOut fields for one fax row and its import (or None)."""
    if record is None:
        placeholder = PLACEHOLDER_DIGESTS.get(row.get('sha256'))
        if placeholder:
            return {'status': 'failed', 'status_text': placeholder, 'source_received_at': None,
                    'provider_fax_id': row.get('provider_sid'), 'sha256': None, 'is_test': False,
                    'retry_at': None, 'problem': None, 'can_fetch_again': False}
        return {'status_text': 'Received.', 'source_received_at': None,
                'provider_fax_id': row.get('provider_sid'), 'sha256': row.get('sha256'), 'is_test': False,
                'retry_at': None, 'problem': None, 'can_fetch_again': False}
    source, state = record['source'], record['state']
    name = SOURCE_NAMES.get(source, 'the provider')
    retry_at = None
    if state == 'received':
        text = 'A test fax created in Faxbot.' if source == 'test' else 'Received.'
    elif state == 'conflict':
        text = 'This fax arrived earlier and is kept as received.'
    elif state == 'failed':
        text = 'Faxbot stopped trying to fetch this document; select Fetch again.'
    elif record['last_error'] and record['next_attempt_at'] is not None and record['claim_token'] is None:
        retry_at = record['next_attempt_at']
        text = f'The document could not be fetched; Faxbot will try again at {_clock_text(retry_at)}.'
    else:
        text = f'Waiting for the document from {name}.'
    return {'status_text': text, 'source_received_at': record['source_received_at'],
            'provider_fax_id': record['operation_id'] if source in FETCHABLE else None,
            'sha256': row.get('sha256') if state in ('received', 'conflict') else None,
            'is_test': source == 'test', 'retry_at': retry_at,
            'problem': record['last_error'] if state != 'received' else None,
            'can_fetch_again': source in FETCHABLE and state in ('pending', 'failed'),
            'recovered': _recovered(record)}


def _recovered(record):
    """Whether Faxbot brought this fax in later from an image the fax engine could not hand over."""
    try:
        report = json.loads(record.get('report') or '{}')
    except (TypeError, ValueError):
        return False
    return isinstance(report, dict) and report.get('recovered') is True


def _settings():
    from ..config import settings
    return settings


def store_document(data, inbound_fax_id, *, provider='The provider'):
    """Validate PDF bytes and store them; raise InvalidDocument with a plain sentence.

    The file is written beside the data directory's other documents under a
    name that includes its digest, then renamed into place, so a retry never
    meets a half-written file and never replaces a different stored document.
    """
    from ..conversion import DocumentConversionError, ensure_dir, validate_pdf
    from ..storage import get_storage
    if not isinstance(data, (bytes, bytearray)) or not bytes(data[:5]).startswith(b'%PDF'):
        raise InvalidDocument(f'{provider} sent something that is not a PDF.')
    data = bytes(data)
    directory = _settings().fax_data_dir
    ensure_dir(directory)
    digest = hashlib.sha256(data).hexdigest()
    final = os.path.join(directory, f'{inbound_fax_id}-{digest[:12]}.pdf')
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix='.inbound-', suffix='.pdf')
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
        try:
            pages = validate_pdf(temporary)
        except DocumentConversionError:
            raise InvalidDocument(f'{provider} sent a PDF that Faxbot cannot read.') from None
        os.replace(temporary, final)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    stored = get_storage().put_pdf(final, os.path.basename(final))
    if stored.startswith('s3://'):
        try:
            os.remove(final)
        except OSError:
            pass
    return StoredArtifact(stored, digest, len(data), pages)


def convert_tiff(tiff_path, inbound_fax_id):
    """Convert a retained SIP TIFF to a stored PDF; the TIFF stays for a later retry."""
    from ..conversion import DocumentConversionError, tiff_to_pdf
    directory = _settings().fax_data_dir
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix='.inbound-', suffix='.pdf')
    os.close(descriptor)
    try:
        try:
            tiff_to_pdf(tiff_path, temporary)
        except DocumentConversionError:
            raise InvalidDocument('The received fax image could not be turned into a PDF.') from None
        with open(temporary, 'rb') as handle:
            data = handle.read()
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return store_document(data, inbound_fax_id, provider='The SIP trunk')


def discard(artifact, completion):
    """Remove a just-stored artifact that was not kept (a conflict or a duplicate)."""
    if completion.stored or artifact.path == completion.kept_path or artifact.path.startswith('s3://'):
        return
    try:
        os.remove(artifact.path)
    except OSError:
        pass


class ImportStore:
    """Begin, complete, schedule and lease acquisitions in the access transaction.

    ``begin`` must insert the fax, its import and its access resource together,
    so every write here uses the access store's serialized transaction. Network
    I/O never happens inside a transaction.
    """

    def __init__(self, resources, *, clock=None):
        self.resources = resources
        self.store = resources.store
        self.engine = resources.store.engine
        self.imports = resources.imports_table()
        if self.imports is None:
            raise AcquisitionError('Received-fax storage is not ready; upgrade the database.')
        self.faxes = resources.tables['inbound_faxes']
        self.clock = clock or utcnow

    # Reads --------------------------------------------------------------
    def get(self, import_id):
        with self.engine.connect() as connection:
            row = connection.execute(sa.select(self.imports).where(self.imports.c.id == import_id)).mappings().first()
            return dict(row) if row is not None else None

    def for_fax(self, inbound_fax_id, connection=None):
        query = (sa.select(self.imports).where(self.imports.c.inbound_fax_id == inbound_fax_id)
                 .order_by(self.imports.c.created_at, self.imports.c.id).limit(1))
        if connection is not None:
            row = connection.execute(query).mappings().first()
        else:
            with self.engine.connect() as conn:
                row = conn.execute(query).mappings().first()
        return dict(row) if row is not None else None

    # Begin --------------------------------------------------------------
    def begin(self, *, source, account, operation_id, revision='', backend, inbound_backend=None,
              to_number=None, from_number=None, reported_pages=None, report=None, source_received_at=None,
              tiff_path=None, artifact_digest=None, schedule=True, country=DEFAULT_COUNTRY):
        """Find or create the import for this source identity, in one transaction.

        An existing ``pending`` or ``failed`` import is scheduled again at once;
        a ``received`` one is returned unchanged unless ``artifact_digest`` names
        different content, which records a conflict and keeps the original.
        ``schedule=False`` means the caller holds the document and will call
        ``complete``; a safety fetch is still scheduled for a fetchable source.
        """
        if (source not in SOURCES or not isinstance(account, str) or not 0 < len(account) <= 100
                or not isinstance(operation_id, str) or not 0 < len(operation_id) <= 100
                or not isinstance(revision, str) or len(revision) > 40 or not isinstance(backend, str)):
            raise AcquisitionError('This notification does not name a fax Faxbot can record.')
        now = self.clock()
        report_text = sanitize_report(report)
        identity = sa.and_(self.imports.c.source == source, self.imports.c.account == account,
                           self.imports.c.operation_id == operation_id, self.imports.c.revision == revision)
        with self.store.transaction() as connection:
            existing = connection.execute(sa.select(self.imports).where(identity)).mappings().first()
            if existing is not None:
                return self._resume_on(connection, dict(existing), now, artifact_digest=artifact_digest,
                                       schedule=schedule)
            inbound_id, import_id = uuid4().hex, uuid4().hex
            due = None
            if source in FETCHABLE:
                due = now if schedule else now + DEFERRED_FETCH
            self.resources.insert_on(connection, dict(
                id=inbound_id, from_number=_number(from_number), to_number=_number(to_number), status='waiting',
                backend=backend[:20], inbound_backend=(inbound_backend or None) and inbound_backend[:20],
                provider_sid=operation_id if source in FETCHABLE else None, pages=reported_pages,
                tiff_path=tiff_path, created_at=now, received_at=now, updated_at=now), now, country=country)
            connection.execute(self.imports.insert().values(
                id=import_id, source=source, account=account, operation_id=operation_id, revision=revision,
                state='pending', attempts=0, next_attempt_at=due, imported_at=now,
                source_received_at=source_received_at, to_number=_number(to_number),
                from_number=_number(from_number), reported_pages=reported_pages, report=report_text,
                inbound_fax_id=inbound_id, created_at=now, updated_at=now))
            return Begun(import_id, inbound_id, 'pending', True, False)

    def _resume_on(self, connection, record, now, *, artifact_digest=None, schedule=True):
        imports = self.imports
        conflict = bool(artifact_digest and record['artifact_digest'] and artifact_digest != record['artifact_digest'])
        if record['state'] in ('received', 'conflict'):
            if conflict and record['state'] == 'received':
                self._conflict_on(connection, record, now, offered=artifact_digest)
                return Begun(record['id'], record['inbound_fax_id'], 'conflict', False, True)
            return Begun(record['id'], record['inbound_fax_id'], record['state'], False, conflict)
        due = now if schedule else now + DEFERRED_FETCH
        if record['source'] not in FETCHABLE:
            due = None
        if record['state'] == 'failed':
            connection.execute(imports.update().where(imports.c.id == record['id']).values(
                state='pending', attempts=0, next_attempt_at=due, claim_token=None, claim_expires_at=None,
                last_error=None, updated_at=now))
            connection.execute(self.faxes.update().where(self.faxes.c.id == record['inbound_fax_id']).values(
                status='waiting', updated_at=now))
        elif record['claim_token'] is None:
            connection.execute(imports.update().where(imports.c.id == record['id']).values(
                next_attempt_at=due, updated_at=now))
        return Begun(record['id'], record['inbound_fax_id'], 'pending', False, False)

    def _conflict_on(self, connection, record, now, *, offered=None):
        connection.execute(self.imports.update().where(self.imports.c.id == record['id']).values(
            state='conflict', last_error='A later copy with different content arrived; the first copy is kept.',
            updated_at=now))
        # Both digests are evidence of what was kept and what was refused.
        _audit('inbound_conflict', job_id=record['inbound_fax_id'], backend=record['source'],
               kept_sha256=record['artifact_digest'], refused_sha256=offered)

    # Complete -----------------------------------------------------------
    def complete(self, import_id, *, artifact_path, digest, size, pages, media_type=MEDIA_TYPE,
                 source_received_at=None):
        """Mark the import and its fax received with the stored artifact, in one transaction."""
        if (not isinstance(artifact_path, str) or not artifact_path or not isinstance(digest, str)
                or len(digest) != 64 or type(size) is not int or size < 0
                or (pages is not None and (type(pages) is not int or pages < 0))):
            raise AcquisitionError('The stored document is not described completely.')
        now = self.clock()
        values = _settings()
        ttl = max(1, int(values.inbound_token_ttl_minutes))
        retention = int(values.inbound_retention_days)
        with self.store.transaction() as connection:
            record = connection.execute(sa.select(self.imports).where(self.imports.c.id == import_id)).mappings().first()
            if record is None:
                raise ImportNotFound('This received fax no longer exists.')
            record = dict(record)
            if record['state'] in ('received', 'conflict'):
                kept = connection.execute(sa.select(self.faxes.c.pdf_path).where(
                    self.faxes.c.id == record['inbound_fax_id'])).scalar_one_or_none()
                if record['state'] == 'received' and record['artifact_digest'] != digest:
                    self._conflict_on(connection, record, now, offered=digest)
                    return Completion(False, 'conflict', record['inbound_fax_id'], kept)
                return Completion(False, record['state'], record['inbound_fax_id'], kept)
            connection.execute(self.imports.update().where(self.imports.c.id == import_id).values(
                state='received', acquired_at=now, artifact_digest=digest, artifact_size=size,
                artifact_media_type=media_type[:64], claim_token=None, claim_expires_at=None, next_attempt_at=None,
                last_error=None, source_received_at=record['source_received_at'] or source_received_at,
                updated_at=now))
            connection.execute(self.faxes.update().where(self.faxes.c.id == record['inbound_fax_id']).values(
                status='received', pdf_path=artifact_path, sha256=digest, size_bytes=size, pages=pages,
                pdf_token=secrets.token_urlsafe(32), pdf_token_expires_at=now + timedelta(minutes=ttl),
                retention_until=now + timedelta(days=retention) if retention > 0 else None, updated_at=now))
        _audit('inbound_received', job_id=record['inbound_fax_id'], backend=record['source'])
        return Completion(True, 'received', record['inbound_fax_id'], artifact_path)

    # Failure and scheduling --------------------------------------------------
    def fail(self, import_id, message, *, retry_at=None, claim_token=None):
        """Record a failed attempt; retry on the schedule, then stop for a person."""
        now = self.clock()
        message = _sentence(message)
        with self.store.transaction() as connection:
            record = connection.execute(sa.select(self.imports).where(self.imports.c.id == import_id)).mappings().first()
            if record is None or record['state'] != 'pending':
                return record['state'] if record is not None else None
            if claim_token is not None and record['claim_token'] != claim_token:
                return 'pending'  # Another worker holds the current lease.
            if retry_at is None:
                if record['attempts'] >= len(RETRY_MINUTES):
                    self._abandon_on(connection, dict(record), message, now)
                    return 'failed'
                retry_at = now + timedelta(minutes=RETRY_MINUTES[max(0, record['attempts'] - 1)])
            connection.execute(self.imports.update().where(self.imports.c.id == import_id).values(
                claim_token=None, claim_expires_at=None, next_attempt_at=retry_at, last_error=message,
                updated_at=now))
            return 'pending'

    def abandon(self, import_id, message):
        now = self.clock()
        with self.store.transaction() as connection:
            record = connection.execute(sa.select(self.imports).where(self.imports.c.id == import_id)).mappings().first()
            if record is None or record['state'] != 'pending':
                return record['state'] if record is not None else None
            self._abandon_on(connection, dict(record), _sentence(message), now)
            return 'failed'

    def _abandon_on(self, connection, record, message, now):
        connection.execute(self.imports.update().where(self.imports.c.id == record['id']).values(
            state='failed', claim_token=None, claim_expires_at=None, next_attempt_at=None, last_error=message,
            updated_at=now))
        connection.execute(self.faxes.update().where(self.faxes.c.id == record['inbound_fax_id']).values(
            status='failed', updated_at=now))
        _audit('inbound_not_received', job_id=record['inbound_fax_id'], backend=record['source'])

    def resume_for_fax(self, inbound_fax_id):
        """A person asks to fetch again: schedule an immediate attempt."""
        now = self.clock()
        with self.store.transaction() as connection:
            record = self.for_fax(inbound_fax_id, connection)
            if record is None or record['source'] not in FETCHABLE:
                raise ImportNotFound('Faxbot has nothing to fetch for this fax.')
            if record['state'] in ('received', 'conflict'):
                raise AlreadyReceived('This fax has already been received.')
            self._resume_on(connection, record, now)
        _audit('inbound_fetch_requested', job_id=inbound_fax_id, backend=record['source'])
        return record['id']

    # Leases ---------------------------------------------------------------
    def _due(self, now):
        return sa.and_(self.imports.c.state == 'pending', self.imports.c.source.in_(FETCHABLE),
                       self.imports.c.claim_token.is_(None), self.imports.c.next_attempt_at.is_not(None),
                       self.imports.c.next_attempt_at <= now)

    def _expired(self, now):
        return sa.and_(self.imports.c.state == 'pending', self.imports.c.claim_token.is_not(None),
                       self.imports.c.claim_expires_at <= now)

    def has_work(self):
        """Read-only check, so an idle worker never takes the write lock."""
        now = self.clock()
        with self.engine.connect() as connection:
            return connection.execute(sa.select(self.imports.c.id).where(
                sa.or_(self._due(now), self._expired(now))).limit(1)).first() is not None

    def recover_expired(self):
        """A worker stopped mid-fetch; fetching by fax ID is safe to repeat."""
        now = self.clock()
        with self.engine.connect() as connection:
            if connection.execute(sa.select(self.imports.c.id).where(self._expired(now)).limit(1)).first() is None:
                return 0
        with self.store.transaction() as connection:
            return connection.execute(self.imports.update().where(self._expired(now)).values(
                claim_token=None, claim_expires_at=None, next_attempt_at=now, updated_at=now)).rowcount

    def claim(self):
        """Lease the import due longest; returns its fields with the fax's TIFF path, or None."""
        now = self.clock()
        with self.engine.connect() as connection:
            if connection.execute(sa.select(self.imports.c.id).where(self._due(now)).limit(1)).first() is None:
                return None
        with self.store.transaction() as connection:
            record = connection.execute(sa.select(self.imports).where(self._due(now)).order_by(
                self.imports.c.next_attempt_at, self.imports.c.id).limit(1)).mappings().first()
            if record is None:
                return None
            token = uuid4().hex
            connection.execute(self.imports.update().where(self.imports.c.id == record['id']).values(
                claim_token=token, claim_expires_at=now + LEASE, attempts=record['attempts'] + 1,
                next_attempt_at=None, updated_at=now))
            tiff_path = connection.execute(sa.select(self.faxes.c.tiff_path).where(
                self.faxes.c.id == record['inbound_fax_id'])).scalar_one_or_none()
            return {**dict(record), 'claim_token': token, 'attempts': record['attempts'] + 1, 'tiff_path': tiff_path}


def _number(value):
    if value is None:
        return None
    text = str(value).strip()
    return text[:64] or None


def _sentence(message):
    text = ' '.join(str(message or '').split())
    return (text or 'Faxbot could not fetch the document.')[:200]


def _audit(event, **fields):
    try:
        from ..audit import audit_event
        audit_event(event, **fields)
    except Exception:
        pass
