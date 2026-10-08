"""Monthly invoices, and the part of each that Faxbot's faxes don't explain (M27, B2).

Some providers report no charge for each fax: HumbleFax sells a flat monthly
plan, eFax prices by quote with a page allowance, and a carrier's invoice also
carries number rental, taxes and fees no call record names. So the
administrator enters each provider account's invoice total once a month,
optionally with the invoice file, and Faxbot compares it with what it can
attribute to that account's faxes in the same billing period:

- what the provider or carrier reported for each sent fax, received fax or call
  (``delivery_attempt_costs``, ``provider_received_charges``,
  ``carrier_charges``);
- Faxbot's estimate from the account's rate card where nothing was reported;
- the plan's monthly fee, any page overage past an allowance and any unused
  committed spend, by the published rule ``plan_budget`` keeps (the deterministic
  rule M27 names);
- charges for faxes and calls the provider or carrier billed that Faxbot has no
  record of (``provider_unrecorded_faxes``, ``carrier_records``).

The residual is the invoice minus that. It is always shown, never hidden. A
fax with no reported charge and no estimate is counted, never priced at zero,
so the residual is then marked incomplete. Amounts in another currency than
the invoice are left out of the comparison and said so.

A recommendation appears when residuals recur: among an account's last three
invoices, at least two whose comparison is complete have a residual of the same
sign that is at least $1 (one whole unit of the invoice's currency) and 5% of
the invoice. An incomplete month never counts toward it.

Invoices are append-only (``provider_invoices``): entering a total again for
the same account and period adds a version that supersedes the earlier one,
and both are kept. The invoice file is kept on disk under the data directory,
named by its SHA-256, and checked against it when read. Nothing here contacts a
provider or changes a fax.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import re
from uuid import uuid4

import sqlalchemy as sa

from .costs import MICROS, Money, attempt_cost, call_seconds, format_amount, money_text
from .database import DeliveryStoreError, read_connection, reflect, utcnow, write_transaction


RECUR_LOOK_BACK = 3
RECUR_TIMES = 2
RECUR_SHARE = Decimal('0.05')
RECUR_FLOOR_MICROS = MICROS
TOTAL = re.compile(r'([0-9]{1,12})(?:\.([0-9]{1,6}))?', re.ASCII)
MONTH = re.compile(r'([0-9]{4})-([0-9]{2})', re.ASCII)
DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', re.ASCII)
CURRENCY = re.compile(r'[A-Z]{3}', re.ASCII)
DIGEST = re.compile(r'[a-f0-9]{64}', re.ASCII)
# The files an invoice may come as, by their first bytes; CSV is checked as text.
FILE_KINDS = (('application/pdf', b'%PDF-'), ('image/png', b'\x89PNG\r\n\x1a\n'), ('image/jpeg', b'\xff\xd8\xff'))
CSV = 'text/csv'
SUFFIXES = {'application/pdf': '.pdf', 'image/png': '.png', 'image/jpeg': '.jpg', CSV: '.csv'}
MAX_NOTE = 500
MAX_FILE_NAME = 200


class InvoiceInputError(ValueError):
    """One plain sentence for the administrator."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# Input ------------------------------------------------------------------------------------------------------

def parse_total(text):
    """``(canonical text, micros)`` for an invoice total such as "13.20"; a comma or currency sign is refused."""
    value = text.strip() if isinstance(text, str) else str(text) if type(text) is int else None
    match = TOTAL.fullmatch(value or '')
    if match is None:
        raise InvoiceInputError('Enter the invoice total as a number with up to six decimal places, such as 13.20.')
    whole, fraction = match[1], match[2] or ''
    canonical = f'{int(whole)}.{(fraction or "00").ljust(2, "0")}'
    return canonical, int(whole) * MICROS + int((fraction + '000000')[:6])


def total_micros(text):
    """Micros of a stored total; Python integers never overflow."""
    return parse_total(text)[1]


