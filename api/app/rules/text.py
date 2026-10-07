"""Plain sentences for a routing decision: what it does, why, what was skipped and what waits.

The reader is the administrator. Amounts are estimates, labelled as such, and
times are local. Names come from the decision's own record (rule names as they
were when the fax was decided) and from labels the caller passes in.
"""
from datetime import datetime

from . import model


FIELD_WORDS = {
    'destination.numbers': 'the fax number', 'destination.prefixes': 'how the number begins',
    'destination.lists': 'the recipient group', 'destination.countries': 'the destination’s country',
    'destination.regions': 'the destination’s region', 'destination.recipients': 'the saved recipient',
    'destination.partner': 'whether the number has a verified partner',
    'destination.own_number': 'whether it is one of your own numbers',
    'destination.approved_alternate': 'whether the recipient has an approved alternate number',
    'destination.in_sender_country': 'whether it is in the sender’s country',
    'sender.people': 'who sent it', 'sender.keys': 'the integration key that sent it',
    'sender.groups': 'the sender’s groups', 'sender.mailboxes': 'the mailbox it was sent from',
    'sender.sites': 'the sender’s site', 'workflows': 'the workflow', 'labels': 'the labels',
    'document.pages_over': 'the number of pages', 'document.pages_under': 'the number of pages',
    'document.size_over': 'the file size', 'document.case_packet': 'whether it is a case packet',
    'urgent': 'whether it is urgent', 'real_call': 'whether a real call was asked for', 'time': 'the time it was sent',
    'unless': 'an exception in the rule',
}
SSL_FAX = ('SSL Fax encrypts the call only when the receiving machine supports it, and Faxbot can’t verify who '
           'answers.')
WAITS = 'It waits for you in Sent.'


def account_label(key, accounts=()):
    for account in accounts:
        if account.key == key and account.label:
            return account.label
    if key == model.LOCAL:
        return 'This Faxbot'
    if key == model.DIRECT:
        return 'Direct delivery'
    try:
        from ..routing.plan import route_label
        return route_label(key)
    except Exception:
        return key


def _join(names):
    names = list(names)
    if len(names) <= 1:
        return ''.join(names)
    return ', '.join(names[:-1]) + ' and ' + names[-1]


def _then(names):
    return ', then '.join(names)


def scope_text(source, scope_names=None):
    """'organization rules, version 7', 'the Leeds mailbox’s rules, version 2' or '… draft'."""
    scope_names = scope_names or {}
    if source.scope == model.ORGANIZATION:
        owner = 'organization rules'
    else:
        name = scope_names.get(source.scope_id) or source.scope_id
        owner = f'the {name} {source.scope}’s rules'
    if source.revision is None:
        return owner
    return f'{owner}, draft' if source.revision == 0 else f'{owner}, version {source.revision}'


def rule_text(source):
    if source is None:
        return 'a rule'
    return f'the rule ‘{source.rule_name or source.rule_id}’'


def _capital(text):
    return text[:1].upper() + text[1:]


def money(micros, currency):
    from ..routing.costs import money_text
    return money_text(micros, currency)


def local_time_text(value, zone_name=None):
    from ..people_time import date_and_time
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return date_and_time(moment, zone_name)


