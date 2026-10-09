"""Costs → Savings parts for the money-saving mechanisms that had none before the savings map.

Each part counts what one mechanism did in the window from the records that mechanism already writes, so the
savings map (``routing/mechanisms.py``) and Costs → Savings read the same numbers. Most are counts: lightened
pages, faxes sent by their cheapest route or through a plan, pages a broken fax did not send again, calls turned
away. Those carry no money (``saved`` stays empty) and never change the total. A partner relay is the exception:
its own records keep what each relayed fax cost and what calling from here would have cost, so it is priced from
them, in the same currency only and never converted.
"""
import sqlalchemy as sa

from .database import read_connection, reflect
from .plan import route_label


def _plural(count, word, plural=None):
    return f'{count} {word}' if count == 1 else f'{count} {plural or word + "s"}'


def _faxes(count):
    return _plural(count, 'fax', 'faxes')


def _duration(seconds):
    """About how long, in the largest whole unit: "2 minutes", "40 seconds"."""
    if seconds >= 60:
        return _plural(seconds // 60, 'minute')
    return _plural(seconds, 'second')


def fax_friendly(engine, *, since, days):
    """Faxes whose shaded areas Faxbot lightened before sending (``pages/friendly.py``), on attempts that worked.

    The seconds are Faxbot's own estimate at full fax speed when it lightened the pages; the money is not priced
    here, because the call's billed time already includes the saving.
    """
    t = reflect(engine, ('fax_friendly_pages', 'delivery_attempt_costs'))
    pages, costs = t['fax_friendly_pages'], t['delivery_attempt_costs']
    worked = sa.exists().where(costs.c.outcome == 'success', sa.or_(
        costs.c.id == pages.c.attempt_id, sa.and_(pages.c.attempt_id.is_(None), costs.c.job_id == pages.c.job_id)))
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(pages.c.job_id, pages.c.pages_changed, pages.c.seconds_saved).where(
            pages.c.created_at >= since, pages.c.pages_changed > 0, worked)).all()
    result = {'faxes': len({row.job_id for row in rows}), 'pages': sum(row.pages_changed or 0 for row in rows),
              'seconds_saved': sum(row.seconds_saved or 0 for row in rows), 'saved': {}}
    if not result['faxes']:
        result['sentence'] = f'No shaded pages were lightened in the last {days} days.'
        return result
    sentence = (f"Shaded areas were lightened on {_plural(result['pages'], 'page')} of "
                f"{_faxes(result['faxes'])}")
    if result['seconds_saved']:
        sentence += f", about {_duration(result['seconds_saved'])} less on the line at full fax speed"
    result['sentence'] = sentence + '.'
    return result


def _decided(engine, reason, since):
    """``(route, job)`` for each delivered attempt Faxbot routed for ``reason`` since ``since``."""
    costs = reflect(engine, ('delivery_attempt_costs',))['delivery_attempt_costs']
    with read_connection(engine) as connection:
        return connection.execute(sa.select(costs.c.route, costs.c.job_id).where(
            costs.c.route_reason == reason, costs.c.outcome == 'success', costs.c.created_at >= since)).all()


def cheapest_route(engine, *, since, days):
    """Faxes delivered by the route with the lowest cost per delivered fax to their number (``cheapest_delivered``).

    Only the reason is stored with a sent fax, never what the other route would have cost, so this is a count.
    """
    rows = _decided(engine, 'cheapest_delivered', since)
    result = {'faxes': len({row.job_id for row in rows}), 'saved': {}}
    result['sentence'] = (
        f"{_faxes(result['faxes'])} went by the route that cost least per delivered fax to "
        f"{'its number' if result['faxes'] == 1 else 'their numbers'}."
        if result['faxes'] else
        f'No fax in the last {days} days went by a route chosen for its lower cost per delivered fax.')
    return result