def parse_currency(text):
    value = text.strip().upper() if isinstance(text, str) else ''
    if CURRENCY.fullmatch(value) is None:
        raise InvoiceInputError('Enter the currency as its three-letter code, such as USD.')
    return value


def _day(text, what):
    if not isinstance(text, str) or DAY.fullmatch(text.strip()) is None:
        raise InvoiceInputError(f'Write the {what} as year-month-day, for example 2026-09-30.')
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise InvoiceInputError(f'The {what} is not a real date.') from None


def _zone(values):
    from ..people_time import zone
    return zone(getattr(values, 'time_zone', '') or '')


def _utc(day, tz):
    return datetime.combine(day, time(), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class InvoicePeriod:
    first_day: date
    last_day: date
    start: datetime   # naive UTC, inclusive
    end: datetime     # naive UTC, exclusive


def period_for(values, *, month=None, billing_day=1, first_day=None, last_day=None):
    """The billing period an invoice covers.

    By month ("2026-09"): from the account's billing day in that month to the day before it in the next (a billing
    day past a month's end falls on its last day), as ``plan_budget.billing_period`` counts a plan's month. Or the
    first and last day given, both inclusive. Bounds are midnights in the installation's time zone.
    """
    tz = _zone(values)
    if first_day or last_day:
        if not (first_day and last_day):
            raise InvoiceInputError('Give both the first and the last day the invoice covers.')
        first, last = _day(first_day, 'first day'), _day(last_day, 'last day')
    else:
        match = MONTH.fullmatch(month.strip()) if isinstance(month, str) else None
        if match is None or not 1 <= int(match[2]) <= 12:
            raise InvoiceInputError('Write the invoice month as year-month, for example 2026-09.')
        year, number = int(match[1]), int(match[2])
        day = billing_day if type(billing_day) is int and 1 <= billing_day <= 31 else 1
        first = date(year, number, min(day, calendar.monthrange(year, number)[1]))
        following = (year + 1, 1) if number == 12 else (year, number + 1)
        last = date(*following, min(day, calendar.monthrange(*following)[1])) - timedelta(days=1)
    if last < first:
        raise InvoiceInputError('The last day comes before the first day.')
    if (last - first).days > 92:
        raise InvoiceInputError('An invoice covers at most three months; enter each month on its own.')
    return InvoicePeriod(first, last, _utc(first, tz), _utc(last + timedelta(days=1), tz))


def file_kind(data, name=None):
    """The media type of an invoice file, from its first bytes; refuses anything else."""
    for kind, magic in FILE_KINDS:
        if data.startswith(magic):
            return kind
    if (name or '').lower().endswith('.csv') and b'\x00' not in data:
        try:
            data.decode('utf-8')
        except UnicodeDecodeError:
            pass
        else:
            return CSV
    raise InvoiceInputError('Attach the invoice as a PDF, PNG or JPEG picture, or a CSV file.')


def clean_text(value, limit):
    if value is None:
        return None
    text = ' '.join(str(value).split())[:limit]
    return text or None


# Storage ----------------------------------------------------------------------------------------------------

class InvoiceStore:
    TABLES = ('provider_invoices',)

    def __init__(self, engine, data_dir=None):
        self.engine, self.data_dir = engine, data_dir
        self.table = reflect(engine, self.TABLES)['provider_invoices']

    def _path(self, digest):
        if self.data_dir is None or DIGEST.fullmatch(digest or '') is None:
            raise InvoiceInputError('Invoice files are not available on this installation.', 409)
        return Path(self.data_dir) / 'invoices' / digest

    def _keep(self, data, digest):
        """Keep the exact bytes once, named by their SHA-256; an existing copy must match."""
        path = self._path(digest)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staged = path.with_name(f'.{digest}.{uuid4().hex}.part')
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(staged, path)
        except FileExistsError:
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise InvoiceInputError('A kept invoice file does not match its record; restore it from a backup.',
                                        409) from None
        finally:
            staged.unlink(missing_ok=True)

    def add(self, *, account_key, provider_id, period, total, currency, note=None, file=None, file_name=None,
            entered_by=None, entered_by_name=None, now=None):
        """Record one invoice total; a total for a period already entered becomes its next version."""
        canonical, _ = parse_total(total)
        currency = parse_currency(currency)
        now = (now or utcnow()).replace(microsecond=0)
        digest = kind = None
        if file is not None:
            if not file:
                raise InvoiceInputError('The invoice file is empty.')
            kind = file_kind(file, file_name)
            digest = hashlib.sha256(file).hexdigest()
            self._keep(file, digest)
        table, first = self.table, period.first_day.isoformat()
        with write_transaction(self.engine) as connection:
            current = connection.execute(sa.select(table).where(
                table.c.account_key == account_key, table.c.first_day == first)
                .order_by(table.c.version.desc()).limit(1)).mappings().one_or_none()
            if current is not None and digest is None and current['file_digest'] is not None:
                # A correction of the total keeps the file entered with the earlier version.
                digest, kind = current['file_digest'], current['file_type']
                file_name, size = current['file_name'], current['file_size']
            else:
                size = len(file) if file is not None else None
            row = dict(id=uuid4().hex, account_key=account_key, provider_id=provider_id, first_day=first,
                       last_day=period.last_day.isoformat(), period_start=period.start, period_end=period.end,
                       total=canonical, currency=currency, note=clean_text(note, MAX_NOTE), file_digest=digest,
                       file_name=clean_text(Path(file_name).name if file_name else None, MAX_FILE_NAME) if digest else None,
                       file_type=kind, file_size=size, version=(current['version'] + 1) if current else 1,
                       supersedes_id=current['id'] if current else None, entered_by=entered_by,
                       entered_by_name=clean_text(entered_by_name, 200), created_at=now)
            connection.execute(table.insert().values(**row))
        return row

    def latest(self, account_key=None):
        """The version in effect of each entered invoice, newest period first."""
        table = self.table
        query = sa.select(table)
        if account_key is not None:
            query = query.where(table.c.account_key == account_key)
        with read_connection(self.engine) as connection:
            rows = [dict(row) for row in connection.execute(query.order_by(table.c.version)).mappings()]
        found = {}
        for row in rows:
            found[(row['account_key'], row['first_day'])] = row
        return sorted(found.values(), key=lambda row: (row['first_day'], row['account_key']), reverse=True)

    def get(self, invoice_id):
        table = self.table
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(table).where(table.c.id == invoice_id)).mappings().one_or_none()
        if row is None:
            raise InvoiceInputError('Faxbot has no invoice with this ID.', 404)
        return dict(row)

    def history(self, account_key, first_day):
        table = self.table
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(table).where(
                table.c.account_key == account_key, table.c.first_day == first_day).order_by(table.c.version))
                .mappings()]

    def read_file(self, row):
        if not row.get('file_digest'):
            raise InvoiceInputError('No file was attached to this invoice.', 404)
        path = self._path(row['file_digest'])
        if path.is_symlink() or not path.is_file():
            raise InvoiceInputError('The kept invoice file is missing; attach it again with a corrected total.', 409)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row['file_digest']:
            raise InvoiceInputError('The kept invoice file has changed; attach it again with a corrected total.', 409)
        return data


