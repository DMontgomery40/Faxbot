"""What a missing fact costs you: the price of one legitimate fact Faxbot does not have (research candidate 9).

Many cheaper ways to reach a recipient are closed because one fact is missing:
the recipient has not confirmed a partner enrollment, a Direct address or a
toll-free number; Faxbot has never seen what the recipient's fax machine
accepts; a carrier's price or a plan's allowance was never entered. For the
last ``WINDOW_DAYS`` days, grouped by recipient, this module prices what the
faxes would have cost by the best route Faxbot is allowed to use, and what
they would have cost with exactly one fact from ``CATALOGUE`` established,
then subtracts what establishing that fact costs in money. The time it takes
you is stated in words, never turned into money.

Rules that keep it honest:

- **Both sides use the real predictor.** Every figure comes from
  ``predict.predict_from`` with the facts ``predict_facts.facts_for`` reads for
  that route and number (``Pricer``); a counterfactual changes exactly one fact
  with ``dataclasses.replace``. Faxes are priced at today's prices and today's
  plan use, from their page counts (sent faxes keep no page sizes).
- **The best route Faxbot is allowed to use** is the cheapest priced route among
  the accounts the automatic choice may use (``accounts.all_accounts``: on,
  sending, ``automatic``), plus what is already established: a verified
  partner, a confirmed Direct address or FHIR endpoint, an approved toll-free
  number. A monthly plan past its normal-use budget at the time of a fax is
  left out for that fax, as route choice leaves it out (``plan_budget``).
- **A missing authorization is a hard boundary.** A route that needs someone's
  confirmation never enters the best allowed route, whatever it would cost; it
  appears only as a fact of kind ``authorization`` that says who must confirm
  what. There is no penalty or score that a cheap route could outweigh.
- **Each fact is priced alone** against the same best allowed route. Facts are
  never added together for one recipient (a partner, a Direct address and a
  toll-free number would each replace the same call); a recipient's figure is
  its largest single fact.
- **Only facts with evidence are offered**: an open partner suggestion or an
  enrollment waiting for its code, a suggested Direct address or FHIR endpoint,
  a toll-free number on file but not approved, documents the recipient already
  acknowledged, a trunk call billed by time with no record of the far machine.
- **Unknown stays unknown.** A route or receiving price nobody entered and no
  carrier publishes has no saving in money: the advice gives the number of
  faxes and the price below which that route would have cost less.
- **Never realized savings.** These are what the past faxes would have cost.
  Once a fact is established and used, Costs → Savings counts what it saved.

Advice only: nothing here enrolls, approves, enters a price or changes a route.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
import math

import sqlalchemy as sa

from .costs import format_amount
from .database import read_connection, reflect, utcnow


WINDOW_DAYS = 90
RESOLUTION = 'standard'
# Calls that are not phone calls: they cost nothing or are priced by their own plans, so no fact makes them cheaper.
NO_CALL = ('local', 'direct')
NOT_A_CALL_PREFIXES = ('relay', 'dsm', 'fhir')
REALIZED = ('These figures are what your faxes would have cost, not savings. Once a fact is established and used, '
            'Costs → Savings counts what it really saved.')
ADVICE_ONLY = ('Faxbot only advises: it never enrolls a partner, records an approval, enters a price or changes a '
               'route for you.')


@dataclass(frozen=True)
class Fact:
    """One kind of missing fact, with the step that establishes it legitimately."""
    key: str
    kind: str          # 'authorization': someone must confirm it; 'information': Faxbot can learn it; 'price'
    title: str         # what is missing, as a heading
    confirm: str       # who must confirm or provide what ({name} is the recipient)
    step: str          # what you do, and where in Faxbot
    time: str          # the time it takes you, in words

    def __post_init__(self):
        if self.kind not in ('authorization', 'information', 'price'):
            raise ValueError('Unknown kind of fact.')


CATALOGUE = (
    Fact('partner', 'authorization', 'Not an enrolled partner',
         '{name} must confirm the code in the enrollment fax, which proves the number is theirs.',
         'Enroll them under Recipients → Partners; Faxbot faxes them a code once.',
         'about 10 minutes of your time and one short fax'),
    Fact('digital_address', 'authorization', 'No confirmed Direct address or FHIR endpoint',
         '{name} must confirm that {address} accepts these documents.',
         'Confirm the address under Recipients → Digital once {name} has agreed in writing.',
         'about 15 minutes of your time'),
    Fact('capabilities', 'information', "What the recipient's fax machine accepts is not on record",
         "Nobody: the next call through {route} records what {name}'s fax machine accepts.",
         'Send the next fax to this number through {route}.',
         'nothing beyond that fax'),
    Fact('toll_free', 'authorization', 'No recorded approval for their toll-free number',
         'Someone at {name} must agree that you fax {address}; they pay for those calls.',
         'Record who agreed and when under Recipients → Details.',
         'about 10 minutes of your time'),
    Fact('case_reuse', 'authorization', 'No recorded approval to list documents they already have',
         '{name} must agree to receive a one-page list in place of documents they already acknowledged.',
         'Turn on "accepts a one-page index" for this recipient under Recipients → Details.',
         'about 10 minutes of your time'),
    Fact('route_price', 'price', 'No price for a route you may use',
         'Nobody but you: {route} publishes no price for these calls, so read it from your bill or contract.',
         'Enter the price under Costs → Prices & plans.',
         'about 10 minutes of your time'),
    Fact('receiving_price', 'price', 'No receiving price for one of your accounts',
         'Nobody but you: {route} publishes no price for receiving faxes, so read it from your bill or contract.',
         'Enter the receiving price under Costs → Prices & plans.',
         'about 10 minutes of your time'),
    Fact('plan_allowance', 'information', "Your plan's real allowance is not known",
         "{route} must tell you in writing how many pages a month its plan carries for normal use.",
         'Set that allowance under Costs → Prices & plans.',
         'about 15 minutes of your time'),
)
FACTS = {fact.key: fact for fact in CATALOGUE}


# -- money --------------------------------------------------------------------------------------------------------

def _money(micros, currency):
    return [] if micros is None or currency is None else [{'currency': currency, 'amount': format_amount(micros)}]


def _about(micros, currency):
    from .receiving import about
    return about(micros, currency)


def _count(number, one, many=None):
    return f"{number:,} {one if number == 1 else (many or one + 's')}"


# -- the predictor seam --------------------------------------------------------------------------------------------

class Pricer:
    """Prices faxes with the shared predictor, reading each route's facts for a number once.

    ``facts`` is ``predict_facts.facts_for`` for one account and number;
    ``price`` hands those facts (or a counterfactual made from them with
    ``dataclasses.replace``) to ``predict.predict_from``. This is exactly what
    ``predict.predict`` does, with the facts kept so one of them can change.
    """

    def __init__(self, engine, values, now):
        self.engine, self.values, self.now = engine, values, now
        self._facts = {}

    def facts(self, route, number, *, account=None):
        key = (route, account or route, number)
        if key not in self._facts:
            from .predict_facts import facts_for
            self._facts[key] = facts_for(route, number, now=self.now, engine=self.engine, values=self.values,
                                         account=account)
        return self._facts[key]

    @staticmethod
    def price(facts, pages):
        from .predict import Shape, predict_from
        return predict_from(facts, Shape(max(1, int(pages or 1)), None, RESOLUTION, 'normal'))


@dataclass(frozen=True)
class Option:
    """One way to reach a recipient: an account (or a no-call route) and the number it calls."""
    key: str           # the account key, or 'direct'
    route: str         # the predictor's route key: the provider ('sip', 'sinch'), or 'direct'
    label: str
    number: str        # the number it calls (the recipient's, or their approved toll-free number)
    plan_budget: object = None   # the plan's normal-use budget (``plan_budget.Budget``) when it has a limited one


def _permitted_accounts(values):
    """Accounts the automatic choice may use now: sending, on and ``automatic`` (else the default sending one)."""
    from ..accounts import all_accounts
    accounts = [account for account in all_accounts(values) if account.sends and account.enabled]
    automatic = [account for account in accounts if account.automatic]
    return automatic or [account for account in accounts if account.default_sending][:1] or accounts[:1]


def _label(account, values):
    """The account's name in a sentence: the trunk by its carrier ("Telnyx"), an extra account by its label."""
    if not account.primary:
        return account.label
    from .predict_facts import route_label
    return route_label(account.provider, getattr(values, 'sip_trunk_preset', '') or '')


