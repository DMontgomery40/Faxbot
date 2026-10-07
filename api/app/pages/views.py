"""What the console and the command line show about dense pages: one sentence per state, dates as ISO for them
to put in local time, money from the route's own billing (always an estimate)."""
from __future__ import annotations

from datetime import timedelta
import re

import sqlalchemy as sa

from . import capability as capabilities
from .capability import LIMIT_TEXT, iso, records_for

_HEX32 = re.compile(r'[a-f0-9]{32}')


def _pages(count, word='page'):
    return f'{count} {word}' if count == 1 else f'{count} {word}s'


def packed_sentence(change):
    """"Sent as 2 long pages instead of 5; the receiving machine accepts unlimited length." or None."""
    if not change or change['sent_pages'] >= change['original_pages']:
        return None
    head = f"Sent as {_pages(change['sent_pages'], 'long page')} instead of {change['original_pages']}"
    limit = change.get('page_limit') or capabilities.DEFAULT_LIMIT
    if change.get('limit_learned_at') is None:
        return (f"{head}; the receiving machine's longest page is not known yet, so Faxbot kept to "
                f"{LIMIT_TEXT[limit]}.")
    return f'{head}; the receiving machine accepts {LIMIT_TEXT[limit]}.'


def trimmed_sentence(change):
    """"Blank space at the bottom of 4 pages was left out; the receiving machine has no error correction."."""
    count = (change or {}).get('trimmed_pages')
    if not count:
        return None
    return (f"Blank space at the bottom of {_pages(count)} was left out; the receiving machine has no error "
            'correction.')


RESOLUTION_SENTENCE = 'Sent at standard resolution, as received.'


def sent_view(engine, job_id, root=None):
    """The Sent detail's page block, or None when Faxbot sent the pages as they were."""
    if engine is None or not _HEX32.fullmatch(str(job_id or '')):
        return None
    change = records_for(engine).change_for_job(job_id)
    resolution = (change or {}).get('resolution')
    sentences = [text for text in (packed_sentence(change), trimmed_sentence(change),
                                   RESOLUTION_SENTENCE if resolution == 'standard' else None) if text]
    if not sentences:
        return None
    change = change or {}
    return {'original_pages': change.get('original_pages'), 'sent_pages': change.get('sent_pages'),
            'page_limit': change.get('page_limit'), 'limit_learned_at': iso(change.get('limit_learned_at')),
            'pages_saved': change.get('pages_saved'), 'trimmed_pages': change.get('trimmed_pages'),
            'seconds_saved': change.get('seconds_saved'), 'resolution': resolution, 'sentences': sentences}


def capability_sentence(cap):
    if not cap.learned:
        return ("Faxbot does not know yet how long a page this fax machine takes, so it keeps to A4 length. "
                'It learns this from the next fax your fast fax service sends to it on your phone line.')
    if cap.limit == 'unlimited':
        return 'This fax machine takes pages of unlimited length.'
    return f'This fax machine takes pages up to {LIMIT_TEXT[cap.limit]}.'


def ecm_sentence(cap):
    if cap.ecm is None:
        return None
    return ('This fax machine has error correction.' if cap.ecm
            else 'This fax machine has no error correction, so every line of a page takes time.')


def recipient_view(engine, number):
    """Recipients, Details: what the machine accepts (and since when) and this number's page settings."""
    records = records_for(engine)
    cap = records.capability(number)
    settings = records.recipient_settings(number)
    return {'number': number, 'page_limit': cap.limit, 'learned': cap.learned, 'learned_at': iso(cap.learned_at),
            'max_width': cap.max_width, 'fine': cap.fine, 'ecm': cap.ecm,
            'boundary_seconds': cap.boundary_seconds, 'packing': settings['packing'],
            'trim_blank': settings['trim_blank'], 'trim_blank_default': records.trim_blank_default(),
            'capability_sentence': capability_sentence(cap), 'ecm_sentence': ecm_sentence(cap)}


def route_sentence(view, label):
    if not view['long_pages_possible']:
        return f'{label} fetches the document from Faxbot itself, so long pages cannot be sent through it.'
    if view['route'] in capabilities.IMAGE_ROUTES:
        return ('Faxbot puts several pages on one long page when the receiving machine takes long pages and it '
                'saves pages or time.')
    if view['long_pages']:
        return f'{label} gets several pages on one long page when the receiving machine takes them and it saves.'
    return (f'Off until you check that {label} sends long pages without shrinking them: turn it on, fax a few '
            'pages to one of your own numbers on your phone line, and compare what arrives.')