# What Faxbot attributes to an account's faxes ---------------------------------------------------------------

@dataclass
class Attribution:
    """What one account's faxes account for in one period, in the invoice's currency."""
    currency: str
    reported: int = 0             # micros the provider or carrier reported
    reported_count: int = 0
    estimated: int = 0            # micros Faxbot estimated from the rate card where nothing was reported
    estimated_count: int = 0
    unpriced: int = 0             # faxes and calls with neither: counted, never priced at zero
    included: int = 0             # faxes a plan carries at no extra charge
    sent: int = 0
    received: int = 0
    calls: int = 0                # received calls on a trunk
    plan_fee: int | None = None
    overage: int | None = None    # pages past an allowance, by the published rule
    overage_pages: int = 0
    commitment_gap: int | None = None  # committed spend not used
    over_budget_faxes: int = 0    # faxes past a flat plan's normal-use budget (nothing charged for them)
    budget_faxes: int | None = None
    unrecorded: int = 0           # micros billed for faxes and calls Faxbot has no record of
    unrecorded_count: int = 0
    unrecorded_unpriced: int = 0
    other_currency: dict = field(default_factory=dict)  # {currency: micros} left out of the comparison

    def add(self, amount, *, reported):
        """Count one fax's amount: reported or estimated money, or unknown (None)."""
        if amount is None:
            self.unpriced += 1
            return
        if amount.currency != self.currency:
            self.other_currency[amount.currency] = self.other_currency.get(amount.currency, 0) + amount.micros
            return
        if reported:
            self.reported += amount.micros
            self.reported_count += 1
        else:
            self.estimated += amount.micros
            self.estimated_count += 1

    @property
    def total(self):
        return (self.reported + self.estimated + (self.plan_fee or 0) + (self.overage or 0)
                + (self.commitment_gap or 0) + self.unrecorded)

    @property
    def complete(self):
        return not self.unpriced and not self.unrecorded_unpriced


