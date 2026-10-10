"""Guided setup's compiler on synthetic installations: fresh, cost-heavy, partner-rich, and two jurisdictions.

The compiler is pure (``setup_plan.packs.compile_plan``): these tests hand it
facts directly. Prices come from a stand-in here, and from the real shared
predictor in ``test_the_real_predictor_prices_a_saving`` (its companion).
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.routing.costs import Money
from app.rules import model
from app.rules.check import CheckContext, check
from app.setup_plan.facts import Facts
from app.setup_plan.packs import compile_plan, target_document
from app.setup_plan.savings import monthly_saving, predicted_cost
from app.setup_plan.context import ContextError, clean_context

import pytest


NOW = datetime(2026, 10, 8, 12, 0)
UK, UK2, US, TOLL_FREE = '+442071234567', '+442079460000', '+12025550123', '+18005550100'
PHAXIO = model.Account('phaxio', 'phaxio', 'Phaxio', default=True, automatic=True)
SINCH = model.Account('sinch', 'sinch', 'Sinch', automatic=True)
SIGNALWIRE = model.Account('signalwire', 'signalwire', 'SignalWire', automatic=True)
EMPTY = {'format': 1, 'limits': [], 'routes': []}


def values(**changes):
    return SimpleNamespace(**{'fax_friendly_documents': 'where_it_saves', 'fax_header': 'Faxbot',
                              'artifact_ttl_days': 0, 'inbound_retention_days': 30, 'fax_reply_numbers': '',
                              'route_min_success_percent': 80, **changes})


def rules(*mailboxes, draft=()):
    found = {'organization': {'kind': 'organization', 'scope_id': '', 'active': None, 'document': EMPTY,
                              'draft': 'organization' in draft}}
    for key in mailboxes:
        found[f'mailbox:{key}'] = {'kind': 'mailbox', 'scope_id': key, 'active': None, 'document': EMPTY,
                                   'draft': f'mailbox:{key}' in draft}
    return found


def facts(**changes):
    boxes = changes.pop('mailboxes', ())
    base = dict(now=NOW, values=values(), config_revision='rev-1', pending_restart=False, home='US',
                accounts=(PHAXIO, SINCH, SIGNALWIRE), mailboxes=boxes, rules=rules(*(box['id'] for box in boxes)),
                reply={'number': None, 'shows': '+13035550100', 'suggestion': None})
    base.update(changes)
    return Facts(**base)


def price(table):
    """A stand-in predictor: {(route, destination): micros per page}; None for anything else."""
    def lookup(route, destination, pages, *, now=None):
        each = table.get((route, destination))
        return None if each is None else Money(each * pages, 'USD')
    return lookup


def items(plan, pack=None, kind=None):
    return [item for found in plan['packs'] if pack in (None, found['key']) for item in found['items']
            if kind in (None, item['kind'])]


def missing(plan):
    return {entry['key']: entry for entry in plan['missing']}


# -- a fresh installation -------------------------------------------------------------------------------------

def test_a_fresh_installation_suggests_only_what_is_known_and_lists_what_is_missing():
    plan = compile_plan(facts(), {}, price=price({}))
    assert [pack['key'] for pack in plan['packs']] == ['cost', 'partners', 'receiving', 'reliability', 'compliance']
    assert items(plan, kind='rule') == [] and items(plan, kind='step') == []
    # Defaults that are already on are said to be on, never proposed again.
    assert [item['key'] for item in items(plan, kind='in_effect')] == ['cost.fax-friendly']
    found = missing(plan)
    assert set(found) == {'country', 'retention', 'templates'}
    assert found['country']['owner'] == 'you' and found['country']['status'] == 'warning'
    assert found['templates']['owner'] == 'faxbot'
    assert found['retention']['owner'] == 'you' and 'never picks a period' in found['retention']['sentence']
    # No country stated: nothing country-specific, and the installation country is never taken as one.
    assert items(plan, 'compliance') == []
    assert plan['targets'] == {} and plan['basis']['configuration'] == 'rev-1'


def test_without_a_sending_provider_the_missing_list_blocks_cost_and_reliability():
    plan = compile_plan(facts(accounts=()), {'country': 'US'}, price=price({}))
    assert missing(plan)['sending']['status'] == 'blocking'


# -- a cost-heavy installation --------------------------------------------------------------------------------

def _cost_heavy(**changes):
    sending = [
        {'number': UK, 'kind': 'cheaper_route', 'sentence': 'Sinch cost $0.10 per delivered fax to this number.',
         'rule_suggestion': {'name': f'Faxes to {UK} go by Sinch', 'when': {'destination': {'numbers': [UK]}},
                             'then': {'use': 'sinch'}}},
        {'number': US, 'kind': 'cheaper_route', 'sentence': 'SignalWire cost $0.01 per delivered fax to this number.',
         'rule_suggestion': {'name': f'Faxes to {US} go by SignalWire', 'when': {'destination': {'numbers': [US]}},
                             'then': {'use': 'signalwire'}}},
    ]
    country = [{'country': 'GB', 'route': 'sinch', 'numbers': 2, 'delivered': 9,
                'sentence': 'Faxes to +44 numbers cost about $0.05 less each through Sinch over the last 30 days '
                            '(9 delivered faxes to 2 numbers). Add as a rule?',
                'rule_suggestion': {'name': 'Numbers in the United Kingdom go by Sinch',
                                    'when': {'destination': {'countries': ['GB']}}, 'then': {'use': 'sinch'}}}]
    history = ((UK, 'phaxio', 2), (UK2, 'phaxio', 1), (US, 'phaxio', 3), (TOLL_FREE, 'phaxio', 2))
    base = dict(sending=tuple(sending), country_rules=tuple(country), history=history,
                values=values(fax_friendly_documents='never'),
                plan_budgets=({'route': 'phaxio', 'label': 'Phaxio', 'source': 'published',
                               'sentence': 'Phaxio’s published plan includes 500 pages a month.'},),
                long_pages=({'route': 'sip', 'label': 'Telnyx', 'on': True, 'set': False},
                            {'route': 'phaxio', 'label': 'Phaxio', 'on': False, 'set': False}))
    base.update(changes)
    return facts(**base)


PRICES = {('phaxio', UK): 100_000, ('sinch', UK): 60_000, ('phaxio', UK2): 100_000, ('sinch', UK2): 60_000,
          ('phaxio', US): 70_000, ('signalwire', US): 10_000, ('sinch', US): 50_000,
          ('phaxio', TOLL_FREE): 70_000, ('sinch', TOLL_FREE): 40_000, ('signalwire', TOLL_FREE): 0}


def test_a_cost_heavy_installation_gets_rules_with_predicted_savings_sources_and_explanations():
    plan = compile_plan(_cost_heavy(), {'country': 'US'}, price=price(PRICES))
    rules_ = {item['key']: item for item in items(plan, 'cost', 'rule')}
    # The country rule covers the UK number, so that number gets no rule of its own.
    assert set(rules_) == {'cost.country.GB.sinch', f'cost.number.{US}', 'cost.toll-free-class.signalwire'}
    country = rules_['cost.country.GB.sinch']
    # Predicted on the window's faxes: 2 pages x $0.04 + 1 page x $0.04 = $0.12.
    assert country['saving'] == {'amount': '0.12', 'currency': 'USD', 'faxes': 2, 'estimate': True}
    assert country['selected'] is True and country['scope'] == 'organization'
    assert country['rule']['when'] == {'destination': {'countries': ['GB']}} and country['rule']['then'] == {'use': 'sinch'}
    assert country['rule']['id'].startswith('setup-') and 'Add as a rule?' not in country['sentence']
    assert 'about $0.12 a month' in country['sentence']
    assert [source['name'] for source in country['sources']] == ['Savings & optimization → Opportunities', 'Faxbot’s cost estimate']
    toll = rules_['cost.toll-free-class.signalwire']
    assert toll['rule']['when'] == {'destination': {'prefixes': ['+1800']}}
    # The route those faxes took stays next, so a failed call can still move on.
    assert toll['rule']['then'] == {'try_in_order': ['signalwire', 'phaxio']}
    assert toll['saving']['amount'] == '0.14'
    # Shading is off: the setting goes back to "where it saves"; long pages are on for one route, a step for another.
    friendly = next(item for item in items(plan, 'cost') if item['key'] == 'cost.fax-friendly')
    assert friendly['kind'] == 'setting' and friendly['changes'] == {'fax_friendly_documents': 'where_it_saves'}
    # Its words are the setting's own (pages.friendly.describe_setting), so they follow the setting when it changes.
    from app.pages.friendly import CHOICES, describe_setting
    setting = describe_setting()
    assert set(setting['choices']) == set(CHOICES) and {setting['default'], setting['off']} <= set(CHOICES)
    label, does = setting['choices'][setting['default']]
    assert friendly['title'] == f"{setting['label']}: {label}"
    assert friendly['sentence'] == f"It is set to {setting['choices']['never'][0]} now. {does}"
    kinds = {item['key']: item['kind'] for item in items(plan, 'cost')}
    assert kinds['cost.long-pages.sip'] == 'in_effect' and kinds['cost.long-pages.phaxio'] == 'step'
    assert kinds['cost.plan-budget.phaxio'] == 'in_effect'
    cost = next(pack for pack in plan['packs'] if pack['key'] == 'cost')
    assert cost['saving'] == {'amount': '0.44', 'currency': 'USD', 'estimate': True}  # 0.12 + 0.18 + 0.14
    assert set(plan['targets']) == {'organization'}


def test_an_unknown_or_negative_saving_is_shown_as_such_and_not_chosen_for_you():
    plan = compile_plan(_cost_heavy(), {}, price=price({**PRICES, ('sinch', UK2): None, ('signalwire', US): 90_000}))
    found = {item['key']: item for item in items(plan, 'cost', 'rule')}
    country = found['cost.country.GB.sinch']
    assert country['saving'] is None and country['selected'] is False
    assert 'Faxbot cannot estimate the saving: Faxbot has no price for some of these faxes.' in country['sentence']
    number = found[f'cost.number.{US}']
    assert number['saving']['amount'] == '-0.06' and number['selected'] is False
    assert 'shows no saving' in number['sentence']


def test_rules_already_in_your_rules_are_in_effect_and_a_draft_blocks_the_scope():
    heavy = _cost_heavy()
    document = {**EMPTY, 'routes': [{'id': 'mine', 'name': 'My UK rule', 'on': True,
                                     'when': {'destination': {'countries': ['GB']}}, 'then': {'use': 'sinch'}}]}
    state = rules()
    state['organization'] = {**state['organization'], 'active': 3, 'document': document}
    plan = compile_plan(Facts(**{**heavy.__dict__, 'rules': state}), {}, price=price(PRICES))
    country = next(item for item in items(plan, 'cost') if item['key'] == 'cost.country.GB.sinch')
    assert country['kind'] == 'in_effect' and '“My UK rule”' in country['sentence']
    assert plan['targets']['organization']['base_revision'] == 3
    drafted = compile_plan(Facts(**{**heavy.__dict__, 'rules': rules(draft=('organization',))}), {},
                           price=price(PRICES))
    for item in items(drafted, kind='rule'):
        assert item['blocked'] and item['selected'] is False
    assert missing(drafted)['draft.organization']['status'] == 'blocking'


def test_the_target_document_puts_new_routes_first_and_passes_the_rules_check():
    plan = compile_plan(_cost_heavy(), {}, price=price(PRICES))
    chosen = [item for item in items(plan, kind='rule') if item['selected']]
    base = {**EMPTY, 'routes': [{'id': 'old', 'name': 'Old', 'on': True, 'when': {}, 'then': {'automatic': True}}]}
    document = target_document(base, chosen)
    assert [rule['id'] for rule in document['routes']] == [item['rule']['id'] for item in chosen] + ['old']
    assert base['routes'][0]['id'] == 'old' and len(base['routes']) == 1  # the base is not changed
    problems = check('organization', '', document, CheckContext(accounts=(PHAXIO, SINCH, SIGNALWIRE)))
    assert [problem.message for problem in problems if problem.level == 'error'] == []


def test_the_real_predictor_prices_a_saving():
    """Companion of the stand-in prices above: the shared predictor itself, from the published prices."""
    one = predicted_cost('phaxio', US, 2)
    assert isinstance(one, Money) and one.currency == 'USD' and one.micros > 0
    saving = monthly_saving([(US, 'phaxio', 2), (US, 'sinch', 1)], 'sinch')
    assert saving.faxes == 1 and saving.known and saving.currency == 'USD'
    assert saving.micros == predicted_cost('phaxio', US, 2).micros - predicted_cost('sinch', US, 2).micros
    # A route with no published price for the number makes the saving unknown, never zero.
    unknown = monthly_saving([(US, 'phaxio', 2)], 'documo')
    assert not unknown.known and unknown.view() is None
    plan = compile_plan(_cost_heavy(), {})
    assert any(item['saving'] for item in items(plan, 'cost', 'rule'))


# -- a partner-rich installation ------------------------------------------------------------------------------

def test_a_partner_rich_installation_gets_steps_that_apply_never_takes():
    found = facts(
        partners=({'number': US, 'display_name': 'County Clinic', 'faxes': 5, 'sentence': 'You sent 5 faxes.',
                   'monthly_cost': {'currency': 'USD', 'amount': '0.09'}},),
        discovered=({'id': 'd' * 32, 'number': UK, 'organization': 'Leeds Office', 'source': 'call'},),
        relay_offers=({'peer_id': 'p' * 32, 'partner': 'Sydney Office', 'country': 'AU', 'faxes': 4,
                       'saving': {'amount_micros': 250_000, 'currency': 'USD'},
                       'sentence': 'Faxes to numbers in Australia would have cost about $0.25 less through Sydney '
                                   'Office.', 'action': 'Accept Sydney Office\'s offer under Partners to use it.'},),
        send_once_offers=({'agreement_id': 'a' * 32, 'partner': 'County Clinic', 'intake': 'Central intake',
                           'numbers': [US, '+12025550124']},))
    plan = compile_plan(found, {}, price=price({}))
    partners = items(plan, 'partners')
    assert [item['kind'] for item in partners] == ['step'] * 4
    assert all(item['selected'] is False and item['link'] == 'recipients/partners' for item in partners)
    assert all(item['sources'] and item['sentence'] for item in partners)
    invite, enroll, relay, once = partners
    assert invite['title'] == 'Invite County Clinic to be a direct partner'
    assert invite['saving'] == {'currency': 'USD', 'amount': '0.09', 'faxes': 5, 'estimate': True}
    assert 'challenge fax' in enroll['sentence']
    assert relay['saving']['amount'] == '0.25' and 'Australia' in relay['title']
    assert 'stop going by telephone' in once['sentence'] and '2 numbers' in once['sentence']
    assert plan['targets'] == {}


# -- receiving and reliability ---------------------------------------------------------------------------------

def test_receiving_and_reliability_items_come_from_history():
    found = facts(
        unplaced=({'number': '+13035550188', 'faxes': 4},),
        junk=({'number': '+13035550999', 'marks': 2, 'active': False, 'expires_at': None, 'rejected': 0},
              {'number': '+13035550998', 'marks': 1, 'active': True, 'expires_at': NOW + timedelta(days=5),
               'rejected': 3}),
        reply={'number': None, 'shows': '+13035550100',
               'suggestion': {'number': '+13035550100', 'sentence': '+13035550100 is your cheapest number.'}},
        fallbacks=({'number': US, 'name': 'County Clinic', 'current': 'phaxio', 'current_label': 'Phaxio',
                    'failed': 4, 'attempts': 5, 'better': 'signalwire', 'better_label': 'SignalWire',
                    'delivered': 3, 'better_attempts': 3, 'chosen_by_you': True},),
        busy=({'number': UK, 'learn': True, 'slots': [{'label': 'Weekdays, 9:00 AM to 10:00 AM',
                                                       'summary': 'Busy on 4 of the last 5 weekdays.'}]},
              {'number': UK2, 'learn': False, 'slots': [{'label': 'Mondays, 9:00 AM to 10:00 AM',
                                                         'summary': 'Busy on 3 of the last 3 Mondays.'}]}))
    plan = compile_plan(found, {}, price=price({}))
    receiving = {item['key']: item for item in items(plan, 'receiving')}
    assert receiving['receiving.mailbox.+13035550188']['kind'] == 'step'
    assert '4 faxes' in receiving['receiving.mailbox.+13035550188']['sentence']
    assert receiving['receiving.junk.+13035550999']['title'] == 'Block +13035550999 again'
    assert receiving['receiving.junk.+13035550998']['title'] == 'Keep blocking +13035550998'
    reply = receiving['receiving.reply-number']
    assert reply['kind'] == 'setting' and reply['changes'] == {'fax_reply_number': '+13035550100'} and reply['selected']
    reliability = {item['key']: item for item in items(plan, 'reliability')}
    fallback = reliability[f'reliability.fallback.{US}']
    assert fallback['kind'] == 'rule' and fallback['rule']['then'] == {'try_in_order': ['signalwire', 'phaxio']}
    assert 'because you chose it' in fallback['sentence']
    assert reliability[f'reliability.busy.{UK}']['kind'] == 'in_effect'
    assert reliability[f'reliability.busy.{UK2}']['kind'] == 'step'


# -- jurisdictions ---------------------------------------------------------------------------------------------

BOXES = ({'id': 'box-us', 'name': 'Denver', 'numbers': ['+13035550100']},
         {'id': 'box-gb', 'name': 'Leeds', 'numbers': ['+441132960000']})


def test_two_mailboxes_with_different_jurisdictions_stay_independent():
    context = {'organization_name': 'Example Health', 'mailboxes': {'box-us': {'country': 'US'},
                                                                    'box-gb': {'country': 'GB'}}}
    plan = compile_plan(facts(mailboxes=BOXES), context, price=price({}))
    header = next(item for item in items(plan, 'compliance'))
    assert header['key'] == 'compliance.header' and header['kind'] == 'setting'
    assert header['changes'] == {'fax_header': 'Example Health'} and header['applies_to'] == ['box-us']
    assert '47 CFR 68.318(d)' in header['sentence'] and 'not legal advice' in header['sentence']
    # The header line is one for the whole installation, and the suggestion says so for the other mailbox.
    assert header['sentence'].endswith('Faxbot has one header line for every fax, so faxes from Leeds will show it too.')
    views = {view['id']: view for view in plan['mailboxes']}
    assert views['box-us']['items'] == ['compliance.header'] and views['box-gb']['items'] == []
    assert views['box-gb']['missing'] == ['reviewed-rules.GB'] and views['box-us']['missing'] == []
    gap = missing(plan)['reviewed-rules.GB']
    assert gap['owner'] == 'faxbot' and 'Leeds' in gap['sentence'] and gap['scope'] == 'box-gb'
    choices = {choice['label']: choice for choice in views['box-gb']['choices']}
    assert choices['Country']['value'] == 'the United Kingdom' and choices['Country']['source'] == 'You stated it'
    assert choices['Header line']['source'] == 'This installation'
    # Changing one mailbox's country changes nothing about the other's.
    other = compile_plan(facts(mailboxes=BOXES), {**context, 'mailboxes': {'box-us': {'country': 'US'},
                                                                           'box-gb': {'country': 'AU'}}},
                         price=price({}))
    again = {view['id']: view for view in other['mailboxes']}
    assert again['box-us'] == views['box-us']
    assert again['box-gb']['missing'] == ['reviewed-rules.AU']


def test_a_mailbox_with_no_country_gets_nothing_country_specific_and_inherits_only_a_stated_one():
    plan = compile_plan(facts(mailboxes=BOXES), {'mailboxes': {'box-us': {'country': 'US'}}}, price=price({}))
    views = {view['id']: view for view in plan['mailboxes']}
    assert views['box-gb']['country'] is None and views['box-gb']['country_source'] == 'not_stated'
    assert views['box-gb']['missing'] == ['country.box-gb'] and views['box-gb']['items'] == []
    # No business name typed: the header is missing, never guessed.
    assert missing(plan)['header-name']['owner'] == 'you'
    inherited = compile_plan(facts(mailboxes=BOXES), {'country': 'GB', 'mailboxes': {'box-us': {'country': 'US'}}},
                             price=price({}))
    views = {view['id']: view for view in inherited['mailboxes']}
    assert views['box-gb']['country'] == 'GB' and views['box-gb']['country_source'] == 'organization'


def test_a_header_already_yours_is_in_effect_and_missing_numbers_are_listed():
    found = facts(values=values(fax_header='Example Health', artifact_ttl_days=30),
                  reply={'number': None, 'shows': None, 'suggestion': None})
    plan = compile_plan(found, {'country': 'US'}, price=price({}))
    header = next(item for item in items(plan, 'compliance'))
    assert header['kind'] == 'in_effect'
    found_missing = missing(plan)
    assert 'header-number' in found_missing and 'retention' not in found_missing


def test_the_description_is_checked_and_never_filled_in():
    assert clean_context({}, set()) == {'organization_name': '', 'country': '', 'mailboxes': {}}
    cleaned = clean_context({'organization_name': '  Example   Health ', 'country': 'us',
                             'mailboxes': {'box-us': {'country': 'us'}, 'box-gb': {'country': ''}}},
                            {'box-us', 'box-gb'})
    assert cleaned == {'organization_name': 'Example Health', 'country': 'US', 'mailboxes': {'box-us': {'country': 'US'}}}
    for bad in ({'country': 'XX'}, {'mailboxes': {'gone': {'country': 'US'}}}, {'organization_name': 'A|B'},
                {'organization_name': 'x' * 81}):
        with pytest.raises(ContextError):
            clean_context(bad, {'box-us'})
