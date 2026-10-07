"""How calls to each number end against the carrier's billing step, from measured call times.

A carrier that bills by the minute charges a 61-second call for two minutes.
For each number Faxbot faxed over the trunk, this module takes the connected
seconds of every measured call, rounds them the way the trunk's rate card bills
(``costs.billed_seconds``: minimum first, then whole steps), and counts the
calls that ended just past a billed step: within ``near_seconds`` of it, 10
seconds for a one-minute step. Each of those calls would have cost one step
less had it been that much shorter, with one page less or a faster mode.

A call inside the card's minimum charge, or inside its first step, can never
cost less by being shorter, so it is never counted. A carrier billing in steps
shorter than ``FINE_STEP_SECONDS`` makes the view pointless and the report says
so in one sentence. Advice only: nothing is sent, changed or re-sent.
"""
from .carriers import carrier_label
from .costs import billed_seconds, format_amount, money_text
from .trunk_calls import trunk_calls


WINDOW_DAYS = 30
# Measured calls to one number needed before Faxbot says how they end.
MIN_CALLS = 3
# Shorter billing steps than this make a few seconds worth almost nothing.
FINE_STEP_SECONDS = 30
SHOWN = 20


def near_seconds(increment):
    """How far past a step still counts as "just past": a sixth of the step, 10 seconds for a minute."""
    return max(1, increment // 6)


def seconds_past_step(card, seconds):
    """Seconds a call ran past its last billed step, or None when a shorter call would cost the same."""
    if seconds is None or seconds <= 0:
        return None
    billed = billed_seconds(card, seconds)
    lower = billed - card.billing_increment_seconds
    if lower < max(card.minimum_seconds, 1):
        return None  # inside the minimum charge or the first step
    return int(-(-seconds // 1)) - lower


def step_price(card):
    """What one billing step costs, rounded up like an invoice."""
    return -(-card.per_minute_micros * card.billing_increment_seconds // 60)


def step_words(increment):
    return 'a billed minute' if increment == 60 else f'a billed {increment}-second step'


def step_noun(increment):
    return 'a minute' if increment == 60 else f'a {increment}-second step'


def _money(micros, currency):
    return {'currency': currency, 'amount': format_amount(micros)}


def _range(values):
    low, high = min(values), max(values)
    return f'{low} s' if low == high else f'{low}–{high} s'


def billing_boundaries(engine, store, values, *, now=None, days=WINDOW_DAYS):
    """Per number: how its calls end against the trunk card's billing step; estimates, never applied."""
    preset = (getattr(values, 'sip_trunk_preset', '') or '').strip()
    carrier = carrier_label(preset) if preset else None
    result = {'days': days, 'estimate': True, 'carrier': carrier, 'min_calls': MIN_CALLS, 'step': None,
              'numbers': [], 'numbers_total': 0, 'calls_near': 0, 'saving': None}
    if not preset:
        return {**result, 'state': 'no_trunk',
                'sentence': 'Faxbot has no carrier line set up, so there are no calls to measure.'}
    card = store.card_for('sip')
    if card is None:
        return {**result, 'state': 'no_price',
                'sentence': (f'Faxbot has no price for calls on your {carrier} line, so it cannot tell where a call '
                             'crosses into another billed minute. Add the price in Costs → Prices & plans.')}
    increment = card.billing_increment_seconds
    price = step_price(card)
    result['step'] = {'seconds': increment, 'minimum_seconds': card.minimum_seconds,
                      'near_seconds': near_seconds(increment), 'price': _money(price, card.currency),
                      'price_text': money_text(price, card.currency), 'source_url': card.source_url,
                      'read_on': card.captured_on.date().isoformat()}
    if not card.per_minute_micros:
        return {**result, 'state': 'not_by_time',
                'sentence': f'Your {carrier} line is not billed by call length, so a shorter call costs the same.'}
    if increment < FINE_STEP_SECONDS:
        return {**result, 'state': 'fine_steps',
                'sentence': (f'{carrier} bills calls in {increment}-second steps, so a call that ends a few seconds '
                             f'sooner saves at most {money_text(price, card.currency)}; there is nothing worth '
                             'changing.')}
    by_number = {}
    for call in trunk_calls(engine, days=days, now=now):
        if call.destination and call.connected_seconds is not None and call.connected_seconds > 0:
            by_number.setdefault(call.destination, []).append(call)
    near = near_seconds(increment)
    rows, examined = [], 0
    for number, calls in sorted(by_number.items()):
        if len(calls) < MIN_CALLS:
            continue
        examined += 1
        past = [seconds_past_step(card, call.connected_seconds) for call in calls]
        close = [value for value in past if value is not None and value <= near]
        if not close:
            continue
        row = store.get_destination(number) or {}
        saving = price * len(close)
        rows.append({
            'number': number, 'display_name': row.get('display_name'), 'calls': len(calls), 'calls_near': len(close),
            'seconds_past': {'least': min(close), 'most': max(close)},
            'saving': _money(saving, card.currency), '_saving': saving,
            'sentence': (f'Calls to this number end {_range(close)} past {step_words(increment)} on {len(close)} of '
                         f'{len(calls)} calls; one page less or a faster mode would have saved {step_noun(increment)} '
                         f'on each, about {money_text(saving, card.currency)} in the last {days} days (estimate).')})
    rows.sort(key=lambda item: (-item['calls_near'], -item['_saving'], item['number']))
    total = sum(item['_saving'] for item in rows)
    calls_near = sum(item['calls_near'] for item in rows)
    for item in rows:
        item.pop('_saving')
    result.update(numbers=rows[:SHOWN], numbers_total=len(rows), calls_near=calls_near,
                  saving=_money(total, card.currency) if rows else None)
    if not examined:
        state = 'too_few'
        sentence = (f'Faxbot needs at least {MIN_CALLS} measured calls to the same number in the last {days} days to '
                    f'show how calls end against {step_words(increment)}; no number has that many yet.')
    elif not rows:
        state = 'none_near'
        sentence = (f'No number\'s calls end just past {step_words(increment)}, so a faster call would not have cost '
                    'less.')
    else:
        state = 'near'
        numbers = 'one number' if len(rows) == 1 else f'{len(rows)} numbers'
        sentence = (f'Calls to {numbers} often end just past {step_words(increment)}: one page less or a faster mode '
                    f'would have saved {step_noun(increment)} on {calls_near} '
                    f"{'call' if calls_near == 1 else 'calls'}, about {money_text(total, card.currency)} in the last "
                    f'{days} days (estimate).')
    result.update(state=state, sentence=sentence)
    return result
