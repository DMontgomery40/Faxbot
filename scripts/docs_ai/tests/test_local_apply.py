"""Local --apply enforces the same prose boundary as the reviewed CI workflow."""
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'generate_docs_from_diff.py'
ORIGINAL = 'Original retained content\n'
REPLACEMENT = 'Proposed replacement\n'


def git(repository, *arguments):
    return subprocess.check_output(['git', *arguments], cwd=repository)


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'repository'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'Synthetic Docs Test')
    git(repo, 'config', 'user.email', 'docs-test@example.invalid')
    git(repo, 'config', 'core.hooksPath', str(repo / '.git/hooks-disabled'))
    for name in ('README.md', 'AGENTS.md', 'planning/enterprise-correspondence.md',
                 'docs/architecture/design.md', 'docs/guide.md'):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ORIGINAL)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Synthetic source')
    return repo


def proposal(repository, names):
    for name in names:
        (repository / name).write_text(REPLACEMENT)
    patch = git(repository, 'diff', '--binary').decode()
    git(repository, 'restore', '--worktree', '--', *names)
    return patch


def run_local_apply(repository, patch):
    response = repository.parent / 'llm-response.patch'
    response.write_text(patch)
    # Stub only the external LLM call. The command parser, plan generation,
    # patch validator and Git application all execute their production code.
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
sys.argv = [sys.argv[1], '--base', 'HEAD', '--llm', 'openai', '--apply']
module.main()
"""
    return subprocess.run([sys.executable, '-c', runner, str(SCRIPT), str(repository), str(response)],
                          cwd=repository, capture_output=True, text=True)


@pytest.mark.parametrize('names', [
    ('README.md',),
    ('AGENTS.md',),
    ('planning/enterprise-correspondence.md',),
    ('docs/architecture/design.md',),
    ('docs/guide.md', 'planning/enterprise-correspondence.md'),
])
def test_local_apply_rejects_protected_changes_before_mutating_checkout(repository, names):
    patch = proposal(repository, names)
    before_index = (repository / '.git/index').read_bytes()
    result = run_local_apply(repository, patch)
    assert result.returncode != 0, result.stdout + result.stderr
    assert (repository / '.git/index').read_bytes() == before_index
    assert git(repository, 'diff', 'HEAD', '--') == b''
    assert all((repository / name).read_text() == ORIGINAL for name in names)
    assert (repository / 'mkdocs-docs-llm.patch').read_text() == patch


def test_local_apply_rejects_malformed_patch_with_nonzero_exit(repository):
    before_index = (repository / '.git/index').read_bytes()
    result = run_local_apply(repository, 'This is not a unified diff.\n')
    assert result.returncode != 0, result.stdout + result.stderr
    assert (repository / '.git/index').read_bytes() == before_index
    assert git(repository, 'diff', 'HEAD', '--') == b''


def test_local_apply_stages_an_ordinary_document_edit(repository):
    patch = proposal(repository, ('docs/guide.md',))
    result = run_local_apply(repository, patch)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repository / 'docs/guide.md').read_text() == REPLACEMENT
    assert git(repository, 'show', ':docs/guide.md').decode() == REPLACEMENT
    assert git(repository, 'diff', '--cached', '--name-only').decode() == 'docs/guide.md\n'
    assert git(repository, 'diff', '--') == b''