def route_views(engine, routes=None):
    """Providers: long pages per route, with one sentence each."""
    from ..routing.plan import route_label
    records = records_for(engine)
    keys = routes if routes is not None else sorted(capabilities.page_models())
    result = []
    for route in keys:
        view = records.route_view(route)
        view['label'] = route_label(route)
        view['sentence'] = route_sentence(view, view['label'])
        result.append(view)
    return result


# Savings -------------------------------------------------------------------------------------------------------

def savings(routes, engine, *, since, days):
    """Pages saved by packing (and seconds by trimming) on delivered sends, priced with each route's billing.

    Always an estimate: what the same fax would have cost sent page by page.
    Per page: the pages not sent. Per minute: the call with the saved seconds
    added back. A flat plan saves no money at the margin; its pages saved are
    room under the plan's fair use and page limits. No card: unpriced.
    """
    from ..routing.costs import attempt_cost, money_text
    from ..routing.database import reflect
    tables = reflect(engine, ('fax_page_changes', 'delivery_attempt_costs'))
    changes, costs = tables['fax_page_changes'], tables['delivery_attempt_costs']
    with engine.connect() as connection:
        rows = connection.execute(sa.select(
            changes.c.route, changes.c.original_pages, changes.c.sent_pages, changes.c.pages_saved,
            changes.c.trimmed_pages, changes.c.seconds_saved, costs.c.billed_seconds,
        ).join(costs, costs.c.id == changes.c.attempt_id).where(
            costs.c.outcome == 'success', changes.c.created_at >= since)).all()
    result = {'faxes': 0, 'pages_saved': 0, 'trimmed_pages': 0, 'seconds_saved': 0, 'priced': 0, 'in_plan': 0,
              'plan_pages': 0, 'unpriced': 0, 'saved': {}}
    cards = {}
    for row in rows:
        result['faxes'] += 1
        result['pages_saved'] += row.pages_saved
        result['trimmed_pages'] += row.trimmed_pages or 0
        result['seconds_saved'] += row.seconds_saved or 0
        if row.route not in cards:
            cards[row.route] = routes.card_for(row.route)
        card = cards[row.route]
        if card is None:
            result['unpriced'] += 1
            continue
        if card.flat_plan:
            result['in_plan'] += 1
            result['plan_pages'] += row.pages_saved
            continue
        if card.per_minute_micros and (row.billed_seconds is None or row.seconds_saved is None):
            result['unpriced'] += 1
            continue
        seconds = row.billed_seconds or 0
        before = attempt_cost(card, seconds=seconds + (row.seconds_saved or 0), pages=row.original_pages,
                              delivered=True)
        after = attempt_cost(card, seconds=seconds, pages=row.sent_pages, delivered=True)
        if before is None or after is None:
            result['unpriced'] += 1
            continue
        result['priced'] += 1
        if before > after:
            result['saved'][card.currency] = result['saved'].get(card.currency, 0) + before - after
    result['sentence'] = savings_sentence(result, days, money_text)
    return result


def savings_sentence(result, days, money_text):
    if not result['faxes']:
        return f'No pages were packed or trimmed in the last {days} days.'
    parts = []
    if result['pages_saved']:
        parts.append(f"{_pages(result['pages_saved'])} saved by packing")
    if result['trimmed_pages']:
        parts.append(f"blank space left out of {_pages(result['trimmed_pages'])}")
    sentence = f"{' and '.join(parts) or 'Pages changed'} on {_pages(result['faxes'], 'fax').replace('faxs', 'faxes')}"
    sentence = sentence[:1].upper() + sentence[1:]
    minutes = result['seconds_saved'] // 60
    if minutes:
        sentence += f", about {_pages(minutes, 'minute')} less on the phone"
    money = ' + '.join(money_text(micros, currency) for currency, micros in sorted(result['saved'].items()))
    sentence += f', saving about {money}.' if money else '.'
    if result['plan_pages']:
        sentence += (f" {_pages(result['plan_pages'])} of them went through your monthly plan: no money saved, but "
                     "more room under its fair use and page limits.")
    return sentence


def window(now, days):
    return now - timedelta(days=days)