def _budget(pricer, account, number):
    """The account's plan budget when it is a flat plan with a page or fax limit; else None."""
    facts = pricer.facts(account.provider, number, account=account.key)
    card = facts.terms.card if facts.terms is not None else None
    if card is None or not card.flat_plan:
        return None
    from .plan_budget import budget_for
    budget = budget_for(account.key, card, pricer.values)
    return budget if budget is not None and budget.limited else None


# -- the history ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Sent:
    job_id: str
    number: str
    pages: int
    route: str
    at: object


def sent_faxes(engine, since, until):
    """Faxes delivered by a phone call in ``[since, until)``: one per fax, its first successful attempt."""
    t = reflect(engine, ('delivery_attempt_costs', 'fax_jobs'))
    costs, jobs = t['delivery_attempt_costs'], t['fax_jobs']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(
            costs.c.job_id, costs.c.destination, costs.c.route, costs.c.created_at, jobs.c.pages, costs.c.billed_pages,
        ).select_from(costs.outerjoin(jobs, jobs.c.id == costs.c.job_id)).where(
            costs.c.outcome == 'success', costs.c.created_at >= since, costs.c.created_at < until,
            costs.c.route.notin_(NO_CALL)).order_by(costs.c.created_at, costs.c.id)).all()
    found, seen = [], set()
    for row in rows:
        if row.job_id in seen or str(row.route).split(':')[0].split('.')[0] in NOT_A_CALL_PREFIXES:
            continue
        seen.add(row.job_id)
        found.append(Sent(row.job_id, row.destination, max(1, int(row.pages or row.billed_pages or 1)), row.route,
                          row.created_at))
    return found


