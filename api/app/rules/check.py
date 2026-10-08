"""Check a rules draft before publishing: what it names must exist, its rules must be able to work, and replay.

Errors block publishing; warnings do not (design §4.8). The replay re-decides
recent faxes under the draft and lists those whose route would change. Faxes
accepted before rules existed have no stored facts; they are rebuilt from the
sent fax with today's preferences and labelled approximate.
"""
from dataclasses import dataclass, field
import json

from . import model
from .compile import Problem, cap_micros, compile_document, document_problems
from .evaluate import decide


@dataclass(frozen=True)
class CheckContext:
    """What exists now, for the names a document uses. ``None`` for a set means "not known; don't check"."""
    accounts: tuple = ()                       # model.Account, in configured order
    people: frozenset | None = None            # principal IDs (people and integrations)
    keys: frozenset | None = None              # integration key bindings
    groups: frozenset | None = None
    mailboxes: frozenset | None = None
    recipients: frozenset | None = None        # saved recipient IDs
    partners: frozenset = frozenset()          # numbers with a verified direct partner
    alternates: frozenset = frozenset()        # numbers with an approved alternate number
    prices: dict = field(default_factory=dict)  # account -> (micros for one page, currency), or None when unknown
    matches_30_days: dict | None = None        # rule ID -> faxes it matched in the last 30 days
    relays: frozenset | None = None            # partner IDs that relay for this installation (direct/relay.py)
    digital: frozenset | None = None           # confirmed Direct address and FHIR endpoint IDs (digital/)


class _Findings:
    def __init__(self):
        self.items = []

    def error(self, code, message, rule_id=None, path=None):
        self.items.append(Problem('error', code, message, rule_id, path))

    def warning(self, code, message, rule_id=None, path=None):
        self.items.append(Problem('warning', code, message, rule_id, path))


def _name(rule):
    return f'‘{rule.get("name", "").strip() or rule.get("id")}’'


def _rule_accounts(then):
    for action in ('use', 'try_in_order', 'cheapest_reliable'):
        if action in then:
            value = then[action]
            return [value] if isinstance(value, str) else list(value)
    return None


def _covers(broad, narrow):
    """True when every fax that matches ``narrow`` also matches ``broad`` (conservative: False when unsure)."""
    broad, narrow = broad or {}, narrow or {}
    for group, value in broad.items():
        if group not in narrow:
            return False
        other = narrow[group]
        spec = model.CONDITIONS[group]
        if spec == 'list':
            if not set(other) <= set(value):
                return False
        elif spec in ('yes_no', 'window'):
            if other != value:
                return False
        else:
            for name, wanted in value.items():
                if name not in other:
                    return False
                kind = spec[name]
                if kind == 'list' and not set(other[name]) <= set(wanted):
                    return False
                if kind == 'yes_no' and other[name] != wanted:
                    return False
                if kind == 'count' and not ((name == 'pages_under' and other[name] <= wanted)
                                            or (name != 'pages_under' and other[name] >= wanted)):
                    return False
    return True


def _always(rule):
    return not rule.get('when') and not rule.get('unless')


def _listed_numbers(rule, definitions):
    """Exact numbers a rule's destination condition names, directly or through its recipient groups."""
    destination = (rule.get('when') or {}).get('destination') or {}
    numbers = set(destination.get('numbers', ()))
    for key in destination.get('lists', ()):
        listed = definitions.get(key) if isinstance(definitions, dict) else None
        if isinstance(listed, dict):
            numbers.update(listed.get('numbers', ()))
    return numbers


