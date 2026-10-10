"""The number a fax dials when its recipient approved another one for the same intake.

A recipient may approve a second number, usually toll-free, that reaches the
same fax intake; calling it then costs the sender little or nothing, and the
recipient pays for the call. Approvals are recorded by a person in an
append-only table (``routing.tollfree``, migration 0026). This module is the
only place delivery reads them:

- ``current`` reads the approval in force for a recipient's number;
- ``dialed_number_for`` decides, once, when a fax is accepted, which number it
  dials. Migration 0027 keeps that choice with the delivery, so an approval
  recorded or withdrawn later changes new faxes only, and the transport never
  decides again;
- ``attempt_number`` applies that choice to one attempt: the approved number,
  unless the attempt's route cannot call it or an earlier attempt to it
  definitely failed, when the fax calls the number the sender entered;
- ``describe`` reads an approval again for the provenance line on Sent.

Any failure to read an approval means no approval: the fax calls the number
the sender entered, which is always a valid destination.
"""
from dataclasses import dataclass
from datetime import datetime
import logging

from .numbers import is_canonical


@dataclass(frozen=True)
class Approval:
    """An approval as delivery uses it; ``id`` and the details are None when the source gives only the number."""
    alternate: str
    id: str | None = None
    number: str | None = None
    recipient_name: str | None = None
    approved_at: datetime | None = None
    withdrawn_at: datetime | None = None


@dataclass(frozen=True)
class DialFacts:
    """What ``dialed_number_for`` decides from: the approval in force, and whether any route may call a number."""
    approval: Approval | None
    reachable: object = None  # callable(number) -> bool; None means every route may


_source = None


def install(source):
    """Use ``source`` (a module or object with ``current_approval`` and ``approval``) instead of ``routing.tollfree``."""
    global _source
    _source = source


def reset():
    install(None)


def _module():
    if _source is not None:
        return _source
    try:
        from . import tollfree
    except ImportError:
        return None  # approvals are not installed: every fax calls the number the sender entered
    return tollfree


def _approval(value, number=None):
    """An ``Approval`` from the source's dict (or bare number), or None when it is not a usable approval."""
    if isinstance(value, str):
        value = {'alternate': value}
    if not isinstance(value, dict):
        return None
    alternate = value.get('alternate')
    if not is_canonical(alternate) or alternate == (number or value.get('number')):
        return None
    identity = value.get('id')
    approved_at = value.get('approved_at')
    withdrawn_at = value.get('withdrawn_at')
    name = value.get('recipient_name')
    return Approval(alternate, str(identity)[:64] if identity else None, value.get('number') or number,
                    name.strip()[:200] or None if isinstance(name, str) else None,
                    approved_at if isinstance(approved_at, datetime) else None,
                    withdrawn_at if isinstance(withdrawn_at, datetime) else None)


def _read(call, *, connection=None, engine=None):
    """Run one read on the caller's open transaction without ever ending it: PostgreSQL reads in a savepoint."""
    if connection is not None and connection.dialect.name == 'postgresql':
        with connection.begin_nested():
            return call(connection=connection)
    if connection is not None:
        return call(connection=connection)
    return call(engine=engine)


def current(number, *, connection=None, engine=None):
    """The approval in force for ``number``, or None."""
    module = _module()
    if module is None or not is_canonical(number):
        return None
    try:
        lookup = getattr(module, 'current_approval', None)
        if lookup is not None:
            value = _read(lambda **where: lookup(number, **where), connection=connection, engine=engine)
        else:
            alternate = getattr(module, 'approved_alternate')
            value = _read(lambda **where: alternate(number, **where), connection=connection, engine=engine)
    except Exception:
        logging.getLogger(__name__).warning('Recipient approvals could not be read; the fax calls the number entered.')
        return None
    return _approval(value, number)


def describe(approval_id, *, engine=None, connection=None):
    """An approval by its identity, withdrawn or not, for provenance; None when it cannot be read."""
    module = _module()
    if module is None or not approval_id:
        return None
    try:
        value = _read(lambda **where: module.approval(approval_id, **where), connection=connection, engine=engine)
    except Exception:
        return None
    return _approval(value)


def dialed_number_for(job, *, facts):
    """``(number, approval id or None)`` a fax dials, decided once when it is accepted.

    ``job['to_number']`` is the recipient the sender entered and stays the
    fax's recipient. The approved alternate is chosen when one is in force and
    at least one of the fax's routes may call it; otherwise the fax calls the
    number entered. The provider-rules work (WP-C) later adds the fax's own
    choice (use, never or only) here, and nothing else changes.
    """
    original = job['to_number']
    approval = facts.approval
    if approval is None or approval.alternate == original:
        return original, None
    if facts.reachable is not None and not facts.reachable(approval.alternate):
        return original, None
    return approval.alternate, approval.id


def attempt_number(original, *, alternate=None, approval_id=None, refused=False, route_reaches=True):
    """``(number, approval id or None)`` one attempt dials, from the choice kept with the fax.

    The approved alternate, unless an earlier attempt to it definitely failed
    (``refused``) or this attempt's route cannot call it; then the number the
    sender entered. Never decided from an uncertain outcome: those faxes are
    not sent again at all.
    """
    if not alternate or alternate == original or refused or not route_reaches:
        return original, None
    return alternate, approval_id


