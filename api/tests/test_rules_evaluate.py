"""The sending-rules evaluator: every condition, action and setting, scopes, and replay. Pure; no database."""
import itertools
import random

import pytest

from api.app.rules import model
from api.app.rules.compile import DocumentError, compile_document, document_problems
from api.app.rules.evaluate import decide


ACCOUNTS = (model.Account('sip', 'sip', 'Telnyx trunk', default=True, automatic=True, sslfax=True),
            model.Account('humblefax', 'humblefax', 'HumbleFax', automatic=True),
            model.Account('sinch-uk', 'sinch', 'Sinch (UK)', site='leeds'),
            model.Account('sip-leeds', 'sip', 'Leeds trunk', site='leeds'),
            model.Account('efax', 'efax', 'eFax', enabled=False),
            model.Account('phaxio', 'phaxio', 'Phaxio', sends=False))
UK, US = '+442071234567', '+13035550100'


def rule(rule_id, then, when=None, **extra):
    return {'id': rule_id, 'name': rule_id.replace('-', ' ').capitalize(), 'on': True, 'when': when or {},
            'then': then, **extra}


def organization(limits=(), routes=(), number=7, **definitions):
    document = {'format': 1, 'limits': list(limits), 'routes': list(routes), **definitions}
    return compile_document(model.RevisionRef('organization', '', f'org-{number}', number), document)


def scope(kind, scope_id, limits=(), routes=(), definitions=None, number=2):
    document = {'format': 1, 'limits': list(limits), 'routes': list(routes)}
    return compile_document(model.RevisionRef(kind, scope_id, f'{kind}-{number}', number), document,
                            definitions.definitions if definitions is not None else None)


def facts(to=UK, **values):
    values.setdefault('country', 'GB' if to.startswith('+44') else 'US')
    return model.Facts(to, values.pop('accepted_at', '2026-10-07T15:00:00'), **values)


def run(compiled, fax=None, accounts=ACCOUNTS):
    if not isinstance(compiled, dict):
        compiled = {'organization': compiled}
    return decide(compiled, fax or facts(), accounts)


# Without rules: exactly today's automatic choice ------------------------------------------------------------

def test_no_rules_is_the_automatic_choice_in_configured_order():
    decision = run({}, facts(US))
    assert decision.outcome == 'route' and decision.route == model.AUTOMATIC
    assert decision.envelope.mode == 'automatic'
    assert decision.envelope.accounts == ('sip', 'humblefax')
    assert decision.envelope.direct and not decision.envelope.local
    assert not decision.envelope.strict_fallback and decision.revisions == () and decision.trace == ()


def test_an_empty_published_rule_set_decides_the_same_as_none():
    empty = run(organization(), facts(US))
    none = run({}, facts(US))
    assert empty.envelope == none.envelope and empty.route == none.route and empty.outcome == none.outcome
    assert [ref.name for ref in empty.revisions] == ['organization']


def test_the_preferred_route_is_pinned_and_ranks_first_as_today():
    decision = run({}, facts(US, preferred_route='humblefax'))
    assert decision.route.kind == 'preferred' and decision.envelope.preferred == 'humblefax'
    # A preferred route the automatic choice does not use is ignored, as RoutePolicy ignores it today.
    other = run({}, facts(US, preferred_route='sinch-uk'))
    assert other.route == model.AUTOMATIC and other.envelope.preferred is None
    assert other.trace[-1].note == 'not_listed'


def test_own_numbers_are_delivered_inside_faxbot_unless_a_real_call_is_asked_for():
    assert run({}, facts(US, own_number=True)).envelope.local
    assert not run({}, facts(US, own_number=True, by_call=True)).envelope.local
    limit = organization([rule('l-call', {'place_a_real_call': True})])
    assert not run(limit, facts(US, own_number=True)).envelope.local


# Conditions ------------------------------------------------------------------------------------------------

DEFINITIONS = dict(
    lists={'uk-clinics': {'name': 'UK clinics', 'numbers': ['+441782684953'], 'prefixes': ['+4420']}},
    labels=['legal', 'clinical'],
    regions={'north': {'name': 'Northern England', 'prefixes': ['+44113', '+44161']},
             'nordics': {'name': 'Nordics', 'countries': ['SE', 'NO']}},
    sites=[{'key': 'leeds', 'name': 'Leeds office', 'country': 'GB', 'time_zone': 'Europe/London',
            'mailboxes': ['mailbox-leeds'], 'groups': ['group-leeds'], 'accounts': ['sip-leeds']},
           {'key': 'denver', 'name': 'Denver office', 'country': 'US', 'time_zone': 'America/Denver',
            'groups': ['group-denver']}],
    workflows=[{'key': 'referrals', 'name': 'Referrals', 'mailboxes': ['mailbox-referrals'], 'labels': ['clinical']}])