class _OverBudget:
    """Whether a flat plan had used its normal-use budget when a fax went, counted in the fax's billing period."""

    def __init__(self, engine, values):
        self.engine, self.values = engine, values
        self._periods = {}

    def __call__(self, budget, at):
        from .plan_budget import billing_period, records
        period = billing_period(at, budget.day, getattr(self.values, 'time_zone', '') or '')
        key = (budget.route, period.start)
        if key not in self._periods:
            self._periods[key] = records(self.engine, budget.route, period.start, period.end,
                                         page_time_seconds=budget.page_time_seconds)
        pages = faxes = 0
        for when, _, counted, _ in self._periods[key]:
            if when >= at:
                break
            pages, faxes = pages + counted, faxes + 1
        return ((budget.pages is not None and pages >= budget.pages)
                or (budget.faxes is not None and faxes >= budget.faxes))


# -- pricing one recipient ---------------------------------------------------------------------------------------------

class Recipient:
    """The faxes to one number, priced by the best allowed route and by each counterfactual."""

    def __init__(self, pricer, number, faxes, options, over_budget):
        self.pricer, self.number, self.faxes = pricer, number, faxes
        self.options, self.over_budget = options, over_budget

    def _facts(self, option):
        if option.route == 'direct':
            from .predict_facts import facts_for
            return facts_for('direct', option.number, engine=self.pricer.engine, values=self.pricer.values)
        return self.pricer.facts(option.route, option.number, account=option.key)

    def best(self, fax, options, *, swap=None, budgets=True):
        """(micros, currency) of the cheapest priced option for one fax; (None, None) when none has a price.

        ``swap`` maps an option key to counterfactual facts; ``budgets`` False keeps a plan past its normal-use
        budget (its allowance confirmed larger).
        """
        best = None
        for option in options:
            if budgets and option.plan_budget is not None and self.over_budget(option.plan_budget, fax.at):
                continue
            facts = (swap or {}).get(option.key) or self._facts(option)
            if facts.refused:
                continue
            cost = self.pricer.price(facts, fax.pages).cost
            if cost is None:
                continue
            if best is None or (cost.currency == best[1] and cost.micros < best[0]):
                best = (cost.micros, cost.currency)
        return best or (None, None)

    def total(self, options, *, faxes=None, **kwargs):
        """(micros, currency, priced faxes, unpriced faxes) over ``faxes`` (all by default)."""
        micros, currency, priced, unpriced = 0, None, 0, 0
        for fax in self.faxes if faxes is None else faxes:
            amount, unit = self.best(fax, options, **kwargs)
            if amount is None or (currency is not None and unit != currency):
                unpriced += 1
                continue
            micros, currency, priced = micros + amount, unit, priced + 1
        return micros, currency, priced, unpriced


