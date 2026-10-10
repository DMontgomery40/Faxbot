"""Accept a Faxbot-generated PDF as an ordinary outbound fax for the requesting person.

Used for challenge faxes, case packets and forms. Acceptance goes through the
same authorized, transactional path as POST /fax: the person must be allowed to
send, the fax is bound to the active provider, and it appears in their faxes.
"""
from datetime import datetime
import logging
import os
import time
from uuid import uuid4

import sqlalchemy as sa


# A document file older than this with no fax behind it was left by an acceptance that never finished (the
# process stopped between writing it and committing the fax), so it may be replaced. An acceptance in progress
# finishes in seconds: the configuration lock waits at most 10.
ORPHAN_SECONDS = 300


class GeneratedFaxBusy(RuntimeError):
    """Another request is queuing a fax under this ID right now; one plain sentence."""


def _accepted(runtime, job_id):
    store = runtime.manager.store
    with store.engine.connect() as connection:
        return connection.execute(sa.select(store.jobs.c.id).where(store.jobs.c.id == job_id)).first() is not None


def _create(path):
    """Open ``path`` for a new document, replacing only a file no fax owns that an unfinished acceptance left."""
    try:
        return open(path, 'xb')
    except FileExistsError:
        try:
            orphan = time.time() - os.stat(path).st_mtime > ORPHAN_SECONDS
        except OSError:
            orphan = False
        if not orphan:
            raise
        os.unlink(path)
        return open(path, 'xb')


log = logging.getLogger(__name__)


def first_send_warning(engine, values, number, name=None):
    """Before accepting a fax: what NPPES records Faxbot already read say about a number never faxed before.

    Stored reads only (``nppes.check_before_sending``), with the recipient's saved name when the fax names none;
    never the registry, so it adds no wait. Returns the answer to record, or None. Storage that can't be read
    leaves the fax unchecked: logged, never a block.
    """
    from .database import DeliveryStoreError
    from .nppes import check_before_sending
    try:
        return check_before_sending(engine, values, number, name)
    except DeliveryStoreError as error:
        log.warning('The check before a first fax could not read stored NPPES records: %s', error)
        return None


def record_first_send_warning(engine, job_id, answer):
    """Keep ``first_send_warning``'s answer with the accepted fax, for Sent details; never a block."""
    if not answer or not answer.get('sentence'):
        return
    from .database import DeliveryStoreError
    from .nppes import NppesStore
    try:
        NppesStore(engine).record_warning(job_id, answer, datetime.utcnow().replace(microsecond=0))
    except DeliveryStoreError as error:
        log.warning('The NPPES warning for fax %s could not be kept: %s', job_id, error)


def accept_generated_fax(runtime, access, actor, revision, *, to_number, document, file_name, pages, job_id=None,
                         case_packet=False, after=None):
    """Write the document beside other fax artifacts and accept it; returns the fax id.

    ``job_id`` (32 hex characters) lets a caller name the fax in advance, so it
    can later tell for certain whether the fax was queued. Naming a fax that is
    already queued returns its ID and changes nothing: no file is written or
    removed and no second fax is made. The organization's sending rules decide
    its route envelope exactly as for POST /fax (``routing.rules_acceptance``);
    ``case_packet`` marks a case packet for them. ``after(connection, now, job_id)`` runs last in the same
    acceptance transaction (a route test pins its account there, ``route_families_http``); raising refuses the fax.
    """
    profile_id = revision.profile_id('outbound')
    if profile_id is None:
        raise RuntimeError('Outbound fax delivery is disabled in this configuration.')
    if job_id is not None and _accepted(runtime, job_id):
        return job_id
    configuration = runtime.manager.store.read_profile(profile_id).configuration
    warning = first_send_warning(runtime.manager.store.engine, revision.values, to_number)
    root = revision.values.fax_data_dir
    job_id = job_id or uuid4().hex
    pdf = os.path.join(root, job_id + '.pdf')
    tiff = ''
    written = []  # only the files this call made are removed when it fails
    try:
        try:
            handle = _create(pdf)
        except FileExistsError:
            # Another request named this fax first: it is queued already, or is being queued now.
            if _accepted(runtime, job_id):
                return job_id
            raise GeneratedFaxBusy('This fax is being queued by another request right now. Reload in a moment to '
                                   'see it.') from None
        written.append(pdf)
        with handle:
            handle.write(document)
        if ((configuration.manifest is None and configuration.provider_id in {'sip', 'freeswitch'})
                or configuration.traits.get('requires_tiff') is True):
            from ..conversion import pdf_to_tiff
            tiff = os.path.join(root, job_id + '.tiff')
            written.append(tiff)
            pdf_to_tiff(pdf, tiff)
        now = datetime.utcnow()
        from .rules_acceptance import recorder_for
        rules = recorder_for(runtime.manager.store.engine, revision, actor, job_id=job_id, destination=to_number,
                             pages=pages, document_path=pdf, case_packet=case_packet,
                             control=getattr(access, 'control', None))
        also = rules if after is None else (lambda connection, at: (rules(connection, at), after(connection, at, job_id)))
        access.outbound.accept(actor, revision, {
            'id': job_id, 'to_number': to_number, 'file_name': file_name, 'tiff_path': tiff, 'status': 'queued',
            'pages': pages, 'created_at': now, 'updated_at': now}, also=also)
        record_first_send_warning(runtime.manager.store.engine, job_id, warning)
    except BaseException:
        for path in written:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    return job_id