CASES = [
    ({'destination': {'numbers': [UK]}}, facts(UK), facts('+442079999999')),
    ({'destination': {'prefixes': ['+4420']}}, facts(UK), facts('+441132000000')),
    ({'destination': {'lists': ['uk-clinics']}}, facts('+441782684953'), facts('+441132000000')),
    ({'destination': {'lists': ['uk-clinics']}}, facts(UK), facts(US)),
    ({'destination': {'countries': ['GB']}}, facts(UK), facts(US)),
    ({'destination': {'regions': ['north']}}, facts('+441132000000'), facts(UK)),
    ({'destination': {'regions': ['nordics']}}, facts('+4681234567', country='SE'), facts(UK)),
    ({'destination': {'recipients': ['destination-1']}}, facts(recipient_id='destination-1'), facts()),
    ({'destination': {'partner': True}}, facts(partner=True), facts()),
    ({'destination': {'own_number': True}}, facts(own_number=True), facts()),
    ({'destination': {'approved_alternate': True}},
     facts(alternate=model.Alternate('+18005550100', 'approval-1')), facts()),
    ({'destination': {'in_sender_country': True}}, facts(UK, mailbox_id='mailbox-leeds'),
     facts(US, mailbox_id='mailbox-leeds')),
    ({'sender': {'people': ['person-1']}}, facts(sender=model.Sender('person-1', 'person')),
     facts(sender=model.Sender('person-2', 'person'))),
    ({'sender': {'keys': ['binding-1']}}, facts(sender=model.Sender('key-principal', 'key', 'binding-1')),
     facts(sender=model.Sender('key-principal', 'key', 'binding-2'))),
    ({'sender': {'groups': ['group-a']}}, facts(sender=model.Sender('p', 'person', groups=('group-a', 'group-b'))),
     facts(sender=model.Sender('p', 'person', groups=('group-b',)))),
    ({'sender': {'mailboxes': ['mailbox-1']}}, facts(mailbox_id='mailbox-1'), facts(mailbox_id='mailbox-2')),
    ({'sender': {'sites': ['leeds']}}, facts(mailbox_id='mailbox-leeds'),
     facts(sender=model.Sender('p', 'person', groups=('group-denver',)))),
    ({'sender': {'sites': ['leeds']}}, facts(sender=model.Sender('p', 'person', groups=('group-leeds',))), facts()),
    ({'workflows': ['referrals']}, facts(workflow='referrals'), facts()),
    ({'workflows': ['referrals']}, facts(mailbox_id='mailbox-referrals'), facts(mailbox_id='mailbox-1')),
    ({'workflows': ['referrals']}, facts(labels=('clinical',)), facts(labels=('legal',))),
    ({'labels': ['legal']}, facts(labels=('billing', 'legal')), facts(labels=('billing',))),
    ({'document': {'pages_over': 20}}, facts(pages=21), facts(pages=20)),
    ({'document': {'pages_under': 3}}, facts(pages=2), facts(pages=3)),
    ({'document': {'size_over': 1000}}, facts(size_bytes=1001), facts(size_bytes=1000)),
    ({'document': {'case_packet': True}}, facts(case_packet=True), facts()),
    ({'urgent': True}, facts(urgent=True), facts()),
    ({'real_call': True}, facts(by_call=True), facts()),
    # 15:00 UTC is 09:00 in Denver (MDT) and 16:00 in London (BST).
    ({'time': {'days': ['wed'], 'from': '15:00', 'until': '16:00'}}, facts(), facts(accepted_at='2026-10-07T16:00:00')),
    ({'time': {'from': '18:00', 'until': '07:00'}}, facts(accepted_at='2026-10-07T02:00:00'), facts()),
    ({'time': {'days': ['tue'], 'from': '18:00', 'until': '07:00'}}, facts(accepted_at='2026-10-07T02:00:00'),
     facts(accepted_at='2026-10-08T02:00:00')),
    ({'time': {'from': '16:00', 'until': '17:00', 'time_zone': 'sender_site'}}, facts(mailbox_id='mailbox-leeds'),
     facts()),
    ({'time': {'from': '08:00', 'until': '10:00', 'time_zone': 'installation'}}, facts(time_zone='America/Denver'),
     facts(time_zone='Europe/London')),
]


