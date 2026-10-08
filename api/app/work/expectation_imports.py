"""Importing open work from another system as expected faxes, and reconciling after its outage (CE3, CE6).

An import source is saved once: its format (CSV with a header row, or JSON: a
list of objects or ``{"rows": [...]}``) and which column holds each of
Faxbot's fields. Each row's identity is the source, its operation ID (the
reference when no column is named) and its revision, and the import follows
the generic import's identity rules (``imports.identity_outcome``):

- an identical row is a replay: nothing is added, and a replayed file resumes
  the same run;
- a changed row under the same revision is a conflict: the first version is
  kept and the change waits for a person;
- a new revision opens a new expectation linked to the earlier one, which is
  replaced if it was still waiting (a matched one stays matched);
- an expectation a later full export no longer lists is reported as missing and
  stays open; nothing cancels it on that account.

Outage recovery: while a source system is down, the administrator records what
was done by fax or email against each item's original ID. The first export
imported after the outage ends is sorted into three lists: already done (record
it, do not submit it again), new (submit normally) and unresolved (held for a
person). Faxbot never sends, resends or submits anything here; it produces the
lists and keeps them unchanged.
"""
import csv
from datetime import datetime, time
import hashlib
import io
import json
import re

import sqlalchemy as sa

from ..access.receiving_rules import normalize_subaddress
from ..routing.numbers import stored_number
from .expectations import OPEN_STATES, ExpectationChanged, details_json, loads, reference_key, same_revision
from .imports import ImportInputError, identity_outcome, parse_time


FIELDS = ('reference', 'operation_id', 'revision', 'kind', 'description', 'counterparty', 'fax_numbers',
          'direct_address', 'due', 'mailbox', 'subaddress', 'email_subject', 'message_id', 'required_parts',
          'required_revision', 'window_start')
FORMATS = ('csv', 'json')
PLACEHOLDERS = ('{reference}', '{operation_id}', '{revision}', '{digits}')
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20000
MAX_PROBLEMS = 200
BATCH = 100
DEFAULT_KIND = 'Reply'
_LOCAL = re.compile(r'(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?')
_LIMITS = {'reference': 200, 'operation_id': 100, 'revision': 40, 'kind': 100, 'description': 500,
           'counterparty': 200, 'direct_address': 320, 'email_subject': 200, 'message_id': 512,
           'required_revision': 40}


class RowProblem(ValueError):
    """One plain sentence about a row."""


def _plain(value, name, limit):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise RowProblem(f'{name} must be text.')
    text = ' '.join(str(value).split())
    if not text:
        return None
    if len(text) > limit or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise RowProblem(f'{name} can be up to {limit} characters of plain text.')
    return text


def clean_template(value, name):
    """An admin pattern such as "PO {reference}": plain text with Faxbot's placeholders only."""
    text = _plain(value, name, 200) if value not in (None, '') else None
    if text is None:
        return None
    for placeholder in re.findall(r'\{[^}]*\}', text):
        if placeholder not in PLACEHOLDERS:
            raise ImportInputError(f'{name} can use {", ".join(PLACEHOLDERS)}; {placeholder} is not one of them.')
    if '{' in re.sub(r'\{[a-z_]+\}', '', text) or '}' in re.sub(r'\{[a-z_]+\}', '', text):
        raise ImportInputError(f'{name} has an unmatched brace.')
    return text


def fill(template, row):
    """The template with this row's values; None when it is empty."""
    if not template:
        return None
    digits = ''.join(c for c in row.get('reference') or '' if c.isdigit())
    text = template
    for placeholder, value in (('{reference}', row.get('reference')), ('{operation_id}', row.get('operation_id')),
                               ('{revision}', row.get('revision')), ('{digits}', digits)):
        text = text.replace(placeholder, value or '')
    text = ' '.join(text.split())
    return text or None


