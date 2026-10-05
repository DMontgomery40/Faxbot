"""Docs Autopilot as an independent reviewer: the prompt, read-only tools, findings and the audit.

No model is reached: the HTTP opener and the model call are replaced. Tools run against throwaway
Git repositories so the path guard and the change limit are exercised for real.
"""
from importlib import import_module
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

SECRET = 'sk-or-synthetic-test-value'
SCRIPT = Path(__file__).resolve().parents[1] / 'generate_docs_from_diff.py'
DIFF = ('diff --git a/docs/guide.md b/docs/guide.md\n--- a/docs/guide.md\n+++ b/docs/guide.md\n'
        '@@ -1 +1 @@\n-The port is 4000.\n+The port is 5000.\n')
REPLY = f'```diff\n{DIFF}```\n\n```findings\n- api/app/ports.py:2 The error says 4000 but the code uses 5000.\n```\n'


def git(repository, *arguments):
    return subprocess.check_output(['git', *arguments], cwd=repository, text=True)


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'repository'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'Synthetic Docs Test')
    git(repo, 'config', 'user.email', 'docs-test@example.invalid')
    files = {'docs/guide.md': 'The port is 4000.\n', 'docs/orphan.md': 'Old page.\n',
             'docs/plugins/v3.md': 'Revision internals.\n', 'api/app/ports.py': 'PORT = 4000\n',
             'mkdocs.yml': 'site_name: Synthetic\nnav:\n  - Guide: guide.md\n  - Plugins: plugins/v3.md\nextra_css: []\n',
             'research/notes.md': 'never shown\n', '.local-handoff/brief.md': 'never shown\n'}
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Synthetic base')
    (repo / 'api/app/ports.py').write_text('PORT = 5000\nMESSAGE = "Faxbot listens on port 4000."\n')
    (repo / '.env').write_text(f'OPENROUTER_API_KEY={SECRET}\n')
    git(repo, 'add', 'api/app/ports.py')
    git(repo, 'commit', '-qm', 'Synthetic change')
    return repo


@pytest.fixture
def autopilot(monkeypatch, repository):
    module = import_module('scripts.docs_ai.generate_docs_from_diff')
    for name in ('DOCS_AI_MODEL', 'DOCS_AI_REASONING_EFFORT', 'OPENROUTER_API_KEY', 'DOCS_AI_AUDIT_BATCH'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module, 'ROOT', repository)
    monkeypatch.chdir(repository)
    monkeypatch.setitem(module.REVIEW, 'base', git(repository, 'rev-parse', 'HEAD~1').strip())
    return module


# -- the prompt ---------------------------------------------------------------------------------------

def test_the_prompt_asks_an_independent_reviewer_to_check_claims_against_the_code(autopilot, repository):
    context = autopilot.review_context('HEAD~1')
    prompt = autopilot.proposal_prompt(context)
    base = context['base']
    for instruction in ('independent reviewer and technical writer', 'You did not write this code',
                        f'`git diff {base}..HEAD`', 'code first', 'Check each claim on those pages against the code',
                        'setting names and defaults', 'button and screen labels', 'CLI commands', 'API routes',
                        'Fix every contradiction', 'add a brief explanation',
                        'Do not restate, reword or reorganize text that is already correct',
                        'loose inspiration only', 'never chop explanations into fragments',
                        'Never change docs/generated/, docs/architecture/, README.md, planning/',
                        '.github/ workflows', 'Never open .env files', '.local-handoff/', 'research/',
                        'where the CODE itself looks wrong', 'a setting that has no effect',
                        '```diff', '```findings', 'or the single word none'):
        assert instruction in prompt, instruction
    assert 'api/app/ports.py' in prompt and 'docs/guide.md' in prompt
    # The same prompt goes to Codex and to OpenRouter; only OpenRouter's system message names its tools.
    assert 'read_file, list_dir, git_diff' in autopilot.SYSTEM_PROMPT


# -- read-only tools and their guard ------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['.env', 'api/../.env', '.env.production', '.local-handoff/brief.md',
                                  'research/notes.md', 'faxdata/faxbot.db', 'api/admin_ui/node_modules/x.js',
                                  '.git/config', 'secrets/server.key', 'owner.cred', '../outside.md', '/etc/hosts'])
def test_the_path_guard_refuses_secrets_private_notes_and_anything_outside(autopilot, path):
    with pytest.raises(autopilot.Refused):
        autopilot.guarded_path(path)
    assert autopilot.run_tool(None, 'read_file', json.dumps({'path': path})).startswith('Refused:')