def _references(kind, document, organization, context, findings):
    accounts = {account.key: account for account in context.accounts}
    lists = (organization or {}).get('lists') or {}
    regions = (organization or {}).get('regions') or {}
    sites = {item.get('key') for item in (organization or {}).get('sites') or () if isinstance(item, dict)}
    workflows = {item.get('key') for item in (organization or {}).get('workflows') or () if isinstance(item, dict)}
    labels = set((organization or {}).get('labels') or ())
    known = {('destination', 'lists'): (set(lists), 'recipient group'),
             ('destination', 'regions'): (set(regions), 'region'),
             ('destination', 'recipients'): (context.recipients, 'saved recipient'),
             ('sender', 'people'): (context.people, 'person or integration'),
             ('sender', 'keys'): (context.keys, 'integration key'),
             ('sender', 'groups'): (context.groups, 'group'),
             ('sender', 'mailboxes'): (context.mailboxes, 'mailbox'),
             ('sender', 'sites'): (sites, 'site'),
             ('workflows', None): (workflows, 'workflow'),
             ('labels', None): (labels, 'label')}

    def account(rule, key, path, *, sending=True):
        if key in model.RESERVED_KEYS:
            return
        if model.is_digital(key):
            if key != model.DIGITAL and context.digital is not None and key.split(':', 1)[1] not in context.digital:
                kind = 'a Direct address' if key.startswith('dsm:') else 'a FHIR endpoint'
                findings.error('unknown_account', f'{_name(rule)} names {kind} that is no longer confirmed; confirm '
                                                  'it again under Recipients, or name digital for every one.',
                               rule.get('id'), path)
            return
        if model.is_relay(key):
            if context.relays is not None and key[len('relay:'):] not in context.relays:
                findings.error('unknown_account', f'{_name(rule)} names a partner relay that is not active; '
                                                  'accept a partner’s offer under Partners first.',
                               rule.get('id'), path)
            return
        found = accounts.get(key)
        if found is None:
            findings.error('unknown_account', f'{_name(rule)} names the account “{key}”, which doesn’t exist.',
                           rule.get('id'), path)
        elif sending and not found.sends:
            findings.error('not_sending', f'{_name(rule)} names {found.label or key}, which doesn’t send faxes.',
                           rule.get('id'), path)

    for section in ('limits', 'routes'):
        for index, rule in enumerate(document.get(section) or ()):
            path = f'{section}[{index}]'
            for part in ('when', 'unless'):
                for group, value in (rule.get(part) or {}).items():
                    names = value if model.CONDITIONS.get(group) == 'list' else None
                    entries = [(None, names)] if names is not None else (
                        list(value.items()) if isinstance(value, dict) else [])
                    for name, values in entries:
                        found = known.get((group, name))
                        if found is None or found[0] is None or not isinstance(values, list):
                            continue
                        for item in values:
                            if item not in found[0]:
                                findings.error('unknown_name', f'{_name(rule)} names the {found[1]} “{item}”, '
                                               'which doesn’t exist.', rule.get('id'), f'{path}.{part}.{group}')
            then = rule.get('then') or {}
            for key in then.get('never', ()):
                account(rule, key, f'{path}.then.never', sending=False)
            for key in _rule_accounts(then) or ():
                account(rule, key, f'{path}.then')
            target = then.get('site_accounts')
            if target and target != 'sender' and target not in sites:
                findings.error('unknown_name', f'{_name(rule)} names the site “{target}”, which doesn’t exist.',
                               rule.get('id'), f'{path}.then.site_accounts')
    if kind == model.ORGANIZATION:
        claimed = {}
        for site in document.get('sites') or ():
            for key in site.get('accounts', ()):
                if key in claimed:
                    findings.error('account_two_sites', f'{site["name"]} and {claimed[key]} both list the account '
                                   f'“{key}”; an account belongs to one site.', path='sites')
                claimed.setdefault(key, site['name'])
                if key not in accounts:
                    findings.error('unknown_account', f'{site["name"]} lists the account “{key}”, which doesn’t '
                                   'exist.', path='sites')
                elif accounts[key].site not in (None, site['key']):
                    findings.error('account_two_sites', f'{site["name"]} lists “{key}”, but that account is set '
                                   f'to the site “{accounts[key].site}”.', path='sites')


def _conflicts(kind, document, organization, context, findings):
    limits = [rule for rule in document.get('limits') or () if rule.get('on', True)]
    routes = [rule for rule in document.get('routes') or () if rule.get('on', True)]
    org_limits = ([] if kind == model.ORGANIZATION else
                  [rule for rule in (organization or {}).get('limits') or () if rule.get('on', True)])
    # A routing rule that an earlier one always catches first can never choose.
    for index, rule in enumerate(routes):
        for earlier in routes[:index]:
            if not earlier.get('unless') and _covers(earlier.get('when'), rule.get('when')):
                findings.error('shadowed', f'{_name(rule)} can never choose: {_name(earlier)} comes first and '
                               'matches every fax it would.', rule.get('id'))
                break
    # A limit that removes every account a routing rule names, for every fax that rule matches.
    for rule in routes:
        accounts = _rule_accounts(rule.get('then') or {})
        if not accounts:
            continue
        for limit in limits + org_limits:
            then = limit.get('then') or {}
            if limit.get('unless') or not _covers(limit.get('when'), rule.get('when')):
                continue
            removed = set(then.get('never', ()))
            if then.get('require_direct'):
                removed.update(accounts)
            if set(accounts) <= removed:
                scope = 'the organization’s limit ' if limit in org_limits else ''
                code = 'not_allowed' if limit in org_limits else 'no_accounts'
                findings.error(code, f'{_name(rule)} has no account left: {scope}{_name(limit)} removes '
                               f'{"it" if len(accounts) == 1 else "all of them"} for every fax it matches.',
                               rule.get('id'))
                break
        # A cap below every known price, while the other accounts have no price at all.
        for limit in limits + org_limits:
            then = limit.get('then') or {}
            if 'cap_cost' not in then or limit.get('unless') or not _covers(limit.get('when'), rule.get('when')):
                continue
            found = cap_micros(then['cap_cost'])
            if found is None:
                continue
            micros, currency = found
            prices = [context.prices.get(key) for key in accounts if key in context.prices]
            known = [price for price in prices if price is not None and price[1] == currency]
            if known and len(prices) == len(accounts) and all(price[0] > micros for price in known):
                findings.error('cap_unreachable', f'{_name(rule)} can never send under {_name(limit)}: every '
                               'account it names costs more than the cap, or its price is unknown.', rule.get('id'))


