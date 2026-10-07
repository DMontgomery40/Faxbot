"""Import a form: a fillable PDF, or a PDF or SVG template with a field-position file.

Import is the one step that may depend on the host: Ghostscript draws a PDF
template's pages once, without its form fields' appearances, and the fixed
8x8 ordered dither turns grey into black and white dots at 204 by 196 dots
per inch. The resulting bilevel pages are stored with the form version and
sent to partners as they are, so nobody draws the template again.

Positions:

- A fillable PDF's fields (AcroForm widgets) carry their own rectangles.
- A field-position file is JSON: ``{"fields": [{"name", "type", "page", "x",
  "y", "width", "height", "unit"}]}``, measured from the top-left corner of
  the page in points (the default), millimetres or inches, plus each type's
  options (``font_size``, ``align``, ``multiline``, ``max_length``,
  ``format``, ``decimals``, ``options``, ``required``, ``label``).

Every page becomes 1728 dots wide, the width of a fax line; its height keeps
the page's proportions at 196 lines per inch.
"""
from fractions import Fraction
import io
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ElementTree

from . import model
from .raster import BAYER, Bitmap, cubic, dither, fill, polygon_edges, quadratic, thick_line


MAX_TEMPLATE_BYTES = 10 * 1024 * 1024
GHOSTSCRIPT_TIMEOUT = 120
UNITS = {'pt': Fraction(1), 'mm': Fraction(72 * 10, 254), 'in': Fraction(72), 'cm': Fraction(72 * 100, 254),
         'px': Fraction(3, 4)}


class ImportProblem(model.FormError):
    """The template or position file cannot be imported; one plain sentence."""


class Imported:
    """What an import produced: addressed content, page bitmaps and the original template."""

    def __init__(self, content, backgrounds, *, template, media_type, source):
        self.content, self.backgrounds = content, backgrounds
        self.template, self.media_type, self.source = template, media_type, source
        self.address = model.address(content)


def _pages(backgrounds):
    return [{'width': page.width, 'height': page.height, 'background': model.sha256(page.packed())}
            for page in backgrounds]


def _finish(backgrounds, fields, *, template, media_type, source):
    if not fields:
        raise ImportProblem('This template has no fields; add a field-position file that places them.')
    return Imported(model.content(_pages(backgrounds), fields), backgrounds, template=template,
                    media_type=media_type, source=source)


def from_template(data, *, file_name='', positions=None):
    """Import a PDF (fillable, or with ``positions``) or an SVG template with ``positions``."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ImportProblem('Choose a template file to import.')
    if len(data) > MAX_TEMPLATE_BYTES:
        raise ImportProblem('The template is larger than 10 MB.')
    data = bytes(data)
    position_fields = None if positions is None else _position_file(positions)
    if data.startswith(b'%PDF'):
        geometry, acro = _pdf_geometry(data)
        backgrounds = _pdf_backgrounds(data, geometry)
        if position_fields is not None:
            fields = _placed(position_fields, geometry)
            source = 'pdf_positions'
        else:
            fields = _acroform_fields(acro, geometry)
            source = 'pdf_acroform'
        return _finish(backgrounds, fields, template=data, media_type='application/pdf', source=source)
    if file_name.lower().endswith('.svg') or data.lstrip()[:5] in (b'<?xml', b'<svg ') or b'<svg' in data[:2048]:
        if position_fields is None:
            raise ImportProblem('An SVG template needs a field-position file that places its fields.')
        background, width_pt, height_pt = svg_background(data)
        geometry = [(Fraction(0), Fraction(height_pt), Fraction(width_pt), Fraction(height_pt))]
        fields = _placed(position_fields, geometry)
        return _finish([background], fields, template=data, media_type='image/svg+xml', source='svg_positions')
    raise ImportProblem('The template must be a PDF or an SVG file.')


# Field-position files ------------------------------------------------------------------------

def _position_file(positions):
    if isinstance(positions, (bytes, bytearray, str)):
        try:
            positions = json.loads(positions)
        except ValueError:
            raise ImportProblem('The field-position file is not valid JSON.') from None
    fields = positions.get('fields') if isinstance(positions, dict) else positions
    if not isinstance(fields, list) or not fields:
        raise ImportProblem('The field-position file must list the fields.')
    return fields


def _dots(geometry, page, x, y, width, height):
    """A rectangle in points from the page's top-left corner, in dots."""
    _, _, page_width, _ = geometry[page - 1]
    across = Fraction(model.PAGE_WIDTH) / page_width
    down = Fraction(model.Y_DPI, 72)
    left, top = math.floor(x * across), math.floor(y * down)
    return [left, top, math.ceil((x + width) * across) - left, math.ceil((y + height) * down) - top]


