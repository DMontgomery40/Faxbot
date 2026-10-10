"""When a line's copper closes: French commune dates from Orange's trajectory file, and carrier notice dates (N16).

France closes its copper network, and the switched telephone network with it,
commune by commune. ARCEP (collectivities page, updated 27 January 2026, read
2026-10-08) lists lot 3 (2,097 communes) closing in January 2027, lot 4
(6,843) through 2028 and lot 5 (10,488) through 2029, with all lines closed by
the end of 2030; most communes closed commercially at the end of January 2026
and the rest close commercially at the end of January 2027. Dates move:
Orange's file of 19 December 2025 moved 8,095 communes back a year.

Orange publishes the commune-level "fichier trajectoire" in its media library
(gallery.orange.com, element 410038, linked from ARCEP's page on Orange's
closure plan), behind a browser check, so Faxbot cannot fetch it itself. The
economy ministry republishes it under the Open Licence 2.0
(data.gouv.fr "Fermeture du réseau cuivre"; export
https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/fermeture-reseau-cuivre/exports/csv,
downloaded 2026-10-10: 35,305 communes, dataset modified 2025-10-20). Its
columns, read from that file: ``code_insee``, ``nom_commune``,
``fermeture_technique``, ``fermeture_commerciale``, ``lot`` (and postal code,
department, region, geometry and a sentence for residents, which Faxbot does
not keep). Both copies import the same way: any CSV (comma or semicolon) with
an INSEE code column and the two date columns, under those names or the
French labels Orange uses ("Code INSEE", "Date de fermeture commerciale",
"Date de fermeture technique"). The file's own date is entered with it, and
Orange's own file wins over the government copy of the same date or older.

Each site in your organization's rules may carry its commune code
(``commune``); a French number placed at that site's account shows the
commune's commercial and technical closure dates, with the file and its
date. For lines whose carrier closes them by letter (Canada, Israel and Italy
publish no usable schedule), a carrier notice date per line gives the same
warning. Before a date, run the receipt test (Numbers, moving a number) from
another route, so you know faxes still arrive once the line moves.

Advice only: nothing here moves, ports or cancels a line.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import io
import re
from uuid import uuid4

import sqlalchemy as sa


ORANGE, GOUV = 'orange', 'gouv'
SOURCES = (ORANGE, GOUV)
GOUV_EXPORT = ('https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/fermeture-reseau-cuivre/exports/csv'
               '?select=code_insee,nom_commune,fermeture_technique,fermeture_commerciale,lot')
ORANGE_PAGE = 'https://gallery.orange.com/element?id=410038'
ARCEP_PAGE = ('https://www.arcep.fr/demarches-et-services/collectivites/'
              'la-fermeture-du-reseau-cuivre-quels-enjeux-pour-la-connectivite-de-mon-territoire.html')
# Warn this long before a closure: a year for a scheduled commune, and always for a carrier's notice, which is short
# (Israel: at least three months).
WARN_DAYS = 365
MAX_ROWS = 60_000
_INSEE = re.compile(r'(?:[0-9]{2}|2[AB])[0-9]{3}')
_COLUMNS = {
    'insee': ('code_insee', 'code insee', 'insee', 'code commune insee', 'code_commune_insee', 'code_commune'),
    'commune': ('nom_commune', 'commune', 'nom de la commune', 'libelle commune'),
    'commercial': ('fermeture_commerciale', 'date de fermeture commerciale', 'date_fermeture_commerciale',
                   'fermeture commerciale'),
    'technical': ('fermeture_technique', 'date de fermeture technique', 'date_fermeture_technique',
                  'fermeture technique'),
    'lot': ('lot', 'numero de lot', 'numéro de lot'),
}


class ClosureFileError(ValueError):
    """A closure file Faxbot cannot read; the message is one plain sentence."""


class NoticeError(ValueError):
    """A carrier notice Faxbot cannot record; one plain sentence."""


@dataclass(frozen=True)
class Closure:
    code_insee: str
    commune: str | None
    lot: str | None
    commercial: date | None
    technical: date | None
    source: str = GOUV
    source_url: str | None = None
    file_date: date | None = None


def _date(text):
    text = str(text or '').strip()
    if not text:
        return None
    for pattern in ('%Y-%m-%d', '%d/%m/%Y', '%Y-%m-%dT%H:%M:%S', '%d-%m-%Y'):
        try:
            return datetime.strptime(text[:19] if 'T' in text else text[:10], pattern).date()
        except ValueError:
            continue
    return None


def parse_file(text, *, source=GOUV):
    """(closures, skipped) from a trajectory CSV; ``skipped`` counts lines without a readable INSEE code."""
    if source not in SOURCES:
        raise ClosureFileError('Choose where the file comes from: orange or gouv.')
    if not isinstance(text, str) or not text.strip():
        raise ClosureFileError('The file is empty.')
    text = text.lstrip('﻿')
    first = text.splitlines()[0]
    delimiter = ';' if first.count(';') > first.count(',') else ','
    csv.field_size_limit(10_000_000)  # the government copy carries each commune's outline in one column
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = [column.strip().lower() for column in next(reader)]
    at = {}
    for key, names in _COLUMNS.items():
        found = next((header.index(name) for name in names if name in header), None)
        if found is not None:
            at[key] = found
    if not {'insee', 'commercial', 'technical'} <= set(at):
        raise ClosureFileError('The file needs a commune INSEE code and both closure dates (commercial and '
                               'technical). Use Orange\'s trajectory file or the government copy on data.gouv.fr.')
    found, skipped, seen = [], 0, set()
    for line in reader:
        if not line or len(line) <= max(at.values()):
            continue
        code = line[at['insee']].strip().upper()
        if _INSEE.fullmatch(code) is None or code in seen:
            skipped += 1
            continue
        seen.add(code)
        found.append(Closure(code, line[at['commune']].strip()[:200] or None if 'commune' in at else None,
                             line[at['lot']].strip()[:40] or None if 'lot' in at else None,
                             _date(line[at['commercial']]), _date(line[at['technical']]), source))
        if len(found) > MAX_ROWS:
            raise ClosureFileError(f'The file has more than {MAX_ROWS:,} communes.')
    if not found:
        raise ClosureFileError('The file has no commune Faxbot can read.')
    return found, skipped


# -- stored files ------------------------------------------------------------------------------------------------------

_TYPES = {'file_date': sa.DateTime(), 'commercial_closure': sa.DateTime(), 'technical_closure': sa.DateTime(),
          'superseded_at': sa.DateTime(), 'created_at': sa.DateTime(), 'closes_on': sa.DateTime(),
          'notice_received_on': sa.DateTime()}


def _light(name, columns):
    return sa.table(name, *(sa.column(column, _TYPES.get(column, sa.String())) for column in columns))


CLOSURES = _light('copper_closures', ('id', 'import_id', 'source', 'source_url', 'file_date', 'code_insee', 'commune',
                                      'lot', 'commercial_closure', 'technical_closure', 'superseded_at', 'imported_by',
                                      'imported_by_name', 'created_at'))
NOTICES = _light('line_notices', ('id', 'number', 'carrier', 'closes_on', 'notice_received_on', 'state', 'note',
                                  'recorded_by', 'recorded_by_name', 'created_at'))


def _datetime(value):
    return datetime(value.year, value.month, value.day) if value else None


def _day(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.date() if isinstance(value, datetime) else value


def import_file(engine, closures, *, source, source_url=None, file_date=None, actor=None, now=None):
    """Make ``closures`` the current file from ``source``; that source's earlier rows are superseded, never changed."""
    from .database import utcnow, write_transaction
    if source_url is not None and re.fullmatch(r'https?://\S+', source_url) is None:
        raise ClosureFileError('The source must be a web address.')
    now = now or utcnow().replace(microsecond=0)
    import_id, actor = uuid4().hex, actor or {}
    with write_transaction(engine) as connection:
        connection.execute(CLOSURES.update().where(CLOSURES.c.source == source, CLOSURES.c.superseded_at.is_(None))
                           .values(superseded_at=now))
        batch = []
        for item in closures:
            batch.append({'id': uuid4().hex, 'import_id': import_id, 'source': source, 'source_url': source_url,
                          'file_date': _datetime(file_date), 'code_insee': item.code_insee, 'commune': item.commune,
                          'lot': item.lot, 'commercial_closure': _datetime(item.commercial),
                          'technical_closure': _datetime(item.technical), 'superseded_at': None,
                          'imported_by': actor.get('id'), 'imported_by_name': actor.get('name'), 'created_at': now})
            if len(batch) >= 5_000:
                connection.execute(CLOSURES.insert(), batch)
                batch = []
        if batch:
            connection.execute(CLOSURES.insert(), batch)
    return files(engine)


