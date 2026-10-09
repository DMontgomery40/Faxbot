"""A recipient's checklist, and the packet Faxbot builds from it, deterministically.

A checklist lists the document types a recipient asks for. Each item may need
a version (for example "final") and a date within some days of the day the
packet is built, and is required or optional. Every change is a new version;
versions are never changed, so a packet always names exactly what it followed.

Building picks, for each item in order, one unchanged original already kept
in the case: the newest dated document of that type that meets the item's
version and date, not already picked for an earlier item. Ties fall to the
version label and then the document's bytes (SHA-256), never to upload order,
so the same checklist, originals and day always give the same packet. Each
pick says why; each item without a pick is listed as missing, with why.

Suggestions are separate and off unless the installation turns them on
(``case_suggestions_enabled``). Faxbot's own suggester compares the words of a
missing item with document titles; a suggestion is never picked by itself,
and only the selection a person confirms is sent.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from uuid import uuid4

import sqlalchemy as sa

from ..routing.database import read_connection, reflect, utcnow, write_transaction
from .ledger import CaseInputError, clean


MAX_ITEMS = 50
MAX_WITHIN_DAYS = 3650
EXAMPLE_NAME = 'Example: discharge follow-up'
# Synthetic, for trying the feature; real recipients' checklists belong to integrations.
EXAMPLE_ITEMS = (
    {'type': 'Discharge summary', 'required': True, 'within_days': 30, 'version': 'final'},
    {'type': 'Medication list', 'required': True, 'within_days': 30, 'version': None},
    {'type': 'Lab results', 'required': False, 'within_days': 90, 'version': None},
    {'type': 'Insurance card', 'required': True, 'within_days': None, 'version': None},
)


def norm(value):
    return ' '.join(str(value or '').split()).casefold()


def parse_items(raw):
    """Checklist items from JSON-shaped input, cleaned, or a plain-sentence refusal."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ITEMS:
        raise CaseInputError(f'A checklist needs between 1 and {MAX_ITEMS} items.')
    items, seen = [], set()
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or set(item) - {'type', 'required', 'within_days', 'version'}:
            raise CaseInputError(f'Item {number} must have a type, and may say required, within_days and version.')
        kind = clean(item.get('type'), 100)
        if not kind:
            raise CaseInputError(f'Item {number} needs a document type, such as "Discharge summary".')
        required = item.get('required', True)
        if not isinstance(required, bool):
            raise CaseInputError(f'Item {number}: say whether it is required with true or false.')
        within = item.get('within_days')
        if within is not None and (isinstance(within, bool) or not isinstance(within, int)
                                   or not 1 <= within <= MAX_WITHIN_DAYS):
            raise CaseInputError(f'Item {number}: the date limit must be from 1 to {MAX_WITHIN_DAYS} days.')
        version = clean(item.get('version'), 64) or None
        key = (norm(kind), norm(version), within)
        if key in seen:
            raise CaseInputError(f'Item {number} repeats an earlier item.')
        seen.add(key)
        items.append({'type': kind, 'required': required, 'within_days': within, 'version': version})
    return items


def check_name(value):
    name = clean(value, 100)
    if not name:
        raise CaseInputError('Give the checklist a name, such as the recipient and the request.')
    return name


def long_date(moment):
    return f'{moment.day} {moment:%B %Y}'


@dataclass(frozen=True)
class Pick:
    item: int
    original: dict
    reason: str


def _matches(item, original, as_of):
    if norm(original['document_type']) != norm(item['type']):
        return False
    if item['version'] and norm(original['version']) != norm(item['version']):
        return False
    if item['within_days']:
        dated = original['document_date']
        if dated is None:
            return False
        day = dated.date()
        return as_of - timedelta(days=item['within_days']) <= day <= as_of
    return True


def _rank(original):
    """Newest dated first; then the version label, the exact bytes and the source: a total order."""
    dated = original['document_date'] or datetime.min
    return (dated, norm(original['version']), original['digest'], original['source'], original['version'])


def _reason(item, original, as_of):
    parts = []
    if item['version']:
        parts.append(f"version '{original['version']}'")
    if original['document_date'] is not None:
        dated = f"dated {long_date(original['document_date'])}"
        if item['within_days']:
            dated += f", within {item['within_days']} days of {long_date(as_of)}"
        parts.append(dated)
    detail = ', '.join(parts)
    return f"Matches '{item['type']}'" + (f": {detail}." if detail else '.')


def _missing_reason(item, originals, used, as_of):
    typed = [row for row in originals if norm(row['document_type']) == norm(item['type'])]
    if not typed:
        return f"No '{item['type']}' is in the case."
    if item['version'] and not any(norm(row['version']) == norm(item['version']) for row in typed):
        return f"The case has '{item['type']}' but not version '{item['version']}'."
    if item['within_days']:
        dated = sorted((row['document_date'] for row in typed if row['document_date'] is not None), reverse=True)
        if not dated:
            return f"The case's '{item['type']}' has no date, and this item needs one within {item['within_days']} days."
        if not any(_matches(item, row, as_of) for row in typed):
            return (f"The newest '{item['type']}' in the case is dated {long_date(dated[0])}, more than "
                    f"{item['within_days']} days before {long_date(as_of)}.")
    return f"Every matching '{item['type']}' is already used for an earlier item."


