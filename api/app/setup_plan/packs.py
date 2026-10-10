"""Compile what Faxbot knows into suggested packs, the missing list and each mailbox's effective choices.

Pure: no database, no network, no clock. ``compile_plan(facts, context,
price=...)`` takes the facts ``facts.gather`` read and what the administrator
said about the organization, and returns the plan as plain data.

Each pack holds items, and every item has its sources and one explanation.
An item is one of four kinds:

- ``rule``: a sending rule added to a scope's rules. Applying publishes it
  through the rules' own draft, check and publish.
- ``setting``: a configuration change, applied through the configuration's
  own validation and activation.
- ``step``: something only you can do on another page, usually because the
  other side must agree first (a partner enrolling, a relay or send-once
  offer, a recipient's approval of their toll-free number). Applying never
  does it; the item names the page.
- ``in_effect``: Faxbot already does this; nothing to apply.

Jurisdiction comes only from what the administrator stated, per mailbox,
with the organization's statement inherited by mailboxes that state none.
Nothing is presumed from the installation country, and a rule from one
country's law is offered only for the mailboxes stated to work there.
"""
import hashlib
import json

from ..routing.destinations import country_name
from .savings import monthly_saving


FORMAT = 1
APPLIES = ('rule', 'setting')
PACKS = (
    ('cost', 'Costs', 'Rules and settings that send your faxes the cheaper way, from what your own faxes cost '
                      'over the last 30 days.'),
    ('partners', 'Partners', 'Recipients who run Faxbot, and partners who offered to carry or file your faxes. '
                             'Each needs the other side to agree, so you take these steps on the Partners page.'),
    ('receiving', 'Receiving', 'Where received faxes go, senders you keep marking as junk, and the number '
                               'replies reach.'),
    ('reliability', 'Reliability', 'Numbers where your first route keeps failing, and the hours recipients are '
                                   'usually busy.'),
    ('compliance', 'Compliance basics', 'Suggestions only, from rules Faxbot knows for the countries you stated. '
                                        'They are not legal advice and do not certify anything.'),
)
SHIPPED_HEADER = 'Faxbot'
HEADER_RULE = '47 CFR 68.318(d)'
HEADER_SOURCE = {'name': 'US fax rules, 47 CFR 68.318(d)',
                 'detail': 'Each page of a fax sent in the United States shows the date and time, the business '
                           'sending it and its fax number.'}
COUNTRY_RULES_REVIEWED = ('US',)
# Console pages, as the console's navigation names them.
RULES_PAGE, PARTNERS_PAGE, RECIPIENTS_PAGE = 'providers/rules', 'recipients/partners', 'recipients/list'
NUMBERS_PAGE, BLOCKED_PAGE, IDENTITY_PAGE = 'numbers/list', 'numbers/blocked', 'numbers/identity'
IN_USE_PAGE, STORAGE_PAGE = 'providers/sending', 'system/storage'


def _plural(count, one, many=None):
    return f"{count} {one if count == 1 else (many or one + 's')}"


def rule_id(rule):
    """A stable rule ID from what the rule matches and does, so the same suggestion keeps the same ID."""
    text = json.dumps({'when': rule.get('when') or {}, 'then': rule.get('then') or {}}, sort_keys=True,
                      separators=(',', ':'))
    return 'setup-' + hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]


def _same_rule(document, section, rule):
    for found in document.get(section) or ():
        if isinstance(found, dict) and (found.get('when') or {}) == (rule.get('when') or {}) \
                and (found.get('then') or {}) == (rule.get('then') or {}):
            return found
    return None