def plan_first(engine, *, since, days):
    """Faxes delivered through a monthly plan with nothing charged for them (``included``), by plan."""
    by_route = {}
    for row in _decided(engine, 'included', since):
        by_route.setdefault(row.route, set()).add(row.job_id)
    result = {'faxes': sum(len(jobs) for jobs in by_route.values()), 'plans': len(by_route), 'saved': {}}
    if not result['faxes']:
        result['sentence'] = f'No fax went through a monthly plan in the last {days} days.'
        return result
    parts = [f"{_faxes(len(jobs))} through your {route_label(route)} plan"
             for route, jobs in sorted(by_route.items(), key=lambda item: (-len(item[1]), item[0]))]
    listed = parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]
    result['sentence'] = f'{listed[:1].upper() + listed[1:]}, with nothing charged per fax.'
    return result


def relay(engine, *, since, days, home=None):
    """Faxes partners relayed for you as local calls, against what calling from here would have cost.

    Read from the relay's own records (``relay_faxes``): a delivered fax with both amounts known, in one currency,
    is priced; any other is counted as not priced. Signed, so a relay that cost more lowers the total.
    """
    from ..direct.relay import country_name, money_list_for
    from ..direct.relay_store import RelayStore
    result = {'faxes': 0, 'priced': 0, 'unpriced': 0, 'saved': {}}
    for fax in RelayStore(engine).faxes_since(since, role='sender'):
        if fax['state'] != 'delivered':
            continue
        result['faxes'] += 1
        cost, own = fax['cost_micros'], fax['own_route_micros']
        if cost is None or own is None or not fax['cost_currency'] or fax['cost_currency'] != fax['own_route_currency']:
            result['unpriced'] += 1
            continue
        result['priced'] += 1
        currency = fax['cost_currency']
        result['saved'][currency] = result['saved'].get(currency, 0) + int(own) - int(cost)
    if not result['faxes']:
        result['sentence'] = f'No fax went through a partner relay in the last {days} days.'
        return result
    sentence = f"{_faxes(result['faxes'])} went through partner relays as local calls"
    saved = {currency: micros for currency, micros in result['saved'].items() if micros > 0}
    more = {currency: -micros for currency, micros in result['saved'].items() if micros < 0}
    place = country_name(home or 'US')
    if saved:
        sentence += f", saving about {money_list_for(saved, home)} against calling from {place}"
    if more:
        sentence += f"{' but' if saved else ','} costing about {money_list_for(more, home)} more than calling from {place}"
    sentence += '.'
    if result['unpriced']:
        sentence += f" {_plural(result['unpriced'], 'fax', 'faxes')} could not be compared, because a price is missing."
    result['sentence'] = sentence
    return result


def continuation(engine, *, since, days):
    """Broken faxes finished by sending only the pages their call did not confirm (``fax_continuations``)."""
    table = reflect(engine, ('fax_continuations',))['fax_continuations']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(table.c.first_page).where(table.c.created_at >= since)).all()
    result = {'faxes': len(rows), 'pages_not_resent': sum(max(0, row.first_page - 1) for row in rows), 'saved': {}}
    result['sentence'] = (
        f"{_plural(result['faxes'], 'broken fax', 'broken faxes')} went on from where "
        f"{'its call' if result['faxes'] == 1 else 'their calls'} stopped: "
        f"{_plural(result['pages_not_resent'], 'page')} not sent again."
        if result['faxes'] else
        f'No broken fax was finished by sending only its remaining pages in the last {days} days.')
    return result


def partner_repair(engine, *, since, days):
    """Broken calls to partners completed by sending only the missing pages directly (``direct/repair.py``)."""
    table = reflect(engine, ('direct_call_repairs',))['direct_call_repairs']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(table.c.pages_held).where(
            table.c.role == 'sender', table.c.state == 'completed', table.c.created_at >= since)).all()
    result = {'faxes': len(rows), 'pages_not_resent': sum(row.pages_held or 0 for row in rows), 'saved': {}}
    result['sentence'] = (
        f"{_plural(result['faxes'], 'broken call')} to partners finished with only the missing pages: "
        f"{_plural(result['pages_not_resent'], 'page')} the partner already had {'was' if result['pages_not_resent'] == 1 else 'were'} "
        'not sent again.'
        if result['faxes'] else
        f'No broken call to a partner needed its missing pages in the last {days} days.')
    return result


