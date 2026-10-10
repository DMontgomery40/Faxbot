"""Your fax lines matched to carrier discontinuance lists and contract end dates (N19), from synthetic files.

The workbook below copies the structure of AT&T's Discontinued TDM Service Areas workbook as downloaded and read on
2026-10-10 (an instructions sheet, a "Location " sheet with a title row above the header, Excel day numbers for
dates, shared and inline strings, a Table G sheet with its own header), with invented wire centers and areas.
The numbers are 555-01xx numbers. Not yet run against a real customer's inventory or a carrier's newer workbook.
"""
from datetime import date, datetime
import io
import zipfile
from xml.sax.saxutils import escape

import pytest

from api.app.config_values import ConfigurationValues
from api.app.routing import closures, inventory, number_placement
from api.app.routing.workbook import WorkbookError, excel_date, table
from api.tests.test_line_advice import received, seeded
from api.tests.test_schema import database  # noqa: F401 (fixture)


TODAY = date(2026, 10, 10)
NOW = datetime(2026, 10, 10, 12, 0)
FAX_A, FAX_B, FAX_C, ALARM, OTHER_CARRIER, NO_WIRE = (
    '+13035550110', '+13035550111', '+13035550112', '+13035550113', '+13035550114', '+13035550115')
IN_FAXBOT = '+13035550100'
MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
RELS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE = 'http://schemas.openxmlformats.org/package/2006/relationships'


def serial(day):
    return str((day - date(1899, 12, 30)).days)


def _cell(reference, value, shared):
    if value is None:
        return ''
    if isinstance(value, tuple):  # ('inline', text)
        return f'<c r="{reference}" t="inlineStr"><is><t>{escape(value[1])}</t></is></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    if value not in shared:
        shared.append(value)
    return f'<c r="{reference}" t="s"><v>{shared.index(value)}</v></c>'


def _sheet(rows, shared):
    body = []
    for number, row in enumerate(rows, start=1):
        cells = ''.join(_cell(f'{chr(65 + column)}{number}', value, shared) for column, value in enumerate(row))
        body.append(f'<row r="{number}">{cells}</row>')
    return f'<?xml version="1.0"?><worksheet xmlns="{MAIN}"><sheetData>{"".join(body)}</sheetData></worksheet>'


