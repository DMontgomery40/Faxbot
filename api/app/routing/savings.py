"""What sending together, direct delivery, case packets, own numbers and approved toll-free numbers saved: always estimates.

Every saving compares what Faxbot sent with calls or pages that never
happened, so each figure stays an estimate even after a carrier reports its
charges. A fax that would have gone through a flat monthly plan saved no
money, only the call; it is counted as included in the plan, never priced.

Case packets are counted from the first ``case_packet_sends`` row: packets
sent before Faxbot recorded what each one left out are not counted.
"""
from datetime import timedelta

import sqlalchemy as sa

from ..batching import money
from ..batching.store import tables as batching_tables
from .costs import Money, estimate_cost, money_text
from .database import read_connection, reflect, utcnow
from .policy import DIRECT


WINDOW_DAYS = 30
SENTENCE = ('Each figure is an estimate: what you paid compared with what the same faxes would have cost '
            'the usual way.')


def _add(totals, currency, micros):
    totals[currency] = totals.get(currency, 0) + micros


def _money_text(totals):
    return ' + '.join(money_text(micros, currency) for currency, micros in sorted(totals.items()))


def _signed(totals):
    """``(saved, more)``: the parts of a signed saving that saved money, and those that cost more (as positives)."""
    return ({currency: micros for currency, micros in totals.items() if micros >= 0},
            {currency: -micros for currency, micros in totals.items() if micros < 0})


def _plural(count, word, plural=None):
    return f'{count} {word}' if count == 1 else f'{count} {plural or word + "s"}'


def _avoided(card, with_pages, without_pages, result):
    """Price what leaving pages or a call out avoided with ``card``; count it priced, in a plan or unpriced."""
    if card is None:
        result['unpriced'] += 1
    elif card.flat_plan:
        result['in_plan'] += 1
    else:
        result['priced'] += 1
        cost = Money(estimate_cost(card, with_pages), card.currency)
        if without_pages is not None:
            cost -= Money(estimate_cost(card, without_pages), card.currency)
        # Signed: something that cost more is a negative saving, never 0.
        _add(result['saved'], card.currency, cost.micros)


def sending_together(routes, engine, *, now, days, separator_pages=None):
    """Calls saved by faxes that shared a call, over every number that sends together.

    ``separator_pages``, when given, is a dict that also sums the separator pages shared calls left out
    (``batching.money.savings``); those are counted apart from, and never inside, the calls saved.
    """
    members = batching_tables(engine)['outbound_batch_members']
    since = now - timedelta(days=days)
    with read_connection(engine) as connection:
        numbers = connection.execute(sa.select(members.c.phone_number).where(
            members.c.state == 'together', members.c.batch_id.is_not(None),
            members.c.updated_at >= since).distinct()).scalars().all()
    result = {'numbers': 0, 'calls': 0, 'faxes': 0, 'calls_saved': 0, 'priced_calls': 0, 'saved': {}}
    for number in sorted(numbers):
        part = money.savings(routes, engine, number, now=now, days=days)
        if not part['calls']:
            continue
        result['numbers'] += 1
        for key in ('calls', 'faxes', 'calls_saved', 'priced_calls'):
            result[key] += part[key]
        for currency, micros in part['saved'].items():
            _add(result['saved'], currency, micros)
        if separator_pages is not None:
            for key in ('calls', 'pages_saved', 'priced_calls'):
                separator_pages[key] += part['separator_pages'][key]
            for currency, micros in part['separator_pages']['saved'].items():
                _add(separator_pages['saved'], currency, micros)
    if not result['calls']:
        result['sentence'] = f'No faxes were sent together in the last {days} days.'
        return result
    same = 'the same number' if result['numbers'] == 1 else 'the same numbers'
    sentence = (f"{_plural(result['faxes'], 'fax', 'faxes')} to {same} went in "
                f"{_plural(result['calls'], 'call')} instead of {result['faxes']}, saving "
                f"{_plural(result['calls_saved'], 'call')}")
    saved, more = _signed(result['saved'])
    if saved:
        sentence += f" and about {_money_text(saved)}"
    if more:
        shared = 'that call' if result['calls'] == 1 else 'those calls'
        sentence += f", but {shared} cost about {_money_text(more)} more than {result['faxes']} separate calls"
    sentence += '.'
    if result['priced_calls'] < result['calls']:
        sentence += " Some calls have no price, because your carrier's prices are not entered in Costs."
    result['sentence'] = sentence
    return result