def _suggest(item, originals, picked):
    words = [word for word in norm(item['type']).split() if len(word) >= 3]
    if not words:
        return []
    found = []
    for row in originals:
        if row['id'] in picked:
            continue
        text = norm(f"{row['title']} {row['document_type']}")
        if all(word in text for word in words):
            found.append(row)
    found.sort(key=_rank, reverse=True)
    return found[:3]


def build(items, originals, as_of, *, suggestions=False):
    """The deterministic selection for a checklist, the missing items, and (when on) suggestions."""
    ordered = sorted(originals, key=_rank, reverse=True)
    picks, missing, suggested, used = [], [], [], set()
    for index, item in enumerate(items):
        chosen = next((row for row in ordered if row['id'] not in used and _matches(item, row, as_of)), None)
        if chosen is not None:
            used.add(chosen['id'])
            picks.append(Pick(index, chosen, _reason(item, chosen, as_of)))
            continue
        missing.append({'item': index, 'type': item['type'], 'required': item['required'],
                        'reason': _missing_reason(item, originals, used, as_of)})
    if suggestions:
        for entry in missing:
            for row in _suggest(items[entry['item']], ordered, used):
                suggested.append({'item': entry['item'], 'original_id': row['id'], 'title': row['title'],
                                  'reason': f"Its title or type mentions '{items[entry['item']]['type']}'. "
                                            'Check it before you add it.'})
    return picks, missing, suggested


class CaseChecklists:
    TABLES = ('case_checklists', 'case_packets', 'access_principals')

    def __init__(self, engine):
        self.engine = engine
        t = reflect(engine, self.TABLES)
        self.checklists, self.packets, self.principals = t['case_checklists'], t['case_packets'], t['access_principals']

    def _view(self, row, used):
        return {'id': row['id'], 'name': row['name'], 'version': row['version'], 'to': row['recipient'],
                'items': json.loads(row['items']), 'created_at': row['created_at'], 'created_by': row['principal_name'],
                'used': used}

    def _used_on(self, connection, ids):
        if not ids:
            return {}
        p = self.packets
        return dict(connection.execute(sa.select(p.c.checklist_id, sa.func.count()).where(
            p.c.checklist_id.in_(ids)).group_by(p.c.checklist_id)).all())

    def add(self, name, items, *, recipient=None, principal_id=None):
        """Save a new version; earlier versions stay exactly as they were."""
        name, items = check_name(name), parse_items(items)
        c, now = self.checklists, utcnow()
        with write_transaction(self.engine) as connection:
            existing = connection.execute(sa.select(c.c.name, c.c.version).where(
                sa.func.lower(c.c.name) == name.lower()).order_by(c.c.version.desc())).first()
            version = existing.version + 1 if existing else 1
            name = existing.name if existing else name
            who = None
            if principal_id is not None:
                who = connection.scalar(sa.select(self.principals.c.display_name).where(
                    self.principals.c.id == principal_id))
            identity = uuid4().hex
            connection.execute(c.insert().values(
                id=identity, name=name, version=version, recipient=recipient,
                items=json.dumps(items, sort_keys=True, separators=(',', ':')), principal_id=principal_id,
                principal_name=who[:200] if isinstance(who, str) and who else None, created_at=now))
            row = connection.execute(sa.select(c).where(c.c.id == identity)).mappings().one()
            return self._view(row, 0)

    def find(self, name=None, *, version=None, identity=None):
        c = self.checklists
        with read_connection(self.engine) as connection:
            query = sa.select(c)
            if identity is not None:
                query = query.where(c.c.id == identity)
            else:
                query = query.where(sa.func.lower(c.c.name) == check_name(name).lower())
                query = query.where(c.c.version == version) if version is not None else query
            row = connection.execute(query.order_by(c.c.version.desc()).limit(1)).mappings().one_or_none()
            if row is None:
                return None
            return self._view(row, self._used_on(connection, [row['id']]).get(row['id'], 0))

    def versions(self, name):
        c = self.checklists
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(c).where(sa.func.lower(c.c.name) == check_name(name).lower())
                                      .order_by(c.c.version.desc())).mappings().all()
            used = self._used_on(connection, [row['id'] for row in rows])
            return [self._view(row, used.get(row['id'], 0)) for row in rows]

    def latest(self):
        """The newest version of every checklist, by name."""
        c = self.checklists
        newest = sa.select(sa.func.lower(c.c.name).label('key'), sa.func.max(c.c.version).label('version')).group_by(
            sa.func.lower(c.c.name)).subquery()
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(c).join(newest, sa.and_(
                sa.func.lower(c.c.name) == newest.c.key, c.c.version == newest.c.version)).order_by(
                c.c.name)).mappings().all()
            used = self._used_on(connection, [row['id'] for row in rows])
            return [self._view(row, used.get(row['id'], 0)) for row in rows]


def parse_day(value, default):
    if value in (None, ''):
        return default
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except ValueError:
        raise CaseInputError('Write the date as year-month-day, for example 2026-10-07.') from None


def local_today():
    from ..people_time import installation_zone_name, zone
    from datetime import timezone
    return datetime.now(timezone.utc).astimezone(zone(installation_zone_name())).date()
