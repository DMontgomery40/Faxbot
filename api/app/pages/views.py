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


def packed_sentence(change, phase=None):
    """One sentence for the layout an attempt kept (dense pages or the experimental encoded pages), for the
    attempt's state, or None for the pages' own layout:

    - delivered (``phase`` 'success', or not known): "Sent as 2 long pages instead of 5; the receiving machine
      accepts unlimited length." / "Sent as 1 encoded page instead of 5 (experimental).";
    - failed: "Tried as 2 long pages instead of 5; the call failed.";
    - cancelled: "Prepared as 2 long pages instead of 5; the fax was cancelled.";
    - uncertain (the call ended and nobody knows whether the fax arrived): "Sent as 2 long pages instead of 5;
      whether it arrived is not confirmed yet." / "Sent as 1 encoded page instead of 5 (experimental); whether
      it arrived is not confirmed yet.";
    - any other state (on its way): "Going as …", worded as when delivered.
    """
    if not change or change.get('layout') not in ('dense', 'codec'):
        return None
    sent, original = change['sent_pages'], change['original_pages']
    if change['layout'] == 'codec':
        what, tail = _pages(sent, 'encoded page'), ' (experimental).'
    elif sent >= original:
        what, tail = _pages(sent), '.'
    else:
        what = _pages(sent, 'long page')
        limit = change.get('page_limit') or capabilities.DEFAULT_LIMIT
        if change.get('limit_learned_at') is None:
            tail = (f"; the receiving machine's longest page is not known yet, so Faxbot kept to "
                    f"{LIMIT_TEXT[limit]}.")
        else:
            tail = f'; the receiving machine accepts {LIMIT_TEXT[limit]}.'
    if phase == 'failed':
        return f'Tried as {what} instead of {original}; the call failed.'
    if phase == 'cancelled':
        return f'Prepared as {what} instead of {original}; the fax was cancelled.'
    if phase == 'uncertain':
        # The pages left Faxbot, but no answer says whether they arrived (never "Going as": the call is over).
        marker = ' (experimental)' if change['layout'] == 'codec' else ''
        return f'Sent as {what} instead of {original}{marker}; whether it arrived is not confirmed yet.'
    verb = 'Sent as' if phase in (None, 'success') else 'Going as'
    return f'{verb} {what} instead of {original}{tail}'


def trimmed_sentence(change):
    """"Blank space at the bottom of 4 pages was left out; the receiving machine has no error correction."."""
    count = (change or {}).get('trimmed_pages')
    if not count:
        return None
    return (f"Blank space at the bottom of {_pages(count)} was left out; the receiving machine has no error "
            'correction.')


RESOLUTION_SENTENCE = 'Sent at standard resolution, as received.'


def newest_attempt_change(engine, job_id):
    """The page change of the fax's newest attempt, or None when that attempt sent its pages as they were.

    The Sent detail follows the attempt, as the lightened pages' line does (``friendly.run_for``): a fax that
    went as encoded pages and then as its own pages on another route shows the second attempt. The change
    carries that attempt's state as ``attempt_phase``. Without attempt records, the newest change (state not
    known)."""
    records = records_for(engine)
    table = records.table('fax_page_changes')

    def read(connection):
        query = sa.select(table).where(table.c.job_id == job_id)
        newest = None
        try:
            attempts = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=connection)
        except sa.exc.NoSuchTableError:
            attempts = None
        if attempts is not None:
            newest = connection.execute(sa.select(attempts.c.id, attempts.c.phase).where(
                attempts.c.job_id == job_id).order_by(attempts.c.sequence.desc()).limit(1)).first()
            if newest is not None:
                query = query.where(table.c.attempt_id == newest[0])
        row = connection.execute(query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)
                                 ).mappings().first()
        if row is None:
            return None
        return {**dict(row), 'attempt_phase': newest[1] if newest is not None else None}
    return records._read(read)