# -- the advice --------------------------------------------------------------------------------------------------------

def _row(fact, *, saving=None, currency=None, faxes=0, establish_micros=0, establish_text=None, chance=None,
         confirm_values=None, sentence=None, break_even=None, unknown=False):
    """One priced fact for a recipient."""
    values = confirm_values or {}
    confirm = fact.confirm.format(**values)
    step = fact.step.format(**values)
    time = fact.time
    establish = (f'{establish_text} and {time}' if establish_text else time)
    net = None if saving is None else saving - (establish_micros or 0)
    if sentence is None:
        if saving is None:
            sentence = f'What {_count(faxes, "fax", "faxes")} would have cost is unknown.'
        else:
            sentence = (f'Would have cost {_about(saving, currency)} less over {_count(faxes, "fax", "faxes")} '
                        '(estimate)')
            if chance is not None:
                sentence += f', if it turns out as {round(chance * 100)}% of the numbers Faxbot has seen did'
            sentence += '.'
    sentence += f' Establishing it takes {establish}.'
    if fact.kind == 'authorization':
        sentence += ' Faxbot will not use it until then.'
    return {'fact': fact.key, 'kind': fact.kind, 'title': fact.title, 'boundary': fact.kind == 'authorization',
            'saving': _money(saving, currency), 'net': _money(net, currency), 'faxes': faxes,
            'establish_cost': _money(establish_micros, currency) if establish_micros else [],
            'establish': establish, 'confirm': confirm, 'step': step, 'chance': chance,
            'break_even': break_even, 'unknown': unknown, 'realized': False, 'sentence': sentence,
            '_net': net}


def _coding_chance(engine, since):
    """The share of numbers whose newest recorded call used MMR or JBIG coding; None when Faxbot recorded none."""
    from .database import DeliveryStoreError
    try:
        calls = reflect(engine, ('fax_engine_calls',))['fax_engine_calls']
    except DeliveryStoreError:
        # No engine call records in this database (an installation without a trunk): no evidence either way.
        return None
    if 'negotiation_by' not in calls.c:
        return None
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(calls.c.number, calls.c.compression, calls.c.created_at).where(
            calls.c.created_at >= since, calls.c.negotiation_by.is_not(None), calls.c.compression.is_not(None),
            calls.c.number.is_not(None)).order_by(calls.c.created_at)).all()
    newest = {}
    for number, compression, _ in rows:
        newest[number] = compression
    if not newest:
        return None
    return sum(1 for coding in newest.values() if coding in ('MMR', 'JBIG')) / len(newest)


