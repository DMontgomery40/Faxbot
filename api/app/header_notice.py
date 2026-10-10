"""The header notice (N6): a confidentiality notice in the header band, and a cover sent as that notice instead.

Organizations fax a cover sheet mostly to carry a confidentiality notice and a
reply number. The header line every page already carries covers the reply
number: 47 CFR 68.318(d) asks for the date and time the fax is sent, the
identity of the sender and the sending number, and the fax engine prints them
(with the page number, which the rule does not ask for). This module adds the
notice:

- **The notice line.** The organization, and each mailbox, may set one line of
  notice text (``header_notices``; the mailbox's wins for faxes sent from that
  mailbox). Faxbot prints it at the top of every page of a fax accepted from
  Send a fax, ``faxbot send`` or ``POST /fax``: the page's own content is
  scaled down a little to make room, so nothing of the document is covered,
  and the top strip stays clear for the header line the engine prints there
  (the band of ``routing/continuation.py``; the built-in engine adds its rows
  above the page and the SSL Fax engine images its line over the top rows).
- **A cover sent as the notice.** At upload the sender may mark the first page
  as a cover sheet whose notice should travel in the header instead. That page
  is then not sent, and Sent details say so. It is the sender's choice for
  their own document; Faxbot never removes a page by itself. A recipient
  marked as needing a cover sheet (``recipient_cover_changes``) always gets
  it, and Sent details say why.

The change is made once, at upload, before the sending rules, prices, quotes
and approvals see the fax, so they all count the pages that are sent. The
sender's whole document is kept beside the fax (``<fax>.upload.pdf``) and goes
with the fax's own files at retention. If the notice cannot be drawn, the fax
is refused rather than sent without it.

Not covered yet: faxes that come in by email or from a folder, forms, case
packets and partner relays are sent as they are (a relayed fax carries its
partner's own identity).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import io
import os
from pathlib import Path
import uuid

import sqlalchemy as sa

from .documents import PreparedDocument, _cleanup_paths


MAX_NOTICE = 120
# The smallest size the notice may be drawn at across an A4 page (the narrower of A4 and Letter), in points.
MIN_POINTS = 6.5
A4_POINTS = 595.0
UPLOAD_SUFFIX = '.upload.pdf'
WHERE = 'Numbers → Sender identity'


class NoticeRefused(ValueError):
    """The fax cannot be sent as asked; one plain sentence (400)."""


class NoticeFailed(RuntimeError):
    """Faxbot could not draw the notice; the fax is not accepted (503)."""


# Text --------------------------------------------------------------------------------------------------------------

def check_text(text):
    """The notice as it will be printed (spaces tidied), None for none, or raises ``NoticeRefused``."""
    if text is None:
        return None
    if not isinstance(text, str):
        raise NoticeRefused('Enter the notice as text.')
    cleaned = ' '.join(text.split())
    if not cleaned:
        return None
    if any(not character.isprintable() for character in cleaned):
        raise NoticeRefused('Use letters, numbers and punctuation only in the notice.')
    if len(cleaned) > MAX_NOTICE:
        raise NoticeRefused(f'Keep the notice to one line of up to {MAX_NOTICE} characters.')
    from .routing.continuation import DASH, _note_font
    try:
        from reportlab.pdfbase.pdfmetrics import stringWidth
        font, dash = _note_font()
        width = stringWidth(cleaned if dash else cleaned.replace(DASH, '-'), font, MIN_POINTS)
    except ImportError:
        width = 0
    if width > A4_POINTS - 36:
        raise NoticeRefused('The notice is too long to print on one line across an A4 page; shorten it.')
    return cleaned


# Records -----------------------------------------------------------------------------------------------------------

def _notices():
    return sa.table('header_notices', sa.column('id'), sa.column('scope'), sa.column('mailbox_id'),
                    sa.column('notice'), sa.column('actor_principal_id'), sa.column('actor_name'),
                    sa.column('created_at', sa.DateTime()))


def _fax_notices():
    return sa.table('fax_header_notices', sa.column('id'), sa.column('notice'), sa.column('scope'),
                    sa.column('mailbox_id'), sa.column('cover'), sa.column('original_pages', sa.Integer()),
                    sa.column('sent_pages', sa.Integer()), sa.column('created_at', sa.DateTime()))


def _covers():
    return sa.table('recipient_cover_changes', sa.column('id'), sa.column('phone_number'),
                    sa.column('needs_cover', sa.Integer()), sa.column('actor_principal_id'), sa.column('actor_name'),
                    sa.column('created_at', sa.DateTime()))


def notices_on(connection):
    """``{'organization': {'notice', 'actor_name', 'changed_at'} | None, 'mailboxes': {id: {...}}}``, newest per
    scope; a removed notice is left out."""
    table = _notices()
    rows = connection.execute(sa.select(table).order_by(table.c.created_at, table.c.id)).mappings().all()
    organization, mailboxes = None, {}
    for row in rows:
        item = {'notice': row['notice'], 'actor_name': row['actor_name'], 'changed_at': row['created_at']}
        if row['scope'] == 'organization':
            organization = item if row['notice'] else None
        elif row['notice']:
            mailboxes[row['mailbox_id']] = item
        else:
            mailboxes.pop(row['mailbox_id'], None)
    return {'organization': organization, 'mailboxes': mailboxes}


def notice_for_on(connection, mailbox_id=None):
    """``(notice, scope)`` for a fax sent from ``mailbox_id`` (the mailbox's, else the organization's), or None."""
    found = notices_on(connection)
    if mailbox_id and mailbox_id in found['mailboxes']:
        return found['mailboxes'][mailbox_id]['notice'], 'mailbox'
    if found['organization']:
        return found['organization']['notice'], 'organization'
    return None


def needs_cover_on(connection, number):
    table = _covers()
    row = connection.execute(sa.select(table.c.needs_cover).where(table.c.phone_number == number).order_by(
        table.c.created_at.desc(), table.c.id.desc()).limit(1)).first()
    return bool(row and row[0])


def set_notice_on(connection, *, mailbox_id=None, text, actor_principal_id=None, actor_name=None, now):
    """Record the organization's notice (``mailbox_id`` None) or a mailbox's; ``text`` None or '' removes it."""
    notice = check_text(text)
    connection.execute(_notices().insert().values(
        id=uuid.uuid4().hex, scope='mailbox' if mailbox_id else 'organization', mailbox_id=mailbox_id or None,
        notice=notice, actor_principal_id=actor_principal_id, actor_name=(actor_name or None) and actor_name[:200],
        created_at=now))
    return notice


def set_needs_cover_on(connection, number, needs_cover, *, actor_principal_id=None, actor_name=None, now):
    connection.execute(_covers().insert().values(
        id=uuid.uuid4().hex, phone_number=number, needs_cover=int(bool(needs_cover)),
        actor_principal_id=actor_principal_id, actor_name=(actor_name or None) and actor_name[:200], created_at=now))


# The plan, at upload --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    notice: str
    scope: str                 # 'organization' or 'mailbox'
    mailbox_id: str | None
    cover: str                 # 'none', 'dropped' or 'kept'
    original_pages: int

    @property
    def sent_pages(self):
        return self.original_pages - (1 if self.cover == 'dropped' else 0)


def plan(engine, *, mailbox=None, destination, cover_requested=False, pages):
    """What to print on a fax at upload, or None when there is nothing to print; raises ``NoticeRefused``."""
    with engine.connect() as connection:
        found = notice_for_on(connection, mailbox)
        needs = needs_cover_on(connection, destination) if cover_requested else False
    if found is None:
        if cover_requested:
            raise NoticeRefused(f'There is no header notice to carry the cover sheet’s notice. Set one under {WHERE}, '
                                'or send the fax with its cover sheet.')
        return None
    notice, scope = found
    cover = 'none'
    if cover_requested:
        if int(pages or 0) < 2:
            raise NoticeRefused('This fax has only one page, so there is nothing to send without its cover sheet.')
        cover = 'kept' if needs else 'dropped'
    return Plan(notice, scope, mailbox if scope == 'mailbox' else None, cover, int(pages))


# Drawing ----------------------------------------------------------------------------------------------------------

def band_layout(width, notice):
    """``(font, size, lines)``: how ``notice`` is drawn across a page ``width`` points wide.

    The continuation band's font, from its note size down to ``MIN_POINTS`` and never smaller (smaller is not
    readable on a standard-resolution fax); a page too narrow for one line takes two. Raises ``NoticeRefused`` when
    even two lines do not fit."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from .routing.continuation import DASH, NOTE_POINTS, _note_font
    font, dash = _note_font()
    text = notice if dash else notice.replace(DASH, '-')
    room = width - 36
    size = NOTE_POINTS
    while size > MIN_POINTS and stringWidth(text, font, size) > room:
        size -= 0.5
    size = max(size, MIN_POINTS)
    if stringWidth(text, font, size) <= room:
        return font, size, [text]
    words, first = text.split(' '), ''
    for count in range(len(words), 0, -1):
        first = ' '.join(words[:count])
        if stringWidth(first, font, MIN_POINTS) <= room:
            rest = ' '.join(words[count:])
            if rest and stringWidth(rest, font, MIN_POINTS) <= room:
                return font, MIN_POINTS, [first, rest]
            break
    raise NoticeRefused('A page of this document is too narrow to carry your header notice; shorten the notice or '
                        'send the document on wider pages.')


