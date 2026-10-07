"""A recipient's toll-free fax number, used only with the recipient's recorded approval.

Some recipients publish a toll-free fax number that reaches the same intake as
their ordinary number. A call to it is paid by the recipient, so the saving is
the recipient's cost, never a free route. Faxbot therefore keeps three facts
apart, append-only in ``toll_free_approvals`` (migration 0026):

- ``noted``: the administrator put the toll-free number on file;
- ``approved``: someone at the recipient agreed, recorded with who, the day
  they agreed and the evidence;
- ``withdrawn``: the approval no longer holds.

The newest row for a number is its state; no row is changed or removed.
``approved_alternate`` is the one read the send path uses: it returns the
toll-free number only while an approval is the newest row.
"""
from datetime import datetime, timedelta
from uuid import uuid4

import phonenumbers
from phonenumbers import PhoneNumberType
import sqlalchemy as sa

from .costs import format_amount
from .database import read_connection, reflect, utcnow, write_transaction
from .delivered import WINDOW_DAYS, short_money_text
from .delivered_store import DeliveredEvidence
from .numbers import InvalidNumber, normalize_number
from .store import RoutingInputError


TABLE = 'toll_free_approvals'
ACTIONS = ('noted', 'approved', 'withdrawn')


def shown(number):
    try:
        return phonenumbers.format_number(phonenumbers.parse(number, None), phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return number


def day_text(moment):
    """A day as people read it: "6 October 2026"."""
    return f'{moment.day} {moment:%B %Y}'


def is_toll_free(number):
    try:
        parsed = phonenumbers.parse(number, None)
    except phonenumbers.NumberParseException:
        return False
    return phonenumbers.is_valid_number(parsed) and phonenumbers.number_type(parsed) == PhoneNumberType.TOLL_FREE


class TollFreeApprovals:
    """Read and append toll-free approvals through one installation engine."""

    def __init__(self, engine):
        self.engine = engine
        self.table = reflect(engine, (TABLE,))[TABLE]

    @staticmethod
    def _view(row):
        return {'id': row['id'], 'number': row['phone_number'], 'alternate_number': row['alternate_number'],
                'alternate_display': shown(row['alternate_number']), 'action': row['action'],
                'approved_by': row['approved_by'],
                'approved_on': row['approved_on'].date().isoformat() if row['approved_on'] else None,
                'evidence': row['evidence'], 'recorded_by_name': row['recorded_by_name'],
                'recorded_at': row['created_at']}

    def _rows(self, connection, number=None):
        query = sa.select(self.table).order_by(self.table.c.created_at.desc(), self.table.c.id.desc())
        if number is not None:
            query = query.where(self.table.c.phone_number == number)
        return connection.execute(query).mappings().all()

    def history(self, number):
        """Every row for a recipient's number, newest first."""
        with read_connection(self.engine) as connection:
            return [self._view(row) for row in self._rows(connection, number)]

    def current(self, number):
        rows = self.history(number)
        return rows[0] if rows else None

    def all_current(self):
        """``{number: newest row}`` for every recipient with a toll-free number on file."""
        found = {}
        with read_connection(self.engine) as connection:
            for row in self._rows(connection):
                found.setdefault(row['phone_number'], self._view(row))
        return found

    def approved_alternate(self, number):
        """The toll-free number to use for ``number`` while its approval holds; None otherwise."""
        return approved_alternate(number, engine=self.engine)

    def record(self, number, *, action, alternate_number=None, approved_by=None, approved_on=None, evidence=None,
               country='US', principal_id=None, now=None):
        """Append one row after checking it; returns the new state."""
        if action not in ACTIONS:
            raise RoutingInputError('Choose noted, approved or withdrawn.')
        now = now or utcnow()
        latest = self.current(number)
        if action == 'withdrawn':
            if latest is None or latest['action'] == 'withdrawn':
                raise RoutingInputError('This recipient has no toll-free number on file to withdraw.')
            alternate = latest['alternate_number']
            approved_by = approved_on = None
        else:
            try:
                alternate = normalize_number(alternate_number or '', country=country)
            except InvalidNumber:
                raise RoutingInputError('Enter the toll-free fax number with its area code.') from None
            if not is_toll_free(alternate):
                raise RoutingInputError(f'{shown(alternate)} is not a toll-free number.')
            if alternate == number:
                raise RoutingInputError('The toll-free number must differ from the recipient\'s own number.')
        evidence = (evidence or '').strip() or None
        if evidence is not None and len(evidence) > 2000:
            raise RoutingInputError('Keep the evidence note under 2,000 characters.')
        if action == 'approved':
            approved_by = (approved_by or '').strip()
            if not approved_by or len(approved_by) > 200:
                raise RoutingInputError('Enter who at the recipient agreed, in up to 200 characters.')
            if not isinstance(approved_on, datetime):
                raise RoutingInputError('Enter the day the recipient agreed.')
            # The day is the administrator's local day, up to 14 hours ahead of UTC: one day of slack.
            if approved_on.date() > (now + timedelta(days=1)).date():
                raise RoutingInputError('The day the recipient agreed cannot be in the future.')
            if evidence is None:
                raise RoutingInputError('Say where the agreement is recorded, such as an email and its date.')
        else:
            approved_by = approved_on = None
        with write_transaction(self.engine) as connection:
            name = None
            if principal_id is not None:
                principals = reflect(self.engine, ('access_principals',))['access_principals']
                name = connection.execute(sa.select(principals.c.display_name).where(
                    principals.c.id == principal_id)).scalar_one_or_none()
            connection.execute(self.table.insert().values(
                id=uuid4().hex, phone_number=number, alternate_number=alternate, action=action,
                approved_by=approved_by, approved_on=approved_on.replace(hour=0, minute=0, second=0, microsecond=0,
                                                                         tzinfo=None) if approved_on else None,
                evidence=evidence, recorded_by=principal_id,
                recorded_by_name=name[:200] if isinstance(name, str) and name else None, created_at=now))
        return self.current(number)


# -- the reads the send path uses ---------------------------------------------------------------------
# Stable for the toll-free dialing work (Builder AE): approved_alternate(number, engine=, connection=) and
# approval(approval_id, engine=, connection=). Pass ``connection`` to read inside the caller's open transaction.

def _read(engine, connection, operation):
    if connection is not None:
        return operation(connection, sa.Table(TABLE, sa.MetaData(), autoload_with=connection))
    with read_connection(engine) as opened:
        return operation(opened, sa.Table(TABLE, sa.MetaData(), autoload_with=opened))


def _latest(connection, table, number):
    return connection.execute(sa.select(table).where(table.c.phone_number == number)
                              .order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().first()


def approved_alternate(number, *, engine=None, connection=None):
    """The approved toll-free number (E.164) for a recipient's number while its approval holds, else None."""
    def read(conn, table):
        latest = _latest(conn, table, number)
        return latest['alternate_number'] if latest is not None and latest['action'] == 'approved' else None
    return _read(engine, connection, read)


def current_approval(number, *, engine=None, connection=None):
    """``approval()`` of the approval that holds now for a recipient's number, or None."""
    def read(conn, table):
        latest = _latest(conn, table, number)
        return latest['id'] if latest is not None and latest['action'] == 'approved' else None
    identity = _read(engine, connection, read)
    return None if identity is None else approval(identity, engine=engine, connection=connection)


def approval(approval_id, *, engine=None, connection=None):
    """One approval row: ``{id, number, alternate, recipient_name, approved_by, approved_at, withdrawn_at, evidence}``.

    ``approved_at`` is the day the recipient agreed (naive UTC midnight); ``withdrawn_at`` is when a later
    withdrawal or change for the same number was recorded, or None while the approval holds. None for a row
    that is not an approval.
    """
    def read(conn, table):
        row = conn.execute(sa.select(table).where(table.c.id == approval_id)).mappings().first()
        if row is None or row['action'] != 'approved':
            return None
        later = conn.execute(sa.select(table.c.created_at).where(
            table.c.phone_number == row['phone_number'],
            sa.or_(table.c.created_at > row['created_at'],
                   sa.and_(table.c.created_at == row['created_at'], table.c.id > row['id'])))
            .order_by(table.c.created_at, table.c.id).limit(1)).scalar_one_or_none()
        destinations = sa.Table('delivery_destinations', sa.MetaData(), autoload_with=conn)
        name = conn.execute(sa.select(destinations.c.display_name).where(
            destinations.c.phone_number == row['phone_number'])).scalar_one_or_none()
        return {'id': row['id'], 'number': row['phone_number'], 'alternate': row['alternate_number'],
                'recipient_name': name, 'approved_by': row['approved_by'], 'approved_at': row['approved_on'],
                'withdrawn_at': later, 'evidence': row['evidence']}
    return _read(engine, connection, read)


# -- advice ---------------------------------------------------------------------------------------

def state_sentence(row, name=None):
    """One sentence for a recipient's toll-free state, as Recipients → Details shows it."""
    who = name or 'the recipient'
    if row is None or row['action'] == 'withdrawn':
        return None if row is None else (f"The approval to fax {who} at {row['alternate_display']} was withdrawn, so "
                                         'Faxbot sends to their own number.')
    if row['action'] == 'noted':
        return (f"{row['alternate_display']} is on file but not approved. Calls to a toll-free number are paid by "
                f'the recipient, so record who at {who} agreed before Faxbot uses it.')
    agreed = datetime.fromisoformat(row['approved_on'])
    return (f"{row['approved_by']} agreed on {day_text(agreed)}. With this approval, Faxbot sends faxes for {who} to "
            f"{row['alternate_display']}, and the recipient pays for those calls.")


def toll_free_recommendations(store):
    """Recipients with a toll-free number on file: what an approval does, and what their faxes cost you now."""
    approvals = TollFreeApprovals(store.engine).all_current()
    evidence = DeliveredEvidence(store)
    items = []
    for number, row in sorted(approvals.items()):
        if row['action'] == 'withdrawn':
            continue
        destination = store.get_destination(number) or {}
        name = destination.get('display_name')
        figures = evidence.for_destination(number)
        paid = [figure for figure in figures.values() if figure.state not in ('local', 'direct', 'included')]
        unpriced = sum(figure.unpriced for figure in paid)
        currencies = {figure.currency for figure in paid if figure.currency}
        total = (sum(figure.cost_micros for figure in paid) if paid and not unpriced and len(currencies) == 1
                 and not any(figure.mixed for figure in paid) else None)
        faxes = sum(figure.attempts for figure in paid)
        if not paid:
            spend = f'You sent no faxes to it by a paid route in the last {WINDOW_DAYS} days.'
        elif total is None:
            spend = f'What its faxes cost you in the last {WINDOW_DAYS} days is unknown.'
        else:
            spend = (f"Its {faxes} {'fax' if faxes == 1 else 'faxes'} in the last {WINDOW_DAYS} days cost you about "
                     f'{short_money_text(total, next(iter(currencies)))} (estimate).')
        items.append({**row, 'display_name': name, 'approved': row['action'] == 'approved',
                      'spend': None if total is None else {'currency': next(iter(currencies)),
                                                           'amount': format_amount(total)},
                      'sentence': f'{state_sentence(row, name)} {spend}'})
    approved = sum(1 for item in items if item['approved'])
    if not items:
        state = 'none'
        sentence = ('No recipient has a toll-free fax number on file. If one publishes a toll-free number for the '
                    'same intake, add it under Recipients → Details; Faxbot uses it only once you record their '
                    'approval, because the recipient pays for those calls.')
    else:
        state = 'on_file'
        sentence = (f"{len(items)} {'recipient has' if len(items) == 1 else 'recipients have'} a toll-free fax number "
                    f'on file, {approved} approved. The recipient pays for each call to a toll-free number.')
    return {'state': state, 'sentence': sentence, 'items': items, 'days': WINDOW_DAYS}