@pytest.mark.parametrize('when, hit, miss', CASES)
def test_each_condition_matches_and_misses(when, hit, miss):
    compiled = organization(routes=[rule('r-1', {'use': 'humblefax'}, when)], **DEFINITIONS)
    assert run(compiled, hit).route.rule_id == 'r-1'
    missed = run(compiled, miss)
    assert missed.route == model.AUTOMATIC
    assert missed.trace[0].result == 'not_matched' and missed.trace[0].field


def test_every_field_must_match_and_unless_excludes():
    when = {'destination': {'countries': ['GB']}, 'document': {'pages_over': 5}}
    compiled = organization(routes=[rule('r-1', {'use': 'humblefax'}, when, unless={'urgent': True})])
    assert run(compiled, facts(pages=6)).route.rule_id == 'r-1'
    assert run(compiled, facts(pages=5)).trace[0].field == 'document.pages_over'
    unless = run(compiled, facts(pages=6, urgent=True))
    assert unless.route == model.AUTOMATIC and unless.trace[0].result == 'unless'


def test_a_rule_that_is_off_is_not_evaluated():
    compiled = organization(routes=[{**rule('r-1', {'use': 'humblefax'}), 'on': False}])
    decision = run(compiled)
    assert decision.route == model.AUTOMATIC and decision.trace == ()


# Routing ---------------------------------------------------------------------------------------------------

def test_routing_is_first_match_and_names_its_mode():
    compiled = organization(routes=[rule('r-uk', {'try_in_order': ['sinch-uk', 'sip']},
                                         {'destination': {'countries': ['GB']}}),
                                    rule('r-all', {'cheapest_reliable': ['sip', 'humblefax']})])
    uk = run(compiled)
    assert (uk.route.rule_id, uk.envelope.mode, uk.envelope.accounts) == ('r-uk', 'ordered', ('sinch-uk', 'sip'))
    assert uk.envelope.strict_fallback
    assert [step.result for step in uk.trace] == ['matched', 'not_reached']
    us = run(compiled, facts(US))
    assert (us.route.rule_id, us.envelope.mode, us.envelope.accounts) == ('r-all', 'cheapest', ('sip', 'humblefax'))
    one = run(organization(routes=[rule('r-one', {'use': 'sinch-uk'})]))
    assert one.envelope.mode == 'one' and one.envelope.accounts == ('sinch-uk',)


def test_an_explicit_automatic_rule_keeps_todays_fallback_and_preference():
    compiled = organization(routes=[rule('r-auto', {'automatic': True, 'page_layout': 'one_per_sheet'})])
    decision = run(compiled, facts(US))
    assert decision.envelope.mode == 'automatic' and not decision.envelope.strict_fallback
    assert decision.envelope.page_layout == 'one_per_sheet' and decision.layout_source.rule_id == 'r-auto'


def test_accounts_that_cannot_send_now_are_left_out_with_the_reason():
    compiled = organization(routes=[rule('r-1', {'try_in_order': ['efax', 'phaxio', 'documo', 'sip']})])
    decision = run(compiled)
    assert decision.envelope.accounts == ('sip',)
    assert {(item.account, item.why) for item in decision.excluded} == {
        ('efax', 'turned_off'), ('phaxio', 'not_sending'), ('documo', 'unknown_account')}


def test_site_accounts_use_the_senders_site_and_hold_without_one():
    compiled = organization(routes=[rule('r-site', {'site_accounts': 'sender', 'mode': 'ordered',
                                                    'when_busy': 'next'})], **DEFINITIONS)
    leeds = run(compiled, facts(mailbox_id='mailbox-leeds'))
    # The site's own list first, then accounts that name the site.
    assert leeds.envelope.accounts == ('sip-leeds', 'sinch-uk') and leeds.envelope.mode == 'ordered'
    assert leeds.envelope.when_busy == 'next' and leeds.site == 'leeds'
    nowhere = run(compiled, facts())
    assert nowhere.outcome == 'blocked' and nowhere.reason == 'no_site'
    named = organization(routes=[rule('r-named', {'site_accounts': 'leeds'})], **DEFINITIONS)
    assert run(named).envelope.accounts == ('sip-leeds', 'sinch-uk')
    assert run(named).envelope.mode == 'cheapest'


