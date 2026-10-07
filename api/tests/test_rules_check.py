"""Checking a rules draft: names that must exist, rules that cannot work, warnings, and replay."""
from api.app.rules import model
from api.app.rules.check import CheckContext, check, compile_for_replay, replay
from api.app.rules.compile import compile_document
from api.app.rules.evaluate import decide


ACCOUNTS = (model.Account('sip', 'sip', 'Telnyx trunk', default=True, automatic=True),
            model.Account('humblefax', 'humblefax', 'HumbleFax', automatic=True),
            model.Account('sinch-uk', 'sinch', 'Sinch (UK)'),
            model.Account('phaxio', 'phaxio', 'Phaxio', sends=False),
            model.Account('sip-leeds', 'sip', 'Leeds trunk', site='leeds'))
CONTEXT = CheckContext(accounts=ACCOUNTS, people=frozenset({'person-1'}), keys=frozenset({'binding-1'}),
                       groups=frozenset({'group-a'}), mailboxes=frozenset({'mailbox-1'}),
                       recipients=frozenset({'destination-1'}), partners=frozenset({'+15555550100'}),
                       alternates=frozenset({'+15555550100'}),
                       prices={'sip': (20_000, 'USD'), 'humblefax': (100_000, 'USD'), 'sinch-uk': None},
                       matches_30_days={'r-used': 4, 'r-idle': 0})


def rule(rule_id, then, when=None, **extra):
    return {'id': rule_id, 'name': f'Rule {rule_id}', 'on': True, 'when': when or {}, 'then': then, **extra}


def organization(limits=(), routes=(), **definitions):
    return {'format': 1, 'limits': list(limits), 'routes': list(routes), **definitions}


def codes(problems, level):
    return [(problem.code, problem.rule_id) for problem in problems if problem.level == level]


def test_a_sound_document_has_no_errors():
    document = organization(
        [rule('l-hf', {'never': ['humblefax']}, {'destination': {'countries': ['GB']}})],
        [rule('r-uk', {'try_in_order': ['sinch-uk', 'sip']}, {'destination': {'countries': ['GB']},
                                                              'sender': {'groups': ['group-a']}})],
        lists={'clinics': {'name': 'Clinics', 'numbers': ['+15555550100']}}, labels=['legal'],
        sites=[{'key': 'leeds', 'name': 'Leeds office', 'accounts': ['sip-leeds']}])
    assert codes(check('organization', '', document, CONTEXT), 'error') == []


def test_every_name_a_rule_uses_must_exist():
    document = organization(
        [rule('l-1', {'never': ['efax']})],
        [rule('r-1', {'use': 'documo'}, {'destination': {'lists': ['nowhere'], 'regions': ['north'],
                                                         'recipients': ['destination-9']},
                                         'sender': {'people': ['person-9'], 'keys': ['binding-9'],
                                                    'groups': ['group-z'], 'mailboxes': ['mailbox-9'],
                                                    'sites': ['paris']},
                                         'workflows': ['intake'], 'labels': ['secret']}),
         rule('r-2', {'site_accounts': 'paris'}, {'urgent': True}),
         rule('r-3', {'use': 'phaxio'}, {'urgent': False})])
    found = check('organization', '', document, CONTEXT)
    errors = codes(found, 'error')
    assert errors.count(('unknown_name', 'r-1')) == 10
    assert ('unknown_account', 'l-1') in errors and ('unknown_account', 'r-1') in errors
    assert ('unknown_name', 'r-2') in errors and ('not_sending', 'r-3') in errors
    assert any('“documo”, which doesn’t exist' in problem.message for problem in found)


def test_rules_that_can_never_choose_or_have_nothing_left():
    document = organization(
        [rule('l-uk', {'never': ['sinch-uk', 'sip']}, {'destination': {'countries': ['GB']}}),
         rule('l-cap', {'cap_cost': {'currency': 'USD', 'amount': '0.01'}})],
        [rule('r-gb', {'use': 'humblefax'}, {'destination': {'countries': ['GB', 'IE']}}),
         rule('r-london', {'use': 'sip'}, {'destination': {'countries': ['GB'], 'prefixes': ['+4420']}}),
         rule('r-uk-only', {'try_in_order': ['sinch-uk', 'sip']}, {'destination': {'countries': ['GB']},
                                                                  'urgent': True})])
    errors = codes(check('organization', '', document, CONTEXT), 'error')
    # r-gb catches every fax r-london would; r-uk-only is caught by r-gb too.
    assert ('shadowed', 'r-london') in errors and ('shadowed', 'r-uk-only') in errors
    assert ('no_accounts', 'r-uk-only') in errors
    # The cap is below HumbleFax's and the trunk's prices.
    assert ('cap_unreachable', 'r-gb') in errors
    # A rule that only overlaps an earlier one is fine.
    narrow = organization(routes=[rule('r-a', {'use': 'sip'}, {'urgent': True}),
                                  rule('r-b', {'use': 'humblefax'}, {'destination': {'countries': ['GB']}})])
    assert codes(check('organization', '', narrow, CONTEXT), 'error') == []