def _tables(engine):
    names = ('delivery_attempt_costs', 'inbound_imports', 'inbound_faxes', 'sip_call_records', 'carrier_charges',
             'carrier_call_checks', 'carrier_records', 'provider_received_charges')
    return reflect(engine, names)


def _received_condition(imports, account):
    """Received faxes that came in on this account: by its key, or for a provider's first account also the faxes
    received before accounts existed (no key, the provider as source), as ``accounts._has_received`` counts them."""
    condition = imports.c.account_key == account.key
    if account.primary:
        condition = sa.or_(condition, sa.and_(imports.c.account_key.is_(None), imports.c.source == account.provider))
    return condition


def attribute(engine, values, account, period, currency, *, now=None):
    """What ``account``'s faxes account for in ``period``, in ``currency``."""
    from . import plan_budget
    from .store import RouteStore
    now = now or utcnow()
    tables = _tables(engine)
    found = Attribution(currency)
    routes = RouteStore(engine, sip_preset=lambda: getattr(values, 'sip_trunk_preset', '') or None)
    out_card = routes.card_for_route(account.key, account.provider, 'outbound')
    in_card = routes.card_for_route(account.key, account.provider, 'inbound')
    start, end = period.start, period.end
    costs = tables['delivery_attempt_costs']
    with read_connection(engine) as connection:
        sent = connection.execute(sa.select(costs).where(
            costs.c.route == account.key, costs.c.outcome != 'pending', costs.c.provider_id != 'local',
            costs.c.created_at >= start, costs.c.created_at < end)).mappings().all()
    flat_out = out_card is not None and out_card.flat_plan
    for row in sent:
        found.sent += 1
        if row['reported_cost_micros'] is not None:
            found.add(Money.of(row['reported_cost_micros'], row['reported_currency']), reported=True)
        elif flat_out:
            found.included += 1
        else:
            found.add(Money.of(row['estimated_cost_micros'], row['currency']), reported=False)
    if account.provider == 'sip':
        _trunk_received(engine, tables, account, period, in_card, found)
    else:
        _cloud_received(engine, tables, account, period, in_card, found)
    _unrecorded_faxes(engine, account, period, found)
    budget = plan_budget.budget_for(account.key, out_card, values, inbound=in_card)
    if budget is not None and budget.currency == currency:
        _plan_rule(engine, budget, period, in_card, found, now)
    return found


