"""A court fax at upload and at acceptance: the case details checked, the MC-005 cover added, the route narrowed.

At upload (``POST /fax``, ``plan`` and ``apply``):

- the sender gives the case details (``cover.Details``), or the fax comes from a mailbox the profile is attached to,
  which then needs them (refused before anything is accepted, with the sentence to fix it);
- for filing, each listed document's first page must say "By fax" (rule 2.304(c)): found in its text, or confirmed
  by the sender for a scan Faxbot can't read; the listed pages must add up to the document's pages;
- for filing, the filled MC-005 goes first; the sender's own PDF is kept beside the fax unchanged;
- the header notice is never printed on a court fax, and it never goes in a shared call.

At acceptance (``narrow``): the route envelope keeps only fax calls by your sending accounts to the number given,
pages one per sheet: no delivery inside Faxbot, no direct delivery or partner relay (the rules need a fax
transmission and the sending machine's record), and no approved alternate number (service goes to the number last
given, rule 2.306(a)(2)). A routing rule that allows only direct delivery blocks the fax, which waits in Sent.

At each attempt (``pages/sending.py``) the pages go exactly as accepted: nothing packed, shaded, encoded, trimmed or
kept at another resolution.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path

from . import cover as covers
from .store import attached, attached_on

NEEDS_DETAILS = ('Faxes from this mailbox go under the California court profile, so give the case details: the '
                 'court, the case number, the parties and each document\'s title and pages.')
NEEDS_DETAILS_RULE = ('Your routing rule "{rule}" sends this fax under the California court profile, so give the case '
                      'details: the court, the case number, the parties and each document\'s title and pages.')


class CourtRefused(ValueError):
    """The fax can't be accepted under the court profile as given; one sentence that says what to fix."""


@dataclass(frozen=True)
class Plan:
    details: covers.Details
    source: str            # 'sender', 'mailbox' or 'rule'
    by_fax: str            # 'found', 'confirmed' or 'not_required'
    cover: bytes | None    # the filled MC-005 (filing only)
    original_pages: int

    @property
    def cover_pages(self):
        return 1 if self.cover is not None else 0

    @property
    def sent_pages(self):
        return self.original_pages + self.cover_pages


def _starts(details):
    """The first page of each listed document, counted from 1, after any processing instruction pages."""
    first, found = 1 + details.processing_pages, []
    for document in details.documents:
        found.append(first)
        first += document.pages
    return found


def _by_fax(pdf_path, details):
    """'found' when every listed document's first page says "By fax"; else 'confirmed', or a refusal."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    unread = []
    missing = []
    try:
        reader = PdfReader(str(pdf_path))
        pages = reader.pages
        for document, start in zip(details.documents, _starts(details)):
            with covers.quiet_pypdf():
                text = pages[start - 1].extract_text() or '' if start <= len(pages) else ''
            if not text.strip():
                unread.append(document.title)
            elif covers.BY_FAX.search(' '.join(text.split())) is None:
                missing.append(document.title)
    except (PdfReadError, OSError, ValueError, KeyError):
        unread = [document.title for document in details.documents]
    if not missing and not unread:
        return 'found'
    if details.by_fax_confirmed:
        return 'confirmed'
    if missing:
        raise CourtRefused(f'Add "By fax" immediately below the title of "{missing[0]}" (California Rules of Court, '
                           'rule 2.304(c)), then send it again.')
    raise CourtRefused(f'Faxbot can\'t read the text of "{unread[0]}" to check that "By fax" is immediately below its '
                       'title (rule 2.304(c)). If it is, confirm it and send again.')


def required_by_mailbox(engine, mailbox):
    return bool(mailbox) and attached(engine, 'mailbox', mailbox)


def plan(engine, text, *, mailbox, pdf_path, pages):
    """The court ``Plan`` for an upload, or None when the fax is not a court fax. Raises ``CourtRefused``."""
    by_mailbox = required_by_mailbox(engine, mailbox)
    if text is None or not str(text).strip():
        if by_mailbox:
            raise CourtRefused(NEEDS_DETAILS)
        return None
    try:
        details = covers.parse(text)
    except covers.DetailsError as error:
        raise CourtRefused(str(error)) from None
    listed = sum(item.pages for item in details.documents) + details.processing_pages
    if listed != pages:
        raise CourtRefused(f'The document has {pages} page{"s" if pages != 1 else ""}, but the documents you listed '
                           f'add up to {listed}. List each document\'s pages so the cover sheet matches what is sent.')
    if details.purpose == 'file':
        by_fax = _by_fax(pdf_path, details)
        try:
            drawn = covers.fill(details)
        except covers.DetailsError as error:
            raise CourtRefused(str(error)) from None
    else:
        by_fax, drawn = 'not_required', None
    return Plan(details, 'mailbox' if by_mailbox else 'sender', by_fax, drawn, pages)


def required_by_rule(engine, decision):
    """The name of the routing rule that chose this fax's route when the profile is attached to it, else None."""
    route = getattr(decision, 'route', None)
    rule_id = getattr(route, 'rule_id', None)
    if rule_id and attached(engine, 'rule', rule_id):
        return getattr(route, 'rule_name', None) or rule_id
    return None