# Each side of the fax over IP comparison needs this many answered calls with a known length and pages.
MIN_CALLS = 10


def _per_page(seconds, pages):
    return round(seconds / pages) if pages else None


def fax_over_ip(engine, *, since, days):
    """Seconds a page on fax over IP (T.38) calls against audio fax calls, from the trunk's own call records.

    Measured, not priced: answered calls with a result, sent and received, divided by the pages they confirmed
    (as Recent calls' negotiation summary does). The comparison is said only when each side has ``MIN_CALLS``.
    """
    table = reflect(engine, ('sip_call_records',))['sip_call_records']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(table.c.t38, table.c.connected_seconds, table.c.pages).where(
            table.c.started_at >= since, table.c.answered_at.is_not(None), table.c.fax_status.is_not(None),
            table.c.t38.in_(('yes', 'no')))).all()
    sides = {'yes': {'calls': 0, 'measured': 0, 'seconds': 0, 'pages': 0},
             'no': {'calls': 0, 'measured': 0, 'seconds': 0, 'pages': 0}}
    for row in rows:
        side = sides[row.t38]
        side['calls'] += 1
        if row.connected_seconds is not None and row.pages:
            side['measured'] += 1
            side['seconds'] += row.connected_seconds
            side['pages'] += row.pages
    t38, audio = sides['yes'], sides['no']
    result = {'calls': t38['calls'], 'audio_calls': audio['calls'],
              'seconds_per_page': _per_page(t38['seconds'], t38['pages']),
              'audio_seconds_per_page': _per_page(audio['seconds'], audio['pages']), 'saved': {}}
    if not t38['calls']:
        result['sentence'] = f'No fax over IP (T.38) calls in the last {days} days.'
        return result
    if t38['measured'] >= MIN_CALLS and audio['measured'] >= MIN_CALLS:
        result['sentence'] = (
            f"Fax over IP (T.38) took about {result['seconds_per_page']} seconds a page over "
            f"{_plural(t38['measured'], 'call')}, and audio fax about {result['audio_seconds_per_page']} seconds a "
            f"page over {_plural(audio['measured'], 'call')}.")
        return result
    fewer = 'audio fax' if audio['measured'] < MIN_CALLS else 'fax over IP'
    result['sentence'] = (f"{_plural(t38['calls'], 'call')} used fax over IP (T.38) in the last {days} days; there "
                          f'are too few {fewer} calls to compare seconds a page (each side needs {MIN_CALLS}).')
    return result


def digital(engine, *, since, days):
    """Faxes delivered as Direct messages or to FHIR servers (ledger routes ``dsm.<id>``, ``fhir.<id>``)."""
    costs = reflect(engine, ('delivery_attempt_costs',))['delivery_attempt_costs']
    with read_connection(engine) as connection:
        jobs = connection.execute(sa.select(costs.c.job_id).where(
            costs.c.outcome == 'success', costs.c.created_at >= since,
            sa.or_(costs.c.route.like('dsm.%'), costs.c.route.like('fhir.%'))).distinct()).scalars().all()
    result = {'faxes': len(jobs), 'saved': {}}
    result['sentence'] = (
        f"{_faxes(result['faxes'])} went as Direct messages or to FHIR servers instead of a fax call."
        if result['faxes'] else f'No fax went as a Direct message or to a FHIR server in the last {days} days.')
    return result


# Fax speed for the time a coding saves, as Faxbot's other page estimates use (14,400 bit/s).
LINE_BITS_PER_SECOND = 14_400
CODINGS = ('MH', 'MR', 'MMR', 'JBIG')  # least to most compact, as pages/coding.py ranks them