class _Plan:
    """Collects items and missing entries while the packs are compiled."""

    def __init__(self, facts, context):
        self.facts = facts
        self.context = context
        self.items = []
        self.missing = []
        self.scope_names = {'organization': 'the organization'}
        self.scope_names.update({f"mailbox:{box['id']}": box['name'] for box in facts.mailboxes})

    def item(self, pack, key, kind, title, sentence, sources, **extra):
        item = {'key': key, 'pack': pack, 'kind': kind, 'title': title, 'sentence': sentence,
                'sources': list(sources), 'saving': None, 'scope': 'organization', 'applies_to': None,
                'blocked': None, 'link': None, 'cli': None}
        item.update(extra)
        item.setdefault('selected', kind in APPLIES)
        if item['blocked']:
            item['selected'] = False
        self.items.append(item)
        return item

    def rule(self, pack, key, title, sentence, sources, rule, *, section='routes', scope='organization', **extra):
        """A rule item, or an in-effect item when the scope's active rules already hold the same rule."""
        state = self.facts.rules.get(scope) or {}
        document = state.get('document') or {}
        existing = _same_rule(document, section, rule)
        if existing is not None:
            name = existing.get('name') or title
            return self.item(pack, key, 'in_effect', title, f'Your rules already do this: “{name}”.', sources,
                             scope=scope, link=RULES_PAGE)
        full = {'id': rule_id(rule), 'name': title[:200], 'on': True, 'when': rule.get('when') or {},
                'then': rule['then']}
        blocked = None
        if state.get('draft'):
            blocked = (f'You have unpublished changes to the rules of {self.scope_names.get(scope, scope)}. Publish '
                       'or discard them on Delivery setup → Routing rules, then preview again.')
        return self.item(pack, key, 'rule', title, sentence, sources, rule=full, section=section, scope=scope,
                         blocked=blocked, link=RULES_PAGE, **extra)

    def setting(self, pack, key, title, sentence, sources, changes, **extra):
        extra.setdefault('link', IN_USE_PAGE)
        return self.item(pack, key, 'setting', title, sentence, sources, changes=dict(changes), **extra)

    def lack(self, key, sentence, *, owner, operation, blocking=False, scope='organization', link=None):
        self.missing.append({'key': key, 'sentence': sentence, 'owner': owner, 'operation': operation,
                             'status': 'blocking' if blocking else 'warning', 'scope': scope, 'link': link})


# Jurisdiction -------------------------------------------------------------------------------------------------

def mailbox_countries(facts, context):
    """{mailbox ID: (country or '', 'stated' | 'organization' | 'not_stated')}."""
    organization = (context.get('country') or '').upper()
    stated = context.get('mailboxes') or {}
    found = {}
    for box in facts.mailboxes:
        own = ((stated.get(box['id']) or {}).get('country') or '').upper()
        if own:
            found[box['id']] = (own, 'stated')
        elif organization:
            found[box['id']] = (organization, 'organization')
        else:
            found[box['id']] = ('', 'not_stated')
    return found


def _stated_countries(facts, context, countries):
    found = {country for country, _ in countries.values() if country}
    if not facts.mailboxes and context.get('country'):
        found.add(context['country'].upper())
    return found


# Costs ------------------------------------------------------------------------------------------------------

def _saving_sentence(saving):
    if saving.known and saving.micros > 0:
        return (f' Faxbot estimates it would have saved {saving.text()} on your last 30 days of faxes '
                f'({_plural(saving.faxes, "fax", "faxes")}).')
    if saving.known:
        return ' Faxbot’s estimate for your last 30 days of faxes shows no saving, so it is not chosen for you.'
    return f' Faxbot cannot estimate the saving: {saving.unknown}.'


def _estimate_source(saving):
    if not saving.faxes:
        return []
    return [{'name': 'Faxbot’s cost estimate',
             'detail': f'{_plural(saving.faxes, "fax", "faxes")} you sent in the last 30 days, priced on both routes '
                       'by the same estimate Send a fax shows.'}]


def _with_saving(saving):
    return {'saving': saving.view(), 'selected': saving.known and saving.micros > 0}


