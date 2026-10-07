"""Send one email or file as one fax, once.

The connector's item for a source identity is the first guard: an identity
already recorded is a duplicate and nothing is sent. The second is ordinary fax
acceptance with an idempotency key derived from the identity, under the
connector's own key: a replay within one key returns the fax already accepted.
The item is written inside that acceptance transaction (``also``), together
with the check that the person who asked may send faxes, so a fax and its
record commit together or not at all; a key replaced on resume therefore
cannot lead to a second send. An uncertain acceptance is left for the next
check, which finds the item if the fax was accepted.
"""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import os
from uuid import uuid4

from ...access.fax_resources import FaxAccessError
from ...access.types import AccessError
from ...request_identity import IdempotencyConflict, IdempotentReplay, RequestIdentity, request_fingerprints
from . import documents, keys, text


class Retry(RuntimeError):
    """Nothing was recorded; the next check tries the same message or file again."""


class PersonRefused(RuntimeError):
    pass


@dataclass
class Submission:
    operation_id: str
    to_number: str
    documents: list                   # mail.Attachment-like: name, kind, data, digest
    part: str = ''
    reference: str | None = None
    subject: str | None = None
    sender: str | None = None
    person_id: str | None = None
    person_name: str | None = None
    received_at: datetime | None = None
    reply_to: str | None = None
    report: dict = field(default_factory=dict)


def idempotency_key(source_id, operation_id, part):
    return 'intake-' + hashlib.sha256(f'{source_id}\n{operation_id}\n{part}'.encode()).hexdigest()


def _item_fields(submission, **extra):
    return dict(reference=submission.reference, subject=submission.subject, sender=submission.sender,
                sender_principal_id=submission.person_id, sender_name=submission.person_name,
                source_received_at=submission.received_at, to_number=submission.to_number, **extra)


def submit(access, runtime, store, source, submission, token):
    """Returns (item, created). Raises Retry when nothing could be decided now."""
    existing = store.item(source.id, submission.operation_id, submission.part)
    if existing is not None:
        store.seen_again(existing)
        return existing, False
    revision = runtime.manager.store.read().active
    values = revision.values
    profile_id = revision.profile_id('outbound')
    if profile_id is None:
        reason = text.NO_SENDING_PROVIDER if not values.effective_outbound else text.SENDING_OFF
        return _refuse(store, source, submission, reason, state='failed')
    configuration = runtime.manager.store.read_profile(profile_id).configuration
    if (configuration.manifest is None and configuration.provider_id == 'sip' and not values.fax_disabled):
        from ...ami import ami_client
        if not ami_client._connected.is_set():
            raise Retry(ami_client.engine_message() or 'The fax engine is not connected.')
    directory = values.fax_data_dir
    prepared, digests = [], []
    try:
        for document in submission.documents:
            try:
                path, _, _, _ = documents.to_pdf(document.data, document.kind, directory)
            except documents.Unreadable:
                return _refuse(store, source, submission, text.UNREADABLE_SEND.format(name=document.name),
                               state='refused')
            prepared.append(path)
            digests.append(document.digest)
        combined, pages = documents.join(prepared, directory)
    finally:
        documents.discard(*prepared)
    source_digest = documents.source_digest(digests)
    try:
        actor = keys.connector_actor(access, token)
    except AccessError:
        documents.discard(combined)
        raise Retry(text.KEY_REVOKED) from None
    except Exception:
        documents.discard(combined)
        raise Retry(text.MAIL_SERVER_ERROR) from None
    fingerprint, legacy = request_fingerprints(entered=submission.to_number, destination=submission.to_number,
                                               queue_only=False, document_sha256=source_digest)
    identity = RequestIdentity.from_key(idempotency_key(source.id, submission.operation_id, submission.part),
                                        principal_scope=actor.replay_scope, fingerprint=fingerprint,
                                        legacy_fingerprints=legacy)
    job_id = uuid4().hex
    pdf, tiff = os.path.join(directory, job_id + '.pdf'), ''
    try:
        os.replace(combined, pdf)
        os.chmod(pdf, 0o600)
        if ((configuration.manifest is None and configuration.provider_id in {'sip', 'freeswitch'})
                or configuration.traits.get('requires_tiff') is True):
            from ...conversion import FAX_IMAGE_MODE, pdf_to_tiff
            tiff = os.path.join(directory, job_id + '.tiff')
            pdf_to_tiff(pdf, tiff)
            os.chmod(tiff, FAX_IMAGE_MODE)
    except Exception:
        documents.discard(combined, pdf, tiff)
        return _refuse(store, source, submission, text.CONVERT_FAILED, state='failed')
    reply = bool(submission.reply_to)

    def also(connection, now):
        if submission.person_id is not None and not keys.may_send_on(access.control, connection,
                                                                     submission.person_id):
            raise PersonRefused()
        store.link_on(connection, source.id, submission.operation_id, submission.part, **_item_fields(
            submission, state='sent', fax_job_id=job_id, document_digest=source_digest,
            submitted_digest=_file_digest(pdf), reply_to=submission.reply_to,
            reply_state='waiting' if reply else 'none', reply_kind='result' if reply else None))

    names = [document.name for document in submission.documents]
    file_name = names[0] if len(names) == 1 else f'{names[0]} and {len(names) - 1} more'
    now = datetime.utcnow()
    job = {'id': job_id, 'to_number': submission.to_number, 'file_name': file_name[:255], 'tiff_path': tiff,
           'status': 'queued', 'pages': pages, 'created_at': now, 'updated_at': now}
    try:
        access.outbound.accept(actor, revision, job, request_identity=identity, also=also)
    except IdempotentReplay as replay:
        documents.discard(pdf, tiff)
        item = store.item(source.id, submission.operation_id, submission.part)
        if item is None:
            item, _ = store.record(source.id, 'send', submission.operation_id, submission.part, **_item_fields(
                submission, state='sent', fax_job_id=replay.job_id, document_digest=source_digest,
                reply_to=submission.reply_to, reply_state='waiting' if reply else 'none',
                reply_kind='result' if reply else None))
            return item, True
        store.seen_again(item)
        return item, False
    except IdempotencyConflict:
        documents.discard(pdf, tiff)
        return _refuse(store, source, submission, text.CONFLICT_SEND, state='conflict')
    except PersonRefused:
        documents.discard(pdf, tiff)
        return _refuse(store, source, submission,
                       text.NO_SEND_PERMISSION.format(person=submission.person_name or submission.sender),
                       state='refused', reply_reason=text.REPLY_NO_PERMISSION)
    except FaxAccessError:
        documents.discard(pdf, tiff)
        raise Retry(text.KEY_REVOKED) from None
    except Exception:
        # Includes an acceptance whose commit is uncertain: the next check finds its item if it was accepted.
        documents.discard(pdf, tiff)
        raise Retry(text.MAIL_SERVER_ERROR) from None
    return store.item(source.id, submission.operation_id, submission.part), True


def _file_digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _refuse(store, source, submission, reason, *, state, reply_reason=None):
    """Record a refusal or failure once; an email sender whose address was confirmed gets one reply."""
    reply = bool(submission.reply_to)
    item, created = store.record(source.id, 'send', submission.operation_id, submission.part, **_item_fields(
        submission, state=state, reason=reason, reply_to=submission.reply_to if reply else None,
        reply_state='due' if reply else 'none', reply_kind='refused' if reply else None,
        reply_note=reply_reason))
    if not created:
        store.seen_again(item)
    return item, created