def page_coding(engine, *, since, days):
    """Faxes whose page coding was measured (``fax_coding_choices``, pages/coding.py), on attempts that worked.

    A choice saves time only when it is smaller than the coding the fax engine would have taken without measuring
    (the most compact usable one, which ``compared`` then names). When the choice is that coding, ``compared`` is
    the next smallest and nothing was saved. The time is an estimate at full fax speed; no money is added, because
    the call's billed time already includes it.
    """
    import json
    t = reflect(engine, ('fax_coding_choices', 'delivery_attempt_costs'))
    choices, costs = t['fax_coding_choices'], t['delivery_attempt_costs']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(choices.c.job_id, choices.c.requested, choices.c.compared,
                                            choices.c.measured, choices.c.bits).join(
            costs, costs.c.id == choices.c.attempt_id).where(
            costs.c.outcome == 'success', choices.c.created_at >= since)).all()
    result = {'faxes': len({row.job_id for row in rows}), 'smaller': 0, 'seconds_saved': 0, 'saved': {}}
    for row in rows:
        if not row.measured or row.compared not in CODINGS or row.requested not in CODINGS:
            continue
        if CODINGS.index(row.compared) <= CODINGS.index(row.requested):
            continue  # the engine's own choice: nothing saved by measuring
        bits = json.loads(row.bits or '{}')
        mine, theirs = bits.get(row.requested), bits.get(row.compared)
        if isinstance(mine, int) and isinstance(theirs, int) and theirs > mine:
            result['smaller'] += 1
            result['seconds_saved'] += (theirs - mine) // LINE_BITS_PER_SECOND
    if not result['faxes']:
        result['sentence'] = f'No fax had its page coding measured in the last {days} days.'
        return result
    sentence = f"Page codings were measured on {_faxes(result['faxes'])}"
    if result['smaller']:
        sentence += (f"; on {result['smaller']} a smaller coding than the fax engine's own choice saved about "
                     f"{_duration(result['seconds_saved'])} on the line at full fax speed")
    else:
        sentence += "; the fax engine's own choice was already the smallest"
    result['sentence'] = sentence + '.'
    return result


def tunnel_calls(engine, *, since, days):
    """Faxes sent to partners as fax calls inside a private tunnel, with no carrier (``sip_call_records.peer_id``)."""
    table = reflect(engine, ('sip_call_records',))['sip_call_records']
    with read_connection(engine) as connection:
        calls = connection.scalar(sa.select(sa.func.count()).select_from(table).where(
            table.c.peer_id.is_not(None), table.c.direction == 'outbound', table.c.fax_status == 'SUCCESS',
            table.c.started_at >= since))
    result = {'faxes': int(calls or 0), 'saved': {}}
    one = result['faxes'] == 1
    result['sentence'] = (
        f"{_faxes(result['faxes'])} went to {'a partner as a fax call' if one else 'partners as fax calls'} inside a "
        'private tunnel, with no carrier.'
        if result['faxes'] else f'No fax went to a partner over a private tunnel in the last {days} days.')
    return result


def blocked_calls(engine, *, since, days):
    """Calls from blocked senders that Asterisk turned away before answering (``screened_call_rejections``)."""
    table = reflect(engine, ('screened_call_rejections',))['screened_call_rejections']
    with read_connection(engine) as connection:
        calls = connection.scalar(sa.select(sa.func.count()).select_from(table).where(table.c.rejected_at >= since))
    result = {'calls': int(calls or 0), 'saved': {}}
    result['sentence'] = (
        f"{_plural(result['calls'], 'call')} from blocked senders {'was' if result['calls'] == 1 else 'were'} "
        'turned away before Faxbot answered.'
        if result['calls'] else
        f'No call from a blocked sender came in the last {days} days.')
    return result