def cost_pack(plan, price):
    facts = plan.facts
    from ..routing.destinations import classify
    covered = set()
    for found in facts.country_rules:
        rule = found['rule_suggestion']
        country, route = found['country'], found['route']
        covered.add((country, route))
        faxes = [row for row in facts.history if classify(row[0], 'ZZ').region == country]
        saving = monthly_saving(faxes, route, price=price, now=facts.now)
        sentence = found['sentence'].removesuffix(' Add as a rule?') + _saving_sentence(saving)
        plan.rule('cost', f'cost.country.{country}.{route}', rule['name'], sentence,
                  [{'name': 'Savings & optimization → Opportunities',
                    'detail': f"{found['delivered']} delivered faxes to {found['numbers']} numbers in "
                              f"{country_name(country)} over the last 30 days"}, *_estimate_source(saving)],
                  rule, **_with_saving(saving))
    for found in facts.sending:
        rule = found['rule_suggestion']
        route = rule['then']['use']
        if (classify(found['number'], 'ZZ').region, route) in covered:
            continue
        faxes = [row for row in facts.history if row[0] == found['number']]
        saving = monthly_saving(faxes, route, price=price, now=facts.now)
        plan.rule('cost', f"cost.number.{found['number']}", rule['name'],
                  found['sentence'] + _saving_sentence(saving),
                  [{'name': 'Savings & optimization → Opportunities',
                    'detail': 'Cost per delivered fax to this number on each route over the last 30 days'},
                   *_estimate_source(saving)], rule, **_with_saving(saving))
    _toll_free_class(plan, price)
    for found in facts.toll_free:
        name = found.get('display_name') or found['number']
        if found.get('approved'):
            plan.item('cost', f"cost.toll-free.{found['number']}", 'in_effect',
                      f'Faxes to {name} dial their toll-free number', found['sentence'],
                      [{'name': 'Recipients → Details', 'detail': 'The approval you recorded'}],
                      link=RECIPIENTS_PAGE)
        else:
            plan.item('cost', f"cost.toll-free.{found['number']}", 'step',
                      f'Record whether {name} approves faxes to their toll-free number', found['sentence'],
                      [{'name': 'Recipients → Details', 'detail': 'The toll-free number on file'}],
                      link=RECIPIENTS_PAGE, cli='faxbot recipients toll-free')
    _shading(plan)
    for found in facts.long_pages:
        if found['on']:
            plan.item('cost', f"cost.long-pages.{found['route']}", 'in_effect',
                      f"Long pages on {found['label']}",
                      f"Faxbot already packs several pages onto one long page on {found['label']} where the "
                      'receiving machine accepts long pages and it saves time.',
                      [{'name': 'Delivery setup → Providers & accounts → Delivery routes', 'detail': f"Long pages on {found['label']}"}])
        elif not found['set']:
            plan.item('cost', f"cost.long-pages.{found['route']}", 'step', f"Check long pages on {found['label']}",
                      f"Long pages save call time, but they are off for {found['label']} until you check that it "
                      'sends them unchanged. Send yourself a test fax with a long page, then turn them on.',
                      [{'name': 'Delivery setup → Providers & accounts → Delivery routes', 'detail': f"Off until checked for {found['label']}"}],
                      link=IN_USE_PAGE, cli='faxbot delivery providers long-pages')
    for found in facts.plan_budgets:
        plan.item('cost', f"cost.plan-budget.{found['route']}", 'in_effect', f"Plan budget for {found['label']}",
                  found['sentence'] + ' Faxbot uses your plan first and moves faxes to metered routes past it.',
                  [{'name': 'Savings & optimization → Prices & plans', 'detail': {'set': 'Your budget', 'published': 'The published plan',
                                                        'default': 'Faxbot’s cautious start'}.get(found['source'],
                                                                                                  'Plan budget')}])


def _shading(plan):
    """The shading setting, in the words of the setting itself (``pages.friendly.describe_setting``)."""
    from ..pages.friendly import describe_setting, documents_choice
    setting = describe_setting()
    current = documents_choice(plan.facts.values)
    label, sentence = setting['choices'][current]
    source = [{'name': 'Delivery setup → Providers & accounts → Delivery routes', 'detail': f"{setting['label']}: {label}"}]
    if current == setting['off'] and setting['default'] != current:
        chosen, does = setting['choices'][setting['default']]
        plan.setting('cost', 'cost.fax-friendly', f"{setting['label']}: {chosen}", f'It is set to {label} now. {does}',
                     source, {setting['setting']: setting['default']})
    else:
        plan.item('cost', 'cost.fax-friendly', 'in_effect', f"{setting['label']}: {label}", sentence, source)


