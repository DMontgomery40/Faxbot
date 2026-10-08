"""What Faxbot already knows about this installation, gathered for guided setup.

Every fact is read from stored state through the owning module's public
functions: accounts and settings, mailboxes and their numbers, the sending
rules, delivery history and the advice each module already gives. Nothing
here writes, sends, dials or fetches: a preview must change nothing and start
no external action, so lookups that reach the internet (the provider
registry, partner cards, price refreshes, carrier charge reads) are never
called from here.
"""
from dataclasses import dataclass, field
from datetime import timedelta
import json

import sqlalchemy as sa

from ..routing.database import read_connection, reflect, utcnow


WINDOW_DAYS = 30
# Recipients whose learned busy hours are read: the most-faxed numbers of the window.
BUSY_NUMBERS = 20
# A route that failed this many times to one number, with fewer than half delivered, needs a fallback.
FAILED_TRIES = 3
# Another route needs this many delivered faxes to the same number to be the fallback.
DELIVERED_TRIES = 2
# A blocked sender whose block ends within this many days, and that called while blocked, is worth keeping.
JUNK_ENDING_DAYS = 14


@dataclass(frozen=True)
class Facts:
    now: object
    values: object                         # the configuration revision's values (desired)
    config_revision: str                   # its revision ID
    pending_restart: bool                  # saved settings wait for a restart
    home: str                              # the installation country, for number classes only
    accounts: tuple = ()                   # rules.model.Account, in configured order
    mailboxes: tuple = ()                  # {'id', 'name', 'numbers'}
    workflows: tuple = ()                  # {'key', 'name'} from the organization's rules
    rules: dict = field(default_factory=dict)  # scope name -> {'kind', 'scope_id', 'active', 'document', 'draft'}
    sending: tuple = ()                    # routing.recommendations.sending_recommendations items
    country_rules: tuple = ()              # routing.recommendations.country_rules
    history: tuple = ()                    # (destination, route, pages) of each delivered fax in the window
    toll_free: tuple = ()                  # routing.tollfree.toll_free_recommendations items
    partners: tuple = ()                   # routing.partners.partner_candidates items
    discovered: tuple = ()                 # open partner discovery suggestions
    relay_offers: tuple = ()               # direct.relay recommendations
    send_once_offers: tuple = ()           # send-once agreements a partner offered and you have not accepted
    reply: dict = field(default_factory=dict)  # routing.reply_http.reply_view
    unplaced: tuple = ()                   # {'number', 'faxes'}: received with no mailbox in the window
    junk: tuple = ()                       # {'number', 'marks', 'active', 'expires_at', 'rejected'}
    fallbacks: tuple = ()                  # {'number', 'name', 'current', 'better', ...}
    busy: tuple = ()                       # {'number', 'name', 'learn', 'slots'}
    long_pages: tuple = ()                 # {'route', 'label', 'on', 'set'}
    plan_budgets: tuple = ()               # {'route', 'label', 'source', 'sentence'}


def _since(now):
    return now - timedelta(days=WINDOW_DAYS)


# Mailboxes and rules -----------------------------------------------------------------------------------------

def mailboxes(engine, values):
    from ..routing.reply_number import mailbox_labels, mailbox_routes
    labels = mailbox_labels(engine)
    numbers = {}
    for route in mailbox_routes(engine, values):
        numbers.setdefault(route.mailbox_id, []).append(route.number)
    return tuple({'id': key, 'name': label, 'numbers': sorted(set(numbers.get(key, ())))}
                 for key, label in sorted(labels.items(), key=lambda item: (item[1].casefold(), item[0])))


def rules_state(rule_store, mailbox_ids):
    """Every scope a plan may touch: the organization and each mailbox, with its active document and any draft."""
    from ..rules import model
    from ..rules.store import empty_document
    found = {}
    for kind, scope_id in [(model.ORGANIZATION, '')] + [(model.MAILBOX, key) for key in mailbox_ids]:
        active = rule_store.active(kind, scope_id)
        found[model.scope_name(kind, scope_id)] = {
            'kind': kind, 'scope_id': scope_id, 'active': active['number'] if active else None,
            'document': json.loads(active['document']) if active else empty_document(),
            'draft': rule_store.draft(kind, scope_id) is not None}
    return found


def workflows(rules):
    from ..rules import model
    document = rules.get(model.ORGANIZATION, {}).get('document') or {}
    return tuple({'key': item['key'], 'name': item.get('name') or item['key']}
                 for item in document.get('workflows') or () if isinstance(item, dict) and item.get('key'))


# Delivery history --------------------------------------------------------------------------------------------

