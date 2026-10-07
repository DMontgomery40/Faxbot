"""Which number a sent fax dialed, and why, for Sent details and ``faxbot sent show``.

A fax keeps the recipient the sender entered. When it called the recipient's
approved alternate instead (``routing.alternates``), Sent says so in one
sentence: "Dialed 1-800-555-0100, the toll-free number Example Clinic
approved on October 3, 2026." When a call to the approved number definitely
failed and Faxbot called the number entered, Sent says that instead. Read
only from what each attempt recorded before it was submitted.
"""
from weakref import WeakKeyDictionary

import sqlalchemy as sa

from .alternates import describe, provenance
from .database import read_connection, reflect
from .dialing import display_number, is_toll_free
from .policy import DIRECT


_TABLES = WeakKeyDictionary()
_NAMES = ('fax_jobs', 'outbound_attempts', 'outbound_deliveries', 'delivery_attempt_costs')


def _tables(engine):
    """The four tables, reflected once per engine (the Sent list asks for up to 100 faxes at a time)."""
    found = _TABLES.get(engine)
    if found is None:
        found = reflect(engine, _NAMES)
        if 'dialed_number' in found['outbound_attempts'].c:
            _TABLES[engine] = found
    return found


def _day(moment):
    return f'{moment:%B} {moment.day}, {moment.year}' if moment is not None else None


def dialed_view(engine, job_id):
    """``{'number', 'display', 'toll_free', 'recipient_name', 'approved_on', 'withdrawn_on', 'sentence'}`` or None.

    None when the fax called the number entered with no approved alternate
    involved, or before Faxbot recorded dialed numbers.
    """
    try:
        t = _tables(engine)
    except Exception:
        return None
    jobs, attempts, deliveries, costs = (t['fax_jobs'], t['outbound_attempts'], t['outbound_deliveries'],
                                         t['delivery_attempt_costs'])
    if 'dialed_number' not in attempts.c or 'alternate_number' not in deliveries.c:
        return None
    with read_connection(engine) as connection:
        job = connection.execute(sa.select(jobs.c.to_number, deliveries.c.alternate_number)
                                 .select_from(jobs.join(deliveries, deliveries.c.id == jobs.c.id))
                                 .where(jobs.c.id == job_id)).first()
        if job is None:
            return None
        # Attempts that placed (or were about to place) a call; a direct or own-number delivery calls nobody.
        rows = connection.execute(
            sa.select(attempts.c.dialed_number, attempts.c.dialed_approval, attempts.c.phase,
                      attempts.c.error_category, attempts.c.submitted_at)
            .select_from(attempts.outerjoin(costs, costs.c.id == attempts.c.id))
            .where(attempts.c.job_id == job_id, attempts.c.dialed_number.is_not(None),
                   attempts.c.phase != 'abandoned',
                   sa.or_(costs.c.route.is_(None), costs.c.route.not_in((DIRECT, 'local'))))
            .order_by(attempts.c.sequence)).all()
    if not rows:
        return None
    latest = rows[-1]
    dialed, original = latest.dialed_number, job.to_number
    if dialed == original:
        refused = next((row.dialed_number for row in rows
                        if job.alternate_number and row.dialed_number == job.alternate_number
                        and row.submitted_at is not None and row.phase == 'failed' and row.error_category is None),
                       None)
        if refused is None:
            return None
        return {'number': dialed, 'display': display_number(dialed), 'toll_free': is_toll_free(dialed),
                'recipient_name': None, 'approved_on': None, 'withdrawn_on': None,
                'sentence': provenance(dialed, original, None, refused_number=refused)}
    approval = describe(latest.dialed_approval, engine=engine) if latest.dialed_approval else None
    sentence = provenance(dialed, original, approval)
    withdrawn = approval.withdrawn_at if approval is not None else None
    if withdrawn is not None:
        sentence = sentence[:-1] + f'; the approval was withdrawn on {_day(withdrawn)}, so new faxes call the number entered.'
    return {'number': dialed, 'display': display_number(dialed), 'toll_free': is_toll_free(dialed),
            'recipient_name': approval.recipient_name if approval is not None else None,
            'approved_on': _day(approval.approved_at) if approval is not None else None,
            'withdrawn_on': _day(withdrawn), 'sentence': sentence}