def _toll_free_class(plan, price):
    """One rule for the toll-free numbers you fax, when another of your routes would have cost less for them."""
    facts = plan.facts
    from ..routing.destinations import TOLL_FREE, classify
    faxes = [row for row in facts.history if classify(row[0], facts.home).kind == TOLL_FREE]
    if not faxes:
        return
    best = None
    for account in facts.accounts:
        if not (account.sends and account.enabled and account.automatic):
            continue
        saving = monthly_saving(faxes, account.key, price=price, now=facts.now)
        if saving.known and saving.micros > 0 and (best is None or saving.micros > best[1].micros):
            best = (account, saving)
    if best is None:
        return
    account, saving = best
    prefixes = sorted({row[0][:5] if row[0].startswith('+1') else row[0] for row in faxes})
    # The route these faxes mostly took stays as the next one to try, so a failed call can still move on.
    keys = {item.key for item in facts.accounts if item.sends and item.enabled}
    used = {}
    for _, route, _ in faxes:
        if route != account.key and route in keys:
            used[route] = used.get(route, 0) + 1
    then = ({'try_in_order': [account.key, max(sorted(used), key=used.get)]} if used else {'use': account.key})
    rule = {'when': {'destination': {'prefixes': prefixes}}, 'then': then}
    label = account.label or account.key
    plan.rule('cost', f'cost.toll-free-class.{account.key}', f'Toll-free numbers go by {label}',
              f'Calls to the toll-free numbers you fax cost less through {label}.' + _saving_sentence(saving),
              [{'name': 'Sent faxes', 'detail': f'{_plural(len(faxes), "fax", "faxes")} to toll-free numbers in the '
                                                'last 30 days'}, *_estimate_source(saving)],
              rule, **_with_saving(saving))


# Partners ---------------------------------------------------------------------------------------------------

def partners_pack(plan):
    facts = plan.facts
    for found in facts.partners:
        name = found.get('display_name') or found['number']
        plan.item('partners', f"partners.invite.{found['number']}", 'step', f'Invite {name} to be a direct partner',
                  found['sentence'],
                  [{'name': 'Savings & optimization → Opportunities → Partner candidates',
                    'detail': f"{_plural(found['faxes'], 'fax', 'faxes')} in the last 30 days"}],
                  saving=({**found['monthly_cost'], 'faxes': found['faxes'], 'estimate': True}
                          if found.get('monthly_cost') else None),
                  link=PARTNERS_PAGE, cli='faxbot recipients partners add')
    for found in facts.discovered:
        name = found.get('organization') or found['number']
        plan.item('partners', f"partners.discovered.{found['id']}", 'step', f'Enroll {name} as a direct partner',
                  f"{name} runs Faxbot at {found['number']}. Enrolling sends them a challenge fax; once they "
                  'answer it, your faxes go straight to them with no call.',
                  [{'name': 'Recipients → Partners → Find partners', 'detail': 'Found from calls, an introduction or a directory'}],
                  link=PARTNERS_PAGE, cli='faxbot recipients partners discover')
    for found in facts.relay_offers:
        saving = found.get('saving') or {}
        view = None
        if saving.get('amount_micros') is not None:
            from ..routing.costs import format_amount
            view = {'amount': format_amount(saving['amount_micros']), 'currency': saving['currency'],
                    'faxes': found.get('faxes'), 'estimate': True}
        plan.item('partners', f"partners.relay.{found['peer_id']}.{found['country']}", 'step',
                  f"Relay faxes to {country_name(found['country'])} through {found['partner']}",
                  f"{found['sentence']} {found['action']}",
                  [{'name': 'Recipients → Partners', 'detail': f"{found['partner']}’s signed price"}],
                  saving=view, link=PARTNERS_PAGE, cli='faxbot recipients partners relay')
    for found in facts.send_once_offers:
        numbers = _plural(len(found['numbers']), 'number')
        intake = f" ({found['intake']})" if found.get('intake') else ''
        plan.item('partners', f"partners.send-once.{found['agreement_id']}", 'step',
                  f"Accept {found['partner']}’s offer to file your faxes once",
                  f"{found['partner']}’s intake{intake} offered to file what you send to {numbers} of theirs: one copy "
                  'crosses once and their intake files it for each number. Faxes to those numbers stop going by '
                  'telephone once you accept.',
                  [{'name': 'Recipients → Partners', 'detail': f"{found['partner']}’s signed offer"}],
                  link=PARTNERS_PAGE, cli='faxbot recipients partners send-once')