# Limits ----------------------------------------------------------------------------------------------------

def test_never_narrows_every_choice_and_says_which_limit():
    compiled = organization([rule('l-hf-uk', {'never': ['humblefax']}, {'destination': {'countries': ['GB']}})],
                            [rule('r-all', {'cheapest_reliable': ['sip', 'humblefax']})])
    decision = run(compiled)
    assert decision.envelope.accounts == ('sip',)
    assert decision.excluded == (model.Excluded('humblefax', 'never', compiled.limits[0].source),)
    assert run(compiled, facts(US)).envelope.accounts == ('sip', 'humblefax')


def test_a_limit_that_leaves_nothing_holds_the_fax():
    compiled = organization([rule('l-no', {'never': ['sip', 'humblefax', 'direct']})])
    decision = run(compiled, facts(US, partner=True))
    assert decision.outcome == 'blocked' and decision.reason == 'no_allowed_account'
    assert decision.envelope.accounts == () and not decision.envelope.direct


def test_limits_give_the_same_result_in_any_order():
    limits = [rule('l-hf', {'never': ['humblefax']}),
              rule('l-cap-1', {'cap_cost': {'currency': 'USD', 'amount': '0.50'}}),
              rule('l-cap-2', {'cap_cost': {'currency': 'USD', 'amount': '0.40'}}),
              rule('l-approve', {'hold_for_approval': {'separate_approver': False}}),
              rule('l-approve-2', {'hold_for_approval': {'separate_approver': True}}),
              rule('l-call', {'place_a_real_call': True})]
    fax = facts(US, quotes=(model.Quote('sip', 300_000, 'USD'), model.Quote('humblefax', 100_000, 'USD')))
    envelopes = set()
    for order in itertools.permutations(limits):
        envelope = run(organization(order), fax).envelope
        envelopes.add((envelope.accounts, tuple((cap.micros, cap.currency) for cap in envelope.caps),
                       tuple((hold.kind, hold.separate_approver) for hold in envelope.holds), envelope.local))
    assert envelopes == {(('sip',), ((400_000, 'USD'),), (('approval', True),), False)}


def test_a_cap_removes_routes_over_it_and_routes_with_unknown_cost():
    quotes = (model.Quote('sip', 300_000, 'USD'), model.Quote('humblefax', None, None))
    compiled = organization([rule('l-cap', {'cap_cost': {'currency': 'USD', 'amount': '0.50'}})])
    decision = run(compiled, facts(US, quotes=quotes))
    assert decision.envelope.accounts == ('sip',)
    assert decision.excluded == (model.Excluded('humblefax', 'unknown_cost', compiled.limits[0].source, soft=True),)
    cheap = organization([rule('l-cap', {'cap_cost': {'currency': 'USD', 'amount': '0.10'}}, mandatory=True)])
    held = run(cheap, facts(US, quotes=quotes))
    assert held.outcome == 'blocked' and held.reason == 'no_account_under_cap'
    assert {(item.account, item.why, item.soft) for item in held.excluded} == {
        ('sip', 'over_cap', False), ('humblefax', 'unknown_cost', False)}
    # A price in another currency is not a known price under this cap.
    other = run(compiled, facts(US, quotes=(model.Quote('sip', 1, 'EUR'), model.Quote('humblefax', 1, 'USD'))))
    assert other.envelope.accounts == ('humblefax',)


def test_require_direct_never_reaches_a_call():
    compiled = organization([rule('l-direct', {'require_direct': True})])
    partner = run(compiled, facts(US, partner=True))
    assert partner.outcome == 'route' and partner.envelope.accounts == () and partner.envelope.require_direct
    assert {item.why for item in partner.excluded} == {'direct_required'}
    none = run(compiled, facts(US))
    assert none.outcome == 'blocked' and none.reason == 'needs_partner'
    # Delivery inside Faxbot places no call at all, so an own number is still delivered.
    assert run(compiled, facts(US, own_number=True)).outcome == 'route'


