"""The optional prose proposal cannot change executable or external source."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

MODULE = Path(__file__).resolve().parents[1] / 'validate_doc_patch.py'
spec = importlib.util.spec_from_file_location('validate_doc_patch', MODULE)
patch_scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch_scope)


def git(*args):
    return subprocess.check_output(['git', *args])


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git('init', '-q')
    git('config', 'user.email', 'synthetic@example.invalid')
    git('config', 'user.name', 'Synthetic Docs Test')
    for name, content in [('docs/guide.md', 'Original guide\n'), ('api/runtime.py', 'original runtime\n')]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git('add', '.')
    git('commit', '-qm', 'Base')
    return tmp_path


def proposal(repository):
    path = repository / 'proposal.patch'
    path.write_bytes(git('diff', '--cached', '--binary'))
    git('reset', '--hard', '-q', 'HEAD')
    return str(path)


def test_rejects_backend_deletion_hidden_in_docs_rename(repository):
    git('mv', 'api/runtime.py', 'docs/runtime.md')
    patch = proposal(repository)
    before = git('status', '--porcelain')
    with pytest.raises(ValueError, match='scope'):
        patch_scope.validate(patch)
    assert git('status', '--porcelain') == before
    assert (repository / 'api/runtime.py').read_text() == 'original runtime\n'


@pytest.mark.parametrize('kind', ['symlink', 'executable'])
def test_rejects_markdown_name_with_non_document_mode(repository, kind):
    path = repository / 'docs/guide.md'
    if kind == 'symlink':
        path.unlink()
        path.symlink_to('../api/runtime.py')
    else:
        path.chmod(0o755)
    git('add', 'docs/guide.md')
    patch = proposal(repository)
    with pytest.raises(ValueError, match='scope'):
        patch_scope.validate(patch)


def test_accepts_regular_prose_edits_additions_and_deletions_without_mutation(repository):
    (repository / 'docs/guide.md').unlink()
    (repository / 'docs/new guide.md').write_text('New instructions\n')
    git('add', 'docs')
    patch = proposal(repository)
    before = git('status', '--porcelain')
    patch_scope.validate(patch)
    assert git('status', '--porcelain') == before


def test_actual_index_rejects_extra_non_docs_staged_change(repository):
    (repository / 'api/runtime.py').write_text('unexpected staged runtime change\n')
    git('add', 'api/runtime.py')
    with pytest.raises(ValueError, match='scope'):
        patch_scope.validate_staged()


@pytest.mark.parametrize('name', [
    'api/notes.md', 'docs/generated/notes.md', 'docs/superpowers/notes.md',
    'docs/modernization/notes.md', 'docs/AGENTS.md', 'docs/CLAUDE.md', 'docs/SKILL.md',
])
def test_rejects_non_instructional_markdown_targets(repository, name):
    path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('Not an instructional prose change\n')
    git('add', name)
    patch = proposal(repository)
    with pytest.raises(ValueError, match='scope'):
        patch_scope.validate(patch)