# Receiving --------------------------------------------------------------------------------------------------

def receiving_pack(plan):
    facts = plan.facts
    for found in facts.unplaced:
        plan.item('receiving', f"receiving.mailbox.{found['number']}", 'step', f"Give {found['number']} a mailbox",
                  f"{_plural(found['faxes'], 'fax', 'faxes')} to {found['number']} arrived in the last 30 days with "
                  'no mailbox, because no number rule places them. Add a number rule that sends them to the right '
                  'mailbox.',
                  [{'name': 'Received faxes', 'detail': 'Faxes with no mailbox in the last 30 days'}],
                  link=NUMBERS_PAGE, cli='faxbot delivery numbers add')
    for found in facts.junk:
        if found['active']:
            plan.item('receiving', f"receiving.junk.{found['number']}", 'step', f"Keep blocking {found['number']}",
                      f"{found['number']} called {_plural(found['rejected'], 'time')} while blocked, and its block "
                      'ends within two weeks. Block it again for longer if you still want its calls turned away.',
                      [{'name': 'Delivery setup → Blocked senders', 'detail': 'Calls turned away before answering'}],
                      link=BLOCKED_PAGE, cli='faxbot delivery blocked add')
        else:
            plan.item('receiving', f"receiving.junk.{found['number']}", 'step', f"Block {found['number']} again",
                      f"You marked {found['number']} as junk {_plural(found['marks'], 'time')}, and its last block "
                      'has ended. Block it again so its calls are turned away before answering, which costs nothing.',
                      [{'name': 'Delivery setup → Blocked senders', 'detail': 'Your earlier junk marks'}],
                      link=BLOCKED_PAGE, cli='faxbot delivery blocked add')
    reply = facts.reply or {}
    suggestion = reply.get('suggestion')
    if reply.get('number'):
        plan.item('receiving', 'receiving.reply-number', 'in_effect', f"Replies reach {reply['number']}",
                  reply.get('sentence') or f"Faxbot prints {reply['number']} on each page for replies.",
                  [{'name': 'Delivery setup → Sending identity', 'detail': 'The reply number you saved'}])
    elif suggestion:
        plan.setting('receiving', 'receiving.reply-number', f"Use {suggestion['number']} as your reply number",
                     f"{suggestion['sentence']} Faxbot prints the reply number on each page, so replies reach a "
                     'mailbox. Saving it keeps it the same even when your numbers change.',
                     [{'name': 'Delivery setup → Sending identity', 'detail': 'Your numbers that receive into a mailbox'}],
                     {'fax_reply_number': suggestion['number']}, link=IDENTITY_PAGE)


# Reliability ------------------------------------------------------------------------------------------------