def test_require_encryption_means_direct_or_ssl_fax_this_number_has_used():
    compiled = organization([rule('l-enc', {'require_encryption': True})])
    seen = run(compiled, facts(US, sslfax_seen=True))
    assert seen.envelope.accounts == ('sip',) and seen.envelope.sslfax and seen.outcome == 'route'
    unseen = run(compiled, facts(US))
    assert unseen.outcome == 'blocked' and unseen.reason == 'needs_encryption'
    partner = run(compiled, facts(US, partner=True))
    assert partner.outcome == 'route' and partner.envelope.accounts == () and not partner.envelope.sslfax


def test_holds_for_approval_and_for_a_time_window():
    compiled = organization([
        rule('l-big', {'hold_for_approval': {'separate_approver': True}}, {'document': {'pages_over': 20}}),
        rule('l-night', {'hold_until': {'days': ['mon', 'tue', 'wed', 'thu', 'fri'], 'from': '18:00',
                                        'until': '07:00'}})])
    # 15:00 UTC on Wednesday 7 October is 09:00 in Denver: the next window opens at 18:00 there (00:00 UTC).
    decision = run(compiled, facts(US, pages=30, time_zone='America/Denver'))
    assert decision.outcome == 'held'
    assert [(hold.kind, hold.separate_approver, hold.release_at) for hold in decision.envelope.holds] == [
        ('approval', True, None), ('window', False, '2026-10-08T00:00:00')]
    inside = run(compiled, facts(US, accepted_at='2026-10-08T01:00:00', time_zone='America/Denver'))
    assert inside.outcome == 'route' and inside.envelope.holds == ()
    # Friday 23:00 in Denver is inside Friday's window; Saturday 10:00 waits for Monday 18:00.
    weekend = run(compiled, facts(US, accepted_at='2026-10-10T16:00:00', time_zone='America/Denver'))
    assert weekend.envelope.holds[0].release_at == '2026-10-13T00:00:00'


# Settings --------------------------------------------------------------------------------------------------

ALTERNATE = model.Alternate('+18005550100', 'approval-1', 'Jane Smith', '2026-10-01T09:00:00', 'Same intake')


def test_the_approved_alternate_number_use_never_and_only():
    use = run(organization(routes=[rule('r', {'automatic': True, 'alternate_number': 'use'})]),
              facts(US, alternate=ALTERNATE))
    assert use.envelope.dial == ALTERNATE and use.envelope.alternate == 'use'
    # With no rule, Faxbot's own default dials an approved alternate.
    assert run({}, facts(US, alternate=ALTERNATE)).envelope.dial == ALTERNATE
    never = organization([rule('l-legal', {'alternate_number': 'never'}, {'labels': ['legal']})],
                         [rule('r', {'automatic': True, 'alternate_number': 'use'})], labels=['legal'])
    kept = run(never, facts(US, alternate=ALTERNATE, labels=('legal',)))
    assert kept.envelope.dial is None and kept.alternate_source.rule_id == 'l-legal'
    only = organization(routes=[rule('r', {'try_in_order': ['sip'], 'alternate_number': 'only'})])
    assert run(only, facts(US, alternate=ALTERNATE)).envelope.dial == ALTERNATE
    missing = run(only, facts(US))
    assert missing.outcome == 'blocked' and missing.reason == 'needs_alternate'


def test_a_cap_prices_the_number_that_will_be_dialed():
    quotes = (model.Quote('sip', 900_000, 'USD'), model.Quote('sip', 0, 'USD', number='alternate'),
              model.Quote('humblefax', 100_000, 'USD'), model.Quote('humblefax', 100_000, 'USD', number='alternate'))
    compiled = organization([rule('l-cap', {'cap_cost': {'currency': 'USD', 'amount': '0.50'}})])
    assert run(compiled, facts(US, quotes=quotes, alternate=ALTERNATE)).envelope.accounts == ('sip', 'humblefax')
    assert run(compiled, facts(US, quotes=quotes)).envelope.accounts == ('humblefax',)


# Scopes ----------------------------------------------------------------------------------------------------