def test_the_tools_read_list_diff_and_search_inside_the_repository(autopilot):
    base = autopilot.REVIEW['base']
    read = autopilot.run_tool(base, 'read_file', json.dumps({'path': 'api/app/ports.py', 'start': 2, 'end': 2}))
    assert read == 'api/app/ports.py lines 2-2 of 2\n    2  MESSAGE = "Faxbot listens on port 4000."'
    listing = autopilot.run_tool(base, 'list_dir', json.dumps({'path': '.'}))
    assert 'docs/' in listing and 'api/' in listing
    assert '.env' not in listing and '.local-handoff' not in listing and 'research' not in listing
    diff = autopilot.run_tool(base, 'git_diff', json.dumps({}))
    assert '+PORT = 5000' in diff and '.env' not in diff
    assert autopilot.run_tool(base, 'git_diff', json.dumps({'paths': ['.env']})).startswith('Refused:')
    found = autopilot.run_tool(base, 'grep', json.dumps({'pattern': 'never shown'}))
    assert found == '(no matches)'  # research/ and .local-handoff/ are tracked here, and still never shown
    assert 'api/app/ports.py:2:' in autopilot.run_tool(base, 'grep', json.dumps({'pattern': 'listens on'}))
    assert autopilot.run_tool(base, 'delete_file', '{}').startswith('Unknown tool')
    assert autopilot.run_tool(base, 'read_file', 'not json').endswith('were not understood.')


def test_tool_results_are_cut_at_the_limit(autopilot, monkeypatch, repository):
    monkeypatch.setattr(autopilot, 'MAX_RESULT_CHARS', 50)
    (repository / 'docs/long.md').write_text('x' * 500)
    result = autopilot.run_tool(None, 'read_file', json.dumps({'path': 'docs/long.md'}))
    assert result.endswith('[... cut at 50 characters; ask for a narrower range ...]\n') and len(result) < 120


# -- the OpenRouter tool loop ---------------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _replies(monkeypatch, autopilot, payloads):
    sent = []

    def urlopen(request, timeout):
        sent.append(json.loads(request.data))
        return FakeResponse(json.dumps(payloads[len(sent) - 1]).encode('utf-8'))
    monkeypatch.setattr(autopilot.urllib.request, 'urlopen', urlopen)
    return sent


def _tool_call(identifier, name, arguments):
    return {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}