def history(route_store, now):
    """(destination, route, pages) for each fax delivered in the window: what a rule's saving is predicted on."""
    costs, jobs = route_store.costs, route_store.jobs
    with read_connection(route_store.engine) as connection:
        rows = connection.execute(
            sa.select(costs.c.destination, costs.c.route, jobs.c.pages)
            .select_from(costs.join(jobs, jobs.c.id == costs.c.job_id))
            .where(costs.c.outcome == 'success', costs.c.created_at >= _since(now))
            .order_by(costs.c.created_at, costs.c.id).limit(20_000)).all()
    return tuple((row.destination, row.route, int(row.pages or 1)) for row in rows
                 if row.destination and row.route)


def sending(route_store, revision, bound):
    from ..routing.recommendations import country_rules, sending_recommendations
    items = sending_recommendations(route_store, revision, bound)
    return tuple(items), tuple(country_rules(items))


def fallbacks(route_store, revision, bound, accounts):
    """Numbers where the route Faxbot tries first now keeps failing and another of your routes delivers."""
    from ..routing.delivered_store import DeliveredEvidence
    from ..routing.plan import RoutePlanner, route_label
    if revision is None or bound is None:
        return ()
    sends = {account.key for account in accounts if account.sends and account.enabled}
    minimum = int(getattr(revision.values, 'route_min_success_percent', 80) or 0)
    planner = RoutePlanner(route_store, direct_ready=lambda: True)
    found = []
    for number, figures in sorted(DeliveredEvidence(route_store).by_destination(timing=False).items()):
        failing = [figure for figure in figures.values()
                   if figure.failed >= FAILED_TRIES and figure.delivered * 2 < figure.attempts]
        if not failing:
            continue
        plan = planner.plan(to_number=number, bound=bound, values=revision.values, pages=1, alternates=True)
        current = figures.get(plan.first.route.key)
        if current is None or current not in failing or current.route not in sends:
            continue
        better = sorted((figure for figure in figures.values()
                         if figure.route in sends and figure.route != current.route
                         and figure.delivered >= DELIVERED_TRIES
                         and figure.delivered * 100 >= minimum * max(1, figure.attempts)),
                        key=lambda figure: (-figure.delivered, figure.route))
        if not better:
            continue
        row = route_store.get_destination(number) or {}
        best = better[0]
        found.append({'number': number, 'name': row.get('display_name'), 'current': current.route,
                      'current_label': route_label(current.route), 'failed': current.failed,
                      'attempts': current.attempts, 'better': best.route, 'better_label': route_label(best.route),
                      'delivered': best.delivered, 'better_attempts': best.attempts,
                      'chosen_by_you': plan.first.reason == 'preferred'})
    return tuple(found)


def busy(engine, values, history_rows, now):
    """Learned busy hours of the most-faxed recipients; Faxbot already waits them out on its own."""
    from ..routing.schedule import for_engine
    counts = {}
    for destination, _, _ in history_rows:
        counts[destination] = counts.get(destination, 0) + 1
    numbers = [number for number, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))][:BUSY_NUMBERS]
    if not numbers:
        return ()
    scheduler = for_engine(engine)
    found = []
    with read_connection(engine) as connection:
        for number in numbers:
            settings = scheduler.settings(connection, number, values)
            slots = scheduler.busy_hours(connection, number, settings, now).busy_slots()
            if slots:
                found.append({'number': number, 'learn': settings.learn_busy,
                              'slots': [{'label': slot.label(), 'summary': slot.summary()} for slot in slots]})
    return tuple(found)


# Partners ---------------------------------------------------------------------------------------------------

def discovered(engine):
    from ..direct.discovery import DiscoveryStore
    return tuple({'id': row['id'], 'number': row['number'], 'organization': row.get('organization'),
                  'source': row.get('source')} for row in DiscoveryStore(engine).open_suggestions())


def send_once_offers(engine):
    """Agreements a partner signed to file your faxes once, still waiting for your acceptance."""
    from ..direct.distribute import DistributionStore
    from ..direct.store import DirectStore
    peers = {row['id']: row for row in DirectStore(engine).list_peers()}
    found = []
    for row in DistributionStore(engine).agreements_for(role='sender'):
        if row['state'] != 'offered':
            continue
        numbers = json.loads(row['numbers']) if isinstance(row['numbers'], str) else list(row['numbers'] or ())
        peer = peers.get(row['peer_id']) or {}
        found.append({'agreement_id': row['id'], 'partner': peer.get('organization') or 'A partner',
                      'intake': row.get('intake'), 'numbers': numbers})
    return tuple(found)


# Receiving --------------------------------------------------------------------------------------------------