def _cloud_received(engine, tables, account, period, card, found):
    imports, faxes, charges = tables['inbound_imports'], tables['inbound_faxes'], tables['provider_received_charges']
    when = sa.func.coalesce(imports.c.source_received_at, imports.c.imported_at)
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(imports.c.inbound_fax_id, imports.c.reported_pages, faxes.c.pages)
                                  .select_from(imports.outerjoin(faxes, faxes.c.id == imports.c.inbound_fax_id))
                                  .where(_received_condition(imports, account), imports.c.state == 'received',
                                         imports.c.revision == '', when >= period.start, when < period.end)).all()
        ids = sorted({row.inbound_fax_id for row in rows})
        effective = {}
        for row in connection.execute(sa.select(charges).where(
                charges.c.inbound_fax_id.in_(ids or ['']), charges.c.applied == 1)
                .order_by(charges.c.inbound_fax_id, charges.c.charge_id, charges.c.version)).mappings():
            effective.setdefault(row['inbound_fax_id'], {})[row['charge_id']] = row
    flat = card is not None and card.flat_plan
    seen = set()
    for row in rows:
        if row.inbound_fax_id in seen:
            continue
        seen.add(row.inbound_fax_id)
        found.received += 1
        reported = effective.get(row.inbound_fax_id)
        if reported:
            for charge in reported.values():
                found.add(Money.of(charge['amount_micros'], charge['currency']), reported=True)
        elif flat:
            found.included += 1
        elif card is None:
            found.add(None, reported=False)
        else:
            pages = row.pages if row.pages is not None else row.reported_pages
            found.add(Money.of(attempt_cost(card, seconds=None, pages=pages, delivered=True), card.currency),
                      reported=False)


def _trunk_received(engine, tables, account, period, card, found):
    """Received calls on the carrier trunk, and every call record the carrier billed that fits no Faxbot call."""
    from .carriers import CarrierChargeStore
    calls = tables['sip_call_records']
    if not account.primary:
        return  # A second trunk's received calls are not told apart from the first's in the call records yet.
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(calls).where(
            calls.c.direction == 'inbound', calls.c.started_at >= period.start,
            calls.c.started_at < period.end)).mappings().all()
    store = CarrierChargeStore(engine)
    effective = store.in_effect([row['id'] for row in rows])
    for row in rows:
        found.calls += 1
        charges = effective.get(row['id'], [])
        if charges:
            for charge in charges:
                found.add(Money.of(charge['amount_micros'], charge['currency']), reported=True)
            continue
        if card is None or row['ended_at'] is None:
            found.add(None, reported=False)
            continue
        seconds = call_seconds(row['connected_seconds'], row['answered_at'], row['ended_at'], row['disposition'])
        cost = attempt_cost(card, seconds=seconds, pages=row['pages'], delivered=row['job_id'] is not None)
        found.add(None if cost is None else Money.of(cost, card.currency), reported=False)
    for record in store.unrecorded_in_effect(since=period.start):
        if record['started_at'] >= period.end:
            continue
        found.unrecorded_count += 1
        if record['currency'] != found.currency:
            found.other_currency[record['currency']] = (found.other_currency.get(record['currency'], 0)
                                                       + record['amount_micros'])
        else:
            found.unrecorded += record['amount_micros']


def _unrecorded_faxes(engine, account, period, found):
    """Faxes the provider listed for this account that Faxbot has no record of (``provider_sweep``)."""
    from .provider_sweep import unrecorded
    for row in unrecorded(engine, account_key=account.key, start=period.start, end=period.end):
        found.unrecorded_count += 1
        if row['amount_micros'] is None:
            found.unrecorded_unpriced += 1
        elif row['currency'] != found.currency:
            found.other_currency[row['currency']] = found.other_currency.get(row['currency'], 0) + row['amount_micros']
        else:
            found.unrecorded += row['amount_micros']


def _plan_rule(engine, budget, period, inbound_card, found, now):
    """The plan's fee, overage past an allowance and unused commitment, by the published rule."""
    from . import plan_budget
    window = plan_budget.Period(period.start, period.end, period.first_day, period.last_day + timedelta(days=1))
    used = plan_budget.usage(engine, budget, window, inbound_card=inbound_card)
    if budget.monthly_fee_micros:
        found.plan_fee = budget.monthly_fee_micros
    if budget.included_pages is not None and budget.page_overage_micros:
        found.overage_pages = max(0, used.pages - budget.included_pages)
        found.overage = found.overage_pages * budget.page_overage_micros
    if budget.flat and budget.faxes is not None:
        found.budget_faxes = budget.faxes
        found.over_budget_faxes = max(0, used.faxes - budget.faxes)
    if budget.commitment_micros is not None:
        spent = found.reported + found.estimated + (found.overage or 0)
        found.commitment_gap = max(0, budget.commitment_micros - spent)