def _accepted_directly(routes, engine, *, since, fax_images):
    """Faxes a partner accepted directly, originals or fax images (0034 ``kind``), each priced on its own provider."""
    t = reflect(engine, ('direct_deliveries', 'fax_jobs', 'delivery_attempt_costs'))
    d, jobs, costs = t['direct_deliveries'], t['fax_jobs'], t['delivery_attempt_costs']
    faxed = sa.exists().where(costs.c.job_id == d.c.job_id, costs.c.route != DIRECT, costs.c.outcome == 'success')
    kind = d.c.kind == 'fax_image' if fax_images else d.c.kind.is_(None)
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(jobs.c.backend, jobs.c.pages).join(jobs, jobs.c.id == d.c.job_id).where(
            d.c.direction == 'outbound', d.c.state == 'accepted', kind,
            sa.func.coalesce(d.c.accepted_at, d.c.updated_at) >= since, ~faxed)).all()
    result = {'faxes': 0, 'calls_avoided': 0, 'pages': 0, 'priced': 0, 'in_plan': 0, 'unpriced': 0, 'saved': {}}
    cards = {}
    for backend, pages in rows:
        result['faxes'] += 1
        result['calls_avoided'] += 1
        result['pages'] += pages or 0
        if backend not in cards:
            cards[backend] = routes.card_for(backend) if backend else None
        _avoided(cards[backend], pages, None, result)
    return result


def direct_fax_images(routes, engine, *, since, days):
    """Telephone calls avoided by fax images a partner accepted directly (M1a); never counted as faxed."""
    result = _accepted_directly(routes, engine, since=since, fax_images=True)
    if not result['faxes']:
        result['sentence'] = f'No fax went to a partner as a fax image in the last {days} days.'
        return result
    sentence = f"{_plural(result['calls_avoided'], 'telephone call')} avoided by direct fax images"
    sentence += f", saving about {_money_text(result['saved'])}." if result['saved'] else '.'
    if result['in_plan']:
        sentence += f" {result['in_plan']} of them would have gone through your monthly plan, so they saved no money."
    result['sentence'] = sentence
    return result


def direct_delivery(routes, engine, *, since, days):
    """Fax calls avoided by original documents a partner accepted directly; fax images are counted on their own."""
    result = _accepted_directly(routes, engine, since=since, fax_images=False)
    if not result['faxes']:
        result['sentence'] = f'No documents went straight to a partner in the last {days} days.'
        return result
    partners = 'a partner' if result['faxes'] == 1 else 'partners'
    sentence = (f"{_plural(result['faxes'], 'document')} went straight to {partners} instead of by fax: "
                f"{_plural(result['calls_avoided'], 'fax call')}")
    sentence += f" and about {_money_text(result['saved'])} saved." if result['saved'] else ' saved.'
    if result['in_plan']:
        sentence += f" {result['in_plan']} of them would have gone through your monthly plan, so they saved no money."
    result['sentence'] = sentence
    return result


