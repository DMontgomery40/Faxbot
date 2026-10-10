"""The account and pages an attempt was bound to after its accounts were compared, and why, in one sentence.

``record`` writes one ``fax_route_selections`` row (migration 0067) per attempt,
before its durable submission marker, and never rewrites it: the account, the
pages it sends (with the SHA-256 of the file and of their pixels), their
expected bill on that account, and the cheapest other account compared with
its own best pages. The price is the one the route choice ranked by
(``routing.joint``); nothing later reconstructs it from a provider's card.

``sentence`` says it the way Sent details and ``faxbot sent show`` print it:
"Sent through Page trunk as 1 long page: about $0.004 instead of $0.005
through Telnyx as 2 pages."
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import uuid

import sqlalchemy as sa

TABLE = 'fax_route_selections'
# At most this many compared candidates are kept with an attempt, cheapest first.
CANDIDATES_KEPT = 40


def _table(engine):
    from .database import reflect
    return reflect(engine, (TABLE,))[TABLE]


def _sha256(path):
    if not path:
        return None
    found = Path(str(path))
    if found.is_symlink() or not found.is_file():
        return None
    return hashlib.sha256(found.read_bytes()).hexdigest()


def summary(plan, measured, key, prices=None):
    """What the record keeps besides the chosen pages: accounts compared, the runner-up (the cheapest other
    account compared, at its own best pages and price) and every candidate, cheapest first."""
    frontiers = measured.frontiers if measured is not None else {}
    prices = prices or {}
    runner = None
    for choice in getattr(plan, 'choices', ()):
        other = choice.route.key
        if other == key or other not in frontiers:
            continue
        price = prices.get(other)
        micros = price.micros if price is not None else choice.estimated_cost_micros
        if micros is None:
            continue
        option = frontiers[other].chosen
        runner = {'key': other, 'layout': option.layout, 'pages': option.pages, 'micros': micros,
                  'currency': (price.currency if price is not None else None) or option.currency}
        break
    candidates = []
    for account, evaluated in frontiers.items():
        for option in evaluated.options:
            candidates.append({'account': account, 'rendering': option.rendering, 'layout': option.layout,
                               'coding': option.coding, 'pages': option.pages, 'micros': option.micros,
                               'currency': option.currency,
                               'seconds': None if option.seconds is None else round(option.seconds, 1),
                               'chosen': account == key and option == evaluated.chosen})
    candidates.sort(key=lambda item: (item['micros'] is None, item['micros'] or 0, item['pages'], item['account']))
    bound = prices.get(key)
    return {'compared': len(frontiers), 'runner': runner, 'candidates': candidates[:CANDIDATES_KEPT],
            'expected': None if bound is None else (bound.micros, bound.currency)}


def record(engine, *, claim, handoff, evaluated, prepared, pdf, now=None):
    """Write this attempt's selection once; False when it already has one. Raises what the database raises
    (``routing.database.DeliveryStoreError`` before migration 0067): the attempt is not sent unrecorded."""
    table = _table(engine)
    chosen, account = evaluated.chosen, evaluated.account
    info = getattr(handoff, 'summary', None) or {}
    expected = info.get('expected') or (chosen.micros, chosen.currency)
    micros, currency = expected
    runner = info.get('runner') or {}
    artifact = None
    if prepared is not None:
        artifact = _sha256(prepared.tiff or prepared.pdf)
    sent_pages = prepared.sent_pages if prepared is not None and prepared.sent_pages else chosen.pages
    now = (now or datetime.utcnow()).replace(microsecond=0)
    row = dict(id=uuid.uuid4().hex, job_id=claim.job_id, attempt_id=claim.attempt_id, account_key=account.key,
               provider_id=account.provider_id,
               number=(evaluated.permission.number if evaluated.permission is not None else None),
               rendering=chosen.rendering, layout=chosen.layout, coding=chosen.coding,
               original_pages=max(1, int(evaluated.original_pages or 1)), sent_pages=max(1, int(sent_pages or 1)),
               expected_micros=micros, currency=currency if micros is not None else None,
               plan_units=None, plan_unit=None, measured=int(bool(evaluated.measured)),
               compared=max(1, int(info.get('compared') or 1)), runner_key=runner.get('key'),
               runner_layout=runner.get('layout'), runner_pages=runner.get('pages'),
               runner_micros=runner.get('micros'),
               runner_currency=runner.get('currency') if runner.get('micros') is not None else None,
               artifact_sha256=artifact, pixels_sha256=chosen.digest, source_sha256=evaluated.sources[0],
               tariff_sha256=account.fingerprint(), slot_at=info.get('slot_at'),
               candidates=json.dumps(info.get('candidates') or [], sort_keys=True, separators=(',', ':')),
               created_at=now)
    with engine.begin() as connection:
        if connection.execute(sa.select(table.c.id).where(table.c.attempt_id == claim.attempt_id)).first():
            return False
        connection.execute(table.insert().values(**row))
    return True


def for_attempt(engine, attempt_id):
    """The selection recorded for one attempt, or None (no comparison, or a database before 0067)."""
    from .database import DeliveryStoreError
    try:
        table = _table(engine)
    except DeliveryStoreError:
        return None
    with engine.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.attempt_id == attempt_id)).mappings().first()
    return _view(row)


def newest_for_job(engine, job_id):
    """The selection of the fax's newest attempt that has one, with that attempt's state, or None."""
    from .database import DeliveryStoreError
    try:
        table = _table(engine)
    except DeliveryStoreError:
        return None
    with engine.connect() as connection:
        rows = connection.execute(sa.select(table).where(table.c.job_id == job_id).order_by(
            table.c.created_at.desc(), table.c.id.desc())).mappings().all()
        if not rows:
            return None
        try:
            attempts = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=connection)
        except sa.exc.NoSuchTableError:
            return _view(rows[0])
        newest = connection.execute(sa.select(attempts.c.id, attempts.c.phase).where(
            attempts.c.job_id == job_id).order_by(attempts.c.sequence.desc()).limit(1)).first()
    if newest is None:
        return _view(rows[0])
    row = next((item for item in rows if item['attempt_id'] == newest[0]), None)
    if row is None:
        return None  # the newest attempt went without comparing accounts; an older comparison no longer applies
    return {**_view(row), 'attempt_phase': newest[1]}


def _view(row):
    if row is None:
        return None
    found = dict(row)
    try:
        found['candidates'] = json.loads(found.get('candidates') or '[]')
    except ValueError:
        logging.getLogger(__name__).warning('A route selection record has unreadable candidates.')
        found['candidates'] = []
    found['measured'] = bool(found.get('measured'))
    return found


def account_label(values, key):
    """An account's name as you gave it (``Page trunk``), else its route's (``Telnyx``)."""
    from .plan import route_label
    try:
        from ..accounts import account_named
        account = account_named(values, key) if values is not None else None
    except Exception:
        account = None
    if account is not None and not account.primary and account.label:
        return account.label
    return route_label(key)


def sent_view(engine, job_id, values=None):
    """Sent details' view of the newest attempt's measured choice, or None: the sentence, the account, its pages
    and price, and the runner-up's."""
    from .costs import format_amount
    found = newest_for_job(engine, job_id)
    if found is None:
        return None

    def money(micros, currency):
        return None if micros is None or not currency else {'currency': currency, 'amount': format_amount(micros)}
    return {'sentence': sentence(found, label=lambda key: account_label(values, key)),
            'account': found['account_key'], 'account_label': account_label(values, found['account_key']),
            'layout': found['layout'], 'original_pages': found['original_pages'], 'sent_pages': found['sent_pages'],
            'expected_cost': money(found.get('expected_micros'), found.get('currency')),
            'compared': found['compared'],
            'runner_up': None if not found.get('runner_key') else {
                'account': found['runner_key'], 'account_label': account_label(values, found['runner_key']),
                'layout': found.get('runner_layout'), 'sent_pages': found.get('runner_pages'),
                'expected_cost': money(found.get('runner_micros'), found.get('runner_currency'))}}