# The residual and its explanation ---------------------------------------------------------------------------

def _count(value, one, many=None):
    return f'{value} {one if value == 1 else (many or one + "s")}'


def _period_name(first, last):
    """'September 2026' for a calendar month, else '15 Sep to 14 Oct 2026'."""
    if first.day == 1 and last == date(first.year, first.month, calendar.monthrange(first.year, first.month)[1]):
        return f'{first:%B} {first.year}'
    if first.year == last.year:
        return f'{first.day} {first:%b} to {last.day} {last:%b} {last.year}'
    return f'{first.day} {first:%b} {first.year} to {last.day} {last:%b} {last.year}'


def explain(invoice, found, label):
    """The residual of one invoice and its explanation, in plain sentences and parts."""
    currency = invoice['currency']
    total = total_micros(invoice['total'])
    first, last = date.fromisoformat(invoice['first_day']), date.fromisoformat(invoice['last_day'])
    name = _period_name(first, last)
    money = lambda micros: money_text(micros, currency)  # noqa: E731
    parts = []
    if found.plan_fee:
        parts.append({'label': 'Plan fee', 'amount': format_amount(found.plan_fee), 'kind': 'plan'})
    if found.overage:
        parts.append({'label': f'{_count(found.overage_pages, "page")} past the plan allowance',
                      'amount': format_amount(found.overage), 'kind': 'plan'})
    if found.commitment_gap:
        parts.append({'label': 'Committed spend not used', 'amount': format_amount(found.commitment_gap),
                      'kind': 'plan'})
    if found.reported_count:
        parts.append({'label': f'{_count(found.reported_count, "fax", "faxes")} and calls {label} reported'
                      if found.calls else f'{_count(found.reported_count, "fax", "faxes")} {label} reported',
                      'amount': format_amount(found.reported), 'kind': 'reported'})
    if found.estimated_count:
        parts.append({'label': f'{_count(found.estimated_count, "fax", "faxes")} estimated from your prices',
                      'amount': format_amount(found.estimated), 'kind': 'estimated'})
    if found.unrecorded_count - found.unrecorded_unpriced > 0:
        parts.append({'label': f'{_count(found.unrecorded_count - found.unrecorded_unpriced, "fax", "faxes")} '
                               'Faxbot has no record of', 'amount': format_amount(found.unrecorded),
                      'kind': 'unrecorded'})
    notes = []
    if found.included:
        notes.append(f'{_count(found.included, "fax", "faxes")} included in the plan at no extra charge.')
    if found.over_budget_faxes:
        notes.append(f'{_count(found.over_budget_faxes, "fax", "faxes")} went past your normal-use budget of '
                     f'{_count(found.budget_faxes, "fax", "faxes")} a month; {label} may charge for use past '
                     'normal use.')
    unknown = found.unpriced + found.unrecorded_unpriced
    if unknown:
        notes.append(f'{_count(unknown, "fax", "faxes")} {"has" if unknown == 1 else "have"} no price, so part '
                     f'of the difference may be {"its" if unknown == 1 else "theirs"}.')
    if found.other_currency:
        other = ', '.join(money_text(micros, unit) for unit, micros in sorted(found.other_currency.items()))
        notes.append(f'Charges of {other} are in another currency than the invoice and are left out.')
    explained = found.total
    residual = total - explained
    if residual > 0:
        summary = f"{money(residual)} of your {label} invoice for {name} isn't explained by your faxes."
    elif residual < 0:
        summary = (f'Your {label} invoice for {name} is {money(-residual)} less than Faxbot attributes to your '
                   'faxes.')
    elif unknown:
        summary = f'Your faxes explain your {label} invoice for {name}, apart from the faxes with no price.'
    else:
        summary = f'Your faxes explain all of your {label} invoice for {name}.'
    state = 'incomplete' if unknown else 'explained' if residual == 0 else 'residual'
    return {'total': format_amount(total), 'currency': currency, 'explained': format_amount(explained),
            'residual': format_amount(residual), 'residual_micros': residual, 'total_micros': total,
            'state': state, 'complete': not unknown, 'summary': summary, 'parts': parts, 'notes': notes,
            'period_name': name, 'faxes': {'sent': found.sent, 'received': found.received, 'calls': found.calls,
                                           'not_priced': unknown}}