def _band(width, height, notice):
    """A page of ``width`` by ``height`` points with only ``notice`` on it, under the strip the engine prints in."""
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader
    from .routing.continuation import CLEAR_POINTS
    font, size, lines = band_layout(width, notice)
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(width, height))
    pdf.setFont(font, size)
    for index, line in enumerate(lines):
        pdf.drawString(18, height - CLEAR_POINTS - size * (index + 1) - index, line)
    pdf.showPage()
    pdf.save()
    return PdfReader(io.BytesIO(output.getvalue())).pages[0]


def render(document, notice, *, drop_first=False):
    """The PDF ``document`` with the first page left out when asked, and ``notice`` in a band at the top of every page.

    Each page keeps its size; its content is scaled into the space below the band and centred, so nothing is
    covered, and the top strip stays clear for the header line the fax engine prints there."""
    from pypdf import PageObject, PdfReader, PdfWriter, Transformation
    from .routing.continuation import BAND_POINTS
    reader = PdfReader(io.BytesIO(document))
    pages = list(reader.pages)[1 if drop_first else 0:]
    if not pages:
        raise NoticeRefused('This fax has only one page, so there is nothing to send without its cover sheet.')
    writer, bands = PdfWriter(), {}
    for source in pages:
        if source.rotation % 360:
            source.transfer_rotation_to_content()
        box = source.mediabox
        left, bottom = float(box.left), float(box.bottom)
        width, height = float(box.width), float(box.height)
        scale = max(0.5, (height - BAND_POINTS) / height)
        page = PageObject.create_blank_page(width=width, height=height)
        page.merge_transformed_page(source, Transformation().translate(-left, -bottom).scale(scale, scale)
                                    .translate(width * (1 - scale) / 2, 0))
        key = (round(width, 2), round(height, 2))
        if key not in bands:
            bands[key] = _band(width, height, notice)
        page.merge_page(bands[key])
        writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue(), len(pages)


