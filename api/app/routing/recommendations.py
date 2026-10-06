"""Cost per delivered fax for people, and the sending recommendations built on it.

Advice only: nothing here changes a route or sends anything. "Use this route"
in the console and ``faxbot recipients set --preferred-route`` set a number's
preferred route through the destination endpoint.
"""
from .costs import format_amount
from .delivered import MIN_DELIVERED, WINDOW_DAYS, short_money_text
from .delivered_store import DeliveredEvidence
from .plan import RoutePlanner, route_label


def _money(micros, currency):
    return None if micros is None or not currency else {'currency': currency, 'amount': format_amount(micros)}


def cost_text(figure):
    """What one delivered fax cost on a route, as a table cell: "$0.0089", "About $0.012", "Included in your plan"."""
    state = figure.state
    if state == 'direct':
        return 'No charge, sent straight to the partner'
    if state == 'included':
        return 'Included in your plan'
    if state == 'mixed':
        return 'Charged in several currencies'
    if state == 'unpriced':
        return 'Unknown'
    if state == 'undelivered':
        return 'None delivered'
    amount = short_money_text(figure.per_delivered_micros, figure.currency)
    return f'About {amount}' if figure.estimate else amount


def basis_text(figure):
    """Where the cost comes from: "14 faxes billed", "9 faxes billed, 5 estimated", "1 fax with no price"."""
    if figure.state in ('direct', 'included'):
        return None
    parts = [(figure.reported, 'billed'), (figure.estimated, 'estimated'), (figure.unpriced, 'with no price')]
    parts = [(count, words) for count, words in parts if count]
    if not parts:
        return None
    (count, words), rest = parts[0], parts[1:]
    first = f"{count} {'fax' if count == 1 else 'faxes'} {words}"
    return ', '.join([first, *(f'{count} {words}' for count, words in rest)])


def route_view(figure):
    """One route's attempts to one number over the window, for the console and ``faxbot``."""
    return {'route': figure.route, 'label': route_label(figure.route), 'attempts': figure.attempts,
            'delivered': figure.delivered, 'failed': figure.failed, 'uncertain': figure.uncertain,
            'cancelled': figure.cancelled, 'delivered_percent': figure.delivered_percent,
            'cost_per_delivered': _money(figure.per_delivered_micros, figure.currency),
            'total_cost': (None if figure.state in ('direct', 'included', 'mixed')
                           else _money(figure.cost_micros, figure.currency)),
            'estimate': figure.estimate, 'charged_attempts': figure.reported,
            'estimated_attempts': figure.estimated, 'unpriced_attempts': figure.unpriced,
            'included_in_plan': figure.state == 'included', 'direct': figure.state == 'direct',
            'average_pages': figure.average_pages, 'average_connected_seconds': figure.average_seconds,
            'enough_evidence': figure.comparable(), 'cost_text': cost_text(figure), 'basis_text': basis_text(figure)}


def route_views(figures):
    """Routes in a steady order: the cheapest per delivered fax first, then the rest by name."""
    def rank(figure):
        per = figure.per_delivered_micros
        return (per is None, per or 0, route_label(figure.route))
    return [route_view(figure) for figure in sorted(figures.values(), key=rank)]


def delivered_costs_for(store, number=None):
    """``{destination: [route view]}`` over the last ``WINDOW_DAYS`` days (one destination with ``number``)."""
    found = DeliveredEvidence(store).by_destination(number=number, timing=number is not None)
    return {destination: route_views(figures) for destination, figures in found.items()}


NO_SENDING = (f'No cheaper routes yet. Faxbot compares routes for a number once each route has delivered at least '
              f'{MIN_DELIVERED} faxes to it in the last {WINDOW_DAYS} days.')


def sending_recommendations(store, revision, bound):
    """Numbers where another available, reliable route cost less per delivered fax than the one used first now.

    Both routes need at least ``MIN_DELIVERED`` delivered faxes, every attempt
    priced, in one currency. A flat plan or a direct partner is never compared
    as money. With no preferred route set, Faxbot already sends by the cheaper
    route, so a recommendation mostly means a preferred route costs more.
    """
    if revision is None or bound is None:
        return []
    values = revision.values
    planner = RoutePlanner(store, direct_ready=lambda: True)
    items = []
    for number, figures in sorted(DeliveredEvidence(store).by_destination(timing=False).items()):
        comparable = {key: figure for key, figure in figures.items() if figure.comparable()}
        if len(comparable) < 2:
            continue
        plan = planner.plan(to_number=number, bound=bound, values=values, pages=1, alternates=True)
        first = plan.first
        current = comparable.get(first.route.key)
        if current is None:
            continue
        usable = {choice.route.key for choice in plan.choices if choice.reason != 'unreliable'}
        options = [figure for key, figure in comparable.items()
                   if key in usable and key != current.route and figure.currency == current.currency]
        if not options:
            continue
        best = min(options, key=lambda figure: (figure.per_delivered_micros, route_label(figure.route)))
        if best.per_delivered_micros >= current.per_delivered_micros:
            continue
        row = store.get_destination(number) or {}
        chosen = first.reason == 'preferred'
        items.append({
            'number': number, 'display_name': row.get('display_name'), 'version': row.get('version') or 0,
            'preferred_route': row.get('preferred_route'), 'chosen_by_you': chosen,
            'current': route_view(current), 'suggested': route_view(best),
            'saving_per_fax': _money(current.per_delivered_micros - best.per_delivered_micros, best.currency),
            'sentence': _sentence(best, current, chosen)})
    return items


def _amount(figure):
    amount = short_money_text(figure.per_delivered_micros, figure.currency)
    return f'about {amount}' if figure.estimate else amount


def _sentence(best, current, chosen):
    which = 'the route you chose for it' if chosen else 'which Faxbot uses now'
    return (f'Over the last {WINDOW_DAYS} days, {route_label(best.route)} cost {_amount(best)} per delivered fax '
            f'to this number. {route_label(current.route)}, {which}, cost {_amount(current)}.')