def test_a_tool_call_round_trip_then_the_final_diff_and_findings(autopilot, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', SECRET)
    asked = {'choices': [{'finish_reason': 'tool_calls', 'message': {
        'role': 'assistant', 'content': None, 'reasoning_details': [{'type': 'reasoning.text', 'text': 'look'}],
        'tool_calls': [_tool_call('call_1', 'read_file', {'path': 'docs/guide.md'}),
                       _tool_call('call_2', 'read_file', {'path': '.env'})]}}]}
    final = {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': REPLY}}]}
    sent = _replies(monkeypatch, autopilot, [asked, final])
    assert autopilot.call_llm('the prompt', 'openrouter') == REPLY
    first, second = sent
    for body in sent:
        assert body['reasoning'] == {'effort': 'medium'} and 'temperature' not in body
        assert body['tools'] == autopilot.TOOL_SPECS and body['tool_choice'] == 'auto'
    assert first['messages'] == [{'role': 'system', 'content': autopilot.SYSTEM_PROMPT},
                                 {'role': 'user', 'content': 'the prompt'}]
    assistant, tool_1, tool_2 = second['messages'][2:]
    assert assistant['tool_calls'][0]['id'] == 'call_1' and assistant['reasoning_details']
    assert tool_1 == {'role': 'tool', 'tool_call_id': 'call_1',
                      'content': 'docs/guide.md lines 1-1 of 1\n    1  The port is 4000.'}
    assert tool_2['tool_call_id'] == 'call_2' and tool_2['content'].startswith('Refused:')
    assert SECRET not in json.dumps(second)


def test_the_tool_budget_ends_with_a_final_answer_and_tools_turned_off(autopilot, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', SECRET)
    monkeypatch.setattr(autopilot, 'MAX_TOOL_CALLS', 1)
    asked = {'choices': [{'finish_reason': 'tool_calls', 'message': {
        'role': 'assistant', 'content': '', 'tool_calls': [_tool_call('call_1', 'list_dir', {'path': 'docs'})]}}]}
    final = {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': REPLY}}]}
    sent = _replies(monkeypatch, autopilot, [asked, final])
    assert autopilot.call_openrouter('the prompt') == REPLY
    assert sent[1]['tool_choice'] == 'none'
    assert sent[1]['messages'][-1]['content'].startswith('You have used all your tool calls.')


# -- replies: patch, findings, or both ---------------------------------------------------------------------------

def test_findings_are_read_from_their_own_block(autopilot):
    assert autopilot.extract_findings(REPLY) == ['api/app/ports.py:2 The error says 4000 but the code uses 5000.']
    assert autopilot.extract_findings('```findings\nnone\n```') == []
    assert autopilot.extract_findings('```findings\n- None.\n```') == []
    assert autopilot.extract_findings(DIFF) is None
    assert autopilot.extract_diff(REPLY) == DIFF
    assert autopilot.extract_diff('```diff\n```\n```findings\n- a.py:1 --- looks odd.\n```') is None


def _run(repository, reply, *arguments):
    """The command line with only the model call replaced; everything else is the production code."""
    response = repository.parent / 'reply.txt'
    response.write_text(reply)
    runner = """
import importlib.util
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location('autopilot', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ROOT = Path(sys.argv[2])
response = Path(sys.argv[3]).read_text()
module.call_llm = lambda prompt, provider: response
sys.argv = [sys.argv[1], *sys.argv[4:]]
module.main()
"""
    return subprocess.run([sys.executable, '-c', runner, str(SCRIPT), str(repository), str(response), *arguments],
                          cwd=repository, capture_output=True, text=True)


def test_findings_without_a_diff_are_saved_and_count_as_success(repository):
    result = _run(repository, '```diff\n```\n\n```findings\n- api/app/ports.py:2 The message names port 4000.\n```\n',
                  '--base', 'HEAD~1', '--llm', 'codex')
    assert result.returncode == 0, result.stderr
    assert 'proposed no documentation changes; its findings are in mkdocs-docs-findings.md' in result.stdout
    findings = (repository / 'mkdocs-docs-findings.md').read_text()
    assert findings.startswith('## Docs Autopilot findings\n\nDocs Autopilot (gpt-6-luna) reviewed ')
    assert 'It proposed no documentation changes.' in findings
    assert '- api/app/ports.py:2 The message names port 4000.' in findings
    assert not (repository / 'mkdocs-docs-llm.patch').exists()


def test_a_diff_and_findings_are_both_saved(repository):
    result = _run(repository, REPLY, '--base', 'HEAD~1', '--llm', 'codex')
    assert result.returncode == 0, result.stderr
    assert (repository / 'mkdocs-docs-llm.patch').read_text() == DIFF
    findings = (repository / 'mkdocs-docs-findings.md').read_text()
    assert 'It proposed documentation changes (1 files, +1 -1 lines).' in findings
    assert 'The error says 4000 but the code uses 5000.' in findings


def test_a_refused_patch_still_leaves_its_findings(repository):
    outside = DIFF.replace('docs/guide.md', 'README.md')
    (repository / 'README.md').write_text('The port is 4000.\n')
    git(repository, 'add', 'README.md')
    git(repository, 'commit', '-qm', 'Readme')
    result = _run(repository, f'```diff\n{outside}```\n```findings\nnone\n```\n', '--base', 'HEAD~2', '--llm', 'codex')
    assert result.returncode != 0 and 'outside maintained docs Markdown' in result.stderr
    assert 'None found.' in (repository / 'mkdocs-docs-findings.md').read_text()
    assert not (repository / 'mkdocs-docs-llm.patch').exists()


# -- audit -----------------------------------------------------------------------------------------------------------

def test_the_audit_checks_pages_in_batches_against_the_current_code_and_flags_pages_missing_from_nav(repository):
    reply = (f'```diff\n{DIFF}```\n'
             '```findings\n- docs/plugins/v3.md:1 Describes plugin revisions the code no longer has; delete it.\n```\n')
    result = _run(repository, reply, '--audit', '--pages', 'docs/guide.md, docs/orphan.md docs/plugins/*.md')
    assert result.returncode == 0, result.stderr
    assert result.stdout.count('Batch ') == 1  # three pages, four per call
    findings = (repository / 'mkdocs-docs-findings.md').read_text()
    assert findings.startswith('## Docs Autopilot audit\n\nDocs Autopilot (gpt-6-luna) audited 3 maintained pages')
    assert '### Batch 1: docs/guide.md, docs/orphan.md, docs/plugins/v3.md' in findings
    assert '- docs/orphan.md: not in mkdocs.yml nav.' in findings
    assert 'docs/guide.md: not in mkdocs.yml nav' not in findings
    assert 'Describes plugin revisions the code no longer has; delete it.' in findings
    assert (repository / 'mkdocs-docs-llm.patch').read_text() == DIFF


def test_the_audit_prompt_has_no_base_and_asks_for_deletions_and_merges(autopilot):
    prompt = autopilot.audit_prompt(['docs/orphan.md'], ['docs/guide.md', 'docs/orphan.md'], {'docs/orphan.md'},
                                    'f' * 40)
    for instruction in ('independent reviewer', 'against the CURRENT code', 'there is no change under review',
                        'no longer exist in the code: delete them', 'propose merging them',
                        'not in mkdocs.yml\'s nav', '- docs/orphan.md  (not in mkdocs.yml nav)', '```findings'):
        assert instruction in prompt, instruction


def test_audit_batches_accumulate_findings_and_a_failed_batch_is_reported(repository, monkeypatch):
    monkeypatch.setenv('DOCS_AI_AUDIT_BATCH', '1')
    result = _run(repository, 'nothing useful', '--audit', '--pages', 'docs/guide.md docs/orphan.md')
    assert result.returncode == 1
    findings = (repository / 'mkdocs-docs-findings.md').read_text()
    assert findings.count('This batch produced no result') == 2
    assert '### Batch 2: docs/orphan.md' in findings and '- docs/orphan.md: not in mkdocs.yml nav.' in findings
    assert _run(repository, 'x', '--audit', '--pages', 'docs/none/*.md').stderr.strip() == (
        'No maintained page under docs/ matches those patterns.')