@dataclass(frozen=True)
class NoticedDocument(PreparedDocument):
    """The prepared upload after the notice: its PDF and fax image rewritten, the sender's whole PDF kept."""
    upload_path: str | None = None

    def cleanup(self) -> None:
        _cleanup_paths(Path(name) for name in (self.pdf_path, self.tiff_path, self.upload_path) if name is not None)


def upload_path(pdf_path):
    path = Path(pdf_path)
    return path.with_name(path.name[:-len('.pdf')] + UPLOAD_SUFFIX if path.name.endswith('.pdf') else path.name)


def apply(prepared, chosen):
    """Rewrite the prepared upload's PDF and fax image for ``chosen`` (a ``Plan``); returns a ``NoticedDocument``.

    The sender's whole PDF stays beside them (``<fax>.upload.pdf``). Raises ``NoticeFailed`` when the notice could
    not be drawn or the result does not have the expected pages, so the fax is never sent without it."""
    from .conversion import FAX_IMAGE_MODE, DocumentConversionError, pdf_to_tiff, validate_pdf
    pdf = Path(prepared.pdf_path)
    kept = upload_path(pdf)
    staged_pdf = pdf.with_name(f'.notice-{pdf.name}')
    staged_tiff = None
    try:
        document = pdf.read_bytes()
        rendered, count = render(document, chosen.notice, drop_first=chosen.cover == 'dropped')
        if count != chosen.sent_pages:
            raise NoticeFailed('The notice changed the page count.')
        staged_pdf.write_bytes(rendered)
        staged_pdf.chmod(0o600)
        if validate_pdf(str(staged_pdf)) != count:
            raise NoticeFailed('The noticed document has other pages than expected.')
        if prepared.tiff_path:
            staged_tiff = Path(prepared.tiff_path).with_name(f'.notice-{Path(prepared.tiff_path).name}')
            pdf_to_tiff(str(staged_pdf), str(staged_tiff))
            os.chmod(staged_tiff, FAX_IMAGE_MODE)
            from PIL import Image
            with Image.open(staged_tiff) as image:
                if getattr(image, 'n_frames', 1) != count:
                    raise NoticeFailed('The fax image has other pages than expected.')
        os.link(pdf, kept)
        os.replace(staged_pdf, pdf)
        if staged_tiff is not None:
            os.replace(staged_tiff, prepared.tiff_path)
    except NoticeRefused:
        raise
    except (OSError, ValueError, DocumentConversionError, NoticeFailed) as error:
        for path in (staged_pdf, staged_tiff):
            if path is not None:
                Path(path).unlink(missing_ok=True)
        raise NoticeFailed(str(error)) from None
    return NoticedDocument(original_name=prepared.original_name, pdf_path=prepared.pdf_path,
                           tiff_path=prepared.tiff_path, pages=count, upload_path=str(kept))