def advice(engine, values, *, routes=None, now=None, days=WINDOW_DAYS):
    """What one missing fact would have saved, per recipient, over the last ``days`` days; never applied."""
    now = (now or utcnow()).replace(microsecond=0)
    since = now - timedelta(days=days)
    pricer = Pricer(engine, values, now)
    over_budget = _OverBudget(engine, values)
    faxes = sent_faxes(engine, since, now)
    by_number = {}
    for fax in faxes:
        by_number.setdefault(fax.number, []).append(fax)
    evidence = _Evidence(engine, values, routes, since)
    accounts = _permitted_accounts(values)
    coding_chance = _coding_chance(engine, since)
    recipients = []
    for number, items in sorted(by_number.items()):
        recipients.append(_recipient(pricer, number, items, accounts, evidence, over_budget, coding_chance, values))
    recipients.extend(_receiving_rows(engine, values, routes, now))
    recipients = [item for item in recipients if item['facts']]
    for item in recipients:
        item['facts'].sort(key=lambda row: (row['_net'] is None, -(row['_net'] or 0)))
        best = next((row for row in item['facts'] if row['_net'] is not None), None)
        item['largest'] = best['saving'] if best is not None else []
        item['_rank'] = best['_net'] if best is not None else None
    recipients.sort(key=lambda item: (item['_rank'] is None, -(item['_rank'] or 0), item['number']))
    for item in recipients:
        item.pop('_rank', None)
        for row in item['facts']:
            row.pop('_net', None)
    if not faxes and not recipients:
        sentence = f'You sent no faxes by a phone call in the last {days} days, so no missing fact cost you anything.'
        state = 'nothing_sent'
    elif not recipients:
        sentence = (f'No missing fact would have made your faxes of the last {days} days cheaper, as far as Faxbot '
                    'can tell.')
        state = 'none'
    else:
        priced = sum(1 for item in recipients if item['largest'])
        sentence = (f"Establishing one fact would have made faxes to {_count(len(recipients), 'recipient')} "
                    f'cheaper or priced over the last {days} days'
                    + (f'; {priced} of them with a known amount.' if priced != len(recipients) else '.'))
        state = 'advice'
    return {'days': days, 'state': state, 'sentence': sentence, 'recipients': recipients, 'estimate': True,
            'realized': REALIZED, 'note': ADVICE_ONLY,
            'catalogue': [{'fact': fact.key, 'kind': fact.kind, 'title': fact.title} for fact in CATALOGUE],
            'assumptions': [f'Each fax of the last {days} days is priced again at today\'s prices and plan use, '
                            'by its page count.',
                            'The best route you may use is the cheapest priced one among the accounts Faxbot may '
                            'choose automatically and what is already confirmed.',
                            'Each fact is priced on its own; a recipient\'s figure is its largest single fact.']}


class _Evidence:
    """The evidence each fact needs, read once for all recipients."""

    def __init__(self, engine, values, routes, since):
        from .store import RouteStore
        self.engine, self.values, self.since = engine, values, since
        self.routes = routes or RouteStore(engine)
        self.suggestions = self._suggestions()
        self.approvals = self._approvals()

    def _suggestions(self):
        """{number: organization} for open partner suggestions and enrollments still waiting for their code."""
        found = {}
        from ..direct.discovery import DiscoveryStore
        for row in DiscoveryStore(self.engine).open_suggestions():
            found.setdefault(row['number'], row.get('organization') or None)
        peers = reflect(self.engine, ('direct_peers',))['direct_peers']
        with read_connection(self.engine) as connection:
            for number, organization in connection.execute(sa.select(peers.c.phone_number, peers.c.organization)
                                                           .where(peers.c.state == 'pending')).all():
                found.setdefault(number, organization)
        return found

    def _approvals(self):
        from .tollfree import TollFreeApprovals
        return TollFreeApprovals(self.engine).all_current()

    def verified_peer(self, number):
        return self.routes.verified_peer(number, covered=True) is not None

    def destination(self, number):
        return self.routes.get_destination(number) or {}

    def digital(self, number):
        """(confirmed views, suggested views) of the recipient's Direct addresses and FHIR endpoints."""
        from ..digital.store import DigitalStore
        views = DigitalStore(self.engine).addresses_for(number)
        return ([view for view in views if view['state'] == 'confirmed'],
                [view for view in views if view['state'] == 'suggested'])

    def held_pages(self, number):
        """[(Sent-like job id, pages sent, pages already acknowledged)] for case packets to ``number`` in the window
        that carried documents the recipient had already acknowledged."""
        t = reflect(self.engine, ('case_packet_sends', 'case_entries', 'case_entry_sends', 'case_entry_events'))
        sends, entries, carried, events = (t['case_packet_sends'], t['case_entries'], t['case_entry_sends'],
                                           t['case_entry_events'])
        with read_connection(self.engine) as connection:
            packets = connection.execute(sa.select(sends.c.id, sends.c.pages_sent, sends.c.created_at).where(
                sends.c.recipient == number, sends.c.created_at >= self.since)).all()
            found = []
            for packet in packets:
                rows = connection.execute(sa.select(entries.c.id, entries.c.page_count).join(
                    carried, carried.c.entry_id == entries.c.id).where(carried.c.job_id == packet.id)).all()
                held = 0
                for entry_id, pages in rows:
                    latest = connection.execute(sa.select(events.c.kind).where(
                        events.c.entry_id == entry_id, events.c.occurred_at < packet.created_at).order_by(
                        events.c.occurred_at.desc(), events.c.id.desc()).limit(1)).scalar()
                    if latest == 'accepted':
                        held += int(pages or 0)
                if held:
                    found.append((packet.id, int(packet.pages_sent or 0), held))
        return found