def clean_mapping(mapping):
    """{Faxbot field: column} with a reference column; raises ImportInputError with a plain sentence."""
    if not isinstance(mapping, dict) or not mapping:
        raise ImportInputError('Choose which column holds each field; the business reference is required.')
    unknown = sorted(set(mapping) - set(FIELDS))
    if unknown:
        raise ImportInputError(f"Faxbot does not use these fields: {', '.join(unknown)}.")
    clean = {}
    for field, column in mapping.items():
        if column in (None, ''):
            continue
        if not isinstance(column, str) or len(column.strip()) == 0 or len(column) > 100:
            raise ImportInputError(f'The column for {field} must be a column name of up to 100 characters.')
        clean[field] = column.strip()
    if 'reference' not in clean:
        raise ImportInputError('Choose the column that holds the business reference, such as the PO number.')
    return clean


def parse_rows(data, fmt):
    """[{column: value}] from a CSV with a header row or a JSON list; raises ImportInputError."""
    if not data:
        raise ImportInputError('The file is empty.')
    if len(data) > MAX_FILE_BYTES:
        raise ImportInputError(f'The file is larger than the {MAX_FILE_BYTES // (1024 * 1024)} MB limit.')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ImportInputError('The file is not UTF-8 text; save it as UTF-8 and try again.') from None
    if fmt == 'csv':
        try:
            reader = csv.DictReader(io.StringIO(text, newline=''))
            if not reader.fieldnames:
                raise ImportInputError('The file has no header row naming its columns.')
            rows = []
            for row in reader:
                if len(rows) >= MAX_ROWS:
                    raise ImportInputError(f'The file has more than {MAX_ROWS} rows; split it and import each part.')
                rows.append({(key or '').strip(): value for key, value in row.items() if key is not None})
        except csv.Error:
            raise ImportInputError('The file is not valid CSV.') from None
        return rows
    try:
        document = json.loads(text)
    except ValueError:
        raise ImportInputError('The file is not valid JSON.') from None
    if isinstance(document, dict) and isinstance(document.get('rows'), list):
        document = document['rows']
    if not isinstance(document, list) or not all(isinstance(row, dict) for row in document):
        raise ImportInputError('The JSON file must be a list of objects, or an object with a "rows" list.')
    if len(document) > MAX_ROWS:
        raise ImportInputError(f'The file has more than {MAX_ROWS} rows; split it and import each part.')
    return document


def read_time(value, zone, *, end_of_day):
    """A time with its offset, or a local date or date and time in the installation's zone; naive UTC."""
    if value in (None, ''):
        return None
    text = str(value).strip()
    try:
        moment = parse_time(text)
        if moment is not None:
            return moment
    except ImportInputError:
        pass
    match = _LOCAL.fullmatch(text)
    if match is None:
        raise RowProblem('Write times as 2026-10-12, 2026-10-12 14:00 or 2026-10-12T14:00:00-06:00.')
    year, month, day, hour, minute, second = match.groups()
    try:
        if hour is None:
            local = datetime.combine(datetime(int(year), int(month), int(day)).date(),
                                     time(23, 59, 59) if end_of_day else time(0, 0))
        else:
            local = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
    except ValueError:
        raise RowProblem(f'{text} is not a real date.') from None
    from datetime import timezone
    return local.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)


