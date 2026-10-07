"""faxbot providers rules, providers accounts and the held-fax commands, against an in-memory server.

The server below answers the provider-rules routes (design §6.2) the way the console's tests expect:
one draft per scope with a version, 409 for a stale draft or publish, holds with versions, and accounts
with a configuration generation. It records every request, so each test checks what the command sent.
Until the commands are registered under `faxbot providers` and `faxbot sent`, they run under a small
root that uses the real global options and error reporting.
"""
import copy
import json
from pathlib import Path

import httpx
import pytest
import typer
from typer.testing import CliRunner

from app.cli import main as cli_main
from app.cli import output
from app.cli.commands import accounts as accounts_module
from app.cli.commands import rules as rules_module

ORIGIN = 'https://testserver'
KEY = 'synthetic-rules-key'
FIXTURE = Path(__file__).resolve().parents[1] / 'admin_ui/src/__tests__/providerRulesSentences.json'


def _options_probe(account: str = rules_module.NUMBER_ACCOUNT, sender: list[str] = rules_module.NUMBER_FROM,
                   days: str = rules_module.NUMBER_DAYS, between: str = rules_module.NUMBER_BETWEEN,
                   email: str = rules_module.NUMBER_EMAIL, no_email: bool = rules_module.NUMBER_NO_EMAIL,
                   urgent: bool = rules_module.NUMBER_URGENT, keep_days: int = rules_module.NUMBER_KEEP,
                   position: int = rules_module.NUMBER_POSITION, any_number: bool = rules_module.NUMBER_ANY):
    """Stands in for faxbot numbers add/update until access.py gains these options: prints the body it would send."""
    from app.cli import state
    body = rules_module.receiving_options(state.api(), account=account, from_numbers=sender, days=days, between=between,
                                          email=email, no_email=no_email, urgent=urgent, keep_days=keep_days,
                                          position=position, any_number=any_number)
    state.out().json(body)


def _root():
    app = typer.Typer(cls=cli_main.FaxbotGroup, no_args_is_help=True, pretty_exceptions_enable=False)
    app.callback()(cli_main.main)
    providers = typer.Typer(no_args_is_help=True)
    providers.add_typer(rules_module.rules, name='rules')
    providers.add_typer(accounts_module.accounts, name='accounts')
    app.add_typer(providers, name='providers')
    sent = typer.Typer(no_args_is_help=True)
    sent.command('route')(rules_module.route_command)
    sent.command('approve')(rules_module.approve_command)
    sent.command('refuse')(rules_module.refuse_command)
    sent.command('held')(rules_module.held_list)
    sent.command('check-again')(rules_module.check_again_command)
    numbers = typer.Typer(no_args_is_help=True)
    numbers.command('explain')(rules_module.numbers_explain)
    numbers.command('options')(_options_probe)
    app.add_typer(numbers, name='numbers')
    app.add_typer(sent, name='sent')
    return app


ROOT = _root()

CHOICES = {
    'accounts': [{'key': 'sip', 'label': 'Telnyx', 'provider': 'sip', 'sends': True, 'enabled': True, 'site': None},
                 {'key': 'sinch-uk', 'label': 'Sinch (UK)', 'provider': 'sinch', 'sends': True, 'enabled': True,
                  'site': 'leeds'},
                 {'key': 'humblefax', 'label': 'HumbleFax', 'provider': 'humblefax', 'sends': True, 'enabled': True,
                  'site': None}],
    'people': [{'id': 'p-ada', 'name': 'Ada Admin', 'kind': 'user'}, {'id': 'p-scan', 'name': 'Front desk scanner',
                                                                      'kind': 'integration'}],
    'keys': [{'id': 'k-scan', 'name': 'Scanner key'}],
    'groups': [{'id': 'g-legal', 'name': 'Legal'}],
    'mailboxes': [{'id': 'm-leeds', 'name': 'Leeds intake'}],
}

ACTIVE = {'format': 1, 'sites': [{'key': 'leeds', 'name': 'Leeds office', 'country': 'GB', 'mailboxes': []}],
          'limits': [], 'routes': [
    {'id': 'r-uk', 'name': 'UK numbers go through Sinch', 'on': True, 'when': {'destination': {'countries': ['GB']}},
     'then': {'try_in_order': ['sinch-uk', 'sip']}}]}