def _pages(count, layout):
    noun = {'dense': 'long page', 'codec': 'encoded page'}.get(layout, 'page')
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _money(micros, currency):
    from .costs import money_text
    if micros is None:
        return None
    return money_text(micros, currency) if currency else None


def sentence(selection, *, label=None, phase=None):
    """One plain sentence for why this attempt went by this account with these pages, or None.

    "Sent through Page trunk as 1 long page: about $0.004 instead of $0.005 through Telnyx as 2 pages." The
    verb follows the attempt (Going, Sent, Tried, Prepared); without a known runner-up price the sentence names
    only the account, pages and price; an unknown price is said as unknown, never as free."""
    if not selection:
        return None
    if label is None:
        from .plan import route_label as label
    phase = phase if phase is not None else selection.get('attempt_phase')
    # 'preview': the document preview (POST /routing/predict) before anything is sent.
    verb = {'failed': 'Tried', 'cancelled': 'Prepared', 'uncertain': 'Sent', 'success': 'Sent',
            'preview': 'Would go'}.get(phase, 'Going')
    what = _pages(selection['sent_pages'], selection['layout'])
    head = f"{verb} through {label(selection['account_key'])} as {what}"
    price = _money(selection.get('expected_micros'), selection.get('currency'))
    runner_price = _money(selection.get('runner_micros'), selection.get('runner_currency'))
    other = None
    if selection.get('runner_key') and runner_price:
        other = (f"{runner_price} through {label(selection['runner_key'])} as "
                 f"{_pages(selection['runner_pages'] or selection['original_pages'], selection.get('runner_layout'))}")
    if price is None:
        text = f'{head}; its price is unknown'
    elif selection.get('expected_micros') == 0:
        text = f'{head}: nothing extra'
    else:
        text = f'{head}: about {price}'
    if other is not None and price is not None and selection.get('currency') == selection.get('runner_currency'):
        text += f' instead of {other}'
    if phase == 'uncertain':
        text += '; whether it arrived is not confirmed yet'
    elif phase == 'failed':
        text += '; the call failed'
    return text + '.'
