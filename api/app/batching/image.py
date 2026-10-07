"""Separator pages, the index page, and the one fax image a shared call sends.

Each fax's own pages are copied into the call image byte for byte from the fax
image Faxbot already made and checked when it accepted the fax; only the
separator pages, or the one index page, are new. A separator names the
document's place in the call, its reference, its page count and its sender,
nothing more. The index page (page 1, where the recipient agreed to it) lists
the same for every document plus its page range in the call, and nothing
else. Covers or barcode pages inside a document are its own pages, so they
are always sent as they are.
"""
from pathlib import Path
import os
import re
import struct
import tempfile


class MemberUnusable(RuntimeError):
    """One fax's own image cannot go in the shared call; ``job_id`` names it."""

    def __init__(self, job_id):
        self.job_id = job_id
        super().__init__('A fax image is unavailable for the shared call.')


class CallImageError(RuntimeError):
    """The shared call image could not be made; nothing was sent."""


_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
_STRIP_OFFSETS, _STRIP_COUNTS, _PAGE_NUMBER = 273, 279, 297
_UNSUPPORTED_TAGS = {324, 325, 330, 34665, 34853}  # tiles, sub-images, EXIF/GPS directories
SEPARATOR_CHUNK = 20  # pages per Ghostscript run, well inside its raster limit


def _printable(text, limit):
    text = ''.join(character for character in str(text or '') if character.isprintable())
    text = text.encode('latin-1', 'replace').decode('latin-1')
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def separator_line(document_number, documents, reference, pages, sender_name=None):
    """"Document 2 of 3 · Faxbot 7f3a9c21 · 4 pages · from Front Desk"."""
    parts = [f'Document {document_number} of {documents}', _printable(reference, 100),
             '1 page' if pages == 1 else f'{pages} pages']
    sender = _printable(sender_name, 80)
    if sender:
        parts.append(f'from {sender}')
    return ' · '.join(parts)


def _count(pages, word='page'):
    return f'1 {word}' if pages == 1 else f'{pages} {word}s'


def index_heading(documents, call_pages):
    """The index page's title and its one summary sentence."""
    return ('Index of documents in this fax',
            f'This fax has {_count(documents, "document")} on {_count(call_pages)}, counting this index page.')


def index_entry(document_number, first_page, last_page, reference, pages, sender_name=None):
    """One document on the index page: ("Document 2: pages 4–7 (4 pages)", "Faxbot 7f3a9c21 · from Front Desk")."""
    where = f'page {first_page}' if first_page == last_page else f'pages {first_page}–{last_page}'
    details = [_printable(reference, 100)]
    sender = _printable(sender_name, 80)
    if sender:
        details.append(f'from {sender}')
    return f'Document {document_number}: {where} ({_count(pages)})', ' · '.join(part for part in details if part)


# The index page's fixed layout, in points on a US Letter page: every document takes at most three
# lines, so ``policy.INDEX_PAGE_DOCUMENTS`` documents always fit (a test renders that worst case).
INDEX_MARGIN = 36
INDEX_TITLE, INDEX_SUMMARY, INDEX_HEADING, INDEX_DETAILS = 18, 12, 12, 10
INDEX_HEADING_STEP, INDEX_DETAILS_STEP, INDEX_GAP = 14, 12, 5
INDEX_DETAIL_LINES = 2


def _wrap(text, measure, width, lines):
    """At most ``lines`` lines no wider than ``width``; a cut-off end is marked with an ellipsis."""
    words, result, current = text.split(' '), [], ''
    for word in words:
        candidate = f'{current} {word}' if current else word
        if measure(candidate) <= width:
            current = candidate
            continue
        if current:
            result.append(current)
        current = word
        while measure(current) > width:  # one word wider than a line: break it by characters
            cut = len(current)
            while cut > 1 and measure(current[:cut]) > width:
                cut -= 1
            result.append(current[:cut])
            current = current[cut:]
    if current:
        result.append(current)
    if len(result) > lines:
        last = result[lines - 1]
        while last and measure(last + '…') > width:
            last = last[:-1]
        result = result[:lines - 1] + [last.rstrip() + '…']
    return result


