"""The case details a sender gives for a court fax, and the MC-005 cover sheet filled from them.

MC-005 (Facsimile Transmission Cover Sheet (Fax Filing), Rev. January 1, 2007) is the Judicial Council's form,
adopted for mandatory use by rule 2.304(b). Faxbot fills the official PDF (``mc005.pdf``, read 2026-10-10 from
courts.ca.gov) field by field and flattens it, so the cover is the form itself, not a look-alike. The form's own
Save, Print and Clear buttons and its on-screen privacy notice are left off the faxed page.

Faxbot never takes a payment card: the form's card fields stay empty. A filing fee goes on the court account the
attorney or party set up with the court (rule 2.304(f)), whose number you give, or you arrange it with the court.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import io
import logging
from pathlib import Path
import re

FORM = Path(__file__).with_name('mc005.pdf')
FORM_SHA256 = '2cb857f59695b3d26d8a0d841929fdb07f121a20bc1dbe4e0288800a77b9b5a1'
PURPOSES = ('file', 'serve')
MAX_DOCUMENTS = 15  # the form lists fifteen documents
# The form's fields, by where they sit on the page (the PDF names them FillText33 ... FillText84).
ATTORNEY_LINES = ('FillText74', 'FillText75', 'FillText76', 'FillText77')
FIELDS = {'telephone': 'FillText70', 'fax': 'FillText71', 'email': 'FillText72', 'attorney_for': 'FillText73',
          'county': 'FillText64', 'street': 'FillText63', 'mailing': 'FillText65', 'city_zip': 'FillText66',
          'branch': 'FillText67', 'plaintiff': 'FillText68', 'defendant': 'FillText69', 'case_number': 'FillText78',
          'processing_pages': 'FillText81', 'fee_account': 'FillText84'}
DOCUMENT_ROWS = tuple((f'FillText{62 - 2 * row}', f'FillText{61 - 2 * row}') for row in range(MAX_DOCUMENTS))
CHECKS = {'processing': 'CheckBox4', 'fee_required': 'CheckBox5', 'filing_fee': 'CheckBox3', 'fax_fee': 'CheckBx3',
          'fee_account': 'CheckBox8'}
_TEXT = re.compile(r'[^\x00-\x1f\x7f]*')
_NUMBER = re.compile(r'\+?[0-9 ()./-]{7,24}')


class DetailsError(ValueError):
    """The case details cannot make a cover sheet or a court fax; one sentence that says what to fix."""


@dataclass(frozen=True)
class Document:
    title: str
    pages: int


@dataclass(frozen=True)
class Details:
    """What the sender gives for one court fax. ``purpose``: 'file' (direct fax filing, rule 2.304) or 'serve'
    (service on a party, rule 2.306)."""
    purpose: str
    county: str
    case_number: str
    plaintiff: str
    defendant: str
    documents: tuple[Document, ...]
    sender: tuple[str, ...] = ()        # attorney or party: name, State Bar number and address, up to four lines
    telephone: str = ''
    fax: str = ''
    email: str = ''
    attorney_for: str = ''
    street: str = ''
    mailing: str = ''
    city_zip: str = ''
    branch: str = ''
    processing_pages: int = 0
    filing_fee: bool = False
    fax_fee: bool = False
    fee_account: str = ''
    by_fax_confirmed: bool = False      # the sender confirms "By fax" is below each title (a scan Faxbot can't read)
    agreement_confirmed: bool = False   # serve: the parties agreed to fax service, confirmed in writing
    number_confirmed: bool = False      # serve: the number is the one last given on a filed and served document
    served_party: str = ''
    extra: dict = field(default_factory=dict)

    @property
    def title(self):
        return f'{self.plaintiff} v. {self.defendant}'

    def public(self):
        """What is kept with the fax and shown in Sent (no payment card is ever taken)."""
        return {'purpose': self.purpose, 'county': self.county, 'case_number': self.case_number,
                'plaintiff': self.plaintiff, 'defendant': self.defendant,
                'documents': [{'title': item.title, 'pages': item.pages} for item in self.documents],
                'sender': list(self.sender), 'telephone': self.telephone, 'fax': self.fax, 'email': self.email,
                'attorney_for': self.attorney_for, 'street': self.street, 'mailing': self.mailing,
                'city_zip': self.city_zip, 'branch': self.branch, 'processing_pages': self.processing_pages,
                'filing_fee': self.filing_fee, 'fax_fee': self.fax_fee, 'fee_account': self.fee_account,
                'by_fax_confirmed': self.by_fax_confirmed, 'agreement_confirmed': self.agreement_confirmed,
                'number_confirmed': self.number_confirmed, 'served_party': self.served_party}


def _text(value, label, *, limit=120, required=False):
    if value is None:
        value = ''
    if not isinstance(value, str) or not _TEXT.fullmatch(value) or len(value.strip()) > limit:
        raise DetailsError(f'{label} must be one line of at most {limit} characters.')
    value = ' '.join(value.split())
    if required and not value:
        raise DetailsError(f'Add {label.lower()}.')
    return value


def _flag(value, label):
    if value in (None, False, True):
        return bool(value)
    raise DetailsError(f'{label} is yes or no.')


def parse(value) -> Details:
    """``Details`` from the sender's JSON object (a dict, or its text); raises ``DetailsError`` in one sentence."""
    import json
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except ValueError:
            raise DetailsError('The court details are not readable; send them as one JSON object.') from None
    if not isinstance(value, dict):
        raise DetailsError('The court details are not readable; send them as one JSON object.')
    known = {'purpose', 'county', 'case_number', 'plaintiff', 'defendant', 'documents', 'sender', 'telephone', 'fax',
             'email', 'attorney_for', 'street', 'mailing', 'city_zip', 'branch', 'processing_pages', 'filing_fee',
             'fax_fee', 'fee_account', 'by_fax_confirmed', 'agreement_confirmed', 'number_confirmed', 'served_party'}
    unknown = sorted(set(value) - known)
    if unknown:
        if any(word in name for name in unknown for word in ('card', 'expir', 'cvv', 'cvc')):
            raise DetailsError("Faxbot never takes a payment card. Give the court account number you set up with "
                               'the court, or arrange the fee with the court.')
        raise DetailsError(f"Faxbot doesn't know the court detail {unknown[0]!r}.")
    purpose = value.get('purpose')
    if purpose not in PURPOSES:
        raise DetailsError('Choose file (filing with the court) or serve (service on a party by fax).')
    documents = value.get('documents')
    if not isinstance(documents, list) or not documents:
        raise DetailsError('List each document you send, with its title and its number of pages.')
    if len(documents) > MAX_DOCUMENTS:
        raise DetailsError(f'The cover sheet lists at most {MAX_DOCUMENTS} documents; send the rest in another fax.')
    parsed = []
    for item in documents:
        if not isinstance(item, dict) or set(item) - {'title', 'pages'}:
            raise DetailsError('Give each document as its title and its number of pages.')
        pages = item.get('pages')
        if type(pages) is not int or not 1 <= pages <= 999:
            raise DetailsError('Give each document its number of pages, from 1 to 999.')
        parsed.append(Document(_text(item.get('title'), 'The document title', required=True), pages))
    sender = value.get('sender') or []
    if not isinstance(sender, list) or len(sender) > len(ATTORNEY_LINES):
        raise DetailsError('Give the attorney or party as up to four lines: name, State Bar number and address.')
    processing = value.get('processing_pages') or 0
    if type(processing) is not int or not 0 <= processing <= 99:
        raise DetailsError('Processing instruction pages are a number from 0 to 99.')
    fee_account = _text(value.get('fee_account'), 'The court account number', limit=30)
    details = Details(
        purpose=purpose, county=_text(value.get('county'), 'The county', limit=40, required=True),
        case_number=_text(value.get('case_number'), 'The case number', limit=40, required=purpose == 'file'),
        plaintiff=_text(value.get('plaintiff'), 'The plaintiff or petitioner', required=True),
        defendant=_text(value.get('defendant'), 'The defendant or respondent', required=True),
        documents=tuple(parsed), sender=tuple(_text(line, 'Each sender line', limit=70) for line in sender),
        telephone=_text(value.get('telephone'), 'The telephone number', limit=30),
        fax=_text(value.get('fax'), 'The fax number', limit=30), email=_text(value.get('email'), 'The email', limit=70),
        attorney_for=_text(value.get('attorney_for'), 'Attorney for', limit=70),
        street=_text(value.get('street'), 'The court street address', limit=70),
        mailing=_text(value.get('mailing'), 'The court mailing address', limit=70),
        city_zip=_text(value.get('city_zip'), 'The court city and ZIP code', limit=70),
        branch=_text(value.get('branch'), 'The branch name', limit=70), processing_pages=processing,
        filing_fee=_flag(value.get('filing_fee'), 'Filing fee'), fax_fee=_flag(value.get('fax_fee'), 'Fax fee'),
        fee_account=fee_account, by_fax_confirmed=_flag(value.get('by_fax_confirmed'), 'By fax confirmed'),
        agreement_confirmed=_flag(value.get('agreement_confirmed'), 'Agreement confirmed'),
        number_confirmed=_flag(value.get('number_confirmed'), 'Number confirmed'),
        served_party=_text(value.get('served_party'), 'The party served', limit=120))
    if purpose == 'file' and not details.sender:
        raise DetailsError('Add the attorney or party filing: name, State Bar number and address (MC-005).')
    if purpose == 'file' and (details.filing_fee or details.fax_fee) and not details.fee_account:
        raise DetailsError("Give the court account number for the fee (rule 2.304(f)); Faxbot never takes a payment "
                           'card.')
    if purpose == 'serve':
        if not details.agreement_confirmed:
            raise DetailsError('Service by fax needs the parties\' agreement, confirmed in writing (rule 2.306(a)(1)); '
                               'confirm you have it.')
        if not details.number_confirmed:
            raise DetailsError('Fax only to the number the party last gave on a filed and served document (rule '
                               '2.306(a)(2)); confirm this is that number.')
        if not details.served_party:
            raise DetailsError('Name the party or attorney you are serving.')
    return details


