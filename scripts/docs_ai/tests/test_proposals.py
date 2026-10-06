"""Docs Autopilot proposal providers: exact requests, no API keys for Codex, one-sentence failures.

Nothing here reaches a model: subprocess and the HTTP opener are replaced.
"""
from importlib import import_module
import io
import json
import subprocess
from types import SimpleNamespace
import urllib.error

import pytest

SECRET = 'sk-or-synthetic-test-value'


@pytest.fixture
def autopilot(monkeypatch):
    module = import_module('scripts.docs_ai.generate_docs_from_diff')
    for name in ('DOCS_AI_MODEL', 'DOCS_AI_REASONING_EFFORT', 'OPENROUTER_API_KEY', 'OPENAI_API_KEY',
                 'ANTHROPIC_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    return module


class FakeCodex:
    """subprocess.run for `codex login status` and `codex exec`, recording each call."""

    def __init__(self, *, signed_in=True, reply='', exit_status=0, timeout=False, status_text='Logged in using ChatGPT'):
        self.calls = []
        self.signed_in, self.reply, self.exit_status = signed_in, reply, exit_status
        self.timeout, self.status_text = timeout, status_text

    def __call__(self, command, **options):
        self.calls.append((command, options))
        if command[:3] == ['codex', 'login', 'status']:
            return SimpleNamespace(returncode=0 if self.signed_in else 1,
                                   stdout='', stderr=self.status_text if self.signed_in else 'Not logged in')
        if self.timeout:
            raise subprocess.TimeoutExpired(command, options.get('timeout'))
        output = command[command.index('-o') + 1]
        if self.exit_status == 0:
            with open(output, 'w', encoding='utf-8') as handle:
                handle.write(self.reply)
        return SimpleNamespace(returncode=self.exit_status, stdout='', stderr='')


def _codex(monkeypatch, autopilot, fake, installed=True):
    monkeypatch.setattr(autopilot.shutil, 'which', lambda name: '/usr/local/bin/codex' if installed else None)
    monkeypatch.setattr(autopilot.subprocess, 'run', fake)


# -- codex -----------------------------------------------------------------------------------------

def test_codex_runs_read_only_on_the_person_sign_in_with_the_prompt_on_stdin(autopilot, monkeypatch):
    fake = FakeCodex(reply='diff --git a/docs/a.md b/docs/a.md\n')
    _codex(monkeypatch, autopilot, fake)
    monkeypatch.setenv('OPENROUTER_API_KEY', SECRET)
    assert autopilot.call_llm('the prompt', 'codex') == 'diff --git a/docs/a.md b/docs/a.md\n'
    (status, _), (command, options) = fake.calls
    assert status == ['codex', 'login', 'status']
    output = command[command.index('-o') + 1]
    assert command == ['codex', 'exec', '-m', 'gpt-6-luna', '-s', 'read-only', '--ephemeral', '--ignore-user-config',
                       '--skip-git-repo-check', '-C', str(autopilot.ROOT), '-c', 'model_reasoning_effort="medium"',
                       '-o', output, '-']
    assert options['input'] == 'the prompt' and options['timeout'] == 15 * 60
    # No API key is passed to Codex in its arguments.
    assert not any(SECRET in part for part in command)


def test_codex_model_and_effort_follow_the_environment(autopilot, monkeypatch):
    fake = FakeCodex(reply='')
    _codex(monkeypatch, autopilot, fake)
    monkeypatch.setenv('DOCS_AI_MODEL', 'gpt-6-luna-mini')
    monkeypatch.setenv('DOCS_AI_REASONING_EFFORT', 'high')
    autopilot.call_codex('prompt')
    command = fake.calls[1][0]
    assert command[command.index('-m') + 1] == 'gpt-6-luna-mini'
    assert 'model_reasoning_effort="high"' in command
    monkeypatch.setenv('DOCS_AI_REASONING_EFFORT', 'high" sandbox="danger')
    with pytest.raises(autopilot.ProposalError, match='must be one word'):
        autopilot.call_codex('prompt')


@pytest.mark.parametrize('case, message', [
    ('missing', 'The Codex CLI is not installed; install it and run codex login, then try again.'),
    ('signed_out', 'Codex is not signed in; run codex login, then try again.'),
    ('timeout', 'Codex did not finish within 15 minutes; nothing was written.'),
    ('failed', 'Codex stopped with exit status 2; nothing was written.'),
])
def test_codex_failures_are_one_sentence(autopilot, monkeypatch, case, message):
    fake = FakeCodex(signed_in=case != 'signed_out', timeout=case == 'timeout', exit_status=2 if case == 'failed' else 0)
    _codex(monkeypatch, autopilot, fake, installed=case != 'missing')
    with pytest.raises(autopilot.ProposalError) as raised:
        autopilot.call_codex('prompt')
    assert str(raised.value) == message
    if case in {'missing', 'signed_out'}:
        assert not any(call[0][:2] == ['codex', 'exec'] for call in fake.calls)


def test_codex_status_must_say_logged_in(autopilot, monkeypatch):
    fake = FakeCodex(status_text='Using an API key')
    _codex(monkeypatch, autopilot, fake)
    with pytest.raises(autopilot.ProposalError, match='not signed in'):
        autopilot.call_codex('prompt')


# -- openrouter ---------------------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _openrouter(monkeypatch, autopilot, payload=None, error=None):
    sent = []

    def urlopen(request, timeout):
        sent.append((request, timeout))
        if error is not None:
            raise error
        return FakeResponse(json.dumps(payload).encode('utf-8'))
    monkeypatch.setattr(autopilot.urllib.request, 'urlopen', urlopen)
    return sent


def _reply(content, finish='stop'):
    return {'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': finish}]}


