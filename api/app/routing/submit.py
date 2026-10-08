"""Accept a Faxbot-generated PDF as an ordinary outbound fax for the requesting person.

Used for challenge faxes and case packets. Acceptance goes through the same
authorized, transactional path as POST /fax: the person must be allowed to
send, the fax is bound to the active provider, and it appears in their faxes.
"""
from datetime import datetime
import logging
import os
from uuid import uuid4


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
                         case_packet=False):
    """Write the document beside other fax artifacts and accept it; returns the fax id.

    ``job_id`` (32 hex characters) lets a caller name the fax in advance, so it
    can later tell for certain whether the fax was queued. The organization's
    sending rules decide its route envelope exactly as for POST /fax
    (``routing.rules_acceptance``); ``case_packet`` marks a case packet for them.
    """
    profile_id = revision.profile_id('outbound')
    if profile_id is None:
        raise RuntimeError('Outbound fax delivery is disabled in this configuration.')
    configuration = runtime.manager.store.read_profile(profile_id).configuration
    warning = first_send_warning(runtime.manager.store.engine, revision.values, to_number)
    root = revision.values.fax_data_dir
    job_id = job_id or uuid4().hex
    pdf = os.path.join(root, job_id + '.pdf')
    tiff = ''
    try:
        with open(pdf, 'xb') as handle:
            handle.write(document)
        if ((configuration.manifest is None and configuration.provider_id in {'sip', 'freeswitch'})
                or configuration.traits.get('requires_tiff') is True):
            from ..conversion import pdf_to_tiff
            tiff = os.path.join(root, job_id + '.tiff')
            pdf_to_tiff(pdf, tiff)
        now = datetime.utcnow()
        from .rules_acceptance import recorder_for
        rules = recorder_for(runtime.manager.store.engine, revision, actor, job_id=job_id, destination=to_number,
                             pages=pages, document_path=pdf, case_packet=case_packet,
                             control=getattr(access, 'control', None))
        access.outbound.accept(actor, revision, {
            'id': job_id, 'to_number': to_number, 'file_name': file_name, 'tiff_path': tiff, 'status': 'queued',
            'pages': pages, 'created_at': now, 'updated_at': now}, also=rules)
        record_first_send_warning(runtime.manager.store.engine, job_id, warning)
    except BaseException:
        for path in (pdf, tiff):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        raise
    return job_id