def sent_view(engine, job_id, root=None):
    """The Sent detail's page block for the fax's newest attempt, or None when it sent the pages as they were.

    One sentence says which layout the attempt kept (dense pages, or the experimental encoded pages), then what
    else changed on the pages as they are or dense pages: blank space left out, standard resolution, shading
    lightened. Encoded pages never have those."""
    if engine is None or not _HEX32.fullmatch(str(job_id or '')):
        return None
    change = newest_attempt_change(engine, job_id)
    resolution = (change or {}).get('resolution')
    # Shaded areas lightened and specks removed (pages/friendly.py, migration 0042).
    from .friendly import call_rate, run_for, seconds_at, sent_sentence
    lightened = run_for(engine, job_id)
    rate = call_rate(engine, job_id) if lightened else None
    # A change with nothing changed carries only why the pages went as they are (a fax too long to change).
    unchanged = (change or {}).get('reason') if change and not change.get('layout') and not change.get(
        'trimmed_pages') and not change.get('resolution') else None
    sentences = [text for text in (packed_sentence(change, (change or {}).get('attempt_phase')),
                                   unchanged, trimmed_sentence(change),
                                   RESOLUTION_SENTENCE if resolution == 'standard' else None,
                                   sent_sentence(lightened, rate)) if text]
    if not sentences:
        return None
    change = change or {}
    return {'layout': change.get('layout') or 'normal', 'original_pages': change.get('original_pages'),
            'sent_pages': change.get('sent_pages'),
            'page_limit': change.get('page_limit'), 'limit_learned_at': iso(change.get('limit_learned_at')),
            'pages_saved': change.get('pages_saved'), 'trimmed_pages': change.get('trimmed_pages'),
            'seconds_saved': change.get('seconds_saved'), 'resolution': resolution,
            'lightened': None if lightened is None else {
                'pages_changed': lightened['pages_changed'], 'seconds_saved': seconds_at(lightened, rate),
                'at': iso(lightened['created_at'])},
            'sentences': sentences}


def capability_sentence(cap):
    if not cap.learned:
        return ("Faxbot does not know yet how long a page this fax machine takes, so it keeps to A4 length. "
                'It learns this from the next fax your fax engine sends to it on your phone line.')
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
        return f'Long pages cannot be sent through {label}.'
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

def savings(routes, engine, *, since, days, layout=None):
    """Pages saved by packing (and seconds by trimming) on delivered sends, priced with each route's billing;
    with ``layout`` 'codec', the pages saved by the experimental encoded pages instead (``encoding_sentence``).

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
            costs.c.outcome == 'success', changes.c.created_at >= since,
            # Encoded pages (experimental) are counted apart from packing.
            changes.c.layout == 'codec' if layout == 'codec' else
            sa.and_(sa.or_(changes.c.layout.is_(None), changes.c.layout != 'codec'),
                    # A fax too long to change records only why; it saved nothing.
                    sa.or_(changes.c.layout.is_not(None), changes.c.trimmed_pages.is_not(None),
                           changes.c.resolution.is_not(None))))).all()
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
    result['sentence'] = (encoding_sentence if layout == 'codec' else savings_sentence)(result, days, money_text)
    return result


def encoding_sentence(result, days, money_text):
    """Costs, Savings: "Pages saved by encoding (experimental)"."""
    if not result['faxes']:
        return f'No faxes went as encoded pages in the last {days} days.'
    faxes = _pages(result['faxes'], 'fax').replace('faxs', 'faxes')
    sentence = f"{_pages(result['pages_saved'])} saved by encoding on {faxes}"
    minutes = result['seconds_saved'] // 60
    if minutes:
        sentence += f", about {_pages(minutes, 'minute')} less on the phone"
    money = ' + '.join(money_text(micros, currency) for currency, micros in sorted(result['saved'].items()))
    sentence += f', saving about {money}.' if money else '.'
    if result['plan_pages']:
        sentence += (f" {_pages(result['plan_pages'])} of them went through your monthly plan: no money saved, but "
                     "more room under its fair use and page limits.")
    return sentence


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