def own_numbers(routes, engine, *, since, days):
    """Fax calls avoided by faxes to the installation's own numbers, delivered inside Faxbot.

    Each is priced as the call its own provider would have placed, when a rate card prices it.
    """
    t = reflect(engine, ('fax_jobs', 'delivery_attempt_costs'))
    jobs, costs = t['fax_jobs'], t['delivery_attempt_costs']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(jobs.c.backend, jobs.c.pages).join(jobs, jobs.c.id == costs.c.job_id).where(
            costs.c.route == 'local', costs.c.outcome == 'success', costs.c.created_at >= since)).all()
    result = {'faxes': 0, 'calls_avoided': 0, 'pages': 0, 'priced': 0, 'in_plan': 0, 'unpriced': 0, 'saved': {}}
    cards = {}
    for backend, pages in rows:
        result['faxes'] += 1
        result['calls_avoided'] += 1
        result['pages'] += pages or 0
        if backend not in cards:
            cards[backend] = routes.card_for(backend) if backend else None
        _avoided(cards[backend], pages, None, result)
    if not result['faxes']:
        result['sentence'] = f'No faxes went to your own numbers in the last {days} days.'
        return result
    calls = result['calls_avoided']
    sentence = (f"{_plural(result['faxes'], 'fax', 'faxes')} to your own numbers went straight into Received, so "
                f"{_plural(calls, 'phone call')} {'was' if calls == 1 else 'were'} not needed")
    sentence += f", saving about {_money_text(result['saved'])}." if result['saved'] else '.'
    result['sentence'] = sentence
    return result


def case_packets(routes, engine, *, since, days):
    """Pages not sent again because a delivered case packet listed accepted documents instead."""
    t = reflect(engine, ('case_packet_sends', 'case_documents', 'outbound_deliveries', 'fax_jobs',
                         'delivery_attempt_costs'))
    sends, documents, deliveries = t['case_packet_sends'], t['case_documents'], t['outbound_deliveries']
    jobs, costs = t['fax_jobs'], t['delivery_attempt_costs']
    with read_connection(engine) as connection:
        counted_from = connection.scalar(sa.select(sa.func.min(sends.c.created_at)))
        earlier = sa.select(documents.c.id).limit(1)
        if counted_from is not None:
            earlier = earlier.where(documents.c.created_at < counted_from)
        earlier_not_counted = connection.execute(earlier).first() is not None
        rows = connection.execute(sa.select(
            sends.c.id, sends.c.pages_sent, sends.c.pages_left_out, sends.c.documents_left_out, jobs.c.backend,
        ).join(deliveries, deliveries.c.id == sends.c.id).join(jobs, jobs.c.id == sends.c.id).where(
            deliveries.c.state == 'success', sends.c.documents_left_out > 0,
            sends.c.created_at >= since)).all()
        carried = {}
        if rows:
            for job_id, provider in connection.execute(sa.select(costs.c.job_id, costs.c.provider_id).where(
                    costs.c.job_id.in_([row.id for row in rows]), costs.c.outcome == 'success').order_by(
                    costs.c.created_at)):
                carried[job_id] = provider
    result = {'counted_from': counted_from, 'earlier_not_counted': earlier_not_counted, 'packets': 0,
              'documents_left_out': 0, 'pages_not_resent': 0, 'pages_saved': 0, 'priced': 0, 'in_plan': 0,
              'unpriced': 0, 'saved': {}}
    cards = {}
    for row in rows:
        result['packets'] += 1
        result['documents_left_out'] += row.documents_left_out
        result['pages_not_resent'] += row.pages_left_out
        # The one-page list of accepted documents is sent in their place.
        result['pages_saved'] += max(0, row.pages_left_out - 1)
        provider = carried.get(row.id, row.backend)
        if provider not in cards:
            cards[provider] = routes.card_for(provider) if provider and provider != DIRECT else None
        full = row.pages_sent - 1 + row.pages_left_out
        _avoided(cards[provider], full, row.pages_sent, result)
    if counted_from is None:
        result['counted_from_sentence'] = (
            'Packets sent before this update are not counted. Faxbot counts from the next packet you send.'
            if earlier_not_counted else None)
    else:
        result['counted_from_sentence'] = (
            f'Counted from {counted_from:%B} {counted_from.day}, {counted_from.year}, when Faxbot started '
            'recording what each packet left out.' if earlier_not_counted else None)
    if not result['packets']:
        result['sentence'] = f'No case packet in the last {days} days left out a document the recipient already had.'
        return result
    sentence = (f"{_plural(result['packets'], 'case packet')} left out "
                f"{_plural(result['documents_left_out'], 'document')} the recipient already had: "
                f"{_plural(result['pages_saved'], 'page')}")
    saved, more = _signed(result['saved'])
    sentence += f" and about {_money_text(saved)} saved." if saved else ' saved.'
    if more:
        sentence += f" Sending the list of those documents instead cost about {_money_text(more)} more."
    if result['in_plan']:
        sentence += (f" {_plural(result['in_plan'], 'packet')} {'was' if result['in_plan'] == 1 else 'were'} sent "
                     'through your monthly plan, so it saved pages but no money.')
    result['sentence'] = sentence
    return result