def files(engine):
    """The current file of each source: its date, where it came from and how many communes it lists."""
    from .database import read_connection
    with read_connection(engine) as connection:
        rows = connection.execute(
            sa.select(CLOSURES.c.source, CLOSURES.c.source_url, CLOSURES.c.file_date, CLOSURES.c.created_at,
                      CLOSURES.c.imported_by_name, sa.func.count().label('communes'))
            .where(CLOSURES.c.superseded_at.is_(None))
            .group_by(CLOSURES.c.source, CLOSURES.c.source_url, CLOSURES.c.file_date, CLOSURES.c.created_at,
                      CLOSURES.c.imported_by_name)).mappings().all()
    return [{'source': row['source'], 'source_url': row['source_url'],
             'file_date': _day(row['file_date']).isoformat() if row['file_date'] else None,
             'imported_at': row['created_at'].isoformat() if hasattr(row['created_at'], 'isoformat') else
             row['created_at'], 'imported_by': row['imported_by_name'], 'communes': int(row['communes'])}
            for row in rows]


def closure_for(engine, code_insee):
    """The commune's closure from the preferred current file (the newest; Orange's own when dates tie), or None."""
    from .database import read_connection
    if not code_insee:
        return None
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(CLOSURES).where(
            CLOSURES.c.code_insee == str(code_insee).upper(), CLOSURES.c.superseded_at.is_(None))).mappings().all()
    if not rows:
        return None
    row = max(rows, key=lambda item: (_day(item['file_date']) or date.min, item['source'] == ORANGE))
    return Closure(row['code_insee'], row['commune'], row['lot'], _day(row['commercial_closure']),
                   _day(row['technical_closure']), row['source'], row['source_url'], _day(row['file_date']))