def claim_dial_state(store, claim, dial):
    """The number choice for one claim: ``{'alternate', 'refused', 'approvals': {job id: approval id}}``.

    ``dial`` is the claim's own fax's (``OutboundStore.dial_state``). A shared
    call carries every fax in it to one number, so it calls the alternate only
    when every fax kept the same one and none was refused there; otherwise all
    of them call the number they share.
    """
    dial = dict(dial or {'alternate': None, 'approval': None, 'refused': False})
    states = {claim.job_id: dial}
    for member in getattr(claim, 'members', ()) or ():
        if member.job_id not in states:
            states[member.job_id] = store.dial_state(member.job_id)
    alternates = {state.get('alternate') for state in states.values()}
    if len(alternates) != 1 or None in alternates or any(state.get('refused') for state in states.values()):
        return {'alternate': None, 'refused': any(state.get('refused') for state in states.values()), 'approvals': {}}
    return {'alternate': alternates.pop(), 'refused': False,
            'approvals': {job_id: state.get('approval') for job_id, state in states.items()}}


def provenance(dialed, original, approval, *, refused_number=None):
    """One sentence for Sent saying which number Faxbot dialed and why; None when it dialed the number entered."""
    from .dialing import display_number, is_toll_free
    if dialed and dialed != original:
        kind = 'toll-free number' if is_toll_free(dialed) else 'number'
        who = (approval.recipient_name if approval is not None and approval.recipient_name else 'the recipient')
        when = ''
        if approval is not None and approval.approved_at is not None:
            moment = approval.approved_at
            when = f' on {moment:%B} {moment.day}, {moment.year}'
        return f'Dialed {display_number(dialed)}, the {kind} {who} approved{when}.'
    if refused_number:
        return (f'Dialed the number you entered, because the call to the approved number '
                f'{display_number(refused_number)} failed.')
    return None


def savings(routes, engine, *, since, days):
    """What calling approved toll-free numbers saved, apart from every other saving; always an estimate.

    Each delivered fax that called its recipient's approved toll-free number is
    priced twice on the route that carried it, with the route's sending card
    (what calling the number entered would have cost) and with its toll-free
    price; the difference is the saving. An unpublished toll-free price leaves
    the fax unpriced, never free, and a flat monthly plan saves no money.
    """
    import sqlalchemy as sa
    from .costs import estimate_cost
    from .database import read_connection, reflect
    from .dialing import class_card, is_toll_free
    from .policy import DIRECT
    result = {'faxes': 0, 'priced': 0, 'in_plan': 0, 'unpriced': 0, 'saved': {}}
    t = reflect(engine, ('fax_jobs', 'delivery_attempt_costs', 'outbound_attempts'))
    jobs, costs, attempts = t['fax_jobs'], t['delivery_attempt_costs'], t['outbound_attempts']
    rows = []
    if 'dialed_number' in attempts.c:
        with read_connection(engine) as connection:
            rows = connection.execute(sa.select(costs.c.provider_id, jobs.c.pages, attempts.c.dialed_number)
                                      .select_from(costs.join(attempts, attempts.c.id == costs.c.id)
                                                   .join(jobs, jobs.c.id == costs.c.job_id))
                                      .where(costs.c.outcome == 'success', costs.c.created_at >= since,
                                             costs.c.route.not_in((DIRECT, 'local')),
                                             attempts.c.dialed_number.is_not(None),
                                             attempts.c.dialed_number != jobs.c.to_number)).all()
    preset = routes.sip_preset() if callable(getattr(routes, 'sip_preset', None)) else ''
    cards = {}
    for provider_id, pages, dialed in rows:
        if not is_toll_free(dialed):
            continue
        result['faxes'] += 1
        if provider_id not in cards:
            cards[provider_id] = routes.card_for(provider_id)
        card = cards[provider_id]
        toll_free = class_card(card, provider_id, dialed, sip_preset=preset)
        if card is not None and card.flat_plan:
            result['in_plan'] += 1
        elif card is None or toll_free is None or toll_free.currency != card.currency:
            result['unpriced'] += 1
        else:
            result['priced'] += 1
            saved = estimate_cost(card, pages) - estimate_cost(toll_free, pages)
            result['saved'][card.currency] = result['saved'].get(card.currency, 0) + saved
    result['sentence'] = savings_sentence(result, days)
    return result


def savings_sentence(result, days):
    """One sentence for Savings & optimization → Savings results; the toll-free numbers' owners pay for those calls, so it says so."""
    from .costs import money_list_text
    faxes = result['faxes']
    if not faxes:
        return f'No faxes called an approved toll-free number in the last {days} days.'
    numbers = 'number' if faxes == 1 else 'numbers'
    sentence = (f"{faxes} {'fax' if faxes == 1 else 'faxes'} called the toll-free {numbers} "
                f"{'its recipient' if faxes == 1 else 'their recipients'} approved")
    if any(result['saved'].values()):
        sentence += (f": about {money_list_text(result['saved'])} saved by using the approved toll-free {numbers}, "
                     'whose owners pay for the calls.')
    elif result['saved']:
        sentence += f", which cost the same as calling the {numbers} entered."
    else:
        sentence += ', whose owners pay for the calls.'
    some = 'It' if faxes == 1 else f"{result['unpriced'] or result['in_plan']} of them"
    if result['unpriced']:
        verb = 'has' if faxes == 1 or result['unpriced'] == 1 else 'have'
        sentence += f" {some} {verb} no price, because the route's toll-free price is not published."
    elif result['in_plan']:
        sentence += f" {some} went through your monthly plan, so {'it' if faxes == 1 else 'they'} saved no money."
    return sentence