def test_a_mailbox_rule_replaces_the_organizations_choice_inside_what_it_allows():
    org = organization([rule('l-hf-uk', {'never': ['humblefax']}, {'destination': {'countries': ['GB']}})],
                       [rule('r-uk', {'use': 'sinch-uk'}, {'destination': {'countries': ['GB']}})], **DEFINITIONS)
    mailbox = scope('mailbox', 'mailbox-1', routes=[rule('m-cheap', {'try_in_order': ['humblefax', 'sip']})],
                    definitions=org)
    compiled = {'organization': org, 'mailbox:mailbox-1': mailbox}
    decision = run(compiled, facts(mailbox_id='mailbox-1'))
    assert decision.route.scope == 'mailbox' and decision.envelope.accounts == ('sip',)
    assert [ref.name for ref in decision.revisions] == ['organization', 'mailbox:mailbox-1']
    overridden = next(step for step in decision.trace if step.rule_id == 'r-uk')
    assert (overridden.result, overridden.note) == ('not_applied', 'overridden')
    # Another mailbox's rules never apply.
    assert run(compiled, facts(mailbox_id='mailbox-2')).route.rule_id == 'r-uk'


def test_mailbox_and_workflow_limits_narrow_further():
    org = organization(routes=[rule('r-all', {'cheapest_reliable': ['sip', 'humblefax', 'sinch-uk']})], **DEFINITIONS)
    mailbox = scope('mailbox', 'mailbox-referrals', [rule('m-no-sinch', {'never': ['sinch-uk']})], definitions=org)
    workflow = scope('workflow', 'referrals', [rule('w-no-hf', {'never': ['humblefax']})], definitions=org)
    compiled = {'organization': org, 'mailbox:mailbox-referrals': mailbox, 'workflow:referrals': workflow}
    decision = run(compiled, facts(mailbox_id='mailbox-referrals'))
    assert decision.workflow == 'referrals' and decision.envelope.accounts == ('sip',)
    assert [ref.name for ref in decision.revisions] == ['organization', 'mailbox:mailbox-referrals',
                                                         'workflow:referrals']


def test_the_workflows_rule_wins_over_the_mailboxs_and_a_mandatory_organization_rule_over_both():
    org = organization(routes=[rule('r-uk', {'use': 'sinch-uk'}, {'destination': {'countries': ['GB']}})],
                       **DEFINITIONS)
    mailbox = scope('mailbox', 'mailbox-referrals', routes=[rule('m', {'use': 'humblefax'})], definitions=org)
    workflow = scope('workflow', 'referrals', routes=[rule('w', {'use': 'sip'})], definitions=org)
    compiled = {'organization': org, 'mailbox:mailbox-referrals': mailbox, 'workflow:referrals': workflow}
    assert run(compiled, facts(mailbox_id='mailbox-referrals')).route.rule_id == 'w'
    mandatory = organization(routes=[rule('r-uk', {'use': 'sinch-uk'}, {'destination': {'countries': ['GB']}},
                                          mandatory=True)], **DEFINITIONS)
    compiled['organization'] = mandatory
    decision = run(compiled, facts(mailbox_id='mailbox-referrals'))
    assert decision.route.rule_id == 'r-uk'
    assert {(step.rule_id, step.note) for step in decision.trace if step.result == 'not_applied'} == {
        ('m', 'mandatory'), ('w', 'mandatory')}


def test_the_preferred_route_beats_the_organizations_list_but_not_its_limits_or_lower_scopes():
    org = organization([rule('l-hf-uk', {'never': ['humblefax']}, {'destination': {'countries': ['GB']}})],
                       [rule('r-all', {'use': 'sip'})])
    us = run(org, facts(US, preferred_route='humblefax'))
    assert us.route.kind == 'preferred' and us.envelope.preferred == 'humblefax'
    uk = run(org, facts(UK, preferred_route='humblefax'))
    assert uk.route.rule_id == 'r-all'
    assert (uk.trace[-1].kind, uk.trace[-1].note) == ('preferred', 'excluded')
    mailbox = scope('mailbox', 'mailbox-1', routes=[rule('m', {'use': 'sinch-uk'})], definitions=org)
    lower = run({'organization': org, 'mailbox:mailbox-1': mailbox},
                facts(US, preferred_route='humblefax', mailbox_id='mailbox-1'))
    assert lower.route.rule_id == 'm'


# Replay ----------------------------------------------------------------------------------------------------