def workbook(sheets):
    """An .xlsx from [(name, rows)], its sheet parts numbered in reverse so only the relationships find them."""
    shared, parts, entries, relations = [], {}, [], []
    for index, (name, rows) in enumerate(sheets, start=1):
        part = f'worksheets/sheet{len(sheets) - index + 1}.xml'
        parts['xl/' + part] = _sheet(rows, shared)
        entries.append(f'<sheet name="{name}" sheetId="{index + 10}" r:id="rId{index}"/>')
        relations.append(f'<Relationship Id="rId{index}" Type="{RELS}/worksheet" Target="{part}"/>')
    strings = ''.join(f'<si><t>{escape(text)}</t></si>' for text in shared)
    files = {
        '[Content_Types].xml': '<?xml version="1.0"?><Types/>',
        'xl/workbook.xml': (f'<?xml version="1.0"?><workbook xmlns="{MAIN}" xmlns:r="{RELS}"><workbookPr/>'
                            f'<sheets>{"".join(entries)}</sheets></workbook>'),
        'xl/_rels/workbook.xml.rels': (f'<?xml version="1.0"?><Relationships xmlns="{PACKAGE}">{"".join(relations)}'
                                       '</Relationships>'),
        'xl/sharedStrings.xml': f'<?xml version="1.0"?><sst xmlns="{MAIN}">{strings}</sst>',
        **parts,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


LOCATION = [
    ['                                  Discontinued TDM Service Areas'],
    ['Region', 'State', 'CITY / TOWNSHIP', 'Development Name', 'Wire Center', 'Distribution Area', 'Effective Date',
     'Ref Table'],
    ['MIDWEST', 'IL', None, '41.000001, -88.000001', 'ZZTSILAA', '1410ZA', int(serial(date(2024, 8, 20))), 'A'],
    ['MIDWEST', 'IL', None, 'TEST RD & SAMPLE LN', 'ZZTSILAA', '3326ZA', int(serial(date(2027, 3, 1))), 'A'],
    ['MIDWEST', 'IL', 'SAMPLEVILLE', None, 'ZZTSILBB', 'ALL', int(serial(date(2025, 3, 16))), 'G'],
    ['SOUTHWEST', 'TX', None, ('inline', '30.1, -97.1'), 'zztstxcc', '2002ZA', int(serial(date(2026, 1, 20))), 'A'],
    ['SOUTHWEST', 'TX', None, None, 'BAD', '2002ZA', int(serial(date(2026, 1, 20))), 'A'],
    # A place code shorter than four letters is padded with a space, as in AT&T's own "ADA MIMN".
    ['MIDWEST', 'MN', None, '45.0, -93.0', 'ZZT MNDD', '1101ZA', int(serial(date(2025, 6, 2))), 'A'],
]
TABLE_G = [
    [None, 'Service Table G'],
    [None, 'ILEC Region', 'Product', 'Part if GB', 'Sect', 'Para', 'FCC Docket #', 'Effective\nDate (on or after)',
     'State', 'Wire Center\nCLLI', 'Wire Center Name'],
    [None, 'Midwest', 'High Capacity Service (1.544 Mbps)', '12', '7', '7.3.10', '23-397',
     int(serial(date(2024, 2, 12))), 'IL', 'ZZTSILBB', 'Sampleville'],
]


def att_workbook():
    return workbook([('Instructions', [['INSTRUCTIONS'], ['1', 'On the "Location" tab...']]), ('Location ', LOCATION),
                     ('Table A', [[None, 'Service Table A']]), ('Table G', TABLE_G)])


INVENTORY = '\n'.join([
    'Number,Service Address,City,State,ZIP,Carrier,USOC,Wire Center,Distribution Area,Contract End,Use,'
    'Monthly Price,Currency',
    '(303) 555-0110,1 Test St,Sampleville,IL,60000,AT&T Illinois,1FB,ZZTSILAA,1410ZA,12/31/2026,Fax,"$34.42",',
    '303-555-0111,2 Test St,Sampleville,IL,60000,Illinois Bell,1FB,ZZTSILBB,,,fax machine,,',
    '3035550112,3 Test St,Sampleville,IL,60000,AT&T,1FB,ZZTSILAA,,3/4/2027,fax,40,USD',
    f'{ALARM},4 Test St,Sampleville,IL,60000,AT&T,1FB,ZZTSILBB,,,fire alarm,,',
    f'{OTHER_CARRIER},5 Test St,Austin,TX,78700,Spectrum,,ZZTSTXCC,2002ZA,,fax,,',
    f'{NO_WIRE},6 Test St,Austin,TX,78700,,,,,,,,',
    f'{IN_FAXBOT},7 Test St,Sampleville,IL,60000,AT&T,1FB,ZZTSILCC,,2028-06-30,fax,25.00,USD',
    f'{FAX_A},duplicate,,,,,,,,,,,',
    'not a number,8 Test St,,,,,,,,,,,',
    '+13035550116,9 Test St,,,,,,,,13/45/2026,,,',
    '+13035550117,10 Test St,,,,,,,,,,12.50,',
    '',
])


# -- the readers -----------------------------------------------------------------------------------------------------

def test_excel_day_numbers_and_the_1904_setting():
    assert excel_date('45525') == date(2024, 8, 21) and excel_date(45364.0) == date(2024, 3, 13)
    assert excel_date('45525', date1904=True) == date(2028, 8, 22)
    assert excel_date('not a day') is None and excel_date('0') is None


def test_us_and_day_first_dates_are_read_as_written():
    assert inventory.parse_day('3/4/2027') == date(2027, 3, 4)
    assert inventory.parse_day('3/4/2027', order='dmy') == date(2027, 4, 3)
    assert inventory.parse_day('31/12/2026') == date(2026, 12, 31)  # a first part above 12 is the day either way
    assert inventory.parse_day('2027-03-04') == inventory.parse_day('46450')
    with pytest.raises(ValueError):
        inventory.parse_day('13/45/2026')


def test_the_att_workbook_is_read_as_published_from_its_location_sheet():
    header, _, _, first = table(att_workbook(), [('wire center',), ('effective date',)], sheet_hint='Location')
    assert first == 3  # the title row and the header come first, as in AT&T's file
    assert header[:8] == ['region', 'state', 'city / township', 'development name', 'wire center',
                          'distribution area', 'effective date', 'ref table']
    parsed = inventory.parse_carrier_list(att_workbook())
    assert (parsed.layout, parsed.carrier, parsed.kind) == ('att_workbook', 'att', 'discontinued')
    by_area = {(item.wire_center, item.distribution_area): item for item in parsed.items}
    assert by_area[('ZZTSILAA', '1410ZA')].effective_on == date(2024, 8, 20)
    # ALL is the whole wire center; the city is kept as AT&T wrote it but never used to match.
    whole = by_area[('ZZTSILBB', None)]
    assert whole.ref_table == 'G' and whole.city == 'SAMPLEVILLE'
    assert by_area[('ZZTSTXCC', '2002ZA')].place == '30.1, -97.1'  # inline string, lower-case code upper-cased
    assert parsed.skipped == ['Row 7: BAD is not an 8-character wire center code.']
    with pytest.raises(inventory.InventoryError, match="AT&T's workbook"):
        inventory.parse_carrier_list(att_workbook(), carrier='Lumen')


def test_another_carriers_list_is_a_documented_csv_and_one_carrier_at_a_time():
    text = ('Carrier,Kind,State,Wire Center,Wire Center Name,Effective Date\n'
            'Lumen,grandfathered,CO,ZZTSCOAA,Sample,11/1/2026\n')
    parsed = inventory.parse_carrier_list(text.encode())
    assert (parsed.layout, parsed.carrier, parsed.kind) == ('csv', 'lumen', 'grandfathered')
    assert parsed.items[0].effective_on == date(2026, 11, 1)
    mixed = text + 'Verizon,discontinued,NY,ZZTSNYAA,Other,11/1/2026\n'
    with pytest.raises(inventory.InventoryError, match='one carrier'):
        inventory.parse_carrier_list(mixed.encode())
    with pytest.raises(inventory.InventoryError, match='Name the carrier'):
        inventory.parse_carrier_list(b'Wire Center,Effective Date\nZZTSCOAA,2026-11-01\n')
    with pytest.raises(inventory.InventoryError, match='wire center and effective date'):
        inventory.parse_carrier_list(b'State,City\nCO,Denver\n')
    with pytest.raises(WorkbookError):
        table(b'PK\x03\x04 not really a zip', [('number',)])


def test_the_inventory_reads_us_numbers_prices_uses_and_counts_what_it_cannot_read():
    parsed = inventory.parse_inventory(INVENTORY.encode(), default_country='US')
    lines = {line.number: line for line in parsed.items}
    assert set(lines) == {FAX_A, FAX_B, FAX_C, ALARM, OTHER_CARRIER, NO_WIRE, IN_FAXBOT}
    first = lines[FAX_A]
    assert (first.contract_end, first.monthly_amount, first.currency, first.use, first.wire_center) == (
        date(2026, 12, 31), '34.42', 'USD', 'fax', 'ZZTSILAA')
    assert lines[FAX_B].use == 'fax' and lines[ALARM].use == 'alarm' and lines[NO_WIRE].use == 'unknown'
    assert lines[FAX_C].contract_end == date(2027, 3, 4) and lines[FAX_B].monthly_amount is None
    assert parsed.skipped_count == 4
    assert any('listed twice' in reason for reason in parsed.skipped)
    assert any('not a phone number' in reason for reason in parsed.skipped)
    assert any('is not a date' in reason for reason in parsed.skipped)
    assert any('needs a currency' in reason for reason in parsed.skipped)
    assert inventory.parse_inventory(INVENTORY.encode(), date_order='dmy').items[2].contract_end == date(2027, 4, 3)
    with pytest.raises(inventory.InventoryError, match='column named number'):
        inventory.parse_inventory(b'Address,City\n1 Test St,Sampleville\n')


# -- matching: by wire center and distribution area only --------------------------------------------------------------

def _row(**changes):
    return {'number': FAX_A, 'carrier': 'AT&T', 'wire_center': 'ZZTSILAA', 'distribution_area': '1410ZA',
            'line_use': 'fax', **changes}


def _areas():
    return [{'carrier': 'att', 'kind': 'discontinued', 'wire_center': item.wire_center,
             'distribution_area': item.distribution_area, 'effective_on': datetime.combine(item.effective_on,
                                                                                           datetime.min.time()),
             'ref_table': item.ref_table, 'source_url': inventory.ATT_WORKBOOK, 'file_date': None}
            for item in inventory.parse_carrier_list(att_workbook()).items]


def _for(wire_center):
    return [row for row in _areas() if row['wire_center'] == wire_center]


def test_a_line_matches_only_by_its_wire_center_and_distribution_area():
    listed = inventory.match(_row(), _for('ZZTSILAA'), TODAY)
    assert (listed['state'], listed['effective_on'], listed['date_state']) == ('listed', '2024-08-20', 'passed')
    assert listed['sentence'].startswith("AT&T's Discontinued TDM Service Areas workbook lists wire center ZZTSILAA, "
                                         'distribution area 1410ZA as discontinued from 20 August 2024')
    future = inventory.match(_row(distribution_area='3326ZA'), _for('ZZTSILAA'), TODAY)
    assert (future['state'], future['date_state']) == ('listed', 'soon')
    whole = inventory.match(_row(wire_center='ZZTSILBB', distribution_area=None), _for('ZZTSILBB'), TODAY)
    assert whole['state'] == 'listed' and '(the whole wire center)' in whole['sentence']
    assert 'Table G' in whole['sentence']
    # Parts of the wire center are listed and the line names none: possible, never a guess.
    possible = inventory.match(_row(distribution_area=None), _for('ZZTSILAA'), TODAY)
    assert possible['state'] == 'possible' and '1410ZA, 3326ZA' in possible['sentence']
    # Its own distribution area is not one of them: no match.
    assert inventory.match(_row(distribution_area='9999ZZ'), _for('ZZTSILAA'), TODAY) is None
    # Another carrier's line is never flagged by AT&T's list; with no carrier the sentence says so.
    assert inventory.match(_row(carrier='Spectrum'), _for('ZZTSILAA'), TODAY) is None
    unsure = inventory.match(_row(carrier=None), _for('ZZTSILAA'), TODAY)
    assert "if this is AT&T's line" in unsure['sentence']
    missing = inventory.match(_row(wire_center=None), [], TODAY)
    assert missing['state'] == 'no_wire_center' and "customer service record" in missing['sentence']
    assert inventory.match(_row(wire_center=None, line_use='alarm'), [], TODAY) is None
    assert inventory.carrier_key('Illinois Bell Telephone') == 'att' and inventory.carrier_key('Lumen') == 'lumen'
    padded = inventory.match(_row(wire_center='ZZTMNDD', distribution_area='1101ZA'), _for('ZZTMNDD'), TODAY)
    assert padded['state'] == 'listed' and inventory._wire_center('ALGNILAQDS0') == 'ALGNILAQ'


# -- imports, notices and the retirement plan --------------------------------------------------------------------------

def _values():
    return ConfigurationValues.from_environment({
        'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': 'sip.telnyx.com',
        'SIP_TRUNK_DIDS': IN_FAXBOT, 'FAX_DEFAULT_COUNTRY': 'US', 'INBOUND_ENABLED': 'true'})


def _import_all(engine, inventory_text=INVENTORY, now=NOW):
    parsed = inventory.parse_inventory(inventory_text.encode())
    inventory.import_inventory(engine, parsed.items, file_name='lines.csv', actor={'name': 'Ada'}, now=now)
    inventory.import_carrier_list(engine, inventory.parse_carrier_list(att_workbook()), file_date=date(2026, 8, 17),
                                  file_name='PrimeAccess.xlsx', actor={'name': 'Ada'}, now=now)


def test_contract_ends_become_notices_beside_a_carriers_letter_and_follow_the_inventory(database):  # noqa: F811
    seeded(database)
    closures.record_notice(database, FAX_A, closes_on=date(2027, 2, 1), carrier='AT&T', received_on=date(2026, 9, 1))
    _import_all(database)
    by_kind = closures.all_notices(database)
    assert by_kind[FAX_A]['contract_end']['closes_on'] == '2026-12-31'
    assert by_kind[FAX_A]['contract_end']['source_label'] == 'Line inventory (lines.csv)'
    assert by_kind[FAX_A]['letter']['closes_on'] == '2027-02-01'  # the letter is not hidden by the contract
    assert closures.notices(database)[FAX_A]['kind'] == 'letter'
    shown = next(line for line in closures.view(database, _values(), today=TODAY)['lines'] if line['number'] == FAX_A)
    assert shown['sentences'][0].startswith('AT&T says this line closes on 1 February 2027.')
    assert shown['sentences'][1].startswith('Its contract with AT&T Illinois ends on 31 December 2026.')
    # The same contract again writes nothing; a changed date writes a row; a dropped one is withdrawn.
    with database.connect() as connection:
        before = connection.exec_driver_sql('SELECT count(*) FROM line_notices').scalar()
    _import_all(database)
    with database.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM line_notices').scalar() == before
    changed = INVENTORY.replace('12/31/2026', '1/31/2027').replace('3/4/2027', '')
    _import_all(database, changed, now=datetime(2026, 10, 11))
    contracts = closures.notices(database, kind='contract_end')
    assert contracts[FAX_A]['closes_on'] == '2027-01-31' and FAX_C not in contracts
    assert len(inventory.inventory_rows(database)) == 7
    with database.connect() as connection:
        assert connection.exec_driver_sql(
            'SELECT count(*) FROM line_inventory WHERE superseded_at IS NOT NULL').scalar() == 14


def test_the_view_lists_dated_lines_first_with_their_source_and_the_lists(database):  # noqa: F811
    seeded(database)
    _import_all(database)
    shown = inventory.view(database, _values(), today=TODAY)
    assert shown['counts']['lines'] == 7 and shown['counts']['listed'] >= 2
    assert shown['lists'][0]['label'] == "AT&T's Discontinued TDM Service Areas workbook"
    assert shown['lists'][0]['source_url'] == inventory.ATT_WORKBOOK and shown['lists'][0]['wire_centers'] == 4
    assert shown['keyed'].startswith("AT&T's workbook lists wire centers")
    first = shown['lines'][0]
    assert first['number'] == FAX_A and first['dates'][0]['kind'] == 'carrier_list'
    assert first['dates'][0]['state'] == 'passed' and first['monthly'] == '$34.42'
    other = next(line for line in shown['lines'] if line['number'] == OTHER_CARRIER)
    assert other['match'] is None and other['dates'] == []
    assert next(line for line in shown['lines'] if line['number'] == NO_WIRE)['match']['state'] == 'no_wire_center'
    assert next(line for line in shown['lines'] if line['number'] == IN_FAXBOT)['in_faxbot']


def test_the_retirement_plan_puts_dated_lines_first_and_adds_inventory_fax_lines(database):  # noqa: F811
    from datetime import timedelta
    routes = seeded(database)
    for day in (2, 5, 9):
        received(database, IN_FAXBOT, NOW - timedelta(days=day), sender=f'+1801555010{day}')
    _import_all(database)
    advice = number_placement.line_advice(database, _values(), routes=routes, now=NOW)
    order = [row['number'] for row in advice['numbers']]
    # Passed first (AT&T discontinued the areas of FAX_A and FAX_B), then soon (FAX_C's contract), then later.
    assert order[:3] == [FAX_A, FAX_B, FAX_C]
    rows = {row['number']: row for row in advice['numbers']}
    assert rows[FAX_A]['verdict'] == 'not_in_faxbot' and rows[FAX_A]['dates'][0]['source'] == inventory.ATT_LABEL
    assert rows[FAX_A]['evidence']['removed_sentence'] == 'Your inventory says it costs $34.42 a month.'
    assert rows[IN_FAXBOT]['verdict'] == 'keep' and rows[IN_FAXBOT]['dates'][0]['kind'] == 'contract_end'
    # A life-safety line needs its own replacement; a line Faxbot cannot date stays out of the plan.
    assert ALARM not in rows and NO_WIRE not in rows and OTHER_CARRIER not in rows
    assert 'have dates set by a carrier or a contract, listed first.' in advice['sentence']
    assert order.index(FAX_C) < order.index(IN_FAXBOT)  # FAX_C is in a soon-discontinued area