class FakeServer:
    """The provider-rules API in memory, recording each request."""

    def __init__(self):
        self.requests = []
        self.scopes = {'organization': {'name': 'Organization', 'revisions': [
            {'number': 1, 'note': 'First rules', 'actor_name': 'Ada Admin', 'created_at': '2026-10-07T15:00:00',
             'document': copy.deepcopy(ACTIVE)}], 'draft': None}}
        self.holds = [{'id': 'h-1', 'job_id': 'f' * 32, 'kind': 'approval', 'to_number': '+15550100001', 'pages': 24,
                       'sender_name': 'Nia New', 'requested_at': '2026-10-07T16:00:00', 'until': None,
                       'reason': "Waiting for approval: the rule 'Faxes over 20 pages need approval' matched.",
                       'can_decide': True, 'version': 3},
                      {'id': 'h-2', 'job_id': 'e' * 32, 'kind': 'no_route', 'to_number': '+442071234567', 'pages': 1,
                       'sender_name': 'Ada Admin', 'requested_at': '2026-10-07T16:05:00', 'until': None,
                       'reason': 'No route your rules allow costs less than $0.10 for this fax.', 'can_decide': True,
                       'version': 1, 'options': [{'account': 'sip', 'label': 'Telnyx', 'reason': 'every line is busy'}],
                       'not_offered': ["HumbleFax is not offered: the limit 'Never send UK faxes by HumbleFax' forbids "
                                       'it.']}]
        self.generation = 7
        # Someone else saves the draft, or publishes, between this command's read and its write.
        self.draft_race = False
        self.publish_race = False
        self.accounts = {
            'generation': 7, 'default_sending': 'sip', 'default_receiving': 'sip', 'sites': [{'key': 'leeds',
                                                                                             'name': 'Leeds office'}],
            'providers': [
                {'id': 'sinch', 'label': 'Sinch', 'supports_inbound': True, 'fields': [
                    {'name': 'project_id', 'label': 'Project ID', 'secret': False, 'required': True},
                    {'name': 'api_key', 'label': 'Access key', 'secret': True, 'required': True},
                    {'name': 'api_secret', 'label': 'Access secret', 'secret': True, 'required': True}]},
                {'id': 'humblefax', 'label': 'HumbleFax', 'supports_inbound': False, 'fields': []},
                {'id': 'sip', 'label': 'Carrier trunk', 'supports_inbound': True, 'fields': [
                    {'name': 'host', 'label': 'Server', 'secret': False, 'required': True},
                    {'name': 'password', 'label': 'Password', 'secret': True, 'required': False}]}],
            'accounts': [
                {'key': 'sip', 'provider': 'sip', 'label': 'Telnyx', 'site': None, 'primary': True, 'sends': True,
                 'receives': True, 'enabled': True, 'numbers': ['+17208565062'],
                 'limits': {'at_once': 2, 'calls_per_second': 1, 'daily_limit': None},
                 'health': {'state': 'ready', 'sentence': 'Ready to send and receive.'}, 'webhook_address': None,
                 'settings': {'host': 'sip.telnyx.com'}, 'secrets_set': ['password']},
                {'key': 'sinch-uk', 'provider': 'sinch', 'label': 'Sinch (UK)', 'site': 'leeds', 'primary': False,
                 'sends': True, 'receives': True, 'enabled': True, 'numbers': ['+442071234567'],
                 'limits': {'at_once': None, 'calls_per_second': None,
                            'daily_limit': {'currency': 'USD', 'amount': '25'}},
                 'health': {'state': 'waiting', 'sentence': 'Waiting for the first fax.'},
                 'webhook_address': 'https://fax.example/sinch-inbound/sinch-uk', 'settings': {'project_id': 'proj-1'},
                 'secrets_set': ['api_key']}]}

    def scope(self, request):
        return self.scopes.setdefault(request.url.params.get('scope'), {'name': 'Leeds intake', 'revisions': [],
                                                                        'draft': None})

    def rules_state(self, scope):
        active = scope['revisions'][-1] if scope['revisions'] else None
        return {'scope': {'kind': 'organization', 'name': scope['name']}, 'active': active, 'draft': scope['draft'],
                'organization': None, 'matches_30_days': {'r-uk': 12}, 'can_write': True, 'choices': CHOICES}

    def handle(self, request):
        body = json.loads(request.content) if request.content else None
        path, method = request.url.path, request.method
        self.requests.append((method, path, dict(request.url.params), body))
        if path == '/access/mailboxes':
            return 200, {'items': [{'id': 'm-leeds', 'label': 'Leeds intake', 'enabled': True, 'resource_id': 'r',
                                    'rule_count': 0, 'version': 1}], 'next_cursor': None}
        if path.startswith('/routing/rules') and path != '/routing/rules/apply-to-waiting':
            scope = self.scope(request)
            if path == '/routing/rules':
                return 200, self.rules_state(scope)
            if path == '/routing/rules/draft' and method == 'PUT':
                if self.draft_race and scope['draft']:
                    scope['draft']['version'] += 1
                    self.draft_race = False
                current = scope['draft']['version'] if scope['draft'] else 0
                if body['expected_version'] != current:
                    return 409, {'detail': 'Someone else changed these rules. Reload them and make your change again.'}
                errors = [{'rule_id': None, 'message': "A rule names the account 'nowhere', which does not exist."}] \
                    if 'nowhere' in json.dumps(body['document']) else []
                scope['draft'] = {'document': body['document'], 'version': current + 1, 'base_revision': 1,
                                  'actor_name': 'Ada Admin', 'updated_at': '2026-10-07T16:00:00',
                                  'check': {'errors': errors, 'warnings': [], 'replay': None}}
                return 200, scope['draft']
            if path == '/routing/rules/draft' and method == 'DELETE':
                scope['draft'] = None
                return 204, None
            if path == '/routing/rules/draft/check':
                return 200, {'errors': [], 'warnings': [{'rule_id': 'r-uk', 'message': "The rule 'UK numbers go "
                                                         "through Sinch' matched no fax in the last 30 days."}],
                             'replay': {'checked': 200, 'changed': 12, 'approximate': 3, 'items': [
                                 {'job_id': 'a' * 32, 'to_number': '+442071234567', 'accepted_at': '2026-10-06T10:00:00',
                                  'before': 'Telnyx', 'after': 'Sinch (UK)', 'approximate': False}]}}
            if path == '/routing/rules/publish':
                if self.publish_race:
                    scope['revisions'].append({**scope['revisions'][-1], 'number': len(scope['revisions']) + 1})
                    self.publish_race = False
                active = scope['revisions'][-1]['number'] if scope['revisions'] else None
                if not scope['draft'] or body['expected_active_revision'] != active or \
                        body['expected_draft_version'] != scope['draft']['version']:
                    return 409, {'detail': 'Someone published other rules meanwhile. Reload them and check again.'}
                revision = {'number': (active or 0) + 1, 'note': body['note'], 'actor_name': 'Ada Admin',
                            'created_at': '2026-10-07T17:00:00', 'document': scope['draft']['document']}
                scope['revisions'].append(revision)
                scope['draft'] = None
                return 200, {key: revision[key] for key in ('number', 'note', 'actor_name', 'created_at')}
            if path == '/routing/rules/revisions':
                return 200, {'revisions': [{key: item[key] for key in ('number', 'note', 'actor_name', 'created_at')}
                                           for item in reversed(scope['revisions'])]}
            parts = path.split('/')
            if path.endswith('/restore'):
                found = scope['revisions'][int(parts[4]) - 1]
                scope['draft'] = {'document': found['document'], 'version': 1, 'base_revision': found['number'],
                                  'actor_name': 'Ada Admin', 'updated_at': '2026-10-07T17:00:00', 'check': None}
                return 200, scope['draft']
            if '/diff/' in path:
                return 200, {'from': int(parts[4]), 'to': int(parts[6]), 'changes': [
                    {'change': 'added', 'section': 'limits', 'id': 'l-hf', 'name': 'No HumbleFax', 'before': None,
                     'after': {'when': {}, 'then': {'never': ['humblefax']}}}]}
            return 200, scope['revisions'][int(parts[4]) - 1]
        if path == '/intake/connectors':
            return 200, {'connectors': [{'id': 'c-night', 'name': 'Night inbox'}]}
        if path == '/access/inbound-rules/explain':
            return 200, {'sentence': 'It would go to Front desk, marked urgent, because of the rule for +17208565062.',
                         'mailbox_label': 'Front desk', 'email': None, 'urgent': True, 'keep_days': None,
                         'rule_to_number': '+17208565062'}
        if path == '/routing/explain':
            return 200, {'outcome': 'route', 'sentence': "Sinch (UK) first, because the rule 'UK numbers go through "
                                                         "Sinch' matched.",
                         'routes': [{'account': 'sinch-uk', 'label': 'Sinch (UK)', 'sentence': 'First in the rule.',
                                     'quote': {'currency': 'USD', 'amount': '0.031'}, 'origin': 'Leeds office',
                                     'usable': True}],
                         'holds': [], 'dial': None, 'page_layout': 'Pages per sheet: as the receiving machine allows.',
                         'trace': [{'scope': 'Organization', 'rule_id': 'r-uk', 'name': 'UK numbers go through Sinch',
                                    'kind': 'route', 'matched': True, 'failed': None}]}
        if path == '/routing/rules/apply-to-waiting':
            return 200, {'checked': 4, 'changed': 1, 'sentence': '1 of 4 waiting faxes will go differently.'}
        if path == '/routing/holds':
            return 200, {'holds': self.holds}
        if path.startswith('/routing/holds/'):
            hold = next(item for item in self.holds if item['id'] == path.split('/')[3])
            if body['version'] != hold['version']:
                return 409, {'detail': 'Someone else decided on this fax meanwhile.'}
            if path.endswith('/check-again'):
                return 200, {**hold, 'sentence': 'Still no route your rules allow. The fax keeps waiting.'}
            return 200, {**hold, 'version': hold['version'] + 1}
        if path.startswith('/routing/faxes/'):
            return 200, {'job_id': path.split('/')[3], 'sentence': "Sent by Sinch (UK) because the rule 'UK numbers go "
                                                                   "through Sinch' matched. Organization rules version 1.",
                         'attempts': [{'number': 1, 'account_label': 'Sinch (UK)', 'dialed_number': '+448005550100',
                                       'alternate': {'original_number': '+442071234567', 'approved_by': 'Jane Smith',
                                                     'approved_on': '2026-10-07', 'note': 'same intake, confirmed by phone',
                                                     'recipient_pays': True},
                                       'page_layout': 'As the receiving machine allows', 'sentence': 'Delivered.',
                                       'estimate': {'currency': 'USD', 'amount': '0.031'}}], 'hold': None}
        if path == '/admin/providers/accounts' and method == 'GET':
            return 200, self.accounts
        if path == '/admin/providers/accounts' and method == 'POST':
            if body['expected_generation'] != self.accounts['generation']:
                return 409, {'detail': 'Settings changed; reload before applying edits.'}
            added = {**{key: body[key] for key in ('key', 'provider', 'label', 'site', 'sends', 'receives', 'numbers',
                                                   'limits', 'settings')},
                     'primary': False, 'enabled': True, 'health': {'state': 'waiting', 'sentence': 'Waiting.'},
                     'webhook_address': f"https://fax.example/{body['provider']}-inbound/{body['key']}",
                     'secrets_set': sorted(body['credentials'])}
            self.accounts['accounts'].append(added)
            self.accounts['generation'] += 1
            return 200, self.accounts
        if path.startswith('/admin/providers/accounts/') and path.endswith('/health'):
            return 200, {'key': path.split('/')[4], 'state': 'failing', 'sentence': 'Sinch refused its access key.',
                         'details': ['Check the access key in your Sinch project, then save it here again.']}
        if path.startswith('/admin/providers/accounts/') and method == 'PATCH':
            self.accounts['generation'] += 1
            return 200, self.accounts
        return 404, {'detail': 'Not Found'}

    def client(self):
        def handler(request):
            status, payload = self.handle(request)
            return httpx.Response(status) if payload is None else httpx.Response(status, json=payload)
        return httpx.Client(base_url=ORIGIN, transport=httpx.MockTransport(handler))

    def __call__(self, *args, input=None):
        client = self.client()
        return CliRunner().invoke(ROOT, ['--url', ORIGIN, '--key', KEY, *[str(arg) for arg in args]], input=input,
                                  obj={'client_factory': lambda address, timeout: (client, False)},
                                  env={'COLUMNS': '220', 'TZ': 'UTC'})

    def sent(self, method, path):
        return [body for verb, where, _, body in self.requests if verb == method and where == path]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(output, 'home_currency', lambda: 'USD')
    monkeypatch.setattr(rules_module, 'home_currency', lambda: 'USD')
    return FakeServer()