def test_a_lower_scope_cannot_name_what_the_organization_never_allows():
    org = organization([rule('l-no-hf', {'never': ['humblefax']})])
    mailbox = {'format': 1, 'limits': [], 'routes': [rule('m-hf', {'use': 'humblefax'})]}
    errors = codes(check('mailbox', 'mailbox-1', mailbox, CONTEXT, organization=org), 'error')
    assert errors == [('not_allowed', 'm-hf')]


def test_an_account_belongs_to_one_site():
    document = organization(sites=[{'key': 'leeds', 'name': 'Leeds office', 'accounts': ['sip']},
                                   {'key': 'york', 'name': 'York office', 'accounts': ['sip', 'sip-leeds']}])
    messages = [problem.message for problem in check('organization', '', document, CONTEXT)
                if problem.code == 'account_two_sites']
    assert len(messages) == 2
    assert any('both list the account “sip”' in message for message in messages)
    assert any('set to the site “leeds”' in message for message in messages)


def test_warnings_do_not_block():
    document = organization(
        [rule('l-direct', {'require_direct': True}, {'destination': {'numbers': ['+15555550100', '+15555550111']}}),
         rule('l-cap', {'cap_cost': {'currency': 'USD', 'amount': '5.00'}}, {'urgent': True})],
        [rule('r-used', {'try_in_order': ['sip', 'humblefax', 'sinch-uk', 'sip-leeds']}, {'urgent': True}),
         rule('r-only', {'automatic': True, 'alternate_number': 'only'},
              {'destination': {'numbers': ['+15555550111']}}),
         rule('r-site', {'site_accounts': 'sender'}, {'labels': ['legal']}),
         rule('r-idle', {'use': 'sip'}, {'destination': {'countries': ['FR']}})], labels=['legal'])
    found = check('organization', '', document, CONTEXT)
    assert codes(found, 'error') == []
    assert sorted(codes(found, 'warning')) == sorted([
        ('no_partner', 'l-direct'), ('no_price', 'l-cap'), ('long_order', 'r-used'), ('no_alternate', 'r-only'),
        ('no_site', 'r-site'), ('unused', 'r-idle')])
    price = next(problem for problem in found if problem.code == 'no_price')
    assert 'Sinch (UK)' in price.message and 'Leeds trunk' in price.message


def test_replay_lists_the_faxes_whose_route_would_change():
    accounts = ACCOUNTS
    uk = model.Facts('+442071234567', '2026-10-07T15:00:00', country='GB')
    us = model.Facts('+13035550100', '2026-10-07T15:00:00', country='US')
    active = {}
    stored = [(job, number, '2026-10-07T15:00:00', facts, decide(active, facts, accounts))
              for job, number, facts in (('job-uk', uk.destination, uk), ('job-us', us.destination, us))]
    stored.append(('job-old', uk.destination, '2026-10-01T12:00:00', uk, None))
    draft = organization(routes=[rule('r-uk', {'use': 'sinch-uk'}, {'destination': {'countries': ['GB']}})])
    compiled = compile_for_replay('organization', '', draft, active)
    checked, changed = replay(stored, compiled, accounts)
    assert checked == 3
    assert [(item.job_id, item.approximate) for item in changed] == [('job-uk', False), ('job-old', True)]
    assert changed[0].after.route.rule_id == 'r-uk' and changed[0].before.route == model.AUTOMATIC


def test_a_draft_organization_recompiles_the_lower_scopes_against_its_definitions():
    org = compile_document(model.RevisionRef('organization', '', 'org-1', 1), organization(
        lists={'clinics': {'name': 'Clinics', 'numbers': ['+15555550100']}}))
    mailbox = compile_document(model.RevisionRef('mailbox', 'mailbox-1', 'm-1', 1), {
        'format': 1, 'limits': [], 'routes': [rule('m', {'use': 'humblefax'}, {'destination': {'lists': ['clinics']}})]},
        org.definitions)
    active = {'organization': org, 'mailbox:mailbox-1': mailbox}
    fax = model.Facts('+15555550199', '2026-10-07T15:00:00', country='US', mailbox_id='mailbox-1')
    assert decide(active, fax, ACCOUNTS).route == model.AUTOMATIC
    draft = organization(lists={'clinics': {'name': 'Clinics', 'numbers': ['+15555550199']}})
    assert decide(compile_for_replay('organization', '', draft, active), fax, ACCOUNTS).route.rule_id == 'm'