def _options(pricer, number, accounts, evidence, *, dial=None):
    """The routes allowed now for ``number``: the accounts, plus a verified partner."""
    options = []
    for account in accounts:
        options.append(Option(account.key, account.provider, _label(account, pricer.values), dial or number,
                              _budget(pricer, account, number)))
    if evidence.verified_peer(number):
        options.append(Option('direct', 'direct', 'Direct delivery', number))
    return options


def _recipient(pricer, number, faxes, accounts, evidence, over_budget, coding_chance, values):
    from .receiving import shown_number
    destination = evidence.destination(number)
    name = destination.get('display_name') or shown_number(number)
    approval = evidence.approvals.get(number)
    options = _options(pricer, number, accounts, evidence)
    if approval is not None and approval['action'] == 'approved':
        # An approved toll-free number is established: each route that can call it may.
        options += [replace(option, key=f'{option.key}@toll-free', number=approval['alternate_number'])
                    for option in options if option.route != 'direct']
    recipient = Recipient(pricer, number, faxes, options, over_budget)
    baseline, currency, priced, unpriced = recipient.total(options)
    rows = []
    known = [fax for fax in faxes if recipient.best(fax, options)[0] is not None]
    values_for = {'name': name}

    def saving(counter_options, *, faxes=None, **kwargs):
        """(saving micros, currency, faxes it applies to) of a counterfactual against the same best allowed route."""
        group = [fax for fax in (known if faxes is None else faxes) if recipient.best(fax, options)[0] is not None]
        before, unit, _, _ = recipient.total(options, faxes=group)
        after, unit_after, counted, missing = recipient.total(counter_options, faxes=group, **kwargs)
        if not group or missing or (unit_after is not None and unit is not None and unit_after != unit):
            return None, unit, len(group)
        return max(0, before - after), unit, len(group)

    one_page = Sent('', number, 1, '', pricer.now)

    def one_fax_cost():
        amount, _ = recipient.best(one_page, options)
        return amount or 0

    # A partner who could take faxes with no call.
    if number in evidence.suggestions and not evidence.verified_peer(number):
        organization = evidence.suggestions[number] or name
        amount, unit, count = saving(options + [Option('direct', 'direct', 'Direct delivery', number)])
        if count:
            cost = one_fax_cost()
            rows.append(_row(FACTS['partner'], saving=amount, currency=unit, faxes=count, establish_micros=cost,
                             establish_text=f'one enrollment fax (about {_about(cost, unit)})' if unit else None,
                             confirm_values={'name': organization}))
    # A Direct address or FHIR endpoint someone suggested but nobody confirmed.
    confirmed, suggested = evidence.digital(number)
    if suggested and not confirmed:
        rows.append(_digital_row(pricer, recipient, options, known, suggested[0], values_for, values))
    # What the far fax machine accepts, on a trunk billed by time.
    trunks = [option for option in options if option.route == 'sip']
    swap = {}
    for option in trunks:
        facts = pricer.facts(option.route, option.number, account=option.key)
        card = facts.terms.card if facts.terms is not None else None
        if facts.link.coding is None and card is not None and card.per_minute_micros and not card.flat_plan:
            swap[option.key] = replace(facts, link=replace(facts.link, coding='MMR'))
    if swap:
        amount, unit, count = saving(options, swap=swap)
        if count and amount:
            route = next(option.label for option in trunks if option.key in swap)
            rows.append(_row(FACTS['capabilities'], saving=amount, currency=unit, faxes=count, chance=coding_chance,
                             confirm_values={**values_for, 'route': route},
                             sentence=(f'Would have cost up to {_about(amount, unit)} less over '
                                       f'{_count(count, "fax", "faxes")} (estimate), if its fax machine accepts the '
                                       'most compact coding'
                                       + (f', as {round(coding_chance * 100)}% of the numbers Faxbot has seen did.'
                                          if coding_chance is not None else '.'))))
    # A toll-free number on file that the recipient has not approved.
    if approval is not None and approval['action'] == 'noted':
        alternate = approval['alternate_number']
        extra = [replace(option, key=f'{option.key}@toll-free', number=alternate, plan_budget=option.plan_budget)
                 for option in options if option.route != 'direct']
        amount, unit, count = saving(options + extra)
        if count:
            rows.append(_row(FACTS['toll_free'], saving=amount, currency=unit, faxes=count,
                             confirm_values={**values_for, 'address': approval['alternate_display']}))
    # Case packets that resent documents the recipient had already acknowledged.
    if not destination.get('accepts_references'):
        rows.extend(_case_rows(recipient, options, evidence.held_pages(number), values_for))
    # A route you may use with no price for these calls.
    for option in options:
        if option.route == 'direct':
            continue
        facts = pricer.facts(option.route, option.number, account=option.key)
        if facts.refused or facts.terms is not None:
            continue
        rows.append(_price_row(pricer, recipient, options, option, facts))
    # A flat plan whose allowance is Faxbot's cautious start, not one the provider gave.
    for option in options:
        budget = option.plan_budget
        if budget is None or budget.source != 'default':
            continue
        held_back = [fax for fax in known if over_budget(budget, fax.at)]
        if not held_back:
            continue
        amount, unit, count = saving(options, faxes=held_back, budgets=False)
        if count:
            rows.append(_row(FACTS['plan_allowance'], saving=amount, currency=unit, faxes=count,
                             confirm_values={**values_for, 'route': option.label},
                             sentence=(f'{_count(count, "fax", "faxes")} went another way because your '
                                       f'{option.label} plan was past the cautious budget Faxbot starts with; had '
                                       f'{option.label} confirmed a larger allowance, they would have cost '
                                       f'{_about(amount, unit)} less (estimate).')))
    best_text = None
    if baseline and currency:
        best_text = (f'{_count(priced, "fax", "faxes")} to {name} cost {_about(baseline, currency)} by the best route '
                     'you may use (estimate)')
        if unpriced:
            best_text += f'; {_count(unpriced, "other has", "others have")} no price'
        best_text += '.'
    elif faxes:
        best_text = f'What {_count(len(faxes), "fax", "faxes")} to {name} cost by the routes you may use is unknown.'
    return {'number': number, 'display': shown_number(number), 'name': destination.get('display_name'),
            'kind': 'recipient', 'faxes': len(faxes), 'baseline': _money(baseline if priced else None, currency),
            'unpriced': unpriced, 'sentence': best_text, 'facts': rows}