def flat(result):
    return ' '.join(result.stdout.split())


# -- sentences -------------------------------------------------------------------------------------------

def test_rules_read_as_the_same_sentences_as_the_console(monkeypatch):
    monkeypatch.setattr(output, 'home_currency', lambda: 'USD')
    monkeypatch.setattr(rules_module, 'home_currency', lambda: 'USD')
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    names = rules_module.Names(fixture['names'])
    for case in fixture['cases']:
        assert rules_module.rule_sentence(case['rule'], names) == case['sentence']
    for case in fixture['receiving_cases']:
        assert rules_module.receiving_sentence(case['rule'], names, fixture['connectors']) == case['sentence']
    # The console's rule editor offers a condition for each of these (providerRules.test.tsx).
    assert list(rules_module.CONDITION_FIELDS) == fixture['cli_condition_fields']
    # Every action and route setting the console's editor writes, the command can write too.
    common = dict(use=None, try_order=None, cheapest=None, site_accounts=None, in_order=False, automatic=False, never=None,
                  require_direct=False, require_encryption=False, cap=None, approval=False, separate_approver=False,
                  send_days=None, send_between=None, real_call_always=False, when_busy=None, pages_per_sheet=None,
                  alternate=None, choices=CHOICES, document=ACTIVE)
    written = set()
    for options in ({'use': 'sip', 'when_busy': 'next', 'pages_per_sheet': 'one', 'alternate': 'only'},
                    {'try_order': ['sip']}, {'cheapest': ['sip']}, {'site_accounts': 'sender', 'in_order': True},
                    {'automatic': True},
                    {'never': ['humblefax'], 'require_direct': True, 'require_encryption': True, 'cap': '0.5',
                     'separate_approver': True, 'send_days': 'mon-fri', 'send_between': '18:00-07:00',
                     'real_call_always': True}):
        then, _ = rules_module.actions_from(**{**common, **options})
        written |= set(then)
    assert sorted(written) == sorted(fixture['action_keys'])
    # Without a name from the fixture, countries are named as the console's browser names them.
    assert rules_module.Names().country('GB') == 'United Kingdom'
    assert rules_module.country_name('CI') == 'Côte d’Ivoire' and rules_module.country_name('QQ') == 'QQ'