def reliability_pack(plan):
    facts = plan.facts
    for found in facts.fallbacks:
        name = found.get('name') or found['number']
        why = ' because you chose it' if found.get('chosen_by_you') else ''
        rule = {'when': {'destination': {'numbers': [found['number']]}},
                'then': {'try_in_order': [found['better'], found['current']]}}
        plan.rule('reliability', f"reliability.fallback.{found['number']}",
                  f"Faxes to {name} try {found['better_label']} first",
                  f"{found['current_label']} failed {found['failed']} of its {found['attempts']} faxes to this number "
                  f"in the last 30 days, and {found['better_label']} delivered {found['delivered']} of its "
                  f"{found['better_attempts']}. Faxbot still tries {found['current_label']} first{why}. With this "
                  f"rule it tries {found['better_label']} first and {found['current_label']} next.",
                  [{'name': 'Sent faxes', 'detail': 'Delivered and failed faxes to this number on each route over '
                                                    'the last 30 days'}], rule)
    for found in facts.busy:
        hours = '; '.join(f"{slot['label']} ({slot['summary']})" for slot in found['slots'])
        if found['learn']:
            plan.item('reliability', f"reliability.busy.{found['number']}", 'in_effect',
                      f"Busy hours of {found['number']}",
                      f"Faxbot already waits these hours out on its own when a failed call would cost money: {hours}",
                      [{'name': 'Recipients → Details', 'detail': 'Calls in the last 30 days'}], link=RECIPIENTS_PAGE)
        else:
            plan.item('reliability', f"reliability.busy.{found['number']}", 'step',
                      f"Let Faxbot learn the busy hours of {found['number']}",
                      f'This number is often busy at the same hours, but learning is turned off for it: {hours} '
                      'Turn learning back on so Faxbot waits those hours out.',
                      [{'name': 'Recipients → Details', 'detail': 'Calls in the last 30 days'}], link=RECIPIENTS_PAGE,
                      cli='faxbot recipients schedule')


# Compliance -------------------------------------------------------------------------------------------------

def compliance_pack(plan, countries):
    facts, context = plan.facts, plan.context
    values = facts.values
    us = sorted(key for key, (country, _) in countries.items() if country == 'US')
    stated = _stated_countries(facts, context, countries)
    if 'US' in stated:
        applies = us or None
        header = (getattr(values, 'fax_header', '') or '').strip()
        name = (context.get('organization_name') or '').strip()
        if header and header != SHIPPED_HEADER:
            plan.item('compliance', 'compliance.header', 'in_effect', 'Your business name is on each page',
                      f'Each page you send shows “{header}”, the date and time, and your reply number.',
                      [HEADER_SOURCE], applies_to=applies)
        elif name:
            now_text = f'shows “{header}”' if header else 'is empty'
            others = [box['name'] for box in facts.mailboxes if countries.get(box['id'], ('',))[0] != 'US']
            shared = (f" Faxbot has one header line for every fax, so faxes from {', '.join(others)} will show it too."
                      if others else '')
            plan.setting('compliance', 'compliance.header', 'Print your business name at the top of each page',
                         f'US fax rules ({HEADER_RULE}) ask that each page shows the date and time, the business '
                         f'sending it and its fax number. Your header line {now_text}; this puts “{name}” there. '
                         f'A suggestion, not legal advice.{shared}',
                         [HEADER_SOURCE, {'name': 'Describe your organization', 'detail': 'The name you typed'}],
                         {'fax_header': name[:80]}, applies_to=applies, link=IDENTITY_PAGE)
        else:
            plan.lack('header-name', 'Type your business name under Describe your organization: US fax rules ask '
                                     'for it at the top of each page, and Faxbot never guesses it.',
                      owner='you', operation='Compliance basics: the header line')
        if not (facts.reply or {}).get('shows'):
            plan.lack('header-number', 'Faxbot has no number to print at the top of each page for replies. Give one '
                                       'of your numbers a mailbox, or set a reply number under Delivery setup → '
                                       'Sending identity.', owner='you', operation='Compliance basics: the header line',
                      link=IDENTITY_PAGE)
    for country in sorted(stated - set(COUNTRY_RULES_REVIEWED)):
        where = [box['name'] for box in facts.mailboxes if countries.get(box['id'], ('',))[0] == country]
        scope = next((box['id'] for box in facts.mailboxes if countries.get(box['id'], ('',))[0] == country),
                     'organization')
        plan.lack(f'reviewed-rules.{country}',
                  f'Faxbot has no reviewed fax rules for {country_name(country)} yet, so it suggests nothing '
                  f"country-specific{' for ' + ', '.join(where) if where else ''}. Check what applies there yourself.",
                  owner='faxbot', operation='Compliance basics', scope=scope)
    if not int(getattr(values, 'artifact_ttl_days', 0) or 0):
        plan.lack('retention', 'Choose how long Faxbot keeps the documents you send. It keeps them until you choose; '
                               'Faxbot never picks a period for you, because how long to keep records is your '
                               'decision.', owner='you', operation='Compliance basics: keeping sent documents',
                  link=STORAGE_PAGE)


