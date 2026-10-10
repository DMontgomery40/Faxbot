"""Read a table from a spreadsheet file someone gives Faxbot: an Excel workbook (.xlsx) or CSV, standard library only.

Carriers and fax servers publish their lists as Excel workbooks (AT&T's Discontinued TDM Service Areas workbook is
one), and administrators keep line inventories in them. ``table`` finds the first sheet that has a header row
naming every wanted column, skips any title rows above it, and returns the header and the rows below as text.
Excel stores dates as day numbers; ``excel_date`` turns one into a date, honouring the workbook's 1904 setting.

A workbook is a zip of XML parts. Each part Faxbot reads is limited in size before it is opened, so a crafted file
cannot fill memory, and Python's XML parser neither fetches external entities nor expands nested ones without
limit. CSV files may use commas, semicolons or tabs.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
import io
import re
import zipfile
import xml.etree.ElementTree as ET


MAIN = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
RELATIONSHIP = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
PACKAGE_RELATIONSHIP = '{http://schemas.openxmlformats.org/package/2006/relationships}Relationship'
# The largest part Faxbot opens: AT&T's 100,512-row location sheet is about 30 MB of XML.
MAX_PART = 256 * 1024 * 1024
MAX_ROWS = 500_000
HEADER_SEARCH = 25
_CELL = re.compile(r'([A-Z]{1,3})([0-9]+)')


class WorkbookError(ValueError):
    """A file Faxbot cannot read as a table; the message is one plain sentence."""


def is_workbook(data):
    return isinstance(data, (bytes, bytearray)) and bytes(data[:4]) == b'PK\x03\x04'


def normal(name):
    """A column name as Faxbot compares it: lower case, one space between words, no line breaks."""
    return ' '.join(str(name or '').replace('_', ' ').split()).strip().lower()


def excel_date(value, *, date1904=False):
    """The date an Excel day number stands for, or None."""
    try:
        days = float(str(value).strip())
    except ValueError:
        return None
    if not 1 <= days < 2_958_466:
        return None
    return (date(1904, 1, 1) if date1904 else date(1899, 12, 30)) + timedelta(days=int(days))


def _part(archive, name):
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART:
        raise WorkbookError('The workbook has a sheet larger than Faxbot reads (256 MB). Save the sheet as CSV.')
    return archive.open(info)


def _shared_strings(archive):
    handle = _part(archive, 'xl/sharedStrings.xml')
    if handle is None:
        return []
    found = []
    with handle:
        for _, element in ET.iterparse(handle):
            if element.tag == MAIN + 'si':
                found.append(''.join(text.text or '' for text in element.iter(MAIN + 't')))
                element.clear()
    return found


def _sheets(archive):
    """[(sheet name, part name)] in the workbook's order, and whether it counts days from 1904."""
    handle = _part(archive, 'xl/workbook.xml')
    if handle is None:
        raise WorkbookError('The file is not an Excel workbook Faxbot can read. Save it as .xlsx or CSV.')
    with handle:
        root = ET.parse(handle).getroot()
    properties = root.find(MAIN + 'workbookPr')
    date1904 = properties is not None and properties.get('date1904') in ('1', 'true')
    targets = {}
    relations = _part(archive, 'xl/_rels/workbook.xml.rels')
    if relations is not None:
        with relations:
            for item in ET.parse(relations).getroot().iter(PACKAGE_RELATIONSHIP):
                target = item.get('Target') or ''
                target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
                targets[item.get('Id')] = target
    sheets = []
    for sheet in root.iter(MAIN + 'sheet'):
        part = targets.get(sheet.get(RELATIONSHIP))
        if part:
            sheets.append((sheet.get('name') or '', part))
    return sheets, date1904


def _column(reference):
    found = _CELL.match(reference or '')
    if found is None:
        return None
    number = 0
    for letter in found.group(1):
        number = number * 26 + ord(letter) - 64
    return number - 1


def _rows(archive, part, shared):
    """Each row of one sheet as a list of text (None for an empty cell)."""
    handle = _part(archive, part)
    if handle is None:
        return
    with handle:
        for _, element in ET.iterparse(handle):
            if element.tag != MAIN + 'row':
                continue
            cells, position = {}, 0
            for cell in element.iter(MAIN + 'c'):
                at = _column(cell.get('r'))
                position = at if at is not None else position
                kind, value = cell.get('t'), cell.find(MAIN + 'v')
                if kind == 's' and value is not None and value.text is not None:
                    index = int(value.text)
                    text = shared[index] if 0 <= index < len(shared) else None
                elif kind == 'inlineStr':
                    text = ''.join(item.text or '' for item in cell.iter(MAIN + 't'))
                else:
                    text = value.text if value is not None else None
                cells[position] = text
                position += 1
            element.clear()
            yield [cells.get(index) for index in range(max(cells) + 1)] if cells else []


def _header_at(rows, wanted):
    """(index, header) of the first row naming every wanted column within the first rows, else (None, None)."""
    for index, row in enumerate(rows[:HEADER_SEARCH]):
        names = [normal(cell) for cell in row]
        if all(any(name in names for name in options) for options in wanted):
            return index, names
    return None, None


def _workbook_table(archive, wanted, sheet_hint):
    sheets, date1904 = _sheets(archive)
    if sheet_hint:
        sheets.sort(key=lambda item: normal(item[0]) != normal(sheet_hint))
    shared = None
    for _, part in sheets:
        rows = []
        if shared is None:
            shared = _shared_strings(archive)
        for row in _rows(archive, part, shared):
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise WorkbookError(f'The sheet has more than {MAX_ROWS:,} rows.')
        at, header = _header_at(rows, wanted)
        if at is not None:
            return header, rows[at + 1:], date1904, at + 2
    raise WorkbookError('No sheet in the workbook has the columns Faxbot needs.')


def table(data, wanted, *, sheet_hint=None):
    """(header, rows, date1904, first row number) of the first table that names every wanted column.

    ``wanted`` is a sequence of alternatives per required column (already ``normal``), such as
    ``[('wire center',), ('effective date',)]``. A workbook's sheets are tried in order (``sheet_hint``'s first,
    by its name ignoring case and spaces); CSV text is one table.
    """
    if is_workbook(data):
        try:
            archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
        except zipfile.BadZipFile:
            raise WorkbookError('The file is not an Excel workbook Faxbot can read. Save it as .xlsx or CSV.') from None
        try:
            with archive:
                return _workbook_table(archive, wanted, sheet_hint)
        except (ET.ParseError, zipfile.BadZipFile, ValueError) as error:
            if isinstance(error, WorkbookError):
                raise
            raise WorkbookError('The workbook is damaged. Open it in Excel and save it again, or save the sheet as '
                                'CSV.') from None
    text = data.decode('utf-8-sig', errors='replace') if isinstance(data, (bytes, bytearray)) else str(data or '')
    text = text.lstrip('﻿')
    if not text.strip():
        raise WorkbookError('The file is empty.')
    first = text.splitlines()[0]
    delimiter = max((',', ';', '\t'), key=first.count)
    rows = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        rows.append([cell if cell.strip() else None for cell in row])
        if len(rows) > MAX_ROWS:
            raise WorkbookError(f'The file has more than {MAX_ROWS:,} rows.')
    at, header = _header_at(rows, wanted)
    if at is None:
        raise WorkbookError('The file has no header row with the columns Faxbot needs.')
    return header, rows[at + 1:], False, at + 2