def recurring(account_label, explained, *, look_back=RECUR_LOOK_BACK, times=RECUR_TIMES):
    """A recommendation when one account's residuals recur, or None.

    ``explained`` is the account's invoices, newest period first, each with ``explain``'s result. Only complete
    comparisons count. A residual counts when it is at least one whole unit and 5% of the invoice.
    """
    recent = [item for item in explained[:look_back]]
    counted = []
    for item in recent:
        result = item['explanation']
        if not result['complete']:
            continue
        residual, total = result['residual_micros'], result['total_micros']
        if abs(residual) >= RECUR_FLOOR_MICROS and Decimal(abs(residual)) >= RECUR_SHARE * Decimal(max(total, 1)):
            counted.append(item)
    if not counted:
        return None
    sign = 1 if counted[0]['explanation']['residual_micros'] > 0 else -1
    same = [item for item in counted if (item['explanation']['residual_micros'] > 0) == (sign > 0)]
    if len(same) < times:
        return None
    same = list(reversed(same))  # oldest first, for the sentence
    periods = ' and '.join(item['explanation']['period_name'] for item in same)
    amounts = ' and '.join(money_text(abs(item['explanation']['residual_micros']), item['invoice']['currency'])
                           for item in same)
    if sign > 0:
        text = (f'Your {account_label} invoices were more than your faxes explain in {periods} ({amounts}). Look on '
                'the invoice for a charge Faxbot does not know about, such as a number fee, taxes or a plan change, '
                f"and add it to {account_label}'s prices under Costs → Prices & plans.")
    else:
        text = (f'Your {account_label} invoices were less than Faxbot attributes to your faxes in {periods} '
                f"({amounts}). Check {account_label}'s prices under Costs → Prices & plans; they may be higher "
                f'than what {account_label} charges you.')
    return {'account_key': same[-1]['invoice']['account_key'], 'direction': 'more' if sign > 0 else 'less',
            'invoices': [item['invoice']['id'] for item in same], 'text': text}


def account_label(values, key):
    from ..accounts import account_named, provider_name
    account = account_named(values, key)
    if account is not None:
        return account.label
    return provider_name(key, values) if key else 'this account'


def reconcile(engine, values, invoices, *, now=None):
    """Each invoice with its explanation, newest period first, and the recommendations for recurring residuals."""
    from ..accounts import account_named
    results, by_account = [], {}
    for invoice in invoices:
        account = account_named(values, invoice['account_key'])
        label = account_label(values, invoice['account_key'])
        if account is None:
            from ..accounts import ProviderAccount
            # A removed account: its faxes are still found by its key, as a primary account of its provider.
            account = ProviderAccount(invoice['account_key'], invoice['provider_id'], label, True, True, True)
        period = InvoicePeriod(date.fromisoformat(invoice['first_day']), date.fromisoformat(invoice['last_day']),
                               invoice['period_start'], invoice['period_end'])
        try:
            found = attribute(engine, values, account, period, invoice['currency'], now=now)
        except DeliveryStoreError:
            raise
        item = {'invoice': invoice, 'label': label, 'explanation': explain(invoice, found, label)}
        results.append(item)
        by_account.setdefault(invoice['account_key'], []).append(item)
    advice = []
    for key, items in by_account.items():
        found = recurring(items[0]['label'], items)
        if found is not None:
            advice.append(found)
    return results, advice