def savings(routes, engine, *, now=None, days=WINDOW_DAYS, home=None):
    """Every part of Costs → Savings over ``days``; ``home`` is the installation country, for relay sentences."""
    now = now or utcnow()
    since = now - timedelta(days=days)
    # Separator pages shared calls left out (index page or page marks): counted apart from the calls saved.
    index = {'calls': 0, 'pages_saved': 0, 'priced_calls': 0, 'saved': {}}
    together = sending_together(routes, engine, now=now, days=days, separator_pages=index)
    index['sentence'] = money.separator_pages_sentence(index, days=days)
    direct = direct_delivery(routes, engine, since=since, days=days)
    fax_images = direct_fax_images(routes, engine, since=since, days=days)
    packets = case_packets(routes, engine, since=since, days=days)
    # Faxes whose pages went over SSL Fax (the fast fax service), priced with the trunk carrier's billing.
    from ..hylafax_records import sslfax_savings
    sslfax = sslfax_savings(routes, engine, since=since, days=days)
    own = own_numbers(routes, engine, since=since, days=days)
    # Faxes that called the toll-free number their recipient approved, kept apart: the recipient pays those calls.
    from .alternates import savings as toll_free_savings
    toll_free = toll_free_savings(routes, engine, since=since, days=days)
    # Pages saved by packing several pages onto long pages, and blank space left out (pages/).
    from ..pages.views import savings as page_savings
    packing = page_savings(routes, engine, since=since, days=days)
    # Pages saved by the experimental encoded pages, each attempt's choice (pages/sending.py).
    encoding = page_savings(routes, engine, since=since, days=days, layout='codec')
    # The parts the savings map added for mechanisms that had none: counts, except the relay's own priced records.
    from . import mechanism_parts as more
    added = {'fax_friendly': more.fax_friendly(engine, since=since, days=days),
             'cheapest_route': more.cheapest_route(engine, since=since, days=days),
             'plan_first': more.plan_first(engine, since=since, days=days),
             'relay': more.relay(engine, since=since, days=days, home=home),
             'continuation': more.continuation(engine, since=since, days=days),
             'partner_repair': more.partner_repair(engine, since=since, days=days),
             'blocked_calls': more.blocked_calls(engine, since=since, days=days),
             't38': more.fax_over_ip(engine, since=since, days=days),
             'digital': more.digital(engine, since=since, days=days)}
    total = {}
    for part in (together, index, direct, fax_images, packets, sslfax, own, toll_free, packing, encoding,
                 *added.values()):
        for currency, micros in part['saved'].items():
            _add(total, currency, micros)  # signed: a part that cost more lowers the total
    return {'days': days, 'since': since, 'sending_together': together, 'separator_pages': index, 'direct_delivery': direct,
            'direct_fax_images': fax_images,
            'case_packets': packets, 'sslfax': sslfax, 'own_numbers': own, 'toll_free': toll_free, 'packing': packing,
            'encoding': encoding, **added, 'total': total,
            'total_sentence': total_sentence(total, days)}


def total_sentence(total, days):
    """The headline: what was saved, or honestly what cost more, in the last ``days``."""
    saved, more = _signed(total)
    if saved and more:
        return f'About {_money_text(saved)} saved and {_money_text(more)} more spent in the last {days} days.'
    if more:
        return f'About {_money_text(more)} more spent than saved in the last {days} days.'
    if saved:
        return f'About {_money_text(saved)} saved in the last {days} days.'
    return f'No money saved in the last {days} days, as far as Faxbot can tell.'
