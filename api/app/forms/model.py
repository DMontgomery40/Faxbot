"""Registered forms: the field schema, typed values and the content address.

A form version is content: its page backgrounds (bilevel rasters at 204 by 196
dots per inch, stored once at import), its typed fields with their positions,
and its resolution. The SHA-256 of that content in canonical JSON is the
form's address; partners use the address to agree on exactly which form a set
of values fills. The title and version number travel beside the content but
are labels, not part of the address.

Values are always normalized before they are drawn or hashed, so a value
typed two ways ("1" and "1.00") fills a form the same way on both ends.
"""
import base64
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import unicodedata


FORMAT = 1
X_DPI, Y_DPI = 204, 196
PAGE_WIDTH = 1728  # dots across every fax page (T.4, 215 mm at 8 dots per millimetre)
MAX_PAGE_HEIGHT = 4000  # dots; a legal-size page is about 2744
MAX_PAGES = 20
MAX_FIELDS = 400
FIELD_TYPES = ('text', 'date', 'checkbox', 'choice', 'number', 'signature')
DATE_FORMATS = ('MM/DD/YYYY', 'DD/MM/YYYY', 'YYYY-MM-DD')
ALIGNMENTS = ('left', 'center', 'right')
MIN_FONT, MAX_FONT, DEFAULT_FONT = 6, 36, 10
MAX_TEXT = 2000
MAX_SIGNATURE = (1200, 400)  # dots
_HEX64 = re.compile(r'[a-f0-9]{64}')


class FormError(ValueError):
    """A form or its values do not fit the rules; the message is one plain sentence."""


class FormValueError(FormError):
    """Values that do not fit their form; ``problems`` holds one sentence for each field."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(self.problems[0] if len(self.problems) == 1
                         else f'{len(self.problems)} fields need attention: ' + ' '.join(self.problems))


def canonical(document):
    return json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def text(value):
    """Text as it is drawn and hashed: Unicode NFC, lines separated by \\n only."""
    return unicodedata.normalize('NFC', str(value)).replace('\r\n', '\n').replace('\r', '\n')


def _name(value):
    name = text(value).strip() if isinstance(value, str) else ''
    if not 0 < len(name) <= 120 or any(unicodedata.category(c)[0] == 'C' for c in name):
        raise FormError('Each field needs a name of 1 to 120 printable characters.')
    return name


def _int(value, low, high, what):
    if type(value) is not int or not low <= value <= high:
        raise FormError(f'{what} must be a whole number from {low} to {high}.')
    return value


def _box(value, page, what):
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(type(v) is not int for v in value):
        raise FormError(f'The field {what} needs a position: x, y, width and height in dots.')
    x, y, width, height = value
    if x < 0 or y < 0 or width < 4 or height < 4 or x + width > page['width'] or y + height > page['height']:
        raise FormError(f'The field {what} does not fit on its page.')
    return [x, y, width, height]


def field(raw, pages):
    """One field in canonical form, with every key its type uses and no others."""
    if not isinstance(raw, dict):
        raise FormError('Each field must be described by its name, type, page and position.')
    name = _name(raw.get('name'))
    kind = raw.get('type')
    if kind not in FIELD_TYPES:
        raise FormError(f'The field {name} has an unknown type; use one of: {", ".join(FIELD_TYPES)}.')
    page = _int(raw.get('page', 1), 1, len(pages), f'The page of field {name}')
    label = raw.get('label', name)
    label = text(label).strip()[:200] if isinstance(label, str) and label.strip() else name
    result = {'name': name, 'label': label, 'type': kind, 'page': page,
              'box': _box(raw.get('box'), pages[page - 1], name), 'required': raw.get('required', False) is True}
    if kind in ('text', 'date', 'choice', 'number'):
        result['font_size'] = _int(raw.get('font_size', DEFAULT_FONT), MIN_FONT, MAX_FONT, f'The text size of {name}')
        align = raw.get('align', 'right' if kind == 'number' else 'left')
        if align not in ALIGNMENTS:
            raise FormError(f'The field {name} must be aligned left, center or right.')
        result['align'] = align
    if kind == 'text':
        result['multiline'] = raw.get('multiline', False) is True
        limit = raw.get('max_length')
        result['max_length'] = None if limit is None else _int(limit, 1, MAX_TEXT, f'The longest value for {name}')
    elif kind == 'date':
        chosen = raw.get('format', 'MM/DD/YYYY')
        if chosen not in DATE_FORMATS:
            raise FormError(f'The date field {name} must use one of: {", ".join(DATE_FORMATS)}.')
        result['format'] = chosen
    elif kind == 'number':
        result['decimals'] = _int(raw.get('decimals', 0), 0, 6, f'The decimal places of {name}')
    elif kind == 'choice':
        options = raw.get('options')
        if (not isinstance(options, list) or not 0 < len(options) <= 100
                or any(not isinstance(option, str) or not option.strip() for option in options)):
            raise FormError(f'The choice field {name} needs a list of options.')
        options = [text(option).strip()[:200] for option in options]
        if len(set(options)) != len(options):
            raise FormError(f'The choice field {name} lists an option twice.')
        result['options'] = options
        boxes = raw.get('option_boxes') or {}
        if not isinstance(boxes, dict) or any(text(key).strip() not in options for key in boxes):
            raise FormError(f'The choice field {name} places an option it does not list.')
        result['option_boxes'] = {text(key).strip(): _box(value, pages[page - 1], name)
                                  for key, value in sorted(boxes.items())}
    return result


def content(pages, fields):
    """A form version's addressed content from page descriptions and raw fields."""
    if not isinstance(pages, list) or not 0 < len(pages) <= MAX_PAGES:
        raise FormError(f'A form has from 1 to {MAX_PAGES} pages.')
    checked_pages = []
    for page in pages:
        if (not isinstance(page, dict) or set(page) != {'width', 'height', 'background'}
                or page['width'] != PAGE_WIDTH or type(page['height']) is not int
                or not 100 <= page['height'] <= MAX_PAGE_HEIGHT or not _HEX64.fullmatch(str(page['background']))):
            raise FormError('A form page must be a fax page width with its background.')
        checked_pages.append(dict(page))
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise FormError(f'A form has at most {MAX_FIELDS} fields.')
    checked = [field(raw, checked_pages) for raw in fields]
    names = [item['name'] for item in checked]
    if len(set(names)) != len(names):
        raise FormError('Two fields have the same name; each field needs its own name.')
    return {'faxbot_form': FORMAT, 'resolution': {'x': X_DPI, 'y': Y_DPI}, 'pages': checked_pages, 'fields': checked}