# -- editing a draft ------------------------------------------------------------------------------------

def test_add_update_move_switch_and_remove_rules_in_the_draft(fake):
    added = fake('providers', 'rules', 'add', 'Big faxes need approval', '--when', 'pages-over=20', '--separate-approver')
    assert added.exit_code == 0, added.stdout
    assert "Limit 'Big faxes need approval' added: When the fax has more than 20 pages, hold the fax for approval by " \
           'someone other than the sender.' in flat(added)
    first = fake.sent('PUT', '/routing/rules/draft')[0]
    assert first['expected_version'] == 0
    assert first['document']['limits'] == [{'id': 'l-big-faxes-need-approval', 'name': 'Big faxes need approval',
                                            'on': True, 'when': {'document': {'pages_over': 20}},
                                            'then': {'hold_for_approval': {'separate_approver': True}}}]
    # The draft started from the published rules, so the existing routing rule is kept.
    assert [rule['id'] for rule in first['document']['routes']] == ['r-uk']

    routed = fake('providers', 'rules', 'add', 'Leeds legal faxes', '--when', 'from-site=leeds',
                  '--when', 'label=legal', '--when', 'from-mailbox=Leeds intake', '--when', 'days=mon-fri',
                  '--when', 'between=18:00-07:00', '--unless', 'own-number=yes', '--site-accounts', 'sender',
                  '--in-order', '--when-busy', 'next', '--pages-per-sheet', 'as-allowed', '--alternate', 'never',
                  '--before', 'r-uk')
    assert routed.exit_code == 0, routed.stdout
    second = fake.sent('PUT', '/routing/rules/draft')[1]
    assert second['expected_version'] == 1
    rule = second['document']['routes'][0]
    assert rule['when'] == {'sender': {'sites': ['leeds'], 'mailboxes': ['m-leeds']}, 'labels': ['legal'],
                            'time': {'days': ['mon', 'tue', 'wed', 'thu', 'fri'], 'from': '18:00', 'until': '07:00'}}
    assert rule['unless'] == {'destination': {'own_number': True}}
    assert rule['then'] == {'site_accounts': 'sender', 'mode': 'ordered', 'when_busy': 'next',
                            'page_layout': 'as_receiver_allows', 'alternate_number': 'never'}

    updated = fake('providers', 'rules', 'update', 'UK numbers go through Sinch', '--cheapest', 'Sinch (UK)',
                   '--cheapest', 'humblefax', '--cap', '0.50')
    assert updated.exit_code == 0, updated.stdout
    assert 'use the cheapest reliable of Sinch (UK) and HumbleFax and use only routes that cost at most $0.50' \
        in flat(updated)
    moved = fake('providers', 'rules', 'move', 'r-leeds-legal-faxes', '--to-end')
    assert moved.exit_code == 0 and "Rule 'Leeds legal faxes' is now number 2." in flat(moved)
    assert fake('providers', 'rules', 'disable', 'r-uk').exit_code == 0
    assert fake.sent('PUT', '/routing/rules/draft')[-1]['document']['routes'][0]['on'] is False
    removed = fake('providers', 'rules', 'remove', 'Big faxes need approval')
    assert removed.exit_code == 0 and fake.sent('PUT', '/routing/rules/draft')[-1]['document']['limits'] == []

    listed = fake('providers', 'rules', 'list')
    shown = flat(listed)
    assert 'You have changes that are not published yet.' in shown
    assert 'Everything else: the cheapest reliable route, as before.' in shown
    assert 'r-leeds-legal-faxes' in shown and '12' in shown