def number_text(number):
    try:
        import phonenumbers
        return phonenumbers.format_number(phonenumbers.parse(number), phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except Exception:
        return number


def toll_free(number):
    try:
        import phonenumbers
        return phonenumbers.number_type(phonenumbers.parse(number)) == phonenumbers.PhoneNumberType.TOLL_FREE
    except Exception:
        return False


def route_sentence(decision, accounts=(), scope_names=None):
    """What the route is and what chose it, in one sentence."""
    envelope, source = decision.envelope, decision.route
    names = [account_label(key, accounts) for key in envelope.accounts]
    where = f' ({scope_text(source, scope_names)})' if source.kind == 'rule' else ''
    if envelope.local:
        return 'This is one of your own fax numbers, so the fax goes straight into Received without a phone call.'
    if envelope.require_direct:
        return f'Delivered straight to the verified partner, with no fax call, because {rule_text(_require(decision, "direct_required"))} requires it.'
    if source.kind == 'automatic':
        return 'No sending rule matched, so Faxbot chooses the cheapest reliable route, as before.'
    if source.kind == 'preferred':
        return f'You chose {account_label(source.rule_id, accounts)} for this number, so it goes first.'
    if envelope.mode == 'automatic':
        return f'{_capital(rule_text(source))} matched{where}, so Faxbot chooses the cheapest reliable route, as before.'
    if not names:
        return f'{_capital(rule_text(source))} matched{where}.'
    if envelope.mode == 'one':
        return f'Goes by {names[0]} because {rule_text(source)} matched{where}.'
    if envelope.mode == 'ordered':
        return f'Tries {_then(names)}, because {rule_text(source)} matched{where}.'
    return f'Goes by the cheapest reliable of {_join(names)}, because {rule_text(source)} matched{where}.'


def _require(decision, why):
    return next((item.source for item in decision.excluded if item.why == why), decision.route)


def _cap_source(decision):
    found = next((item.source for item in decision.excluded if item.why in ('over_cap', 'unknown_cost')), None)
    return found or (decision.envelope.caps[0].source if decision.envelope.caps else None)


def blocked_sentence(decision, accounts=()):
    reason = decision.reason
    if reason == 'no_allowed_account':
        sources = []
        for item in decision.excluded:
            if item.source is not None and item.source not in sources and item.source.kind == 'rule':
                sources.append(item.source)
        if sources:
            text = _join(rule_text(source) for source in sources)
            return f'No account is allowed for this fax: {text} {"removes" if len(sources) == 1 else "remove"} every one it could use. {WAITS}'
        return f'No account is allowed for this fax. {WAITS}'
    if reason == 'no_account_under_cap':
        source = _cap_source(decision)
        cap = next((cap for cap in decision.envelope.caps if cap.source == source), None)
        amount = f'the {money(cap.micros, cap.currency)} cap' if cap else 'the cap'
        return f'No account is estimated to cost less than {amount} in {rule_text(source)}. {WAITS}'
    if reason == 'needs_partner':
        return (f'{_capital(rule_text(_require(decision, "direct_required")))} requires direct delivery, and this '
                f'number has no verified partner. {WAITS}')
    if reason == 'needs_encryption':
        return (f'{_capital(rule_text(_require(decision, "not_encrypted")))} requires encryption: this number has no '
                f'verified partner and has never taken an SSL Fax call. {WAITS}')
    if reason == 'needs_alternate':
        return (f'{_capital(rule_text(decision.alternate_source))} sends only to an approved alternate number, and '
                f'this recipient has none. {WAITS}')
    if reason == 'no_site':
        return (f'{_capital(rule_text(decision.route))} uses the sender’s site’s accounts, and the sender belongs to '
                f'no site. {WAITS}')
    return WAITS


def hold_sentence(hold, zone_name=None):
    if hold.kind == 'approval':
        who = ' Someone other than the sender must approve it.' if hold.separate_approver else ''
        return f'Waits for approval: {rule_text(hold.source)} matched.{who}'
    return f'Waits until {local_time_text(hold.release_at, zone_name)}: {rule_text(hold.source)} matched.'


def excluded_sentence(item, accounts=(), facts=None, caps=()):
    name = account_label(item.account, accounts)
    rule = rule_text(item.source)
    if item.why == 'never':
        return f'{name} is skipped: {rule} applies.'
    if item.why == 'over_cap':
        quote = next((q for q in (facts.quotes if facts else ()) if q.account == item.account
                      and q.micros is not None), None)
        cap = next((cap for cap in caps if cap.source == item.source), None)
        estimate = f'its estimate, {money(quote.micros, quote.currency)}, is' if quote else 'its estimate is'
        limit = f'the {money(cap.micros, cap.currency)} cap' if cap else 'the cap'
        return f'{name} is skipped: {estimate} over {limit} in {rule}.'
    if item.why == 'unknown_cost':
        return f'{name} is skipped: its price is unknown, and {rule} caps the cost.'
    if item.why == 'not_sending':
        return f'{name} doesn’t send faxes.'
    if item.why == 'turned_off':
        return f'{name} is turned off.'
    if item.why == 'unknown_account':
        return f'{_capital(rule)} names “{item.account}”, which isn’t one of your accounts.'
    if item.why == 'direct_required':
        return f'{name} is skipped: {rule} requires direct delivery.'
    return f'{name} is skipped: {rule} requires encryption, and this number has never taken an SSL Fax call.'


def dial_sentence(decision, original):
    dial = decision.envelope.dial
    if dial is None:
        return None
    approved = ''
    if dial.approved_by or dial.approved_at:
        parts = ['approved']
        if dial.approved_by:
            parts.append(f'by {dial.approved_by}')
        if dial.approved_at:
            from ..people_time import _local, installation_zone_name
            moment = _local(datetime.fromisoformat(dial.approved_at), installation_zone_name())
            parts.append(f'on {moment.day} {moment:%B %Y}')
        approved = ', ' + ' '.join(parts)
    note = f' (‘{dial.note}’)' if dial.note else ''
    pays = ' The recipient pays for calls to their toll-free number.' if toll_free(dial.number) else ''
    return (f'Dials the recipient’s approved alternate number {number_text(dial.number)} instead of '
            f'{number_text(original)}{approved}{note}.{pays}')


def decision_sentence(decision, accounts=(), scope_names=None, zone_name=None):
    """The one sentence for a decision: blocked, held, or the route."""
    if decision.outcome == 'blocked':
        return blocked_sentence(decision, accounts)
    if decision.outcome == 'held':
        return hold_sentence(decision.envelope.holds[0], zone_name)
    return route_sentence(decision, accounts, scope_names)


def failed_words(step):
    """The first condition that did not match, in words, or None."""
    if step.field is None:
        return None
    return FIELD_WORDS.get(step.field, step.field)