def check_content(document):
    """Validate content received from elsewhere (a partner); returns it unchanged when it is canonical."""
    if not isinstance(document, dict) or set(document) != {'faxbot_form', 'resolution', 'pages', 'fields'} \
            or document['faxbot_form'] != FORMAT or document['resolution'] != {'x': X_DPI, 'y': Y_DPI}:
        raise FormError('This is not a Faxbot form.')
    rebuilt = content(document['pages'], document['fields'])
    if canonical(rebuilt) != canonical(document):
        raise FormError('This is not a Faxbot form in its exact form.')
    return rebuilt


def address(document):
    return sha256(canonical(document))


# Values ----------------------------------------------------------------------------------------

TRUE = {'true', 'yes', 'on', '1', 'x', 'checked'}
FALSE = {'false', 'no', 'off', '0', ''}


def _empty(value):
    return value is None or (isinstance(value, str) and not value.strip())


def signature_value(width, height, packed):
    return {'width': width, 'height': height, 'bits': base64.b64encode(packed).decode('ascii')}


def signature_from_picture(picture, name):
    """A PNG, JPEG or GIF picture (base64, or a data: address) as a signature value: trimmed, at most
    1200 by 400 dots, black where the picture is darker than mid-grey."""
    from io import BytesIO
    from PIL import Image, UnidentifiedImageError
    if not isinstance(picture, str):
        raise FormError(f'The signature for {name} must be a picture.')
    if picture.startswith('data:'):
        picture = picture.split(',', 1)[-1]
    try:
        data = base64.b64decode(picture, validate=True)
        if len(data) > 5 * 1024 * 1024:
            raise ValueError
        with Image.open(BytesIO(data)) as image:
            if image.format not in ('PNG', 'JPEG', 'GIF') or image.width * image.height > 25_000_000:
                raise ValueError
            gray = image.convert('RGBA')
            paper = Image.new('RGBA', gray.size, (255, 255, 255, 255))
            gray = Image.alpha_composite(paper, gray).convert('L')
    except (ValueError, UnidentifiedImageError, OSError):
        raise FormError(f'The signature for {name} must be a PNG, JPEG or GIF picture.') from None
    box = gray.point(lambda value: 255 if value < 128 else 0).getbbox()
    if box is None:
        raise FormError(f'The signature picture for {name} is blank.')
    gray = gray.crop(box)
    scale = min(1, MAX_SIGNATURE[0] / gray.width, MAX_SIGNATURE[1] / gray.height)
    if scale < 1:
        gray = gray.resize((max(1, int(gray.width * scale)), max(1, int(gray.height * scale))), Image.LANCZOS)
    width, height = gray.size
    raw = gray.tobytes()
    rows = bytearray()
    for y in range(height):
        bits = ''.join('1' if value < 128 else '0' for value in raw[y * width:(y + 1) * width])
        bits += '0' * ((-width) % 8)
        rows += int(bits, 2).to_bytes(len(bits) // 8, 'big')
    return signature_value(width, height, bytes(rows))


def check_signature(value, name):
    if not isinstance(value, dict) or set(value) != {'width', 'height', 'bits'}:
        raise FormError(f'The signature for {name} must be a picture.')
    width, height = value['width'], value['height']
    if (type(width) is not int or type(height) is not int or not 1 <= width <= MAX_SIGNATURE[0]
            or not 1 <= height <= MAX_SIGNATURE[1] or not isinstance(value['bits'], str)):
        raise FormError(f'The signature for {name} is too large or empty.')
    try:
        packed = base64.b64decode(value['bits'], validate=True)
    except ValueError:
        raise FormError(f'The signature for {name} is not a picture Faxbot can read.') from None
    if len(packed) != (width + 7) // 8 * height or base64.b64encode(packed).decode('ascii') != value['bits']:
        raise FormError(f'The signature for {name} is not a picture Faxbot can read.')
    return {'width': width, 'height': height, 'bits': value['bits']}


def value_for(item, raw, *, drawable):
    """One value in canonical form; ``drawable(text)`` names characters the form font lacks."""
    name, kind = item['label'], item['type']
    if kind == 'checkbox':
        if isinstance(raw, bool):
            return raw
        word = str(raw).strip().lower()
        if word in TRUE:
            return True
        if word in FALSE:
            return False
        raise FormError(f'{name} must be checked or not (yes or no).')
    if kind == 'signature':
        if isinstance(raw, dict) and set(raw) == {'picture'}:
            # A picture is turned into black and white dots once, by the sender; the dots are what is sent.
            raw = signature_from_picture(raw['picture'], name)
        return check_signature(raw, name)
    if not isinstance(raw, (str, int)) or isinstance(raw, bool):
        raise FormError(f'{name} must be text.')
    value = text(raw).strip() if kind != 'text' else text(raw)
    if kind == 'text':
        if not item['multiline'] and '\n' in value:
            raise FormError(f'{name} must be a single line.')
        limit = item['max_length'] or MAX_TEXT
        if len(value) > limit:
            raise FormError(f'{name} can be at most {limit} characters long.')
        if any(unicodedata.category(c)[0] == 'C' and c != '\n' for c in value):
            raise FormError(f'{name} contains a character that cannot be printed.')
    elif kind == 'date':
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is None or not re.fullmatch(r'\d{4}-\d\d-\d\d', value):
            raise FormError(f'{name} must be a date written as year-month-day, for example 2026-10-07.')
        value = parsed.isoformat()
    elif kind == 'number':
        try:
            number = Decimal(value.replace(',', ''))
        except InvalidOperation:
            number = None
        if number is None or not number.is_finite():
            raise FormError(f'{name} must be a number.')
        places = item['decimals']
        quantum = Decimal(1).scaleb(-places)
        if number != number.quantize(quantum):
            raise FormError(f'{name} can have at most {places} decimal places.')
        number = number.quantize(quantum)
        value = f'{number:f}'
        if value.startswith('-0') and number == 0:
            value = value[1:]
        if len(value) > 40:
            raise FormError(f'{name} is too long a number.')
    elif kind == 'choice':
        if value not in item['options']:
            raise FormError(f'{name} must be one of: {", ".join(item["options"])}.')
    missing = drawable(value) if kind in ('text', 'choice') else ''
    if missing:
        raise FormError(f'{name} uses characters the form font cannot print: {missing}.')
    return value


def values(document, raw, *, drawable):
    """Normalize values for a form's content; raises FormValueError listing every problem."""
    if not isinstance(raw, dict):
        raise FormValueError(['The values must name each field.'])
    fields = {item['name']: item for item in document['fields']}
    problems = [f'This form has no field named {key}.' for key in sorted(raw) if key not in fields]
    result = {}
    for item in document['fields']:
        given = raw.get(item['name'])
        if _empty(given) or (item['type'] == 'checkbox' and given is False):
            if item['required'] and item['type'] != 'checkbox':
                problems.append(f'{item["label"]} is required.')
            continue
        try:
            value = value_for(item, given, drawable=drawable)
        except FormError as error:
            problems.append(str(error))
            continue
        if item['type'] == 'checkbox' and value is False:
            continue
        if item['type'] == 'text' and not value.strip():
            continue
        result[item['name']] = value
    if problems:
        raise FormValueError(problems)
    return result


def shown(item, value):
    """The text a typed value prints as (dates in the field's format; never the reader's locale)."""
    if item['type'] == 'date':
        year, month, day = value.split('-')
        return {'MM/DD/YYYY': f'{month}/{day}/{year}', 'DD/MM/YYYY': f'{day}/{month}/{year}',
                'YYYY-MM-DD': value}[item['format']]
    return value