def index_pdf(heading, summary, entries, path):
    """Write the one index page; ``ValueError`` when it cannot hold every entry (never cut silently)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    width, height = letter
    usable = width - 2 * INDEX_MARGIN
    # invariant: no creation time or random ID in the file, so one call's index page is always the same.
    document = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    document.setTitle('Index page')
    y = height - INDEX_MARGIN - INDEX_TITLE
    document.setFont('Helvetica-Bold', INDEX_TITLE)
    document.drawString(INDEX_MARGIN, y, heading)
    y -= INDEX_TITLE + 2
    document.setFont('Helvetica', INDEX_SUMMARY)
    document.drawString(INDEX_MARGIN, y, summary)
    y -= 10
    document.setLineWidth(1)
    document.line(INDEX_MARGIN, y, width - INDEX_MARGIN, y)
    y -= 20
    for title, details in entries:
        lines = _wrap(details, lambda text: document.stringWidth(text, 'Helvetica', INDEX_DETAILS), usable - 18,
                      INDEX_DETAIL_LINES) if details else []
        if y - INDEX_HEADING_STEP - INDEX_DETAILS_STEP * max(0, len(lines) - 1) < INDEX_MARGIN:
            raise ValueError('The index page cannot list every document.')
        document.setFont('Helvetica-Bold', INDEX_HEADING)
        document.drawString(INDEX_MARGIN, y, title)
        y -= INDEX_HEADING_STEP
        document.setFont('Helvetica', INDEX_DETAILS)
        for line in lines:
            document.drawString(INDEX_MARGIN + 18, y, line)
            y -= INDEX_DETAILS_STEP
        y -= INDEX_GAP
    document.showPage()
    document.save()


def _separator_pdf(lines, path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    width, height = letter
    document = canvas.Canvas(str(path), pagesize=letter)
    document.setTitle('Separator pages')
    for line in lines:
        heading, _, rest = line.partition(' · ')
        document.setFont('Helvetica-Bold', 28)
        document.drawCentredString(width / 2, height / 2 + 24, heading)
        size = 14
        while size > 8 and document.stringWidth(line, 'Helvetica', size) > width - 72:
            size -= 1
        document.setFont('Helvetica', size)
        document.drawCentredString(width / 2, height / 2 - 12, line)
        document.showPage()
    document.save()


def _read_ifds(data):
    """[(entries, offset)] for every page of a little-endian classic TIFF; entries are (tag, type, count, value bytes)."""
    if len(data) < 8 or data[:4] != b'II*\x00':
        raise ValueError('Unsupported fax image.')
    offset = struct.unpack_from('<I', data, 4)[0]
    pages, seen = [], set()
    while offset:
        if offset in seen or offset + 2 > len(data) or len(pages) > 1000:
            raise ValueError('Unsupported fax image.')
        seen.add(offset)
        count = struct.unpack_from('<H', data, offset)[0]
        entries = []
        for index in range(count):
            tag, kind, number = struct.unpack_from('<HHI', data, offset + 2 + 12 * index)
            if kind not in _TYPE_SIZES or tag in _UNSUPPORTED_TAGS:
                raise ValueError('Unsupported fax image.')
            size = _TYPE_SIZES[kind] * number
            field = offset + 2 + 12 * index + 8
            start = field if size <= 4 else struct.unpack_from('<I', data, field)[0]
            if start + size > len(data):
                raise ValueError('Unsupported fax image.')
            entries.append((tag, kind, number, data[start:start + size]))
        pages.append(entries)
        next_field = offset + 2 + 12 * count
        offset = struct.unpack_from('<I', data, next_field)[0] if next_field + 4 <= len(data) else 0
    return pages


def _strips(data, entries):
    fields = {tag: (kind, number, value) for tag, kind, number, value in entries}
    if _STRIP_OFFSETS not in fields or _STRIP_COUNTS not in fields:
        raise ValueError('Unsupported fax image.')

    def numbers(field):
        kind, number, value = field
        if kind == 3:
            return list(struct.unpack(f'<{number}H', value))
        if kind == 4:
            return list(struct.unpack(f'<{number}I', value))
        raise ValueError('Unsupported fax image.')
    offsets, counts = numbers(fields[_STRIP_OFFSETS]), numbers(fields[_STRIP_COUNTS])
    if len(offsets) != len(counts) or any(start + size > len(data) for start, size in zip(offsets, counts)):
        raise ValueError('Unsupported fax image.')
    return [data[start:start + size] for start, size in zip(offsets, counts)]


def concatenate(pages, out_path):
    """Write the listed pages ``[(tiff bytes, page index)]`` into one TIFF, copying their image data unchanged."""
    parsed = {}
    output = bytearray(b'II*\x00\x00\x00\x00\x00')
    previous_next = 4  # where the offset of the next page's directory goes
    total = len(pages)
    for number, (data, index) in enumerate(pages):
        key = id(data)
        if key not in parsed:
            parsed[key] = _read_ifds(data)
        entries = parsed[key][index]
        strips = _strips(data, entries)
        new_offsets = []
        for strip in strips:
            if len(output) % 2:
                output.append(0)
            new_offsets.append(len(output))
            output += strip
        written = []
        for tag, kind, count, value in sorted(entries, key=lambda entry: entry[0]):
            if tag == _STRIP_OFFSETS:
                kind, count, value = 4, len(new_offsets), struct.pack(f'<{len(new_offsets)}I', *new_offsets)
            elif tag == _STRIP_COUNTS:
                kind, count, value = 4, len(strips), struct.pack(f'<{len(strips)}I', *(len(s) for s in strips))
            elif tag == _PAGE_NUMBER and kind == 3 and count == 2:
                value = struct.pack('<2H', number, total)
            written.append((tag, kind, count, value))
        # Values longer than four bytes go before the directory that points at them.
        placed = []
        for tag, kind, count, value in written:
            if len(value) > 4:
                if len(output) % 2:
                    output.append(0)
                placed.append((tag, kind, count, struct.pack('<I', len(output))))
                output += value
            else:
                placed.append((tag, kind, count, value.ljust(4, b'\x00')))
        if len(output) % 2:
            output.append(0)
        struct.pack_into('<I', output, previous_next, len(output))
        output += struct.pack('<H', len(placed))
        for tag, kind, count, value in placed:
            output += struct.pack('<HHI', tag, kind, count) + value
        previous_next = len(output)
        output += b'\x00\x00\x00\x00'
    _write_atomically(out_path, bytes(output))


def _write_atomically(path, data):
    path = Path(path)
    from ..conversion import FAX_IMAGE_MODE
    descriptor, temporary = tempfile.mkstemp(prefix='.batch-', dir=str(path.parent))
    try:
        # The call image Asterisk sends: readable by its group too (FAX_IMAGE_MODE).
        os.fchmod(descriptor, FAX_IMAGE_MODE)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def page_count(path):
    from PIL import Image
    with Image.open(path) as image:
        return getattr(image, 'n_frames', 1)


def index_tiff(folder, heading, summary, entries):
    """The index page as one fax page ``(tiff bytes, 0)``, at the fax image's fine resolution."""
    from ..conversion import pdf_to_tiff
    pdf, tiff = Path(folder) / 'index.pdf', Path(folder) / 'index.tiff'
    index_pdf(heading, summary, entries, pdf)
    pdf_to_tiff(str(pdf), str(tiff))
    data = tiff.read_bytes()
    if len(_read_ifds(data)) != 1:
        raise ValueError('The index page is not one page.')
    return data, 0