def _digital_row(pricer, recipient, options, known, view, values_for, values):
    from ..digital import accounts as digital_accounts
    from ..digital.routes import ACCOUNT_KIND, price
    from ..digital.text import address_label, route_key
    fact = FACTS['digital_address']
    kind = ACCOUNT_KIND[view['kind']]
    account = (digital_accounts.digital_account(values, view['account_key']) if view.get('account_key')
               else digital_accounts.default_account(values, kind))
    address = address_label(view['kind'], view['address'], view.get('organization'))
    confirm = {**values_for, 'address': address}
    if account is None or account.kind != kind:
        return _row(fact, faxes=len(recipient.faxes), confirm_values=confirm, unknown=True,
                    sentence=(f'{_count(len(recipient.faxes), "fax", "faxes")} could have gone to {address}, and you '
                              'have no account for sending there yet, so what they would have cost is unknown.'))
    key = route_key(view['kind'], view['id'])
    before = after = 0
    unit = None
    for fax in known:
        amount, currency = recipient.best(fax, options)
        message = price(pricer.engine, values, account, key, recipient.number, fax.pages, now=pricer.now)
        if message.micros is None or (currency is not None and message.currency != currency):
            return _row(fact, faxes=len(known), confirm_values=confirm, unknown=True,
                        sentence=(f'{_count(len(known), "fax", "faxes")} could have gone to {address}, and that '
                                  'account has no price on file, so what they would have cost is unknown.'))
        before += amount
        after += min(amount, message.micros)
        unit = currency
    return _row(fact, saving=max(0, before - after), currency=unit, faxes=len(known), confirm_values=confirm)


