"""What sending together, direct delivery and case packets saved: always estimates.

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
from .costs import estimate_cost, money_text
from .database import read_connection, reflect, utcnow
from .policy import DIRECT


WINDOW_DAYS = 30
SENTENCE = ('Each figure is an estimate: what you paid compared with what the same faxes would have cost '
            'the usual way.')


def _add(totals, currency, micros):
    totals[currency] = totals.get(currency, 0) + micros


def _money_text(totals):
    return ' + '.join(money_text(micros, currency) for currency, micros in sorted(totals.items()))


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
        cost = estimate_cost(card, with_pages) - (0 if without_pages is None else estimate_cost(card, without_pages))
        _add(result['saved'], card.currency, max(0, cost))


def sending_together(routes, engine, *, now, days):
    """Calls saved by faxes that shared a call, over every number that sends together."""
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
    if not result['calls']:
        result['sentence'] = f'No faxes were sent together in the last {days} days.'
        return result
    same = 'the same number' if result['numbers'] == 1 else 'the same numbers'
    sentence = (f"{_plural(result['faxes'], 'fax', 'faxes')} to {same} went in "
                f"{_plural(result['calls'], 'call')} instead of {result['faxes']}, saving "
                f"{_plural(result['calls_saved'], 'call')}")
    if result['saved']:
        sentence += f" and about {_money_text(result['saved'])}"
    sentence += '.'
    if result['priced_calls'] < result['calls']:
        sentence += " Some calls have no price, because your carrier's prices are not entered in Costs."
    result['sentence'] = sentence
    return result


def direct_delivery(routes, engine, *, since, days):
    """Fax calls avoided by documents a partner accepted directly; each priced on the fax's own provider."""
    t = reflect(engine, ('direct_deliveries', 'fax_jobs', 'delivery_attempt_costs'))
    d, jobs, costs = t['direct_deliveries'], t['fax_jobs'], t['delivery_attempt_costs']
    faxed = sa.exists().where(costs.c.job_id == d.c.job_id, costs.c.route != DIRECT, costs.c.outcome == 'success')
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(jobs.c.backend, jobs.c.pages).join(jobs, jobs.c.id == d.c.job_id).where(
            d.c.direction == 'outbound', d.c.state == 'accepted',
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
    sentence += f" and about {_money_text(result['saved'])} saved." if result['saved'] else ' saved.'
    if result['in_plan']:
        sentence += (f" {_plural(result['in_plan'], 'packet')} {'was' if result['in_plan'] == 1 else 'were'} sent "
                     'through your monthly plan, so it saved pages but no money.')
    result['sentence'] = sentence
    return result


def savings(routes, engine, *, now=None, days=WINDOW_DAYS):
    now = now or utcnow()
    since = now - timedelta(days=days)
    together = sending_together(routes, engine, now=now, days=days)
    direct = direct_delivery(routes, engine, since=since, days=days)
    packets = case_packets(routes, engine, since=since, days=days)
    total = {}
    for part in (together, direct, packets):
        for currency, micros in part['saved'].items():
            _add(total, currency, micros)
    return {'days': days, 'since': since, 'sending_together': together, 'direct_delivery': direct,
            'case_packets': packets, 'total': total}