def test_wrong_names_and_conditions_are_refused_before_anything_is_sent(fake):
    for args, message in [
        (('--when', 'to-country'), 'Write each condition as FIELD=VALUE'),
        (('--when', 'colour=blue'), 'Write each condition as FIELD=VALUE'),
        (('--when', 'from-group=Nobody'), "No group is called 'Nobody'."),
        (('--when', 'days=someday'), 'Write days as mon-fri'),
        (('--when', 'between=18:00'), 'Write a time window as 18:00-07:00'),
        (('--use', 'sip', '--try', 'humblefax'), 'Give a rule one way to send'),
        ((), 'Say what the rule does'),
    ]:
        result = fake('providers', 'rules', 'add', 'Broken', *args)
        assert result.exit_code != 0, args
        assert message in ' '.join((result.stdout + result.stderr).split()), (args, result.stdout, result.stderr)
    assert fake.sent('PUT', '/routing/rules/draft') == []
    missing = fake('providers', 'rules', 'update', 'No such rule', '--use', 'sip')
    assert missing.exit_code == 5 and "No rule is called 'No such rule'." in missing.stderr


def test_a_stale_draft_and_a_stale_publish_say_so_in_one_sentence(fake):
    assert fake('providers', 'rules', 'add', 'No HumbleFax', '--never', 'humblefax').exit_code == 0
    fake.draft_race = True
    result = fake('providers', 'rules', 'enable', 'r-uk')
    assert result.exit_code == 6
    assert result.stderr.strip() == 'Someone else changed these rules. Reload them and make your change again.'
    published = fake('providers', 'rules', 'publish', '--note', 'No HumbleFax')
    assert published.exit_code == 0, published.stderr
    assert fake.sent('POST', '/routing/rules/publish') == [
        {'expected_active_revision': 1, 'expected_draft_version': 2, 'note': 'No HumbleFax'}]
    assert 'Version 2 is in effect for new faxes.' in flat(published)
    nothing = fake('providers', 'rules', 'publish', '--note', 'again')
    assert nothing.exit_code == 1 and 'There is no draft to publish.' in nothing.stderr
    assert fake('providers', 'rules', 'disable', 'r-uk').exit_code == 0
    fake.publish_race = True
    stale = fake('providers', 'rules', 'publish', '--note', 'Late')
    assert stale.exit_code == 6
    assert stale.stderr.strip() == 'Someone published other rules meanwhile. Reload them and check again.'


def test_check_history_diff_restore_and_discard(fake):
    checked = fake('providers', 'rules', 'check', '--replay', '500')
    assert checked.exit_code == 0
    assert fake.sent('POST', '/routing/rules/draft/check') == [{'replay': 500}]
    shown = flat(checked)
    assert "Worth a look: The rule 'UK numbers go through Sinch' matched no fax in the last 30 days." in shown
    assert '12 of your last 200 faxes would go differently. 3 of them were sent before rules existed' in shown
    history = fake('providers', 'rules', 'history')
    assert 'First rules' in flat(history) and 'Ada Admin' in flat(history)
    diff = fake('providers', 'rules', 'diff', '1', '2')
    assert 'Added Limit No HumbleFax For every fax, never use HumbleFax.' in flat(diff)
    restored = fake('providers', 'rules', 'restore', '1')
    assert restored.exit_code == 0 and 'Version 1 is now your draft.' in flat(restored)
    declined = fake('providers', 'rules', 'discard', input='n\n')
    assert declined.exit_code == 1 and fake.sent('DELETE', '/routing/rules/draft') == []
    assert fake('providers', 'rules', 'discard', '--yes').exit_code == 0
    assert fake.scopes['organization']['draft'] is None


def test_a_check_with_errors_exits_1(fake, monkeypatch):
    original = fake.handle

    def with_errors(request):
        if request.url.path == '/routing/rules/draft/check':
            fake.requests.append((request.method, request.url.path, {}, None))
            return 200, {'errors': [{'message': "A rule names the account 'nowhere', which does not exist."}],
                         'warnings': [], 'replay': None}
        return original(request)
    monkeypatch.setattr(fake, 'handle', with_errors)
    result = fake('providers', 'rules', 'check')
    assert result.exit_code == 1
    assert "Needs fixing: A rule names the account 'nowhere', which does not exist." in flat(result)