@contextmanager
def quiet_pypdf():
    """pypdf warns about the form's embedded fonts on every read; those notes are not problems with the fax."""
    logger = logging.getLogger('pypdf')
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(level)


def fill(details: Details) -> bytes:
    """The MC-005 cover sheet for ``details`` as a one-page PDF: the official form, filled and flattened."""
    with quiet_pypdf():
        return _fill(details)


def _fill(details: Details) -> bytes:
    import hashlib
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, NameObject
    data = FORM.read_bytes()
    if hashlib.sha256(data).hexdigest() != FORM_SHA256:
        raise DetailsError('The MC-005 form shipped with Faxbot is not the one it was checked against.')
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    page = writer.pages[0]
    values = {name: line for name, line in zip(ATTORNEY_LINES, details.sender)}
    for key, name in FIELDS.items():
        if key == 'processing_pages':
            values[name] = str(details.processing_pages) if details.processing_pages else ''
        else:
            values[name] = getattr(details, key)
    for (title_field, pages_field), document in zip(DOCUMENT_ROWS, details.documents):
        values[title_field], values[pages_field] = document.title, str(document.pages)
    writer.update_page_form_field_values(page, values, auto_regenerate=False, flatten=True)
    checks = {CHECKS['processing']: details.processing_pages > 0,
              CHECKS['fee_required']: details.filing_fee or details.fax_fee,
              CHECKS['filing_fee']: details.filing_fee, CHECKS['fax_fee']: details.fax_fee,
              CHECKS['fee_account']: bool(details.fee_account) and (details.filing_fee or details.fax_fee)}
    writer.update_page_form_field_values(page, {name: '/1' if on else '/Off' for name, on in checks.items()},
                                         auto_regenerate=False, flatten=True)
    # The flattened page keeps no form: the filled fields are part of the page's drawing now, and every widget left
    # (the buttons, the on-screen privacy notice, the empty card fields) is dropped.
    kept = [annotation for annotation in page.get('/Annots', []) or []
            if annotation.get_object().get('/Subtype') != '/Widget']
    page[NameObject('/Annots')] = ArrayObject(kept)
    if '/AcroForm' in writer._root_object:
        del writer._root_object['/AcroForm']
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


# "By fax" below a document's title (rule 2.304(c)), as court/acceptance.py looks for it on each document's first page.
BY_FAX = re.compile(r'\bby\s+fax\b', re.IGNORECASE)
