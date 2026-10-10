"""What one more fax costs on each route, worked out one way for ranking, cost caps, quotes and the dry run.

Every figure comes from the shared pre-dial predictor (``predict``: the rate
card for the number's class, the time on the line) and, for a monthly plan,
from that plan's budget (``plan_budget``: what this one fax adds while the
plan has room, its overage past an allowance, "over your normal-use budget"
past a fair-use budget). So the route a fax takes, the cap it is checked
against and the quote a person reads agree, except where a scarce plan's room
is held for other waiting faxes (below): a quote prices the plan against its
whole room.

- An unknown price stays unknown (``micros`` None), never $0.
- A fax a monthly plan carries reads "In your plan" (or "In your plan; over
  your normal-use budget"), never "$0.00": the plan's fee is paid either way,
  and this fax adds nothing to it.
- A provider's first account is priced exactly as before accounts existed. An
  extra account (``sinch-uk``) is priced by its own rate card when it has one,
  else by its provider's published terms, and counts only its own plan use.
- A queued fax (``prices_for(..., job_id=...)``) sees a scarce plan's room after
  the pages held for faxes already on their way, for other waiting faxes given
  the plan, and for the reserve (``plan_allocation``); a fax not given the plan
  ranks it at its marginal price after them. ``Price.held`` says what was held.
"""
from dataclasses import dataclass
from datetime import datetime, timezone


IN_PLAN = 'In your plan'
IN_PLAN_OVER = 'In your plan; over your normal-use budget'
UNKNOWN = 'Price unknown'


@dataclass(frozen=True)
class Price:
    key: str
    micros: int | None                 # what this fax adds to the bill; None is unknown, never 0
    currency: str | None
    in_plan: bool = False              # a monthly plan carries it (included, or within its allowance)
    over_budget: bool = False          # the plan is past the normal-use budget you set: prefer another route
    uses_budget: bool = False          # it uses part of a plan's budget or allowance
    sentence: str = ''                 # how it was worked out, one plain sentence
    number: str = 'original'           # 'alternate' when the route calls the recipient's approved alternate
    origin: str | None = None          # the origin-rated row that priced it ('any', a site, 'country:GB'); WP-T
    held: object = None                # plan_allocation.Hold: part of the plan's room held for other faxes
    unheld_micros: int | None = None   # with ``held``: what this fax would add with the whole room
    unheld_over: bool = False          # with ``held``: whether the whole room would be past the normal-use budget
    rate_card: object = None           # the applicable destination tariff, for displaying its rate and plan fee
    pages: int | None = None           # the physical pages priced (a measured candidate's sent pages)
    measured: bool = False             # priced from the fax's own measured pages, not from its page count
    # The account does not take this number at all (its country or number class): ineligible, never ranked.
    refused: bool = False

    @property
    def known(self):
        return self.micros is not None

    @property
    def plan(self):
        """'over_budget' or 'included' for a plan's own fax, else None (``rules.model.Quote.plan``)."""
        if not self.in_plan:
            return None
        return 'over_budget' if self.over_budget else 'included'

    def text(self):
        """'In your plan', 'In your plan; over your normal-use budget', 'Price unknown', or None for an amount."""
        return plan_text(self.plan, self.micros)

    def money(self):
        """``{'currency', 'amount'}`` for an API answer, or None for a plan's fax or an unknown price."""
        if self.in_plan or self.micros is None:
            return None
        from .costs import format_amount
        return {'currency': self.currency, 'amount': format_amount(self.micros)}


def plan_text(plan, micros):
    if plan == 'over_budget':
        return IN_PLAN_OVER
    if plan == 'included':
        return IN_PLAN
    if micros is None:
        return UNKNOWN
    return None


def _now(now):
    return (now or datetime.now(timezone.utc)).replace(tzinfo=None, microsecond=0)


def account_facts(engine, values, key, destination, *, provider=None, now=None, site=None, mailbox_id=None):
    """The ``predict.RouteFacts`` one account's call to ``destination`` is priced with: its own rate card when it
    has one, else its provider's, for the number's class and where the call starts. The one tariff contract route
    ranking and page preparation share (JOINT-OPTIMIZER-AUDIT step 1), so an extra account is never priced by its
    provider's first account. A missing price stays missing (``terms`` None), never zero."""
    from .predict_facts import facts_for
    from .store import RouteStore
    provider = provider or key
    facts_key = key
    if key != provider and RouteStore(engine).card_for(key) is None:
        facts_key = provider
    return facts_for(facts_key, destination, now=_now(now), engine=engine, values=values, account=key, site=site,
                     mailbox_id=mailbox_id)