# -- carrier notices ---------------------------------------------------------------------------------------------------

def _canonical(number):
    from .numbers import InvalidNumber, canonical_number
    try:
        return canonical_number(str(number or '').strip())
    except InvalidNumber:
        return None


def _after_newest(connection, number, now):
    newest = connection.execute(sa.select(sa.func.max(NOTICES.c.created_at)).where(NOTICES.c.number == number)).scalar()
    if isinstance(newest, str):
        newest = datetime.fromisoformat(newest)
    return max(now, newest + timedelta(microseconds=1)) if newest is not None else now


def record_notice(engine, number, *, closes_on, carrier=None, received_on=None, note=None, actor=None, now=None):
    """Record a carrier's notice that ``number``'s line closes on ``closes_on``. Earlier notices stay as history."""
    from .database import utcnow, write_transaction
    canonical = _canonical(number)
    if canonical is None:
        raise NoticeError('Enter the line\'s number with its country code, such as +14165550100.')
    if not isinstance(closes_on, date):
        raise NoticeError('Enter the date the carrier says the line closes, such as 2026-11-04.')
    if carrier is not None and len(carrier) > 100 or note is not None and len(note) > 2000:
        raise NoticeError('Keep the carrier to 100 characters and the note to 2,000.')
    actor = actor or {}
    with write_transaction(engine) as connection:
        connection.execute(NOTICES.insert().values(
            id=uuid4().hex, number=canonical, carrier=(carrier or '').strip() or None, closes_on=_datetime(closes_on),
            notice_received_on=_datetime(received_on), state='active', note=(note or '').strip() or None,
            recorded_by=actor.get('id'), recorded_by_name=actor.get('name'),
            created_at=_after_newest(connection, canonical, now or utcnow())))
    return notices(engine).get(canonical)


def remove_notice(engine, number, *, actor=None, now=None):
    from .database import utcnow, write_transaction
    canonical = _canonical(number)
    current = notices(engine).get(canonical) if canonical else None
    if current is None:
        raise NoticeError('This line has no carrier notice.')
    actor = actor or {}
    with write_transaction(engine) as connection:
        connection.execute(NOTICES.insert().values(
            id=uuid4().hex, number=canonical, carrier=current['carrier'], closes_on=None, notice_received_on=None,
            state='removed', note=None, recorded_by=actor.get('id'), recorded_by_name=actor.get('name'),
            created_at=_after_newest(connection, canonical, now or utcnow())))
    return current