def unplaced(engine, now):
    """Your numbers whose faxes arrived with no mailbox in the window, with how many did."""
    faxes = reflect(engine, ('inbound_faxes',))['inbound_faxes']
    with read_connection(engine) as connection:
        rows = connection.execute(
            sa.select(faxes.c.to_number, sa.func.count().label('count'))
            .where(faxes.c.mailbox_label.is_(None), faxes.c.to_number.is_not(None),
                   faxes.c.received_at >= _since(now))
            .group_by(faxes.c.to_number).order_by(sa.func.count().desc(), faxes.c.to_number)).all()
    return tuple({'number': row.to_number, 'faxes': int(row.count)} for row in rows if row.to_number)


def junk(engine, now):
    """Senders you marked as junk more than once, or whose block ends soon while they keep calling."""
    from ..inbound.screening import ScreeningStore
    store = ScreeningStore(engine)
    rejected = store.counts()
    marks = {}
    for entry in store.entries(limit=500):
        marks.setdefault(entry['number'], []).append(entry)
    found = []
    for number, entries in sorted(marks.items()):
        active = [entry for entry in entries if entry['removed_at'] is None and entry['expires_at'] > now]
        current = max(active, key=lambda entry: entry['expires_at']) if active else None
        calls = rejected.get(current['id'], 0) if current else 0
        ending = current is not None and current['expires_at'] <= now + timedelta(days=JUNK_ENDING_DAYS)
        if (current is None and len(entries) >= 2) or (ending and calls >= 1):
            found.append({'number': number, 'marks': len(entries), 'active': current is not None,
                          'expires_at': current['expires_at'] if current else None, 'rejected': calls,
                          'removed': all(entry['removed_at'] is not None for entry in entries)})
    # A sender you unblocked yourself is your decision; it is not suggested again.
    return tuple(item for item in found if not item.pop('removed'))


# Costs ------------------------------------------------------------------------------------------------------

def long_pages(engine, accounts):
    from ..pages.capability import records_for, route_default
    from ..routing.plan import route_label
    records = records_for(engine)
    found = []
    for account in accounts:
        if not account.sends or not account.automatic or not route_default(account.key)[1]:
            continue
        on, chosen = records.route_long_pages(account.key)
        found.append({'route': account.key, 'label': account.label or route_label(account.key), 'on': on,
                      'set': chosen})
    return tuple(found)


def plan_budgets(route_store, values, accounts):
    from ..routing.plan_budget import budget_for
    found = []
    for account in accounts:
        if not account.sends:
            continue
        budget = budget_for(account.key, route_store.card_for(account.key), values)
        if budget is not None:
            found.append({'route': account.key, 'label': budget.label, 'source': budget.source,
                          'sentence': budget.sentence})
    return tuple(found)


# Everything -------------------------------------------------------------------------------------------------

def gather(engine, snapshot, *, bound=None, relay=None, now=None):
    """The facts for one preview. ``snapshot`` is the configuration snapshot; ``bound`` the active outbound
    provider (as the route planner names it); ``relay`` a ``RelayService`` whose stored prices give relay offers."""
    from ..accounts import sending_accounts
    from ..routing.partners import partner_candidates
    from ..routing.reply_http import reply_view
    from ..routing.store import RouteStore
    from ..routing.tollfree import toll_free_recommendations
    from ..rules.store import RuleStore
    now = now or utcnow()
    values = snapshot.desired.values
    accounts = tuple(sending_accounts(values))
    route_store = RouteStore(engine)
    boxes = mailboxes(engine, values)
    rules = rules_state(RuleStore(engine), [box['id'] for box in boxes])
    revision = snapshot.active
    items, by_country = sending(route_store, revision, bound)
    rows = history(route_store, now)
    return Facts(
        now=now, values=values, config_revision=snapshot.desired.id, pending_restart=snapshot.pending is not None,
        home=(getattr(values, 'fax_default_country', '') or 'US').upper(), accounts=accounts, mailboxes=boxes,
        workflows=workflows(rules), rules=rules, sending=items, country_rules=by_country, history=rows,
        toll_free=tuple(toll_free_recommendations(route_store)['items']),
        partners=tuple(partner_candidates(route_store)['items']), discovered=discovered(engine),
        relay_offers=tuple(relay.recommendations(now=now)) if relay is not None else (),
        send_once_offers=send_once_offers(engine), reply=reply_view(values, engine),
        unplaced=unplaced(engine, now), junk=junk(engine, now),
        fallbacks=fallbacks(route_store, revision, bound, accounts), busy=busy(engine, values, rows, now),
        long_pages=long_pages(engine, accounts), plan_budgets=plan_budgets(route_store, values, accounts))