# Missing ----------------------------------------------------------------------------------------------------

def _general_missing(plan, countries):
    facts, context = plan.facts, plan.context
    for box in facts.mailboxes:
        if countries[box['id']][1] == 'not_stated':
            plan.lack(f"country.{box['id']}", f"Say which country {box['name']} works in. Until you do, Faxbot "
                                              'suggests no country-specific settings for it.',
                      owner='you', operation='Compliance basics', scope=box['id'])
    if not facts.mailboxes and not context.get('country'):
        plan.lack('country', 'Say which country your organization works in. Until you do, Faxbot suggests no '
                             'country-specific settings.', owner='you', operation='Compliance basics')
    if not any(account.sends and account.enabled for account in facts.accounts):
        plan.lack('sending', 'Choose a provider for sending on the first step. Cost and reliability rules need one.',
                  owner='you', operation='Costs and reliability', blocking=True, link='system/setup')
    for scope, state in sorted(facts.rules.items()):
        if state.get('draft') and any(item['kind'] == 'rule' and item['scope'] == scope for item in plan.items):
            plan.lack(f'draft.{scope}', f"Your rules for {plan.scope_names.get(scope, scope)} have unpublished "
                                        'changes. Publish or discard them on Delivery setup → Routing rules, then preview again.',
                      owner='you', operation='Rules in this plan', blocking=True,
                      scope=state['scope_id'] or 'organization', link=RULES_PAGE)
    if facts.pending_restart:
        plan.lack('restart', 'Some saved settings wait for Faxbot to restart. Restart it so the plan is built on the '
                             'settings in use.', owner='you', operation='Settings in this plan')
    plan.lack('templates', 'Industry templates, such as reviewed healthcare correspondence settings, are not in '
                           'Faxbot yet. Every mailbox uses Faxbot’s general settings.',
              owner='faxbot', operation='Templates for regulated work')


# Per mailbox ------------------------------------------------------------------------------------------------