def recorder(job_id, chosen):
    """The acceptance-transaction step that keeps what was printed on the fax and what happened to its cover."""
    def record(connection, now):
        connection.execute(_fax_notices().insert().values(
            id=job_id, notice=chosen.notice, scope=chosen.scope, mailbox_id=chosen.mailbox_id, cover=chosen.cover,
            original_pages=chosen.original_pages, sent_pages=chosen.sent_pages, created_at=now))
    return record


# Sent details ------------------------------------------------------------------------------------------------------

def _pages(count):
    return f'{count} page{"" if count == 1 else "s"}'


def fax_sentence(row):
    if row['cover'] == 'dropped':
        return (f'The first page was a cover sheet. Its notice went in the header of every page instead, so it was not '
                f'sent: {_pages(row["sent_pages"])} went instead of {row["original_pages"]}.')
    if row['cover'] == 'kept':
        return ('You marked the first page as a cover sheet, but this recipient needs a cover sheet, so it was sent too. '
                'Every page also carried your header notice.')
    return 'Every page carried your header notice.'


def fax_view(engine, job_id):
    """What Sent details say about a fax's header notice and cover, or None when it carried none."""
    table = _fax_notices()
    changes = sa.table('fax_page_changes', sa.column('job_id'), sa.column('layout'))
    with engine.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.id == job_id)).mappings().one_or_none()
        # Encoded pages (codec/) carry the document as data, so the notice is in what the recipient's Faxbot
        # decodes, not on the fax pages; nothing is drawn into the encoded pages themselves.
        encoded = row is not None and connection.execute(sa.select(changes.c.job_id).where(
            changes.c.job_id == job_id, changes.c.layout == 'codec').limit(1)).first() is not None
    if row is None:
        return None
    return {'notice': row['notice'], 'cover': row['cover'], 'original_pages': row['original_pages'],
            'sent_pages': row['sent_pages'], 'sentence': fax_sentence(row),
            'encoded': ENCODED_SENTENCE if encoded else None,
            'whose': 'mailbox' if row['scope'] == 'mailbox' else 'organization'}


ENCODED_SENTENCE = ('This fax went as encoded pages, so your header notice is in the document the recipient’s Faxbot '
                    'decodes, not on the fax pages.')


def _when(value):
    return value.isoformat(timespec='seconds') if isinstance(value, datetime) else value


def settings_view(connection, labels=None):
    """The organization's notice and each mailbox's, for Sender identity and ``faxbot numbers reply notice``."""
    found = notices_on(connection)
    labels = labels or {}

    def item(value):
        return None if value is None else {'notice': value['notice'], 'actor_name': value['actor_name'],
                                           'changed_at': _when(value['changed_at'])}
    return {'organization': item(found['organization']),
            'mailboxes': [{'mailbox_id': key, 'mailbox': labels.get(key, key), **item(value)}
                          for key, value in sorted(found['mailboxes'].items(),
                                                   key=lambda pair: labels.get(pair[0], pair[0]).lower())],
            'max_length': MAX_NOTICE}