def test_explain_sends_the_fax_facts_and_reads_the_answer(fake):
    result = fake('providers', 'rules', 'explain', '--to', '+442071234567', '--pages', '3', '--as', 'Ada Admin',
                  '--mailbox', 'leeds intake', '--label', 'legal', '--urgent', '--at', '2026-10-07 18:30', '--draft')
    assert result.exit_code == 0, result.stderr
    assert fake.sent('POST', '/routing/explain') == [{
        'to': '+442071234567', 'pages': 3, 'size_bytes': None, 'as': 'p-ada', 'mailbox': 'm-leeds', 'workflow': None,
        'urgent': True, 'real_call': False, 'labels': ['legal'], 'at': '2026-10-07T18:30', 'source': 'draft',
        'scope': 'organization'}]
    shown = flat(result)
    assert "Sinch (UK) first, because the rule 'UK numbers go through Sinch' matched." in shown
    assert '$0.031' in shown and 'Leeds office' in shown and 'Pages per sheet: as the receiving machine allows.' in shown
    both = fake('providers', 'rules', 'explain', '--to', '+15550100', '--draft', '--revision', '1')
    assert both.exit_code == 1 and 'Choose --draft or --revision, not both.' in both.stderr
    when = fake('providers', 'rules', 'explain', '--to', '+15550100', '--at', 'tonight')
    assert when.exit_code == 1 and "Write the time as 2026-10-07 18:30, in this installation's time zone." in when.stderr


def test_mailbox_scope_by_name_and_apply_to_waiting(fake):
    result = fake('providers', 'rules', 'add', 'Leeds uses Sinch', '--scope', 'mailbox:Leeds intake', '--use', 'sinch-uk')
    assert result.exit_code == 0, result.stderr
    saved = [params for verb, path, params, _ in fake.requests if verb == 'PUT']
    assert saved == [{'scope': 'mailbox:m-leeds'}]
    bad = fake('providers', 'rules', 'list', '--scope', 'team:x')
    assert bad.exit_code == 1 and 'organization, mailbox:NAME or workflow:KEY' in bad.stderr
    applied = fake('providers', 'rules', 'apply-to-waiting', '--yes')
    assert applied.exit_code == 0 and '1 of 4 waiting faxes will go differently.' in flat(applied)


def test_lists_regions_sites_and_workflows(fake):
    assert fake('providers', 'rules', 'lists', 'set', 'uk-clinics', '--name', 'UK clinics', '--number', '+441782684953',
                '--prefix', '+4420').exit_code == 0
    assert fake('providers', 'rules', 'lists', 'labels', 'legal,clinical', 'billing').exit_code == 0
    document = fake.sent('PUT', '/routing/rules/draft')[-1]['document']
    assert document['lists'] == {'uk-clinics': {'name': 'UK clinics', 'numbers': ['+441782684953'],
                                                'prefixes': ['+4420']}, 'labels': ['legal', 'clinical', 'billing']}
    assert fake('providers', 'rules', 'regions', 'set', 'north', '--name', 'Northern England', '--prefix', '+44113'
                ).exit_code == 0
    assert fake('providers', 'rules', 'sites', 'set', 'leeds', '--name', 'Leeds office', '--country', 'gb',
                '--time-zone', 'Europe/London', '--mailbox', 'Leeds intake', '--group', 'legal').exit_code == 0
    assert fake('providers', 'rules', 'workflows', 'set', 'referrals', '--name', 'Referrals', '--label', 'clinical'
                ).exit_code == 0
    document = fake.sent('PUT', '/routing/rules/draft')[-1]['document']
    assert document['regions'] == {'north': {'name': 'Northern England', 'countries': [], 'prefixes': ['+44113']}}
    assert document['sites'] == [{'key': 'leeds', 'name': 'Leeds office', 'country': 'GB', 'time_zone': 'Europe/London',
                                  'mailboxes': ['m-leeds'], 'groups': ['g-legal']}]
    assert document['workflows'] == [{'key': 'referrals', 'name': 'Referrals', 'labels': ['clinical'], 'mailboxes': []}]
    sites = fake('providers', 'rules', 'sites', 'list')
    assert 'Leeds office' in flat(sites) and 'Sinch (UK)' in flat(sites) and 'Leeds intake' in flat(sites)
    listed = fake('providers', 'rules', 'lists', 'list')
    assert 'Labels: legal, clinical, billing' in flat(listed)
    # A rule can now name them by key or by name.
    added = fake('providers', 'rules', 'add', 'Clinics', '--when', 'to-list=UK clinics', '--when', 'workflow=Referrals',
                 '--when', 'to-region=north', '--use', 'sip')
    assert added.exit_code == 0, added.stderr
    assert fake.sent('PUT', '/routing/rules/draft')[-1]['document']['routes'][-1]['when'] == {
        'destination': {'lists': ['uk-clinics'], 'regions': ['north']}, 'workflows': ['referrals']}
    gone = fake('providers', 'rules', 'regions', 'remove', 'south')
    assert gone.exit_code == 5 and "No region has the key 'south'." in gone.stderr