def apply(prepared, court):
    """The prepared upload with the MC-005 cover first (filing); the sender's own PDF stays beside the fax as
    ``<fax>.upload.pdf``. Service (no cover) returns ``prepared`` as it is. Raises ``CourtRefused`` when the cover
    can't be added, so a filing never goes without it."""
    if court.cover is None:
        return prepared
    from pypdf import PdfReader, PdfWriter
    from ..conversion import FAX_IMAGE_MODE, DocumentConversionError, pdf_to_tiff, validate_pdf
    from ..header_notice import NoticedDocument, upload_path
    import io
    pdf = Path(prepared.pdf_path)
    kept = upload_path(pdf)
    staged_pdf = pdf.with_name(f'.court-{pdf.name}')
    staged_tiff = None
    try:
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(court.cover)))
        writer.append(PdfReader(str(pdf)))
        with open(staged_pdf, 'wb') as handle:
            writer.write(handle)
        staged_pdf.chmod(0o600)
        if validate_pdf(str(staged_pdf)) != court.sent_pages:
            raise CourtRefused('The cover sheet could not be added to this document.')
        if prepared.tiff_path:
            staged_tiff = Path(prepared.tiff_path).with_name(f'.court-{Path(prepared.tiff_path).name}')
            pdf_to_tiff(str(staged_pdf), str(staged_tiff))
            os.chmod(staged_tiff, FAX_IMAGE_MODE)
        os.link(pdf, kept)
        os.replace(staged_pdf, pdf)
        if staged_tiff is not None:
            os.replace(staged_tiff, prepared.tiff_path)
    except (OSError, ValueError, DocumentConversionError) as error:
        for path in (staged_pdf, staged_tiff):
            if path is not None:
                Path(path).unlink(missing_ok=True)
        if isinstance(error, CourtRefused):
            raise
        raise CourtRefused('Faxbot could not add the MC-005 cover sheet to this document, so it was not accepted. '
                           'Try again in a moment.') from None
    return NoticedDocument(original_name=prepared.original_name, pdf_path=prepared.pdf_path,
                           tiff_path=prepared.tiff_path, pages=court.sent_pages, upload_path=str(kept))


def recorder(job_id, court, source=None):
    from .store import recorder as store_recorder
    return store_recorder(job_id, purpose=court.details.purpose, source=source or court.source,
                          details=court.details.public(), cover_pages=court.cover_pages, by_fax=court.by_fax,
                          original_pages=court.original_pages, sent_pages=court.sent_pages)


# The route --------------------------------------------------------------------------------------------------------

def narrow(decision, accounts):
    """The sending rules' decision for a court fax: fax calls only, by your sending accounts, to the number given.

    An automatic route becomes the cheapest of your sending accounts that are on; a rule's own accounts stay. A rule
    that allows only direct delivery blocks the fax (it waits in Sent); a blocked or held decision keeps its holds.
    """
    envelope = decision.envelope
    plain = dict(local=False, direct=False, alternate='never', dial=None, page_layout='one_per_sheet')
    if envelope.require_direct:
        return replace(decision, outcome='blocked', reason='no_allowed_account',
                       envelope=replace(envelope, require_direct=False, accounts=(), mode='one', **plain))
    if envelope.mode == 'automatic':
        keys = tuple(account.key for account in accounts if getattr(account, 'enabled', True))
        envelope = replace(envelope, mode='cheapest', accounts=keys)
    if decision.outcome == 'route' and not envelope.accounts:
        return replace(decision, outcome='blocked', reason='no_allowed_account',
                       envelope=replace(envelope, **plain))
    return replace(decision, envelope=replace(envelope, **plain))


def is_court_fax_on(connection, job_id):
    from .store import send_on
    return send_on(connection, job_id) is not None


__all__ = ['CourtRefused', 'Plan', 'apply', 'attached_on', 'narrow', 'plan', 'recorder', 'required_by_mailbox',
           'required_by_rule']
