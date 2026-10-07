"""Deciding a fax's route stays well under 5 ms with the check's largest rule sets (design §6.4)."""
import time

from api.app.rules import model
from api.app.rules.compile import compile_document, document_problems
from api.app.rules.evaluate import decide


ACCOUNTS = tuple(model.Account(key, key, automatic=index < 2) for index, key in
                 enumerate(('sip', 'humblefax', 'sinch', 'phaxio', 'documo', 'signalwire')))
KEYS = [account.key for account in ACCOUNTS]
RULES = 500
NUMBERS = 10_000


def _number(index):
    return f'+1303{index:07d}'


def _rules(prefix, offset):
    """100 limits and 400 routing rules, each with several conditions, none matching the test fax."""
    limits, routes = [], []
    for index in range(RULES):
        when = {'destination': {'numbers': [_number(offset + index * 6 + n) for n in range(6)]},
                'document': {'pages_over': index % 50}, 'urgent': bool(index % 2),
                'sender': {'groups': [f'group-{index}']}}
        if index < 100:
            limits.append({'id': f'{prefix}-l{index}', 'name': f'Limit {index}', 'on': True, 'when': when,
                           'then': {'never': [KEYS[index % len(KEYS)]], 'cap_cost': {'currency': 'USD',
                                                                                    'amount': '9.00'}}})
        else:
            routes.append({'id': f'{prefix}-r{index}', 'name': f'Route {index}', 'on': True, 'when': when,
                           'then': {'try_in_order': [KEYS[index % len(KEYS)], KEYS[(index + 1) % len(KEYS)]]}})
    return limits, routes


def _listed(rules):
    return sum(len(rule['when']['destination']['numbers']) for rule in rules)


def _scopes():
    limits, routes = _rules('o', 0)
    listed = _listed(limits + routes)
    # 3 scopes x 500 rules x 6 numbers, plus 1,000 in recipient groups: 10,000 listed numbers.
    lists = {f'list-{n}': {'name': f'List {n}', 'numbers': [_number(5_000_000 + n * 100 + k) for k in range(100)]}
             for n in range(10)}
    organization = {'format': 1, 'lists': lists, 'labels': ['legal'], 'limits': limits, 'routes': routes,
                    'workflows': [{'key': 'referrals', 'name': 'Referrals', 'labels': ['legal']}]}
    assert not [problem for problem in document_problems('organization', organization) if problem.level == 'error']
    listed += sum(len(item['numbers']) for item in lists.values())
    org = compile_document(model.RevisionRef('organization', '', 'org', 1), organization)
    compiled = {'organization': org}
    for kind, scope_id, offset in (('mailbox', 'mailbox-1', 0), ('workflow', 'referrals', 0)):
        limits, routes = _rules(kind[0], offset)
        listed += _listed(limits + routes)
        compiled[model.scope_name(kind, scope_id)] = compile_document(
            model.RevisionRef(kind, scope_id, f'{kind}-1', 1), {'format': 1, 'limits': limits, 'routes': routes},
            org.definitions)
    return compiled, listed


def test_one_decision_with_three_full_scopes_takes_under_five_milliseconds():
    compiled, listed = _scopes()
    assert listed == NUMBERS
    # Nothing matches, so every limit and every routing rule in all three scopes is evaluated.
    fax = model.Facts('+442071234567', '2026-10-07T15:00:00', country='GB', pages=60, urgent=True,
                      mailbox_id='mailbox-1', labels=('legal',), sender=model.Sender('person-1', 'person',
                                                                                      groups=('group-x',)),
                      quotes=tuple(model.Quote(key, 10_000, 'USD') for key in KEYS))
    decision = decide(compiled, fax, ACCOUNTS)
    assert decision.route == model.AUTOMATIC and len(decision.trace) == 3 * RULES
    assert [ref.name for ref in decision.revisions] == ['organization', 'mailbox:mailbox-1', 'workflow:referrals']
    timings = []
    for _ in range(200):
        start = time.perf_counter()
        decide(compiled, fax, ACCOUNTS)
        timings.append(time.perf_counter() - start)
    timings.sort()
    p95 = timings[int(len(timings) * 0.95)]
    print(f'decide: median {timings[100] * 1000:.2f} ms, p95 {p95 * 1000:.2f} ms over {len(timings)} runs')
    assert p95 < 0.005, f'p95 {p95 * 1000:.2f} ms'
