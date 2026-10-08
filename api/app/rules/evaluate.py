"""Decide one fax's route envelope from its facts, the accounts and the compiled scopes. Pure and repeatable.

``decide`` reads no clock, database or configuration: the same facts, the same
scope revisions and the same accounts always give the same ``Decision``. It is
called when a fax is accepted, by the dry run, by the check's replay and by
"apply the current rules to waiting faxes".

How the scopes combine (design §4.5):

- every matching limit of every applicable scope applies, and limits only
  narrow, so their order cannot change the result;
- the route is chosen by a matching mandatory organization rule, else the
  workflow's first match, else the mailbox's, else the recipient's preferred
  route (an implicit organization rule), else the organization's first match,
  else the automatic choice, which is Faxbot's behaviour without rules;
- the chosen rule's accounts are kept only where every scope's limits allow
  them. An empty result holds the fax with a reason (owner's answer Q1).
"""
from datetime import datetime, timedelta, timezone

from . import model
from .compile import Context, EMPTY_DEFINITIONS, Window, cap_micros, local_time
from .model import (AUTOMATIC, DIRECT, LOCAL, ORGANIZATION, Cap, Decision, Envelope, Excluded, Hold,
                    Source, Step)


def applicable_scopes(compiled, facts):
    """The compiled scopes that apply to this fax, organization first: ``[(scope name, Compiled)]``.

    ``compiled`` maps scope names ('organization', 'mailbox:<id>', 'workflow:<key>') to compiled
    active revisions; the mailbox is the fax's sending mailbox and the workflow is worked out from the
    organization's definitions.
    """
    organization = compiled.get(ORGANIZATION)
    definitions = organization.definitions if organization is not None else EMPTY_DEFINITIONS
    from .compile import fax_workflow
    found = []
    if organization is not None:
        found.append((ORGANIZATION, organization))
    if facts.mailbox_id:
        name = model.scope_name(model.MAILBOX, facts.mailbox_id)
        if compiled.get(name) is not None:
            found.append((name, compiled[name]))
    workflow = fax_workflow(definitions, facts)
    if workflow:
        name = model.scope_name(model.WORKFLOW, workflow)
        if compiled.get(name) is not None:
            found.append((name, compiled[name]))
    return found


def _quote(facts, key, number):
    for quote in facts.quotes:
        if quote.account == key and quote.number == number:
            return quote
    return None