def _warnings(kind, document, organization, context, findings):
    definitions = (organization or {}).get('lists') or {}
    for section in ('limits', 'routes'):
        for rule in document.get(section) or ():
            then = rule.get('then') or {}
            rule_id = rule.get('id')
            if len(then.get('try_in_order', ())) > 3:
                findings.warning('long_order', f'{_name(rule)} lists more than three accounts. Faxbot sends a fax '
                                 'at most three times, though later accounts are still used when earlier ones are '
                                 'not ready.', rule_id)
            if then.get('require_direct'):
                numbers = _listed_numbers(rule, definitions)
                missing = sorted(numbers - set(context.partners))
                if missing or not numbers:
                    findings.warning('no_partner', f'{_name(rule)} requires direct delivery, but '
                                     f'{"some of its numbers have" if numbers else "not every number has"} no verified '
                                     'partner. Those faxes will wait for you in Sent.', rule_id)
            if then.get('alternate_number') == 'only':
                numbers = _listed_numbers(rule, definitions)
                if not numbers or numbers - set(context.alternates):
                    findings.warning('no_alternate', f'{_name(rule)} sends only to approved alternate numbers, and '
                                     'some recipients it matches have none. Those faxes will wait for you in Sent.',
                                     rule_id)
            if then.get('site_accounts') == 'sender' and not ((rule.get('when') or {}).get('sender') or {}).get('sites'):
                findings.warning('no_site', f'{_name(rule)} uses the sender’s site, but it also matches senders '
                                 'without a site. Their faxes will wait for you in Sent.', rule_id)
            if 'cap_cost' in then:
                unpriced = sorted(account.label or account.key for account in context.accounts
                                  if account.sends and context.prices.get(account.key) is None)
                if unpriced:
                    findings.warning('no_price', f'{_name(rule)} caps the cost, and these accounts have no price, so '
                                     f'the cap never lets them send: {", ".join(unpriced)}.', rule_id)
            if context.matches_30_days is not None and rule_id in context.matches_30_days \
                    and context.matches_30_days[rule_id] == 0:
                findings.warning('unused', f'{_name(rule)} matched no fax in the last 30 days.', rule_id)


def check(kind, scope_id, document, context, *, organization=None):
    """Errors and warnings for one scope's draft. ``organization`` is the organization's document for lower scopes."""
    findings = _Findings()
    findings.items.extend(document_problems(kind, document))
    if any(problem.level == 'error' for problem in findings.items):
        return findings.items
    org = document if kind == model.ORGANIZATION else organization
    _references(kind, document, org, context, findings)
    _conflicts(kind, document, org, context, findings)
    _warnings(kind, document, org, context, findings)
    return findings.items


# Replay ------------------------------------------------------------------------------------------------------

def route_key(decision):
    """What "the route would change" compares: outcome, method, accounts, delivery inside, holds, number, layout and
    subaddress."""
    envelope = decision.envelope
    return (decision.outcome, decision.reason, envelope.mode, envelope.accounts, envelope.local, envelope.direct,
            envelope.require_direct, tuple(hold.kind for hold in envelope.holds),
            envelope.dial.number if envelope.dial else None, envelope.page_layout, envelope.subaddress)


@dataclass(frozen=True)
class ReplayItem:
    job_id: str
    to_number: str
    accepted_at: str
    before: model.Decision
    after: model.Decision
    approximate: bool


def replay(entries, compiled, accounts):
    """Re-decide stored faxes under ``compiled`` (scope name -> Compiled).

    ``entries`` are ``(job_id, to_number, accepted_at, facts, before)``; ``before`` is the stored decision, or
    None for a fax accepted before rules (its "before" is then the automatic choice, and the item is approximate).
    Returns ``(checked, [ReplayItem for faxes whose route would change])``.
    """
    changed, checked = [], 0
    for job_id, to_number, accepted_at, facts, before in entries:
        checked += 1
        approximate = before is None or facts.approximate
        if before is None:
            before = decide({}, facts, accounts)
        after = decide(compiled, facts, accounts)
        if route_key(before) != route_key(after):
            changed.append(ReplayItem(job_id, to_number, accepted_at, before, after, approximate))
    return checked, changed


def compile_for_replay(kind, scope_id, document, active):
    """The scopes a draft would decide with: the active ones, with this scope's replaced by the draft."""
    from .store import draft_ref
    compiled = dict(active)
    name = model.scope_name(kind, scope_id)
    if kind == model.ORGANIZATION:
        draft = compile_document(draft_ref(kind, scope_id), document)
        compiled[name] = draft
        for other, scope in list(compiled.items()):
            if other != name:
                compiled[other] = compile_document(scope.ref, _document_of(scope), draft.definitions)
        return compiled
    organization = compiled.get(model.ORGANIZATION)
    compiled[name] = compile_document(draft_ref(kind, scope_id), document,
                                      organization.definitions if organization is not None else None)
    return compiled


def _document_of(scope):
    """Lower scopes are recompiled against a draft organization from their stored documents."""
    document = getattr(scope, 'document', None)
    if document is None:
        raise ValueError('Compiled scope has no document.')
    return json.loads(document) if isinstance(document, str) else document
