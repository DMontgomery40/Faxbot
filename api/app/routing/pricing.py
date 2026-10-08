"""What one more fax costs on each route, worked out one way for ranking, cost caps, quotes and the dry run.

Every figure comes from the shared pre-dial predictor (``predict``: the rate
card for the number's class, the time on the line) and, for a monthly plan,
from that plan's budget (``plan_budget``: what this one fax adds while the
plan has room, its overage past an allowance, "over your normal-use budget"
past a fair-use budget). So the route a fax takes, the cap it is checked
against and the quote a person reads always agree.

- An unknown price stays unknown (``micros`` None), never $0.
- A fax a monthly plan carries reads "In your plan" (or "In your plan; over
  your normal-use budget"), never "$0.00": the plan's fee is paid either way,
  and this fax adds nothing to it.
- A provider's first account is priced exactly as before accounts existed. An
  extra account (``sinch-uk``) is priced by its own rate card when it has one,
  else by its provider's published terms, and counts only its own plan use.
"""
from dataclasses import dataclass, replace
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


def price(routes, values, key, destination, pages, *, provider=None, now=None, layout='normal', number='original',
          site=None):
    """The ``Price`` of one fax of ``pages`` pages to ``destination`` by account ``key``.

    An origin-rated row for where the account's calls start prices it when one matches (``origin_rates``);
    ``site`` prices it as if the call started from that site.
    """
    from .plan_budget import budget_left, marginal, plan_use
    from .predict import Shape, predict_from
    from .predict_facts import facts_for
    engine = routes.engine
    provider = provider or key
    moment = _now(now)
    facts_key = key
    if key != provider and routes.card_for(key) is None:
        facts_key = provider
    facts = facts_for(facts_key, destination, now=moment, engine=engine, values=values, account=key, site=site)
    if facts_key != key and facts.plan is not None:
        # The provider's terms, this account's own use: a second plan never shares the first one's month.
        try:
            own = plan_use(engine, key, now=moment, values=values)
        except Exception:
            own = None
        facts = replace(facts, plan=own)
    shape = Shape(max(int(pages or 1), 1), None, 'standard', layout if layout in ('normal', 'dense') else 'normal')
    prediction = predict_from(facts, shape)
    try:
        left = budget_left(key, moment, engine=engine, values=values)
    except Exception:
        left = None
    found = marginal(left, shape.pages, prediction)
    cost = found.cost
    micros = cost.micros if cost is not None else None
    in_plan = bool(prediction.marginal and micros == 0) or bool(left is not None and micros == 0 and found.uses_budget)
    return Price(key, micros, cost.currency if cost is not None else None, in_plan=in_plan,
                 over_budget=bool(found.over_budget), uses_budget=bool(found.uses_budget),
                 sentence=found.sentence or prediction.basis, number=number,
                 origin=getattr(facts, 'origin', None))


def _accounts(values):
    try:
        from ..accounts import all_accounts
        return {account.key: account for account in all_accounts(values) if account.sends}
    except Exception:
        return {}


def prices_for(routes, values, to_number, pages, *, pinned=None, bound=None, dial=None, keys=None, now=None):
    """``{account key: Price}`` for the accounts a fax may use; an account that cannot be priced is left out."""
    from . import dialing
    from .alternates import attempt_number
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
    found = {}
    for key in keys:
        if not key or key in found:
            continue
        account = owners.get(key)
        provider = account.provider if account is not None else key
        number, _ = attempt_number(destination, alternate=alternate, route_reaches=bool(alternate) and
                                   dialing.reaches(provider, alternate, values, sip_preset=preset))
        try:
            found[key] = price(routes, values, key, number, pages, provider=provider, now=now,
                               number='alternate' if number != destination else 'original')
        except Exception:
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