def test_export_and_import_move_the_whole_draft(fake, tmp_path):
    target = tmp_path / 'rules.json'
    assert fake('providers', 'rules', 'export', '--file', target).exit_code == 0
    document = json.loads(target.read_text())
    assert document == ACTIVE
    document['limits'].append({'id': 'l-x', 'name': 'No HumbleFax', 'on': True, 'when': {},
                               'then': {'never': ['humblefax']}})
    target.write_text(json.dumps(document))
    imported = fake('providers', 'rules', 'import', target)
    assert imported.exit_code == 0 and 'Draft replaced with 2 rules' in flat(imported)
    assert fake.sent('PUT', '/routing/rules/draft')[-1] == {'document': document, 'expected_version': 0}
    piped = fake('providers', 'rules', 'import', '-', input='{"format": 2}')
    assert piped.exit_code == 1 and 'is not JSON written by faxbot providers rules export' in piped.stderr


# -- held faxes ------------------------------------------------------------------------------------------

def test_held_faxes_are_listed_approved_and_refused_with_their_versions(fake):
    held = fake('sent', 'held')
    assert held.exit_code == 0
    assert "Waiting for approval: the rule 'Faxes over 20 pages need approval' matched." in flat(held)
    approved = fake('sent', 'approve', 'f' * 32)
    assert approved.exit_code == 0 and 'Approved. The fax to +15550100001 is no longer held.' in flat(approved)
    assert fake.sent('POST', '/routing/holds/h-1/approve') == [{'version': 3}]
    refused = fake('sent', 'refuse', 'f' * 32, '--reason', 'Wrong recipient')
    assert refused.exit_code == 0 and 'Refused. Nothing was sent to +15550100001.' in flat(refused)
    assert fake.sent('POST', '/routing/holds/h-1/refuse') == [{'version': 3, 'reason': 'Wrong recipient'}]
    fake.holds[0]['can_decide'] = False
    blocked = fake('sent', 'approve', 'f' * 32)
    assert blocked.exit_code == 1 and 'Someone other than the sender must approve this fax.' in blocked.stderr
    missing = fake('sent', 'approve', 'd' * 32)
    assert missing.exit_code == 5 and 'is not waiting for you' in missing.stderr


def test_a_fax_with_no_allowed_route_is_sent_anyway_only_by_an_offered_account(fake):
    asked = fake('sent', 'approve', 'e' * 32)
    assert asked.exit_code == 1
    assert 'Choose an account with --account:' in asked.stderr and 'sip: Telnyx, every line is busy' in asked.stderr
    assert "HumbleFax is not offered: the limit 'Never send UK faxes by HumbleFax' forbids it." in asked.stderr
    refused = fake('sent', 'approve', 'e' * 32, '--account', 'humblefax')
    assert refused.exit_code == 1 and fake.sent('POST', '/routing/holds/h-2/approve') == []
    sent = fake('sent', 'approve', 'e' * 32, '--account', 'sip')
    assert sent.exit_code == 0 and 'Approved. The fax to +442071234567 goes by Telnyx.' in flat(sent)
    assert fake.sent('POST', '/routing/holds/h-2/approve') == [{'version': 1, 'account': 'sip'}]
    wrong = fake('sent', 'approve', 'f' * 32, '--account', 'sip')
    assert wrong.exit_code == 1 and '--account is only for a fax with no route your rules allow.' in wrong.stderr
    again = fake('sent', 'check-again', 'e' * 32)
    assert again.exit_code == 0 and 'Still no route your rules allow. The fax keeps waiting.' in flat(again)
    assert fake.sent('POST', '/routing/holds/h-2/check-again') == [{'version': 1}]


def test_number_rule_options_and_try_a_received_fax(fake):
    result = fake('numbers', 'options', '--account', 'sinch-uk', '--from', '+13035550100', '--from', '+1303*',
                  '--days', 'weekdays', '--between', '18:00-07:00', '--email', 'night inbox', '--urgent',
                  '--keep-days', '30', '--position', '2', '--any-number')
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        'account_key': 'sinch-uk', 'from_numbers': ['+13035550100', '+1303*'], 'days': ['mon', 'tue', 'wed', 'thu', 'fri'],
        'start_minute': 1080, 'end_minute': 420, 'email_connector_id': 'c-night', 'email_off': False, 'urgent': True,
        'keep_days': 30, 'position': 2, 'any_number': True}
    quiet = fake('numbers', 'options', '--no-email', '--not-urgent')
    assert json.loads(quiet.stdout) == {'email_connector_id': None, 'email_off': True, 'urgent': False}
    both = fake('numbers', 'options', '--email', 'Night inbox', '--no-email')
    assert both.exit_code == 1 and 'Choose --email CONNECTOR or --no-email, not both.' in both.stderr
    explained = fake('numbers', 'explain', '--to', '+17208565062', '--from', '+13035550100', '--account', 'sip',
                     '--at', '2026-10-07 18:30')
    assert explained.exit_code == 0 and 'It would go to Front desk, marked urgent' in flat(explained)
    assert fake.sent('POST', '/access/inbound-rules/explain') == [
        {'to_number': '+17208565062', 'from_number': '+13035550100', 'account_key': 'sip', 'at': '2026-10-07T18:30'}]


def test_why_this_route(fake):
    result = fake('sent', 'route', 'a' * 32)
    assert result.exit_code == 0
    shown = flat(result)
    assert "Sent by Sinch (UK) because the rule 'UK numbers go through Sinch' matched." in shown
    assert '$0.031 estimate' in shown
    assert ("Attempt 1: Dialed the recipient's approved alternate number +448005550100 instead of +442071234567, "
            "approved by Jane Smith on 2026-10-07 (‘same intake, confirmed by phone’). The recipient pays for calls "
            'to this number.') in shown


# -- accounts --------------------------------------------------------------------------------------------