def notices(engine):
    """{number: the active notice} (the newest row per number, when it is not removed)."""
    from .database import read_connection
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(NOTICES).order_by(NOTICES.c.created_at, NOTICES.c.id)).mappings().all()
    newest = {}
    for row in rows:
        newest[row['number']] = row
    return {number: {'number': number, 'carrier': row['carrier'],
                     'closes_on': _day(row['closes_on']).isoformat() if row['closes_on'] else None,
                     'notice_received_on': _day(row['notice_received_on']).isoformat()
                     if row['notice_received_on'] else None,
                     'note': row['note'], 'recorded_by': row['recorded_by_name']}
            for number, row in newest.items() if row['state'] == 'active'}


# -- what each line and site shows ---------------------------------------------------------------------------------------

def _when(day):
    return f'{day.day} {day:%B %Y}'


def _warning(closes, today, what):
    """One sentence about a closing date: passed, within the warning time, or later; None without a date."""
    if closes is None:
        return None, None
    left = (closes - today).days
    if left < 0:
        return 'passed', f'{what} closed on {_when(closes)}.'
    if left <= WARN_DAYS:
        return 'soon', (f'{what} closes on {_when(closes)}. Before then, run a receipt test from another route, so you '
                        'know faxes to this number still arrive once the line moves.')
    return 'later', f'{what} closes on {_when(closes)}.'


def _site_of(values, account_key, sites):
    from .origin_rates import account_site
    return account_site(values, account_key, sites)


def view(engine, values, *, today=None):
    """Sites with a commune, your French numbers at those sites, and every line with a carrier notice."""
    from ..accounts import all_accounts
    from .origin_rates import organization_sites
    today = today or datetime.utcnow().date()
    sites = organization_sites(engine)
    site_rows, by_site = [], {}
    for key, site in sites.items():
        code = str(site.get('commune') or '').upper() or None
        if not code:
            continue
        found = closure_for(engine, code)
        by_site[key] = found
        state, sentence = _warning(found.technical if found else None, today,
                                   f'Copper in {found.commune or code}' if found else '')
        site_rows.append({'site': key, 'name': site.get('name') or key, 'commune': code,
                          'closure': _closure_view(found), 'state': state if found else 'unknown',
                          'sentence': sentence if found else (
                              f'No imported closure file lists commune {code}. Import Orange\'s trajectory file, or '
                              'see ARCEP\'s page for your commune.')})
    lines, notice_by_number = [], notices(engine)
    seen = set()
    for account in all_accounts(values):
        site = _site_of(values, account.key, sites)
        for number in account.numbers:
            if number in seen:
                continue
            seen.add(number)
            found = by_site.get(site) if site and number.startswith('+33') else None
            notice = notice_by_number.get(number)
            if found is None and notice is None:
                continue
            lines.append(_line(number, account.label, site, found, notice, today))
    for number, notice in notice_by_number.items():
        if number not in seen:
            lines.append(_line(number, None, None, None, notice, today))
    return {'sites': site_rows, 'lines': lines, 'files': files(engine),
            'sources': {'orange': ORANGE_PAGE, 'gouv': GOUV_EXPORT, 'arcep': ARCEP_PAGE}}


def _closure_view(found):
    if found is None:
        return None
    return {'commune': found.commune, 'code_insee': found.code_insee, 'lot': found.lot,
            'commercial': found.commercial.isoformat() if found.commercial else None,
            'technical': found.technical.isoformat() if found.technical else None, 'source': found.source,
            'source_url': found.source_url, 'file_date': found.file_date.isoformat() if found.file_date else None}


def _line(number, account, site, found, notice, today):
    sentences, states = [], []
    if found is not None:
        state, sentence = _warning(found.technical, today, f'Copper in {found.commune or found.code_insee}')
        if sentence:
            sentences.append(sentence)
            states.append(state)
        if found.commercial and found.commercial >= today:
            sentences.append(f'Orange stops selling copper lines there on {_when(found.commercial)}.')
    if notice is not None and notice.get('closes_on'):
        state, sentence = _warning(date.fromisoformat(notice['closes_on']), today,
                                   f'{notice.get("carrier") or "The carrier"} says this line')
        sentences.append(sentence)
        states.append(state)
    order = ('passed', 'soon', 'later')
    return {'number': number, 'account': account, 'site': site, 'closure': _closure_view(found), 'notice': notice,
            'state': next((item for item in order if item in states), None), 'sentences': sentences}