def normalize_row(raw, mapping, source, *, country, zone, mailboxes):
    """The row as Faxbot stores it; ``mailboxes`` maps folded labels to ids. Raises RowProblem."""
    def get(field):
        column = mapping.get(field)
        return raw.get(column) if column else None

    values = {field: _plain(get(field), field.replace('_', ' '), limit) for field, limit in _LIMITS.items()}
    if not values['reference']:
        raise RowProblem('It has no business reference.')
    values['operation_id'] = values['operation_id'] or values['reference'][:100]
    values['revision'] = values['revision'] or ''
    values['kind'] = values['kind'] or DEFAULT_KIND
    label = _plain(get('mailbox'), 'mailbox', 100)
    if label:
        mailbox = mailboxes.get(reference_key(label))
        if mailbox is None:
            raise RowProblem(f'There is no mailbox called {label}.')
    else:
        mailbox = source['mailbox_id']
        if mailbox is None:
            raise RowProblem('It names no mailbox, and the import source has no mailbox set.')
    values['mailbox_id'] = mailbox
    numbers = []
    for part in re.split(r'[;,]', str(get('fax_numbers') or '')):
        part = part.strip()
        if part:
            if len(part) > 40:
                raise RowProblem(f'{part[:40]} is not a fax number.')
            numbers.append(stored_number(part, country=country))
    values['fax_numbers'] = sorted(set(numbers))[:20]
    parts = [' '.join(part.split()) for part in re.split(r';', str(get('required_parts') or ''))]
    values['required_parts'] = [part[:100] for part in parts if part][:10]
    sub = _plain(get('subaddress'), 'subaddress', 40)
    if sub:
        values['subaddress'] = normalize_subaddress(sub)
        if values['subaddress'] is None:
            raise RowProblem('A subaddress is up to 20 digits, such as 4831; it may also use +, # and *.')
    else:
        values['subaddress'] = (normalize_subaddress(fill(source['subaddress_template'], values) or '')
                                if source['subaddress_template'] else normalize_subaddress(values['reference']))
    values['email_subject'] = values['email_subject'] or fill(source['subject_template'], values)
    due = read_time(get('due'), zone, end_of_day=True)
    start = read_time(get('window_start'), zone, end_of_day=False)
    values['due'] = due.isoformat(timespec='seconds') if due else None
    values['window_start'] = start.isoformat(timespec='seconds') if start else None
    return values


def row_digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
                          .encode('ascii')).hexdigest()


def expectation_values(row, source, *, run_id, digest, now):
    """The expectation's columns from a normalized row (the due time is filled in by the caller)."""
    return {
        'reference': row['reference'], 'kind': row['kind'], 'description': row['description'],
        'required_parts': json.dumps(row['required_parts']) if row['required_parts'] else None,
        'required_revision': row['required_revision'], 'counterparty': row['counterparty'],
        'counterparty_numbers': json.dumps(row['fax_numbers']) if row['fax_numbers'] else None,
        'direct_address': row['direct_address'], 'mailbox_id': row['mailbox_id'],
        'subaddress_key': row['subaddress'], 'subject_key': row['email_subject'], 'message_key': row['message_id'],
        'form_field': source['form_field'], 'revision_field': source['revision_field'],
        'source_key': source['id'], 'operation_id': row['operation_id'], 'revision': row['revision'],
        'row_digest': digest, 'last_import_id': run_id,
        'window_start': datetime.fromisoformat(row['window_start']) if row['window_start'] else now,
    }


# -- outage reconciliation ---------------------------------------------------------------

def delivered_on(connection, engine, job_id):
    """Whether a sent fax is confirmed delivered (its delivery record says success)."""
    from ..routing.database import reflect
    tables = reflect(engine, ('outbound_deliveries', 'fax_jobs'))
    state = connection.execute(sa.select(tables['outbound_deliveries'].c.state).where(
        tables['outbound_deliveries'].c.id == job_id)).scalar_one_or_none()
    if state is not None:
        return state == 'success'
    status = connection.execute(sa.select(tables['fax_jobs'].c.status).where(
        tables['fax_jobs'].c.id == job_id)).scalar_one_or_none()
    return (status or '').lower() in ('success', 'completed', 'completed_ok', 'delivered')


def action_view(row):
    return {'operation_id': row['operation_id'], 'revision': row['revision'] or None,
            'reference': row['reference'], 'action': row['action'], 'channel': row['channel'],
            'outcome': row['outcome'], 'fax_job_id': row['fax_job_id'], 'evidence_note': row['evidence_note'],
            'occurred_at': row['occurred_at'].isoformat(timespec='seconds'), 'recorded_by': row['recorded_by_name']}