def _random_facts(seed):
    pick = random.Random(seed)
    return facts(
        pick.choice([UK, US, '+441132000000', '+441782684953', '+4681234567']),
        accepted_at=f'2026-10-{pick.randint(1, 28):02d}T{pick.randint(0, 23):02d}:{pick.randint(0, 59):02d}:00',
        country=pick.choice(['GB', 'US', 'SE']), pages=pick.randint(0, 40), urgent=pick.random() < 0.3,
        partner=pick.random() < 0.2, own_number=pick.random() < 0.1, sslfax_seen=pick.random() < 0.5,
        preferred_route=pick.choice([None, 'sip', 'humblefax']),
        mailbox_id=pick.choice([None, 'mailbox-leeds', 'mailbox-referrals']),
        labels=tuple(sorted(pick.sample(['legal', 'clinical'], pick.randint(0, 2)))),
        alternate=pick.choice([None, ALTERNATE]), time_zone=pick.choice(['', 'America/Denver', 'Europe/London']),
        quotes=(model.Quote('sip', pick.randint(0, 900_000), 'USD'),))


def test_a_stored_decision_replays_exactly_from_its_stored_facts():
    org = organization(
        [rule('l-hf-uk', {'never': ['humblefax']}, {'destination': {'countries': ['GB']}}),
         rule('l-cap', {'cap_cost': {'currency': 'USD', 'amount': '0.50'}}, {'urgent': False}),
         rule('l-night', {'hold_until': {'from': '18:00', 'until': '07:00', 'time_zone': 'sender_site'}},
              {'document': {'pages_over': 30}})],
        [rule('r-leeds', {'site_accounts': 'sender'}, {'sender': {'sites': ['leeds']}}),
         rule('r-uk', {'try_in_order': ['sinch-uk', 'sip'], 'alternate_number': 'use'},
              {'destination': {'regions': ['north']}}),
         rule('r-all', {'cheapest_reliable': ['sip', 'humblefax'], 'page_layout': 'as_receiver_allows'},
              {'time': {'days': ['mon', 'tue'], 'from': '09:00', 'until': '17:00'}})], **DEFINITIONS)
    compiled = {'organization': org}
    for seed in range(300):
        fax = _random_facts(seed)
        decision = run(compiled, fax)
        stored_facts, stored = fax.to_json(), decision.to_json()
        again = run(compiled, model.Facts.from_json(stored_facts))
        assert again.to_json() == stored
        assert model.Decision.from_json(stored) == decision
        assert decision.facts_digest == model.Facts.from_json(stored_facts).digest()


# Document shape --------------------------------------------------------------------------------------------

@pytest.mark.parametrize('document, words', [
    ({'format': 2}, 'different version'),
    ({'format': 1, 'routes': [rule('r', {'use': 'sip', 'try_in_order': ['sip']})]}, 'exactly one way'),
    ({'format': 1, 'routes': [rule('r', {'never': ['sip']})]}, 'cannot take: never'),
    ({'format': 1, 'limits': [rule('l', {'use': 'sip'})]}, 'cannot take: use'),
    ({'format': 1, 'limits': [rule('l', {'cap_cost': {'currency': 'usd', 'amount': '1'}})]}, 'cost cap'),
    ({'format': 1, 'limits': [rule('l', {'hold_until': {'from': '18:00', 'until': '18:00'}})]}, 'same time'),
    ({'format': 1, 'routes': [rule('r', {'use': 'sip'}, {'destination': {'numbers': ['020 7123 4567']}})]},
     'full numbers'),
    ({'format': 1, 'routes': [rule('r', {'use': 'sip'}, {'destination': {'colour': ['red']}})]}, 'does not know'),
    ({'format': 1, 'routes': [rule('r', {'use': 'sip'}), rule('r', {'use': 'sip'})]}, 'same ID'),
    ({'format': 1, 'routes': [rule('r', {'site_accounts': 'sender', 'mode': 'random'})]}, 'site accounts only'),
    ({'format': 1, 'routes': [rule('r', {'use': 'direct'})]}, 'one account'),
    ({'format': 1, 'sites': [{'key': 'x', 'name': 'X', 'time_zone': 'Mars/Olympus'}]}, 'time zone'),
    ({'format': 1, 'limits': [rule('l', {'alternate_number': 'use'})]}, 'never used'),
])
def test_document_shape_problems_are_plain_sentences(document, words):
    problems = document_problems('organization', document)
    assert problems and any(words in problem.message for problem in problems), problems
    with pytest.raises(DocumentError):
        compile_document(model.RevisionRef('organization', '', 'x', 1), document)


def test_a_mailbox_document_holds_only_limits_and_routes():
    problems = document_problems('mailbox', {'format': 1, 'sites': [], 'routes': [
        rule('r', {'use': 'sip'}, mandatory=True)]})
    assert {problem.code for problem in problems} == {'shape'}
    assert len(problems) == 2
