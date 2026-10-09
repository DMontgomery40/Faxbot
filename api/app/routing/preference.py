"""The fax marker (RFC 6913 Accept-Contact, "mark calls as fax"), measured from call history only.

Faxbot marks every call it places over the carrier trunk as a fax call unless
the administrator turned the setting off (``sip_fax_preference_header``, on by
default). This module compares the calls Faxbot recorded with the marker on
against those with it off: how many were delivered, how many used fax over IP
(T.38) rather than audio, how long they took, and what each delivered fax cost.

It changes no setting and places no call. The two groups are calls from
different days, not a controlled test, and the report says so. Fewer than
``MIN_CALLS`` calls on either side is too few to conclude anything, and the
report says that instead of comparing.
"""
from .costs import format_amount
from .delivered import short_money_text
from .trunk_calls import trunk_calls


WINDOW_DAYS = 90
# Calls needed on each side before Faxbot compares them.
MIN_CALLS = 10
# Differences smaller than this many percentage points are not called a difference.
CLEAR_POINTS = 5


def _percent(part, whole):
    return None if not whole else round(100 * part / whole)


def _plural(count, one, many=None):
    return f"{count} {one if count == 1 else (many or one + 's')}"


def side(calls):
    """One group of calls in numbers: results, T.38 or audio, time and cost per delivered fax."""
    delivered = [call for call in calls if call.delivered is True]
    failed = sum(1 for call in calls if call.delivered is False)
    unknown = sum(1 for call in calls if call.delivered is None)
    t38 = sum(1 for call in calls if call.t38 == 'yes')
    audio = sum(1 for call in calls if call.t38 == 'no')
    timed = [call for call in delivered if call.connected_seconds is not None]
    paged = [call for call in timed if call.pages and call.pages > 0]
    unpriced = sum(1 for call in calls if call.basis is None)
    currencies = {call.currency for call in calls if call.currency}
    counts = {basis: sum(1 for call in calls if call.basis == basis) for basis in ('settled', 'reported', 'estimated')}
    per_delivered, text = None, None
    if unpriced:
        text = 'Unknown'
    elif len(currencies) > 1:
        text = 'Charged in several currencies'
    elif not delivered:
        text = 'None delivered'
    else:
        currency = currencies.pop()
        total = sum(call.cost_micros for call in calls)
        micros = -(-total // len(delivered))
        per_delivered = {'currency': currency, 'amount': format_amount(micros)}
        shown = short_money_text(micros, currency)
        # Only settled carrier charges are exact; anything else may still change.
        text = shown if counts['settled'] == len(calls) else f'About {shown}'
    return {'calls': len(calls), 'delivered': len(delivered), 'failed': failed, 'result_unknown': unknown,
            'delivered_percent': _percent(len(delivered), len(delivered) + failed),
            't38': t38, 'audio': audio, 'mode_unknown': len(calls) - t38 - audio,
            't38_percent': _percent(t38, t38 + audio),
            'average_seconds': round(sum(call.connected_seconds for call in timed) / len(timed)) if timed else None,
            'seconds_per_page': (round(sum(call.connected_seconds for call in paged)
                                       / sum(call.pages for call in paged)) if paged else None),
            'cost_per_delivered': per_delivered, 'cost_text': text,
            'settled': counts['settled'], 'reported': counts['reported'], 'estimated': counts['estimated'],
            'unpriced': unpriced}


def _setting_sentence(on):
    if on:
        return 'Mark calls as fax is on, and Faxbot leaves it on: this comparison never changes a setting.'
    return 'Mark calls as fax is off on your carrier trunk; this comparison never changes a setting.'


def _difference(marked, plain):
    """One sentence on what the numbers show, or None when nothing differs by ``CLEAR_POINTS`` or more."""
    parts = []
    for key, words in (('delivered_percent', 'delivered more often'), ('t38_percent', 'used fax over IP (T.38) more often')):
        a, b = marked[key], plain[key]
        if a is None or b is None or abs(a - b) < CLEAR_POINTS:
            continue
        parts.append(f"calls {'marked' if a > b else 'not marked'} as fax {words}")
    if not parts:
        return 'The marker made no clear difference to delivery or to fax over IP (T.38).'
    text = ' and '.join(parts)
    return text[:1].upper() + text[1:] + '.'


def _side_text(name, figures):
    text = (f"{name}: {figures['delivered_percent']}% delivered, "
            f"{'no call' if figures['t38_percent'] is None else str(figures['t38_percent']) + '%'} "
            'used fax over IP (T.38)')
    if figures['seconds_per_page'] is not None:
        text += f", {figures['seconds_per_page']} seconds a page"
    cost = figures['cost_text']
    if figures['cost_per_delivered'] is not None:
        return text + f', {cost[:1].lower() + cost[1:]} per delivered fax'
    return text + (', cost per delivered fax unknown' if cost == 'Unknown' else f', {cost[:1].lower() + cost[1:]}')


def fax_marker_report(engine, values, *, now=None, days=WINDOW_DAYS):
    """Calls with the fax marker on against calls with it off, from history; advice only."""
    calls = trunk_calls(engine, days=days, now=now)
    recorded = [call for call in calls if call.marker is not None]
    marked = [call for call in recorded if call.marker]
    plain = [call for call in recorded if not call.marker]
    setting_on = bool(getattr(values, 'sip_fax_preference_header', True))
    result = {'days': days, 'min_calls': MIN_CALLS, 'setting_on': setting_on,
              'setting_sentence': _setting_sentence(setting_on), 'marked': side(marked), 'not_marked': side(plain),
              'left_out': len(calls) - len(recorded), 'caveat': None, 'difference': None}
    window = f'the last {days} days'
    if not recorded:
        state = 'no_calls'
        sentence = f'Faxbot placed no calls over your carrier line in {window}, so there is nothing to compare yet.'
    elif not plain or not marked:
        state = 'one_side'
        which = 'marked' if marked else 'not marked'
        sentence = (f"Every call in {window} was {which} as fax ({_plural(len(recorded), 'call')}), so there is "
                    'nothing to compare it with.')
    elif len(marked) < MIN_CALLS or len(plain) < MIN_CALLS:
        state = 'too_few'
        sentence = (f"Too few calls to tell: {len(marked)} marked as fax and {len(plain)} not marked in {window}. "
                    f'Faxbot compares them once it has at least {MIN_CALLS} of each.')
    else:
        state = 'compared'
        result['difference'] = _difference(result['marked'], result['not_marked'])
        sentence = (f"{_side_text('Marked as fax', result['marked'])}. "
                    f"{_side_text('Not marked', result['not_marked'])}. {result['difference']}")
        result['caveat'] = ('The two groups are calls from different days, not a controlled test, so other changes '
                            'may explain part of any difference.')
    result.update(state=state, sentence=sentence, enough=state == 'compared')
    if result['left_out']:
        result['left_out_sentence'] = (f"{_plural(result['left_out'], 'call')} "
                                       f"{'is' if result['left_out'] == 1 else 'are'} left out because Faxbot did not "
                                       'record whether it was marked.')
    else:
        result['left_out_sentence'] = None
    return result