@pytest.mark.parametrize('model', ['openai/gpt-6-luna', 'anthropic/claude-sonnet-5.5'])
def test_openrouter_request_body_has_model_reasoning_and_no_temperature(autopilot, monkeypatch, model):
    monkeypatch.setenv('OPENROUTER_API_KEY', SECRET)
    if model != 'openai/gpt-6-luna':
        monkeypatch.setenv('DOCS_AI_MODEL', model)
    sent = _openrouter(monkeypatch, autopilot, _reply('diff --git a/docs/a.md b/docs/a.md\n'))
    assert autopilot.call_llm('the prompt', 'openrouter') == 'diff --git a/docs/a.md b/docs/a.md\n'
    (request, timeout), = sent
    assert request.full_url == 'https://openrouter.ai/api/v1/chat/completions' and request.get_method() == 'POST'
    body = json.loads(request.data)
    assert body == {'model': model, 'max_tokens': 16000, 'reasoning': {'effort': 'medium'},
                    'messages': [{'role': 'system', 'content': autopilot.SYSTEM_PROMPT},
                                 {'role': 'user', 'content': 'the prompt'}],
                    # The read-only tools are offered on every request (OpenRouter validates them each time).
                    'tools': autopilot.TOOL_SPECS, 'tool_choice': 'auto'}
    assert 'temperature' not in body and 'reasoning_effort' not in body
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers['authorization'] == 'Bearer ' + SECRET
    assert headers['x-title'] == 'Faxbot Docs Autopilot'
    assert headers['http-referer'] == 'https://github.com/DMontgomery40/Faxbot'
    assert 0 < timeout <= 15 * 60
    assert model in autopilot.OPENROUTER_MODELS


def test_openrouter_needs_its_key_and_never_echoes_it(autopilot, monkeypatch):
    with pytest.raises(autopilot.ProposalError) as missing:
        autopilot.call_openrouter('prompt')
    assert str(missing.value) == ('OPENROUTER_API_KEY is not set; add it as a repository secret, or run locally '
                                  'with --llm codex.')
    monkeypatch.setenv('OPENROUTER_API_KEY', SECRET)
    refused = urllib.error.HTTPError('https://openrouter.ai', 401, 'Unauthorized', {}, io.BytesIO(SECRET.encode()))
    _openrouter(monkeypatch, autopilot, error=refused)
    with pytest.raises(autopilot.ProposalError) as raised:
        autopilot.call_openrouter('prompt')
    assert str(raised.value) == 'OpenRouter refused the request (HTTP 401); nothing was written.'
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize('payload, finish', [
    (_reply('partial diff', 'content_filter'), 'content_filter'),
    (_reply('', 'length'), 'length'),
    (_reply(None, 'error'), 'error'),
    (_reply('   ', 'stop'), 'stop'),
])
def test_openrouter_unusable_replies_are_one_sentence(autopilot, monkeypatch, payload, finish):
    monkeypatch.setenv('OPENROUTER_API_KEY', SECRET)
    _openrouter(monkeypatch, autopilot, payload)
    with pytest.raises(autopilot.ProposalError) as raised:
        autopilot.call_openrouter('prompt')
    assert str(raised.value) == f'OpenRouter returned no usable text (finish reason: {finish}); nothing was written.'


def test_openrouter_error_body_and_unreachable_service(autopilot, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', SECRET)
    _openrouter(monkeypatch, autopilot, {'error': {'code': 402, 'message': 'Insufficient credits'}})
    with pytest.raises(autopilot.ProposalError, match='reported an error'):
        autopilot.call_openrouter('prompt')
    _openrouter(monkeypatch, autopilot, error=urllib.error.URLError('offline'))
    with pytest.raises(autopilot.ProposalError, match='could not be reached'):
        autopilot.call_openrouter('prompt')


# -- reply handling -------------------------------------------------------------------------------------

def test_unknown_provider_and_diff_extraction(autopilot):
    with pytest.raises(autopilot.ProposalError, match='use codex or openrouter'):
        autopilot.call_llm('prompt', 'openai')
    diff = 'diff --git a/docs/a.md b/docs/a.md\n--- a/docs/a.md\n+++ b/docs/a.md\n'
    assert autopilot.extract_diff('Sure.\n```diff\n' + diff + '```\nDone.') == diff
    assert autopilot.extract_diff(diff.rstrip('\n')) == diff
    assert autopilot.extract_diff('No changes are needed.') is None
    assert autopilot.extract_diff('') is None


def test_plan_mode_never_calls_a_model(autopilot, monkeypatch, tmp_path):
    monkeypatch.setattr(autopilot, 'ROOT', tmp_path)
    monkeypatch.setattr(autopilot, 'build_plan', lambda base: '# plan')
    monkeypatch.setattr(autopilot, 'call_llm', lambda *args: pytest.fail('plan mode called a model'))
    monkeypatch.setattr('sys.argv', ['generate_docs_from_diff.py', '--base', 'HEAD'])
    autopilot.main()
    assert (tmp_path / 'mkdocs-docs-plan.md').read_text() == '# plan'