def price(routes, values, key, destination, pages, *, provider=None, now=None, layout='normal', number='original',
          site=None, hold=None, shape=None, facts=None, mailbox_id=None):
    """The ``Price`` of one fax of ``pages`` pages to ``destination`` by account ``key``.

    An origin-rated row for where the account's calls start prices it when one matches (``origin_rates``);
    ``site`` prices it as if the call started from that site. ``hold`` (``plan_allocation.Hold``) is the part of
    the plan's room this fax may not use. ``shape`` (``predict.Shape``) prices the pages as measured (a candidate
    from ``pages.sending``) instead of the page count; ``facts`` are the account's facts already read
    (``account_facts``), so a measured candidate and its price use the same tariff.
    """
    from .plan_budget import budget_left, marginal
    from .predict import Shape, predict_from
    engine = routes.engine
    provider = provider or key
    moment = _now(now)
    if facts is None:
        facts_key = key
        if key != provider and routes.card_for(key) is None:
            facts_key = provider
        from .predict_facts import facts_for
        facts = facts_for(facts_key, destination, now=moment, engine=engine, values=values, account=key, site=site,
                          mailbox_id=mailbox_id)
    measured = shape is not None
    if shape is None:
        shape = Shape(max(int(pages or 1), 1), None, 'standard', layout if layout in ('normal', 'dense') else 'normal')
    prediction = predict_from(facts, shape)
    try:
        left = budget_left(key, moment, engine=engine, values=values)
    except Exception:
        left = None
    if left is not None and not prediction.marginal:
        # Room on a domestic plan does not cover an unpriced destination or a separate tariff. A monetary
        # commitment can cover a known charge in its own currency, without assuming a destination allowance.
        commitment = (left.budget.commitment_micros is not None and not left.budget.flat
                      and not left.budget.included_pages and not left.budget.included_minutes)
        if not commitment or prediction.cost is None or prediction.cost.currency != left.budget.currency:
            left = None
    found = marginal(left, shape.pages, prediction)
    unheld = None
    if hold is not None and left is not None:
        from .plan_allocation import after_hold
        unheld, found = found, marginal(after_hold(left, hold), shape.pages, prediction)
    cost = found.cost
    micros = cost.micros if cost is not None else None
    in_plan = bool(prediction.marginal and micros == 0) or bool(left is not None and micros == 0 and found.uses_budget)
    return Price(key, micros, cost.currency if cost is not None else None, in_plan=in_plan,
                 over_budget=bool(found.over_budget), uses_budget=bool(found.uses_budget),
                 sentence=found.sentence or prediction.basis, number=number,
                 origin=getattr(facts, 'origin', None), held=hold if unheld is not None else None,
                 unheld_micros=(unheld.cost.micros if unheld is not None and unheld.cost is not None else None),
                 unheld_over=bool(unheld is not None and unheld.over_budget),
                 rate_card=facts.terms.card if facts.terms is not None and not facts.refused else None,
                 pages=shape.pages, measured=measured, refused=bool(facts.refused))


def _accounts(values):
    try:
        from ..accounts import all_accounts
        return {account.key: account for account in all_accounts(values) if account.sends}
    except Exception:
        return {}


def prices_for(routes, values, to_number, pages, *, pinned=None, bound=None, dial=None, keys=None, now=None,
               job_id=None, measured=None, mailbox_id=None):
    """``{account key: Price}`` for the accounts a fax may use; an account that cannot be priced is left out.

    ``job_id``: the queued fax being planned. Its scarce plans are priced after the room held for other faxes
    (``plan_allocation.hold_for``); without it every plan is priced against its whole room, as for a quote.
    ``measured``: ``{account key: (predict.Shape, predict.RouteFacts)}``, the pages each account would actually
    send as measured (``routing.joint``); those accounts are priced by them, the others by the page count.
    ``mailbox_id``: the fax's sending mailbox, so a price by caller ID follows the number its call presents.
    """
    import sqlalchemy as sa
    from . import dialing
    from .alternates import attempt_number
    from .costs import InvalidRateCard
    from .database import DeliveryStoreError
    from .plan import extra_routes
    from .store import destination_key
    destination = destination_key(to_number, getattr(values, 'fax_default_country', 'US'))
    owners = _accounts(values)
    if keys is None:
        if pinned is not None and not pinned.automatic:
            keys = list(pinned.envelope.accounts)
        else:
            keys = [bound] + extra_routes(values, bound) if bound else list(owners)
    alternate = (dial or {}).get('alternate')
    if (dial or {}).get('refused') or alternate == destination:
        alternate = None
    preset = getattr(values, 'sip_trunk_preset', '') or ''
    holds = {}
    if job_id is not None:
        from .plan_allocation import hold_for
        holds = hold_for(routes, values, job_id, [key for key in keys if key], now=now, dial=dial)
    found = {}
    for key in keys:
        if not key or key in found:
            continue
        account = owners.get(key)
        provider = account.provider if account is not None else key
        number, _ = attempt_number(destination, alternate=alternate, route_reaches=bool(alternate) and
                                   dialing.reaches(provider, alternate, values, sip_preset=preset))
        shape, facts = (measured or {}).get(key, (None, None))
        try:
            found[key] = price(routes, values, key, number, pages, provider=provider, now=now,
                               number='alternate' if number != destination else 'original', hold=holds.get(key),
                               shape=shape, facts=facts, mailbox_id=mailbox_id)
        except (DeliveryStoreError, sa.exc.SQLAlchemyError, InvalidRateCard) as error:
            # This account's prices or plan could not be read: it is left out, and the cause logged. Anything else
            # (a bug in pricing or in the plan's allocation) raises.
            import logging
            logging.getLogger(__name__).warning('Account %s could not be priced: %s', key, error)
            continue
    return found


def quotes_for(routes, values, accounts, destination, pages, alternate=None, *, now=None):
    """``rules.model.Quote`` per sending account for a fax's facts: the original number, and the approved
    alternate when there is one. The same figures the planner ranks by and caps check against."""
    from ..rules import model
    found = []
    for account in accounts:
        if not account.sends:
            continue
        numbers = [('original', destination)] + ([('alternate', alternate.number)] if alternate is not None else [])
        for which, number in numbers:
            try:
                item = price(routes, values, account.key, number, pages, provider=account.provider, now=now,
                             number=which)
            except Exception:
                item = Price(account.key, None, None, number=which)
            found.append(model.Quote(account.key, item.micros, item.currency if item.micros is not None else None,
                                     number=which, origin=item.origin, pages=max(int(pages or 1), 1),
                                     plan=item.plan))
    return tuple(found)