def reconcile_on(connection, store, outage, run, source, *, now, actor_id=None, actor_name=None):
    """Sort one import run against an outage's recorded actions; store and return the reconciliation's id."""
    rows = loads(run['rows'], []) or []
    actions = [dict(row) for row in connection.execute(sa.select(store.actions).where(
        store.actions.c.outage_id == outage['id']).order_by(store.actions.c.occurred_at, store.actions.c.id))
        .mappings()]
    by_operation = {}
    for action in actions:
        by_operation.setdefault(action['operation_id'], []).append(action)
    until = outage['ended_at'] or now
    expectations = store.expectations
    known = {row.operation_id: row for row in connection.execute(sa.select(
        expectations.c.operation_id, expectations.c.code, expectations.c.mailbox_id, expectations.c.state,
        expectations.c.matched_at).where(expectations.c.source_key == source['id'])
        .order_by(expectations.c.created_at))}
    answered = {operation for operation, row in known.items() if row.state == 'matched' and row.matched_at
                and outage['started_at'] <= row.matched_at <= until}
    done, new, unresolved = [], [], []
    listed = set()
    for operation, revision, _digest, mailbox_id, reference in rows:
        listed.add(operation)
        entry = {'operation_id': operation, 'revision': revision or None, 'reference': reference,
                 'mailbox_id': mailbox_id, 'code': known[operation].code if operation in known else None}
        found = by_operation.get(operation, [])
        if not found:
            (unresolved if operation in answered else new).append(dict(entry, reason='answered' if operation in
                                                                         answered else 'new'))
            continue
        entry['actions'] = [action_view(action) for action in found]
        action = found[0]
        if len(found) > 1:
            reason = 'several'
        elif action['outcome'] == 'uncertain':
            reason = 'uncertain'
        elif action['fax_job_id'] and not delivered_on(connection, store.engine, action['fax_job_id']):
            reason = 'fax_not_confirmed'
        elif action['revision'] and not same_revision(action['revision'], revision):
            reason, entry['recorded_revision'] = 'revision', action['revision']
        else:
            reason = 'done'
        (done if reason == 'done' else unresolved).append(dict(entry, reason=reason))
    for operation, found in by_operation.items():
        if operation in listed:
            continue
        place = known.get(operation)
        unresolved.append({'operation_id': operation, 'revision': found[0]['revision'] or None,
                           'reference': found[0]['reference'], 'code': place.code if place else None,
                           'mailbox_id': place.mailbox_id if place else source['mailbox_id'],
                           'actions': [action_view(action) for action in found], 'reason': 'not_in_export'})
    from uuid import uuid4
    identity = uuid4().hex
    connection.execute(store.reconciliations.insert().values(
        id=identity, outage_id=outage['id'], import_id=run['id'],
        already_done=json.dumps(done, sort_keys=True), new_items=json.dumps(new, sort_keys=True),
        unresolved=json.dumps(unresolved, sort_keys=True), already_done_count=len(done), new_count=len(new),
        unresolved_count=len(unresolved), created_by=actor_id, created_by_name=actor_name, created_at=now))
    return identity


# -- applying an import ------------------------------------------------------------------