def release_at(accepted_at, zone_name, window):
    """When a time-window hold opens (naive UTC ISO), or None when the fax was accepted inside the window."""
    local = local_time(accepted_at, zone_name)
    weekday, minute = local.weekday(), local.hour * 60 + local.minute
    if window.contains(weekday, minute):
        return None
    start = window.start or 0
    for ahead in range(0, 8):
        day = local.date() + timedelta(days=ahead)
        if day.weekday() not in window.days:
            continue
        opening = datetime(day.year, day.month, day.day, start // 60, start % 60, tzinfo=local.tzinfo)
        if opening.astimezone(timezone.utc) > local.astimezone(timezone.utc):
            return opening.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
    return None


def _site_accounts(site, accounts):
    """The site's listed accounts, then those whose own site names it, in configured order."""
    keys = list(site.accounts)
    keys += [account.key for account in accounts if account.site == site.key and account.key not in keys]
    return keys


class _Limits:
    """What every matching limit adds up to."""

    def __init__(self):
        self.never = {}               # account -> Source
        self.caps = {}                # currency -> (Cap, mandatory)
        self.require_direct = None    # Source
        self.require_encryption = None
        self.real_call = None
        self.alternate_never = None
        self.approval = None          # (Source, separate)
        self.windows = []             # (Source, Window)

    def apply(self, rule):
        then, source = rule.then, rule.source
        for key in then.get('never', ()):
            self.never.setdefault(key, source)
        if then.get('require_direct'):
            self.require_direct = self.require_direct or source
        if then.get('require_encryption'):
            self.require_encryption = self.require_encryption or source
        if then.get('place_a_real_call'):
            self.real_call = self.real_call or source
        if then.get('alternate_number') == 'never':
            self.alternate_never = self.alternate_never or source
        if 'cap_cost' in then:
            micros, currency = cap_micros(then['cap_cost'])
            current = self.caps.get(currency)
            if current is None or micros < current[0].micros:
                self.caps[currency] = (Cap(micros, currency, source), rule.mandatory)
            elif micros == current[0].micros and rule.mandatory and not current[1]:
                self.caps[currency] = (current[0], True)
        if 'hold_for_approval' in then:
            separate = bool((then['hold_for_approval'] or {}).get('separate_approver', False))
            if self.approval is None:
                self.approval = (source, separate)
            elif separate and not self.approval[1]:
                self.approval = (self.approval[0], True)
        if 'hold_until' in then:
            self.windows.append((source, Window.read(then['hold_until'])))


def decide(compiled, facts, accounts):
    """The decision for one fax. ``compiled`` maps scope names to compiled revisions (see ``applicable_scopes``);
    ``accounts`` are the fax's configuration's accounts in configured order (``model.Account``)."""
    scopes = applicable_scopes(compiled, facts)
    organization = compiled.get(ORGANIZATION)
    ctx = Context(facts, organization.definitions if organization is not None else EMPTY_DEFINITIONS)
    by_key = {account.key: account for account in accounts}
    trace = []
    limits = _Limits()

    # Limits: all of them, in every scope.
    for _, scope in scopes:
        for rule in scope.limits:
            step = rule.evaluate(ctx)
            trace.append(step)
            if step is rule.matched:
                limits.apply(rule)

    # Routing: the first match within each scope, then precedence between scopes.
    matches, positions = {}, {}
    for name, scope in scopes:
        found = None
        for rule in scope.routes:
            if found is not None:
                trace.append(rule.not_reached)
                continue
            step = rule.evaluate(ctx)
            if step is rule.matched:
                found = rule
                positions[scope.scope] = len(trace)
            trace.append(step)
        matches[scope.scope] = found
    automatic_keys = [account.key for account in accounts if account.automatic and account.sends and account.enabled]
    preferred = facts.preferred_route
    preferred_step = None
    if preferred:
        if preferred in limits.never:
            preferred_step = Step('preferred', 'not_applied', rule_id=preferred, note='excluded')
        elif preferred not in automatic_keys:
            preferred_step = Step('preferred', 'not_applied', rule_id=preferred, note='not_listed')
        else:
            preferred_step = Step('preferred', 'matched', rule_id=preferred)
    organization_match = matches.get(ORGANIZATION)
    if organization_match is not None and organization_match.mandatory:
        chosen = organization_match
    else:
        chosen = matches.get(model.WORKFLOW) or matches.get(model.MAILBOX)
        if chosen is None and preferred_step is not None and preferred_step.result == 'matched':
            chosen = 'preferred'
        chosen = chosen or organization_match
    for kind in (model.WORKFLOW, model.MAILBOX, ORGANIZATION):
        rule = matches.get(kind)
        if rule is not None and rule is not chosen:
            trace[positions[kind]] = rule.step('not_applied', 'mandatory' if chosen is organization_match
                                               else 'overridden')
    if preferred_step is not None:
        if chosen != 'preferred' and preferred_step.result == 'matched':
            preferred_step = Step('preferred', 'not_applied', rule_id=preferred, note='overridden')
        trace.append(preferred_step)

    # The chosen rule's accounts and mode.
    reason = None
    settings = {}
    if chosen is None or chosen == 'preferred':
        route = AUTOMATIC if chosen is None else Source('preferred', rule_id=preferred)
        mode, keys = 'automatic', automatic_keys
    else:
        route, settings = chosen.source, chosen.then
        if 'use' in settings:
            mode, keys = 'one', [settings['use']]
        elif 'try_in_order' in settings:
            mode, keys = 'ordered', list(settings['try_in_order'])
        elif 'cheapest_reliable' in settings:
            mode, keys = 'cheapest', list(settings['cheapest_reliable'])
        elif 'site_accounts' in settings:
            mode = 'ordered' if settings.get('mode') == 'ordered' else 'cheapest'
            site = ctx.site if settings['site_accounts'] == 'sender' else ctx.definitions.site(settings['site_accounts'])
            keys = _site_accounts(site, accounts) if site is not None else []
            if site is None:
                reason = 'no_site'
        else:
            mode, keys = 'automatic', automatic_keys

    # Settings: a limit's "never dial the alternate" wins over the route's setting.
    alternate, alternate_source = settings.get('alternate_number'), (route if 'alternate_number' in settings else None)
    if limits.alternate_never is not None:
        alternate, alternate_source = 'never', limits.alternate_never
    dial = facts.alternate if facts.alternate is not None and alternate != 'never' else None
    quoted = 'alternate' if dial is not None else 'original'
    page_layout = settings.get('page_layout')
    layout_source = route if page_layout is not None else None

    # Narrow the chosen accounts by availability and every limit.
    excluded, order = [], []
    for key, source in limits.never.items():
        excluded.append(Excluded(key, 'never', source))
    for key in keys:
        account = by_key.get(key)
        if key in limits.never:
            continue
        if model.is_relay(key):
            # A partner relay (direct/relay.py): its local call is an ordinary call at the partner, and the
            # partner sees the document, so a relay never meets "direct only" or "encrypted only".
            if model.RELAY in limits.never:
                excluded.append(Excluded(key, 'never', limits.never[model.RELAY]))
            elif limits.require_direct is not None:
                excluded.append(Excluded(key, 'direct_required', limits.require_direct))
            elif limits.require_encryption is not None:
                excluded.append(Excluded(key, 'not_encrypted', limits.require_encryption))
            elif key not in order:
                order.append(key)
            continue
        if model.is_digital(key):
            # A Direct message or a FHIR document (digital/): no call, encrypted on the way, but not a verified Faxbot
            # partner, so it never meets "direct only". Which recipients have one is known only at dispatch.
            if model.DIGITAL in limits.never:
                excluded.append(Excluded(key, 'never', limits.never[model.DIGITAL]))
            elif limits.require_direct is not None:
                excluded.append(Excluded(key, 'direct_required', limits.require_direct))
            elif key not in order:
                order.append(key)
            continue
        if account is None:
            excluded.append(Excluded(key, 'unknown_account', route))
        elif not account.sends:
            excluded.append(Excluded(key, 'not_sending', route))
        elif not account.enabled:
            excluded.append(Excluded(key, 'turned_off', route))
        elif limits.require_direct is not None:
            excluded.append(Excluded(key, 'direct_required', limits.require_direct))
        elif limits.require_encryption is not None and not (account.sslfax and facts.sslfax_seen):
            excluded.append(Excluded(key, 'not_encrypted', limits.require_encryption))
        elif key not in order:
            order.append(key)
    capped = []
    for key in order:
        if model.is_digital(key):
            # Priced at dispatch, where the recipient's addresses are known; a cap is checked there (routing/plan.py).
            capped.append(key)
            continue
        quote = _quote(facts, key, quoted)
        for cap, mandatory in limits.caps.values():
            if quote is None or quote.micros is None or quote.currency != cap.currency:
                excluded.append(Excluded(key, 'unknown_cost', cap.source, soft=not mandatory))
                break
            if quote.micros > cap.micros:
                excluded.append(Excluded(key, 'over_cap', cap.source, soft=not mandatory))
                break
        else:
            capped.append(key)
    cap_removed_all = bool(order) and not capped
    order = capped
    if alternate == 'only' and facts.alternate is None and order:
        order = []
        reason = reason or 'needs_alternate'
    local = facts.own_number and not facts.by_call and limits.real_call is None and LOCAL not in limits.never
    direct = DIRECT not in limits.never
    partner_ready = direct and facts.partner
    sslfax = limits.require_encryption is not None and limits.require_direct is None and bool(order)

    if limits.require_direct is not None:
        usable = local or partner_ready
        reason = reason or (None if usable else 'needs_partner')
    elif limits.require_encryption is not None:
        usable = local or partner_ready or bool(order)
        reason = reason or (None if usable else 'needs_encryption')
    else:
        usable = local or partner_ready or bool(order)
        if not usable and reason is None:
            reason = 'no_account_under_cap' if cap_removed_all else 'no_allowed_account'
    if usable and reason in ('needs_alternate', 'no_site'):
        reason = None

    holds = []
    if limits.approval is not None:
        holds.append(Hold('approval', limits.approval[0], separate_approver=limits.approval[1]))
    for source, window in limits.windows:
        opens = release_at(facts.accepted_at, ctx.zone_name(window.zone), window)
        if opens is not None:
            holds.append(Hold('window', source, release_at=opens))
    holds.sort(key=lambda hold: (hold.kind != 'approval', hold.release_at or ''))

    envelope = Envelope(
        mode=mode, accounts=tuple(order), local=local, direct=direct,
        require_direct=limits.require_direct is not None, require_encryption=limits.require_encryption is not None,
        sslfax=sslfax, caps=tuple(cap for cap, _ in sorted(limits.caps.values(), key=lambda item: item[0].currency)),
        holds=tuple(holds), when_busy=settings.get('when_busy', 'wait'),
        preferred=preferred if mode == 'automatic' and preferred in order else None,
        alternate=alternate, dial=dial, page_layout=page_layout,
        strict_fallback=route.kind == 'rule' and mode != 'automatic')
    outcome = 'blocked' if reason is not None else ('held' if holds else 'route')
    return Decision(
        outcome=outcome, envelope=envelope, route=route, facts_digest=facts.digest(),
        revisions=tuple(scope.ref for _, scope in scopes), reason=reason,
        site=ctx.site.key if ctx.site is not None else None, workflow=ctx.workflow,
        alternate_source=alternate_source, layout_source=layout_source, excluded=tuple(excluded),
        trace=tuple(trace))