def _case_rows(recipient, options, packets, values_for):
    if not packets:
        return []
    by_job = {fax.job_id: fax for fax in recipient.faxes}
    before = after = 0
    unit, count = None, 0
    for job_id, pages_sent, held in packets:
        fax = by_job.get(job_id)
        if fax is None:
            continue
        smaller = replace(fax, pages=max(1, pages_sent - held + 1))
        amount, currency = recipient.best(fax, options)
        lower, other = recipient.best(smaller, options)
        if amount is None or lower is None or other != currency:
            continue
        before, after, unit, count = before + amount, after + lower, currency, count + 1
    if not count:
        return []
    return [_row(FACTS['case_reuse'], saving=max(0, before - after), currency=unit, faxes=count,
                 confirm_values=values_for)]


def _price_row(pricer, recipient, options, option, facts):
    """Faxes a route with no price could have carried: how many, and the price below which it would have won."""
    fact = FACTS['route_price']
    known, minutes, pages = 0, 0, 0
    baseline, unit = 0, None
    for fax in recipient.faxes:
        amount, currency = recipient.best(fax, options)
        prediction = pricer.price(facts, fax.pages)
        if amount is None:
            continue
        known += 1
        baseline, unit = baseline + amount, currency
        pages += fax.pages
        if prediction.seconds is not None:
            minutes += max(1, math.ceil(prediction.seconds / 60))
    values = {'route': option.label}
    count = len(recipient.faxes)
    sentence = f'{option.label} publishes no price for these calls and you have entered none, so ' \
               f'{_count(count, "fax", "faxes")} could not be compared with it.'
    break_even = None
    if known and unit is not None and baseline:
        per_page = baseline // pages if pages else None
        per_minute = baseline // minutes if minutes else None
        parts = [f'{_about(per_minute, unit)} a minute' if per_minute else None,
                 f'{_about(per_page, unit)} a page' if per_page else None]
        parts = [part for part in parts if part]
        if parts:
            sentence += (f' If it charges less than {" or ".join(parts)}, it would have cost less than the '
                         f'{_about(baseline, unit)} they cost by the best route you may use.')
            break_even = {'per_minute': _money(per_minute, unit), 'per_page': _money(per_page, unit)}
    return _row(fact, faxes=count, confirm_values=values, sentence=sentence, break_even=break_even, unknown=True)


def _receiving_rows(engine, values, routes, now):
    """Your own numbers whose faxes could not be priced at one of your accounts: no receiving price there."""
    from .database import DeliveryStoreError
    from .number_placement import placement
    try:
        result = placement(engine, values, routes=routes, now=now)
    except DeliveryStoreError as error:
        # The received-fax records could not be read: no receiving facts this time, and the cause logged.
        import logging
        logging.getLogger(__name__).warning('Number placement could not be read: %s', error)
        return []
    fact = FACTS['receiving_price']
    found = []
    for row in result.get('numbers') or []:
        current = next((cost for cost in row['costs'] if cost['current']), None)
        missing = [cost for cost in row['costs'] if not cost['current'] and cost['reason']
                   and cost['reason'].startswith('Faxbot has no price for receiving at')]
        if not missing or current is None:
            continue
        facts = []
        for cost in missing:
            sentence = (f"{row['received']:,} {'fax' if row['received'] == 1 else 'faxes'} reached {row['display']} "
                        f"in the last {result['days']} days, and {cost['account']} has no receiving price, so Faxbot "
                        'cannot tell whether it would cost less there.')
            if current['monthly']:
                sentence += (f" If receiving there costs less than {current['monthly'][0]['amount']} "
                             f"{current['monthly'][0]['currency']} a month in all, it would.")
            facts.append(_row(fact, faxes=row['received'], confirm_values={'route': cost['account']},
                              sentence=sentence, break_even={'per_month': current['monthly']}, unknown=True))
        found.append({'number': row['number'], 'display': row['display'], 'name': None, 'kind': 'your_number',
                      'faxes': row['received'], 'baseline': current['monthly'], 'unpriced': 0,
                      'sentence': f"{row['display']} is one of your numbers, at {row['account']}.", 'facts': facts})
    return found