def apply_row(connection, store, row, digest, *, run, source, now, actor_id, actor_name, installation_hours):
    """Apply one normalized row; returns 'created', 'unchanged', 'revised' or 'conflict'."""
    expectations = store.expectations
    existing = connection.execute(sa.select(expectations).where(
        expectations.c.source_key == source['id'], expectations.c.operation_id == row['operation_id'],
        expectations.c.revision == row['revision'])).mappings().one_or_none()
    existing = dict(existing) if existing is not None else None
    earlier = [dict(item) for item in connection.execute(sa.select(expectations).where(
        expectations.c.source_key == source['id'], expectations.c.operation_id == row['operation_id'],
        expectations.c.revision != row['revision']).order_by(expectations.c.created_at.desc())).mappings()]
    outcome = identity_outcome(existing['row_digest'] if existing else None, digest, earlier_revision=bool(earlier))
    if outcome == 'duplicate':
        connection.execute(expectations.update().where(expectations.c.id == existing['id'])
                           .values(last_import_id=run['id']))
        if existing['missing_since'] is not None:
            existing['last_import_id'] = run['id']
            store.change_on(connection, existing, {'missing_since': None}, kind='back_in_export', source='import',
                            now=now, evidence={'import_id': run['id']})
        return 'unchanged'
    if outcome == 'conflict':
        key = 'conflict:' + digest[:48]
        seen = connection.execute(sa.select(store.events.c.id).where(
            store.events.c.expectation_id == existing['id'], store.events.c.dedupe_key == key)).first()
        connection.execute(expectations.update().where(expectations.c.id == existing['id'])
                           .values(last_import_id=run['id']))
        if seen is None:
            existing['last_import_id'] = run['id']
            store.change_on(connection, existing, {'conflict_at': now}, kind='conflict', source='import', now=now,
                            details={'row': row, 'digest': digest}, evidence={'import_id': run['id']},
                            dedupe_key=key)
        return 'conflict'
    values = expectation_values(row, source, run_id=run['id'], digest=digest, now=now)
    due = datetime.fromisoformat(row['due']) if row['due'] else None
    values['due_at'], values['due_hours'], values['due_source'] = store.due_for(
        connection, row['mailbox_id'], start=values['window_start'], due_at=due,
        due_hours=source['due_hours'] or None, due_source='import' if due else 'source',
        installation_hours=installation_hours)
    if outcome == 'revision':
        latest = earlier[0]
        values['replaces_id'] = latest['id']
        if latest['state'] in OPEN_STATES:
            # Replace the earlier revision first: if the matcher changed it meanwhile, nothing is written.
            store.change_on(connection, latest, {'state': 'replaced', 'closed_at': now}, kind='replaced',
                            source='import', now=now, details={'revision': row['revision'] or None},
                            evidence={'import_id': run['id']})
            store.settle_proposals_on(connection, latest['id'], now=now, keep=None)
        created = store.create_on(connection, values, source='import', now=now, actor_id=actor_id,
                                  actor_name=actor_name,
                                  details={'source_name': source['name'], 'revision': row['revision'],
                                           'replaces': latest['code']},
                                  evidence={'import_id': run['id'], 'replaces_id': latest['id']})
        store.event_on(connection, created['id'], 'revised', source='import', now=now,
                       details={'revision': row['revision'] or None, 'replaces': latest['code']},
                       evidence={'import_id': run['id'], 'replaces_id': latest['id']})
        return 'revised'
    store.create_on(connection, values, source='import', now=now, actor_id=actor_id, actor_name=actor_name,
                    details={'source_name': source['name'], 'revision': row['revision'] or None},
                    evidence={'import_id': run['id']})
    return 'created'


def missing_on(connection, store, source, run, seen, *, now):
    """Expectations still waiting that this full export no longer lists; reported, never cancelled."""
    expectations = store.expectations
    rows = [dict(row) for row in connection.execute(sa.select(expectations).where(
        expectations.c.source_key == source['id'], expectations.c.state.in_(OPEN_STATES))
        .order_by(expectations.c.created_at, expectations.c.id)).mappings()]
    missing = []
    for row in rows:
        if (row['operation_id'], row['revision']) in seen:
            continue
        if row['missing_since'] is None:
            try:
                store.change_on(connection, row, {'missing_since': now}, kind='missing_from_export',
                                source='import', now=now, evidence={'import_id': run['id']},
                                dedupe_key='missing:' + run['id'])
            except ExpectationChanged:
                continue  # A received fax or a person changed it meanwhile; the next full export looks again.
        missing.append({'code': row['code'], 'reference': row['reference'], 'mailbox_id': row['mailbox_id'],
                        'missing_since': (row['missing_since'] or now).isoformat(timespec='seconds')})
    return missing


def run_summary(problems, missing, note=None):
    return details_json({'problems': problems[:MAX_PROBLEMS], 'missing': missing[:2000], 'note': note}, 2_000_000)