def test_accounts_list_and_show_never_print_secrets(fake):
    listed = flat(fake('providers', 'accounts', 'list'))
    assert 'Telnyx sip Carrier trunk - Sends (default) and receives (default) on Ready +17208565062' in listed
    assert 'Sinch (UK) sinch-uk Sinch Leeds office Sends and receives on Waiting for the first fax' in listed
    shown = flat(fake('providers', 'accounts', 'show', 'Sinch (UK)'))
    assert 'Give your provider this address https://fax.example/sinch-inbound/sinch-uk' in shown
    assert 'Access key Set' in shown and 'Access secret Not set' in shown and 'Project ID proj-1' in shown
    assert 'Limits $25.00 a day' in shown


def test_adding_an_account_reads_secrets_from_stdin_or_a_hidden_prompt(fake):
    added = fake('providers', 'accounts', 'add', '--provider', 'sinch', '--key', 'sinch-us', '--label', 'Sinch (US)',
                 '--number', '+13035550100', '--at-once', '4', '--daily-limit', '10', '--setting', 'project_id=proj-2',
                 '--secrets-from-stdin', input='api_key=synthetic-key\napi_secret=synthetic-secret\n')
    assert added.exit_code == 0, added.stderr
    body = fake.sent('POST', '/admin/providers/accounts')[0]
    assert body == {'key': 'sinch-us', 'provider': 'sinch', 'label': 'Sinch (US)', 'site': None, 'sends': True,
                    'receives': True, 'numbers': ['+13035550100'],
                    'limits': {'at_once': 4, 'calls_per_second': None, 'daily_limit': {'currency': 'USD', 'amount': '10'}},
                    'settings': {'project_id': 'proj-2'},
                    'credentials': {'api_key': 'synthetic-key', 'api_secret': 'synthetic-secret'},
                    'expected_generation': 7}
    assert 'synthetic-key' not in added.stdout
    assert 'Give Sinch this address for received faxes: https://fax.example/sinch-inbound/sinch-us' in flat(added)
    prompted = fake('providers', 'accounts', 'add', '--provider', 'sip', '--key', 'sip-leeds', '--label', 'Leeds trunk',
                    '--site', 'leeds', '--at-once', '2', '--calls-per-second', '1', '--setting', 'host=sip.gamma.example',
                    input='synthetic-password\n')
    assert prompted.exit_code == 0, prompted.stderr
    assert fake.sent('POST', '/admin/providers/accounts')[1]['credentials'] == {'password': 'synthetic-password'}
    assert 'synthetic-password' not in prompted.stdout
    missing = fake('providers', 'accounts', 'add', '--provider', 'sinch', '--key', 'sinch-x', '--secrets-from-stdin',
                   input='')
    assert missing.exit_code == 1 and 'Sinch needs these settings too: Project ID.' in missing.stderr
    cannot = fake('providers', 'accounts', 'add', '--provider', 'humblefax', '--key', 'hf-2', '--receives')
    assert cannot.exit_code == 1 and 'HumbleFax cannot receive faxes' in cannot.stderr
    unknown = fake('providers', 'accounts', 'add', '--provider', 'sinch', '--key', 'sinch-y', '--setting', 'project_id=p',
                   '--secrets-from-stdin', input='token=abc\n')
    assert unknown.exit_code == 1 and 'Sinch has no secret called token.' in unknown.stderr
    # A secret never comes from the command line, where the shell keeps it in its history.
    for args in (('add', '--provider', 'sinch', '--key', 'sinch-z', '--setting', 'project_id=p', '--setting',
                  'api_key=synthetic-leak'), ('update', 'sinch-uk', '--setting', 'api_secret=synthetic-leak')):
        leaked = fake('providers', 'accounts', *args)
        assert leaked.exit_code == 1, args
        assert 'is a secret, so Faxbot asks for it instead of reading it from the command line.' in leaked.stderr
    typo = fake('providers', 'accounts', 'update', 'sinch-uk', '--setting', 'projectid=p')
    assert typo.exit_code == 1 and 'Sinch has no setting called projectid. Its settings are: project_id.' in typo.stderr
    assert len(fake.sent('POST', '/admin/providers/accounts')) == 2
    assert fake.sent('PATCH', '/admin/providers/accounts/sinch-uk') == []


def test_switching_accounts_and_defaults(fake):
    refused = fake('providers', 'accounts', 'disable', 'sip')
    assert refused.exit_code == 1 and 'Telnyx is the default sending account.' in refused.stderr
    assert fake('providers', 'accounts', 'disable', 'sinch-uk').exit_code == 0
    assert fake('providers', 'accounts', 'default-sending', 'sinch-uk').exit_code == 0
    assert fake('providers', 'accounts', 'update', 'sinch-uk', '--daily-limit', 'none', '--no-site').exit_code == 0
    assert fake.sent('PATCH', '/admin/providers/accounts/sinch-uk') == [
        {'enabled': False, 'expected_generation': 7}, {'default_sending': True, 'expected_generation': 8},
        {'site': None, 'limits': {'at_once': None, 'calls_per_second': None, 'daily_limit': None},
         'expected_generation': 9}]
    nothing = fake('providers', 'accounts', 'update', 'sinch-uk')
    assert nothing.exit_code == 1 and 'Nothing to change.' in nothing.stderr
    health = flat(fake('providers', 'accounts', 'health', 'sinch-uk'))
    assert 'Sinch (UK): Failing. Sinch refused its access key.' in health
    assert 'Check the access key in your Sinch project' in health
