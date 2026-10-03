"""Accept a Faxbot-generated PDF as an ordinary outbound fax for the requesting person.

Used for challenge faxes and case packets. Acceptance goes through the same
authorized, transactional path as POST /fax: the person must be allowed to
send, the fax is bound to the active provider, and it appears in their faxes.
"""
from datetime import datetime
import os
from uuid import uuid4


def accept_generated_fax(runtime, access, actor, revision, *, to_number, document, file_name, pages):
    """Write the document beside other fax artifacts and accept it; returns the fax id."""
    profile_id = revision.profile_id('outbound')
    if profile_id is None:
        raise RuntimeError('Outbound fax delivery is disabled in this configuration.')
    configuration = runtime.manager.store.read_profile(profile_id).configuration
    root = revision.values.fax_data_dir
    job_id = uuid4().hex
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
        access.outbound.accept(actor, revision, {
            'id': job_id, 'to_number': to_number, 'file_name': file_name, 'tiff_path': tiff, 'status': 'queued',
            'pages': pages, 'created_at': now, 'updated_at': now})
    except BaseException:
        for path in (pdf, tiff):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        raise
    return job_id