def build_call_image(root, call_id, members, *, index=None):
    """Write ``batch-<call_id>.tiff`` in ``root`` and return its path.

    ``members`` lists, in call order, ``(job_id, pages, separator line)``.
    Each fax's ``<job_id>.tiff`` must hold exactly its pages; otherwise
    ``MemberUnusable`` names it. With ``index`` (the index page's heading,
    summary and entries, as ``index_heading``/``index_entry`` make them) the
    call starts with that one page and has no separator pages. The image's
    page count is checked against the layout before it is used.
    """
    from ..conversion import DocumentConversionError, pdf_to_tiff
    root = Path(root)
    if re.fullmatch('[a-f0-9]{32}', call_id) is None:
        raise CallImageError('Unusable call identity.')
    images = []
    for job_id, pages, _ in members:
        path = root / (job_id + '.tiff')
        if re.fullmatch('[a-f0-9]{32}', job_id) is None or path.is_symlink() or not path.is_file():
            raise MemberUnusable(job_id)
        try:
            data = path.read_bytes()
            if len(_read_ifds(data)) != pages:
                raise MemberUnusable(job_id)
        except (OSError, ValueError, struct.error):
            raise MemberUnusable(job_id) from None
        images.append(data)
    if index is not None:
        return _index_call(root, call_id, members, images, index)
    separators = []
    with tempfile.TemporaryDirectory(prefix='faxbot-separators-', dir=str(root)) as folder:
        lines = [line for _, _, line in members]
        for start in range(0, len(lines), SEPARATOR_CHUNK):
            pdf, tiff = Path(folder) / f'{start}.pdf', Path(folder) / f'{start}.tiff'
            try:
                _separator_pdf(lines[start:start + SEPARATOR_CHUNK], pdf)
                pdf_to_tiff(str(pdf), str(tiff))
                data = tiff.read_bytes()
                separators += [(data, index) for index in range(len(_read_ifds(data)))]
            except (DocumentConversionError, OSError, ValueError, struct.error):
                raise CallImageError('Separator pages could not be made.') from None
    if len(separators) != len(members):
        raise CallImageError('Separator pages could not be made.')
    pages = []
    for separator, data, (_, count, _) in zip(separators, images, members):
        pages.append(separator)
        pages += [(data, index) for index in range(count)]
    return _write_call(root, call_id, pages, sum(1 + count for _, count, _ in members))


def _index_call(root, call_id, members, images, index):
    from ..conversion import DocumentConversionError
    heading, summary, entries = index
    if len(entries) != len(members):
        raise CallImageError('The index page could not be made.')
    with tempfile.TemporaryDirectory(prefix='faxbot-index-', dir=str(root)) as folder:
        try:
            pages = [index_tiff(folder, heading, summary, entries)]
        except (DocumentConversionError, OSError, ValueError, struct.error):
            raise CallImageError('The index page could not be made.') from None
    for data, (_, count, _) in zip(images, members):
        pages += [(data, number) for number in range(count)]
    return _write_call(root, call_id, pages, 1 + sum(count for _, count, _ in members))


def _write_call(root, call_id, pages, expected):
    out = root / f'batch-{call_id}.tiff'
    try:
        concatenate(pages, out)
        if page_count(out) != expected:
            raise CallImageError('The shared call image has the wrong number of pages.')
    except (OSError, ValueError, struct.error):
        raise CallImageError('The shared call image could not be written.') from None
    return out
