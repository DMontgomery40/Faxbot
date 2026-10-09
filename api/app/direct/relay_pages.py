"""The pages a relay sends for a partner: the partner's own document, with the true sender on every page.

A relayed fax leaves from the relay's line, so a line at the top of each page
names who really sent it: the date and time, the sending organization's name
(as the relay enrolled it), the number replies should go to (the reply number
the sender gave when it accepted the agreement) and the page number, in the
order and form the built-in fax engine uses (``faximage.header_line``). It is
added above the page as a band of rows, so nothing on the page is covered, and
it works on every route, including cloud providers that print no per-fax
header of their own.

47 CFR 68.318(d) requires that line on every fax sent to the United States.
For other countries Faxbot prints it anyway, because the recipient should see
who the fax is from; the UK (PECR reg. 24) and Australia (Fax Marketing
Industry Standard 2021, s. 9) require sender details only on marketing faxes.
When the sender says its relayed faxes are marketing, the first page also
carries its business number, contact details and opt-out address, which
Australia's standard asks for.

The document is rasterized once (``conversion.pdf_to_tiff``) and both the fax
image and the PDF people read are made from the stamped pages.
"""
from datetime import timezone
import io
from pathlib import Path
import tempfile

from .faximage import BAND_ROWS_FINE, FONT_PIXELS, _fit_width, header_line


class RelayPagesError(ValueError):
    """The document could not be turned into fax pages; nothing was accepted."""


# Marketing details are printed at least 10 point, as Australia's standard asks: 30 rows at fine resolution
# (196 lines an inch) is about 11 point, on a row 40 lines tall.
MARKETING_FONT_PIXELS = 30
MARKETING_ROWS_FINE = 40


def marketing_lines(marketing, destination=None):
    """The first page's extra lines for a marketing fax, or ().

    The Telecommunications (Fax Marketing) Industry Standard 2021 (Australia) asks a marketing fax to show, on its
    first page at least and in 10-point type or larger, the advertiser's name (the header line has it), its ABN or
    a foreign equivalent, its contact details, the number the fax is sent to, and how to opt out (ACMA's summary at
    donotcall.gov.au, read 2026-10-07).
    """
    if not isinstance(marketing, dict):
        return ()
    parts = [('Business number', marketing.get('business_number')), ('Contact', marketing.get('contact')),
             ('Sent to', destination), ('To stop these faxes', marketing.get('opt_out'))]
    found = tuple(f'{label}: {value}' for label, value in parts if isinstance(value, str) and value.strip())
    return found if any(isinstance(marketing.get(key), str) and marketing[key].strip()
                        for key in ('business_number', 'contact', 'opt_out')) else ()


def _band(width, lines, y_dpi, *, large=0):
    """A band of text rows; the last ``large`` lines (marketing details) are printed larger."""
    from PIL import Image, ImageDraw, ImageFont
    sizes = [(FONT_PIXELS, BAND_ROWS_FINE)] * (len(lines) - large) + [(MARKETING_FONT_PIXELS,
                                                                        MARKETING_ROWS_FINE)] * large
    rows = sum(height for _, height in sizes)
    fine = Image.new('1', (width, rows), 1)
    draw = ImageDraw.Draw(fine)
    draw.fontmode = '1'
    top = 0
    for text, (pixels, height) in zip(lines, sizes):
        draw.text((16, top + 4), text, font=ImageFont.load_default(size=pixels), fill=0)
        top += height
    height = max(1, round(rows * y_dpi / 196))
    return fine if height == rows else fine.resize((width, height), Image.NEAREST)


def stamp_tiff(tiff, *, header, station, moment, zone_name='', first_page=()):
    """(stamped TIFF bytes, pages, the first page's header line) for an engine TIFF."""
    from PIL import Image
    pages, first_line = [], None
    with Image.open(io.BytesIO(tiff)) as image:
        count = getattr(image, 'n_frames', 1)
        for index in range(count):
            image.seek(index)
            frame = image.convert('1') if image.mode != '1' else image.copy()
            dpi = image.info.get('dpi') or (204, 196)
            y_dpi = 98 if float(dpi[1]) < 150 else 196
            frame = _fit_width(frame)
            line = header_line(moment, header=header, station=station, page=index + 1, zone_name=zone_name)
            first_line = first_line or line
            extra = tuple(first_page) if index == 0 else ()
            band = _band(frame.size[0], (line, *extra), y_dpi, large=len(extra))
            page = Image.new('1', (frame.size[0], band.size[1] + frame.size[1]), 1)
            page.paste(band, (0, 0))
            page.paste(frame, (0, band.size[1]))
            page.info['dpi'] = (204, y_dpi)
            pages.append((page, y_dpi))
    if not pages:
        raise RelayPagesError('The document has no pages.')
    output = io.BytesIO()
    first, y_dpi = pages[0]
    first.save(output, 'TIFF', compression='group4', save_all=True, append_images=[page for page, _ in pages[1:]],
               dpi=(204, y_dpi))
    from ..tiff_bytes import settle
    return settle(output.getvalue()), len(pages), first_line  # the same pages always give the same bytes


def stamp(document, *, header, station, moment, zone_name='', first_page=(), folder):
    """(PDF bytes, TIFF bytes, pages, header line) for the PDF a partner sent, with the true sender on each page."""
    from ..conversion import DocumentConversionError, pdf_to_tiff, tiff_to_pdf
    aware = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment
    try:
        with tempfile.TemporaryDirectory(prefix='.relay-', dir=str(folder)) as temporary:
            source, image = Path(temporary) / 'document.pdf', Path(temporary) / 'document.tiff'
            source.write_bytes(document)
            pdf_to_tiff(str(source), str(image))
            stamped, pages, line = stamp_tiff(image.read_bytes(), header=header, station=station, moment=aware,
                                              zone_name=zone_name, first_page=first_page)
            image.write_bytes(stamped)
            readable = Path(temporary) / 'readable.pdf'
            tiff_to_pdf(str(image), str(readable))
            return readable.read_bytes(), stamped, pages, line
    except (DocumentConversionError, OSError, ValueError):
        raise RelayPagesError('The document could not be turned into fax pages.') from None