def _number(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ImportProblem(f'{what} must be a number.')
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        raise ImportProblem(f'{what} must be a number.') from None


def _placed(raw_fields, geometry):
    fields = []
    for raw in raw_fields:
        if not isinstance(raw, dict):
            raise ImportProblem('Each field in the position file must be an object.')
        name = raw.get('name', '?')
        unit = raw.get('unit', 'pt')
        if unit not in UNITS:
            raise ImportProblem(f'The field {name} uses an unknown unit; use pt, mm or in.')
        page = raw.get('page', 1)
        if type(page) is not int or not 1 <= page <= len(geometry):
            raise ImportProblem(f'The field {name} is on a page the template does not have.')
        scale = UNITS[unit]
        x, y, width, height = (_number(raw.get(key), f'The {key} of field {name}') * scale
                               for key in ('x', 'y', 'width', 'height'))
        item = {key: value for key, value in raw.items()
                if key not in ('x', 'y', 'width', 'height', 'unit', 'box', 'option_boxes')}
        item['box'] = _dots(geometry, page, x, y, width, height)
        boxes = raw.get('option_boxes')
        if isinstance(boxes, dict):
            item['option_boxes'] = {}
            for option, place in boxes.items():
                if not isinstance(place, dict):
                    raise ImportProblem(f'Each option of {name} must give x, y, width and height.')
                item['option_boxes'][option] = _dots(geometry, page, *(
                    _number(place.get(key), f'The {key} of an option of {name}') * scale
                    for key in ('x', 'y', 'width', 'height')))
        fields.append(item)
    return fields


# PDF templates ------------------------------------------------------------------------------

def _pdf_geometry(data):
    """Each page's visible box (left, top, width, height in points) and the AcroForm widgets."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ImportProblem('The template is password protected; save an unprotected copy first.')
        pages = list(reader.pages)
    except ImportProblem:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError):
        raise ImportProblem('The template is not a PDF Faxbot can read.') from None
    if not 0 < len(pages) <= model.MAX_PAGES:
        raise ImportProblem(f'A form has from 1 to {model.MAX_PAGES} pages.')
    geometry, widgets = [], []
    for number, page in enumerate(pages, start=1):
        if int(page.get('/Rotate', 0) or 0) % 360:
            raise ImportProblem('The template has rotated pages; save it with upright pages first.')
        box = page.cropbox
        left, bottom = Fraction(str(float(box.left))), Fraction(str(float(box.bottom)))
        width, height = Fraction(str(float(box.width))), Fraction(str(float(box.height)))
        if width <= 0 or height <= 0 or height * model.Y_DPI / 72 > model.MAX_PAGE_HEIGHT:
            raise ImportProblem('The template has a page size Faxbot cannot fax.')
        geometry.append((left, bottom + height, width, height))
        for reference in page.get('/Annots') or []:
            try:
                annotation = reference.get_object()
            except Exception:
                continue
            if annotation.get('/Subtype') == '/Widget':
                widgets.append((number, annotation))
    return geometry, widgets


def _ghostscript():
    executable = shutil.which('gs')
    if executable is None:
        raise ImportProblem('This installation cannot draw PDF templates: Ghostscript is not installed.')
    return executable


def _pdf_backgrounds(data, geometry):
    """Each page drawn once at 1728 dots across and 196 lines per inch, without form field appearances."""
    from PIL import Image
    executable = _ghostscript()
    backgrounds = []
    with tempfile.TemporaryDirectory(prefix='faxbot-form-import-') as folder:
        source = Path(folder) / 'template.pdf'
        source.write_bytes(data)
        for number, (_, _, width, height) in enumerate(geometry, start=1):
            across = Fraction(model.PAGE_WIDTH * 72) / width
            target = Path(folder) / f'page-{number}.png'
            arguments = [executable, '-q', '-dSAFER', '-dNOPAUSE', '-dBATCH', '-dUseCropBox', '-dShowAnnots=false',
                         '-dTextAlphaBits=1', '-dGraphicsAlphaBits=1', '-sDEVICE=pnggray',
                         f'-r{float(across):.6f}x{model.Y_DPI}', f'-dFirstPage={number}', f'-dLastPage={number}',
                         f'-sOutputFile={target}', '-f', str(source)]
            try:
                subprocess.run(arguments, check=True, timeout=GHOSTSCRIPT_TIMEOUT, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                with Image.open(target) as image:
                    image.load()
                    gray = image.convert('L')
            except (subprocess.SubprocessError, OSError):
                raise ImportProblem('Faxbot could not draw this PDF template.') from None
            rows_wanted = math.floor(height * model.Y_DPI / 72 + Fraction(1, 2))
            raw = gray.tobytes()
            gray_rows = [raw[y * gray.width:(y + 1) * gray.width] for y in range(min(gray.height, rows_wanted))]
            gray_rows += [b'\xff' * gray.width] * (rows_wanted - len(gray_rows))
            backgrounds.append(Bitmap(model.PAGE_WIDTH, rows_wanted, dither(gray_rows, model.PAGE_WIDTH)))
    return backgrounds


def _inherited(annotation, key):
    node, depth = annotation, 0
    while node is not None and depth < 32:
        if key in node:
            return node[key]
        parent = node.get('/Parent')
        node = parent.get_object() if parent is not None else None
        depth += 1
    return None


def _full_name(annotation):
    names, node, depth = [], annotation, 0
    while node is not None and depth < 32:
        if '/T' in node:
            names.append(str(node['/T']))
        parent = node.get('/Parent')
        node = parent.get_object() if parent is not None else None
        depth += 1
    return '.'.join(reversed(names))


def _script(annotation):
    actions = _inherited(annotation, '/AA')
    try:
        action = actions.get_object().get('/F') if actions is not None else None
        script = action.get_object().get('/JS') if action is not None else None
        if script is None:
            return ''
        script = script.get_object()
        return script.get_data().decode('latin-1') if hasattr(script, 'get_data') else str(script)
    except Exception:
        return ''


DATE_SCRIPTS = {'mm/dd/yyyy': 'MM/DD/YYYY', 'dd/mm/yyyy': 'DD/MM/YYYY', 'yyyy-mm-dd': 'YYYY-MM-DD',
                'm/d/yyyy': 'MM/DD/YYYY', 'd/m/yyyy': 'DD/MM/YYYY'}


def _rect_dots(annotation, geometry, number):
    left, top, width, _ = geometry[number - 1]
    x1, y1, x2, y2 = (Fraction(str(float(value))) for value in annotation['/Rect'])
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    return _dots(geometry, number, x1 - left, top - y2, x2 - x1, y2 - y1)


def _acroform_fields(widgets, geometry):
    fields, by_name = [], {}
    for number, annotation in widgets:
        name = _full_name(annotation)
        kind = str(_inherited(annotation, '/FT') or '')
        flags = int(_inherited(annotation, '/Ff') or 0)
        if not name or not kind:
            continue
        box = _rect_dots(annotation, geometry, number)
        appearance = str(_inherited(annotation, '/DA') or '')
        size = re.search(r'(\d+(?:\.\d+)?)\s+Tf', appearance)
        font_size = round(float(size.group(1))) if size else 0
        font_size = model.DEFAULT_FONT if font_size == 0 else min(model.MAX_FONT, max(model.MIN_FONT, font_size))
        item = {'name': name, 'page': number, 'box': box, 'required': bool(flags & 2)}
        if kind == '/Btn':
            if flags & (1 << 16):  # a push button holds no value
                continue
            if flags & (1 << 15):  # a radio group: one option per widget
                on = [str(key)[1:] for key in (annotation.get('/AP', {}).get('/N', {}) or {}) if str(key) != '/Off']
                option = on[0] if on else str(len(by_name.get(name, {}).get('options', [])) + 1)
                if name in by_name:
                    if option not in by_name[name]['options']:
                        by_name[name]['options'].append(option)
                        by_name[name]['option_boxes'][option] = box
                    continue
                item.update(type='choice', options=[option], option_boxes={option: box}, font_size=font_size)
            else:
                if name in by_name:
                    continue
                item['type'] = 'checkbox'
        elif kind == '/Tx':
            script = _script(annotation)
            date_format = re.search(r'AFDate_(?:Format|Keystroke)Ex\(\s*"([^"]+)"', script)
            number_format = re.search(r'AFNumber_(?:Format|Keystroke)\(\s*(\d+)', script)
            if name in by_name:
                continue
            if date_format:
                item.update(type='date', format=DATE_SCRIPTS.get(date_format.group(1).lower(), 'MM/DD/YYYY'),
                            font_size=font_size)
            elif number_format:
                item.update(type='number', decimals=min(6, int(number_format.group(1))), font_size=font_size)
            else:
                limit = _inherited(annotation, '/MaxLen')
                item.update(type='text', multiline=bool(flags & (1 << 12)), font_size=font_size,
                            max_length=int(limit) if limit is not None and 0 < int(limit) <= model.MAX_TEXT else None)
        elif kind == '/Ch':
            if name in by_name:
                continue
            options = []
            for entry in _inherited(annotation, '/Opt') or []:
                entry = entry.get_object() if hasattr(entry, 'get_object') else entry
                shown = entry[1] if isinstance(entry, list) and len(entry) == 2 else entry
                shown = str(shown).strip()
                if shown and shown not in options:
                    options.append(shown)
            if not options:
                continue
            item.update(type='choice', options=options, font_size=font_size)
        elif kind == '/Sig':
            if name in by_name:
                continue
            item['type'] = 'signature'
        else:
            continue
        by_name[name] = item
        fields.append(item)
    return fields


# SVG templates (a drawing subset, rasterized by Faxbot itself) ---------------------------------

NAMED = {'black': 0, 'white': 255, 'gray': 128, 'grey': 128, 'silver': 192, 'lightgray': 211, 'lightgrey': 211,
         'darkgray': 169, 'darkgrey': 169, 'dimgray': 105, 'dimgrey': 105, 'gainsboro': 220, 'whitesmoke': 245}
SVG = '{http://www.w3.org/2000/svg}'
_NUMBER = re.compile(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')
# Unit circle points (millionths), so circles are the same polygon everywhere.
CIRCLE = tuple((Fraction(round(math.cos(2 * math.pi * k / 48) * 1_000_000), 1_000_000),
                Fraction(round(math.sin(2 * math.pi * k / 48) * 1_000_000), 1_000_000)) for k in range(48))


def _length(value, default=None):
    if value is None:
        return default
    match = re.fullmatch(r'\s*([-+]?(?:\d+\.?\d*|\.\d+))\s*(pt|mm|in|cm|px)?\s*', str(value))
    if not match:
        raise ImportProblem('The SVG template uses a size Faxbot cannot read; give its width and height in pt, mm or in.')
    return Fraction(match.group(1)) * UNITS[match.group(2) or 'px']


def _gray(value):
    """A paint as grey 0..255, or None for no paint."""
    if value is None:
        return None
    value = value.strip().lower()
    if value in ('none', 'transparent', ''):
        return None
    if value in NAMED:
        return NAMED[value]
    match = re.fullmatch(r'#([0-9a-f]{3}|[0-9a-f]{6})', value)
    if match:
        digits = match.group(1)
        if len(digits) == 3:
            digits = ''.join(c * 2 for c in digits)
        red, green, blue = (int(digits[i:i + 2], 16) for i in (0, 2, 4))
    else:
        match = re.fullmatch(r'rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', value)
        if not match:
            return 0
        red, green, blue = (min(255, int(part)) for part in match.groups())
    return (red * 299 + green * 587 + blue * 114) // 1000


def _style(element, inherited):
    style = dict(inherited)
    for key in ('fill', 'stroke', 'stroke-width', 'font-size', 'text-anchor'):
        if element.get(key) is not None:
            style[key] = element.get(key)
    for part in (element.get('style') or '').split(';'):
        if ':' in part:
            key, value = (piece.strip() for piece in part.split(':', 1))
            if key in ('fill', 'stroke', 'stroke-width', 'font-size', 'text-anchor'):
                style[key] = value
    return style


def _transform(text, matrix):
    for name, arguments in re.findall(r'(\w+)\s*\(([^)]*)\)', text or ''):
        values = [Fraction(value) for value in _NUMBER.findall(arguments)]
        if name == 'translate':
            step = (1, 0, 0, 1, values[0], values[1] if len(values) > 1 else 0)
        elif name == 'scale':
            step = (values[0], 0, 0, values[1] if len(values) > 1 else values[0], 0, 0)
        elif name == 'matrix' and len(values) == 6:
            step = tuple(values)
        else:
            raise ImportProblem(f'The SVG template uses a {name} transform; Faxbot draws translate, scale and matrix only.')
        a, b, c, d, e, f = matrix
        a2, b2, c2, d2, e2, f2 = step
        matrix = (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2,
                  a * e2 + c * f2 + e, b * e2 + d * f2 + f)
    return matrix


def _path(data):
    """Subpaths of an SVG path (M, L, H, V, C, Q, Z in both cases) as lists of points."""
    tokens = re.findall(r'[MmLlHhVvCcQqZzAaSsTt]|' + _NUMBER.pattern, data or '')
    paths, current, start, position, command, index = [], [], (0, 0), (Fraction(0), Fraction(0)), None, 0

    def take(count):
        nonlocal index
        values = [Fraction(token) for token in tokens[index:index + count]]
        if len(values) != count or any(re.fullmatch('[A-Za-z]', token) for token in tokens[index:index + count]):
            raise ImportProblem('The SVG template has a path Faxbot cannot read.')
        index += count
        return values
    while index < len(tokens):
        token = tokens[index]
        if re.fullmatch('[A-Za-z]', token):
            command = token
            index += 1
            if command in 'AaSsTt':
                raise ImportProblem('The SVG template uses curved arcs Faxbot cannot draw; convert them to paths of curves.')
            if command in 'Zz':
                if current:
                    paths.append((current, True))
                current, position = [], start
                continue
        elif command is None:
            raise ImportProblem('The SVG template has a path Faxbot cannot read.')
        relative = command.islower()
        x0, y0 = position if relative else (0, 0)
        upper = command.upper()
        if upper == 'M':
            x, y = take(2)
            if current:
                paths.append((current, False))
            position = (x0 + x, y0 + y)
            start, current = position, [position]
            command = 'l' if relative else 'L'
        elif upper == 'L':
            x, y = take(2)
            position = (x0 + x, y0 + y)
            current.append(position)
        elif upper == 'H':
            (x,) = take(1)
            position = ((position[0] if relative else 0) + x, position[1])
            current.append(position)
        elif upper == 'V':
            (y,) = take(1)
            position = (position[0], (position[1] if relative else 0) + y)
            current.append(position)
        elif upper == 'C':
            x1, y1, x2, y2, x, y = take(6)
            end = (x0 + x, y0 + y)
            current.extend(cubic(position, (x0 + x1, y0 + y1), (x0 + x2, y0 + y2), end, 12))
            position = end
        elif upper == 'Q':
            x1, y1, x, y = take(4)
            end = (x0 + x, y0 + y)
            current.extend(quadratic(position, (x0 + x1, y0 + y1), end, 8))
            position = end
    if current:
        paths.append((current, False))
    return paths


def _paint_fill(page, spans, gray):
    if gray is None or gray >= 255:
        return
    for row, x0, x1 in spans:
        if not 0 <= row < page.height:
            continue
        x0, x1 = max(0, x0), min(page.width, x1)
        if x1 <= x0:
            continue
        if gray == 0:
            page.span(row, x0, x1)
            continue
        thresholds = BAYER[row % 8]
        target = page.rows[row]
        for x in range(x0, x1):
            if gray * 64 < (thresholds[x % 8] * 2 + 1) * 128:
                target[x] = 1


def svg_background(data):
    """Draw an SVG template on a fax page; returns (bitmap, width in points, height in points)."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise ImportProblem('The SVG template is not valid SVG.') from None
    if root.tag not in ('svg', SVG + 'svg'):
        raise ImportProblem('The template must be a PDF or an SVG file.')
    box = [Fraction(value) for value in _NUMBER.findall(root.get('viewBox') or '')]
    width_pt = _length(root.get('width'))
    height_pt = _length(root.get('height'))
    if len(box) != 4:
        if width_pt is None or height_pt is None:
            raise ImportProblem('The SVG template needs a width and height or a viewBox.')
        box = [Fraction(0), Fraction(0), width_pt / UNITS['px'], height_pt / UNITS['px']]
    if box[2] <= 0 or box[3] <= 0:
        raise ImportProblem('The SVG template has no drawing area.')
    if width_pt is None:
        width_pt = box[2] * UNITS['px']
    if height_pt is None:
        height_pt = width_pt * box[3] / box[2]
    points_per_unit = width_pt / box[2]
    height_dots = math.floor(height_pt * model.Y_DPI / 72 + Fraction(1, 2))
    if not 100 <= height_dots <= model.MAX_PAGE_HEIGHT:
        raise ImportProblem('The SVG template has a page size Faxbot cannot fax.')
    across = Fraction(model.PAGE_WIDTH) / box[2]
    down = points_per_unit * model.Y_DPI / 72
    page = Bitmap(model.PAGE_WIDTH, height_dots)
    stroke_scale = (across + down) / 2

    def dots(point, matrix):
        a, b, c, d, e, f = matrix
        x, y = point
        return ((a * x + c * y + e - box[0]) * across, (b * x + d * y + f - box[1]) * down)

    def draw(paths, style, matrix):
        fill_gray = _gray(style.get('fill', 'black'))
        stroke_gray = _gray(style.get('stroke'))
        if fill_gray is not None:
            edges = []
            for points, _ in paths:
                if len(points) >= 3:
                    edges.extend(polygon_edges([dots(point, matrix) for point in points]))
            _paint_fill(page, fill(edges), fill_gray)
        if stroke_gray is not None:
            width = _length(style.get('stroke-width', '1')) / UNITS['px'] * abs(matrix[0]) * stroke_scale
            width = max(Fraction(1), width)
            for points, closed in paths:
                placed = [dots(point, matrix) for point in points] + ([dots(points[0], matrix)] if closed else [])
                for (x0, y0), (x1, y1) in zip(placed, placed[1:]):
                    _paint_fill(page, fill(thick_line(x0, y0, x1, y1, width)), stroke_gray)

    def text(element, style, matrix):
        from .renderer import _draw_line, missing_characters
        words = ''.join(element.itertext()).strip()
        if not words:
            return
        if missing_characters(words):
            raise ImportProblem('The SVG template has text the form font cannot print.')
        size_units = _length(style.get('font-size', '16')) / UNITS['px']
        size = max(model.MIN_FONT, round(size_units * points_per_unit * abs(matrix[0])))
        x, y = dots((Fraction(_NUMBER.findall(element.get('x') or '0')[0]),
                     Fraction(_NUMBER.findall(element.get('y') or '0')[0])), matrix)
        from .renderer import text_width
        anchor = style.get('text-anchor', 'start')
        width = text_width(words, size)
        left = math.floor(x) - (width if anchor == 'end' else width // 2 if anchor == 'middle' else 0)
        _draw_line(page, words, left, math.floor(y), size, (0, 0, page.width, page.height))

    def walk(element, style, matrix):
        tag = element.tag.replace(SVG, '')
        style = _style(element, style)
        matrix = _transform(element.get('transform'), matrix)
        number = lambda key, default='0': Fraction(_NUMBER.findall(element.get(key) or default)[0])
        if tag in ('svg', 'g'):
            for child in element:
                walk(child, style, matrix)
        elif tag == 'rect':
            x, y, w, h = number('x'), number('y'), number('width'), number('height')
            draw([([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True)], style, matrix)
        elif tag == 'line':
            draw([([(number('x1'), number('y1')), (number('x2'), number('y2'))], False)],
                 {**style, 'fill': 'none'}, matrix)
        elif tag in ('polyline', 'polygon'):
            values = [Fraction(value) for value in _NUMBER.findall(element.get('points') or '')]
            points = list(zip(values[0::2], values[1::2]))
            draw([(points, tag == 'polygon')], style if tag == 'polygon' else {**style, 'fill': style.get('fill', 'none')},
                 matrix)
        elif tag in ('circle', 'ellipse'):
            cx, cy = number('cx'), number('cy')
            rx = number('r') if tag == 'circle' else number('rx')
            ry = number('r') if tag == 'circle' else number('ry')
            draw([([(cx + rx * cos, cy + ry * sin) for cos, sin in CIRCLE], True)], style, matrix)
        elif tag == 'path':
            draw(_path(element.get('d')), style, matrix)
        elif tag == 'text':
            text(element, style, matrix)
        elif tag in ('title', 'desc', 'metadata', 'defs', 'style'):
            return
        elif tag in ('image', 'use', 'foreignObject', 'pattern', 'linearGradient', 'radialGradient', 'clipPath', 'mask'):
            raise ImportProblem(f'The SVG template uses a {tag} element Faxbot cannot draw; save it as a PDF and import that.')

    walk(root, {}, (1, 0, 0, 1, 0, 0))
    return page, width_pt, height_pt