def _mailbox_views(plan, countries):
    facts = plan.facts
    values = facts.values
    from ..routing.reply_number import mailbox_numbers
    own_reply = mailbox_numbers(values)
    reply = facts.reply or {}
    header = (getattr(values, 'fax_header', '') or '').strip()
    kept_sent = int(getattr(values, 'artifact_ttl_days', 0) or 0)
    kept_received = int(getattr(values, 'inbound_retention_days', 0) or 0)
    organization = facts.rules.get('organization') or {}
    views = []
    for box in facts.mailboxes:
        country, source = countries[box['id']]
        scope = f"mailbox:{box['id']}"
        state = facts.rules.get(scope) or {}
        own_rules = len((state.get('document') or {}).get('routes') or ()) + \
            len((state.get('document') or {}).get('limits') or ())
        org_rules = len((organization.get('document') or {}).get('routes') or ()) + \
            len((organization.get('document') or {}).get('limits') or ())
        items = [item['key'] for item in plan.items
                 if item['scope'] == scope or (item['applies_to'] is not None and box['id'] in item['applies_to'])]
        number = own_reply.get(box['id'])
        choices = [
            {'label': 'Country', 'value': country_name(country) if country else 'Not stated',
             'source': {'stated': 'You stated it', 'organization': 'From the organization',
                        'not_stated': 'Say which country it works in'}[source]},
            {'label': 'Numbers', 'value': ', '.join(box['numbers']) or 'None yet', 'source': 'Numbers'},
            {'label': 'Sending rules',
             'value': (f"{_plural(own_rules, 'rule')} of its own, then the organization’s "
                       f"{_plural(org_rules, 'rule')}"),
             'source': 'Delivery setup → Routing rules'},
            {'label': 'Reply number', 'value': number or reply.get('shows') or 'None',
             'source': 'Its own' if number else 'The organization’s'},
            {'label': 'Header line', 'value': header or 'Empty', 'source': 'This installation'},
            {'label': 'Received faxes kept', 'value': f"{_plural(kept_received, 'day')}" if kept_received
             else 'Until you remove them', 'source': 'This installation'},
            {'label': 'Sent documents kept', 'value': f"{_plural(kept_sent, 'day')}" if kept_sent
             else 'Until you choose a period', 'source': 'This installation'},
        ]
        views.append({'id': box['id'], 'name': box['name'], 'country': country or None, 'country_source': source,
                      'choices': choices, 'items': items,
                      'missing': [entry['key'] for entry in plan.missing if entry['scope'] == box['id']]})
    workflows = []
    for flow in facts.workflows:
        state = facts.rules.get(f"workflow:{flow['key']}") or {}
        count = len((state.get('document') or {}).get('routes') or ()) + \
            len((state.get('document') or {}).get('limits') or ())
        workflows.append({'key': flow['key'], 'name': flow['name'],
                          'choices': [{'label': 'Sending rules',
                                       'value': (f"{_plural(count, 'rule')} of its own" if state
                                                 else 'The organization’s rules'),
                                       'source': 'Delivery setup → Routing rules'}]})
    return views, workflows


# The plan ---------------------------------------------------------------------------------------------------

def compile_plan(facts, context, *, price=None):
    """The plan as plain data: packs with items, the missing list, mailboxes, workflows and rule targets."""
    from .savings import predicted_cost
    price = price or predicted_cost
    plan = _Plan(facts, context)
    countries = mailbox_countries(facts, context)
    cost_pack(plan, price)
    partners_pack(plan)
    receiving_pack(plan)
    reliability_pack(plan)
    compliance_pack(plan, countries)
    _general_missing(plan, countries)
    mailboxes, workflows = _mailbox_views(plan, countries)
    packs = []
    for key, title, sentence in PACKS:
        items = [item for item in plan.items if item['pack'] == key]
        packs.append({'key': key, 'title': title, 'sentence': sentence, 'items': items,
                      'saving': pack_saving(items)})
    targets = {scope: {'kind': state['kind'], 'scope_id': state['scope_id'], 'base_revision': state['active'],
                       'base': state['document']}
               for scope, state in facts.rules.items()
               if any(item['kind'] == 'rule' and item['scope'] == scope for item in plan.items)}
    return {'format': FORMAT, 'packs': packs, 'missing': plan.missing, 'mailboxes': mailboxes,
            'workflows': workflows, 'targets': targets,
            'basis': {'configuration': facts.config_revision,
                      'rules': {scope: {'active': state['active'], 'draft': state['draft']}
                                for scope, state in sorted(facts.rules.items())}}}


def pack_saving(items):
    """The pack's chosen items' predicted monthly saving, when every one is known and in one currency."""
    from ..routing.costs import format_amount, parse_amount
    total, currency = 0, None
    for item in items:
        saving = item.get('saving')
        if not item.get('selected') or not saving:
            continue
        if currency is not None and saving['currency'] != currency:
            return None
        currency = saving['currency']
        total += parse_amount(saving['amount'])
    return {'amount': format_amount(total), 'currency': currency, 'estimate': True} if currency else None


def target_document(base, items):
    """A scope's rules after applying ``items`` (rule items of that scope): new routes first, new limits last."""
    routes = [item['rule'] for item in items if item['section'] == 'routes']
    limits = [item['rule'] for item in items if item['section'] == 'limits']
    document = json.loads(json.dumps(base))
    document['routes'] = routes + list(document.get('routes') or [])
    document['limits'] = list(document.get('limits') or []) + limits
    return document
