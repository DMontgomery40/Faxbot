"""Exercise artifact reuse and Mike's real Git history against a local remote."""
from importlib import import_module
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]


def hook():
    try:
        return import_module("scripts.docs_ai.use_built_artifact")
    except ModuleNotFoundError:
        pytest.fail("Verified artifact publication hook is missing")


def test_post_build_uses_verified_artifact_instead_of_incidental_rebuild(tmp_path, monkeypatch):
    module = hook()
    artifact = tmp_path / "artifact"
    (artifact / "generated").mkdir(parents=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    (artifact / "generated/provenance.json").write_text(json.dumps({"source_sha": revision, "source_tree_dirty": False}))
    (artifact / "index.html").write_text("verified artifact")
    target = tmp_path / "site"
    target.mkdir()
    (target / "incidental-rebuild.txt").write_text("not the artifact")
    monkeypatch.setenv("FAXBOT_DOCS_BUILT_SITE", str(artifact))
    module.on_post_build({"site_dir": str(target)})
    assert (target / "index.html").read_text() == "verified artifact"
    assert not (target / "incidental-rebuild.txt").exists()


def test_generated_pages_do_not_offer_edit_links_to_nonexistent_source_files():
    module = hook()
    generated = SimpleNamespace(file=SimpleNamespace(src_uri="generated/index.md"), edit_url="invalid-generated-edit-url")
    instructional = SimpleNamespace(file=SimpleNamespace(src_uri="setup/phaxio.md"), edit_url="maintained-page-edit-url")
    module.on_page_markdown("# Generated", generated, {}, [])
    module.on_page_markdown("# Instructions", instructional, {}, [])
    assert generated.edit_url is None
    assert instructional.edit_url == "maintained-page-edit-url"


def run(directory, *arguments, env=None):
    result = subprocess.run(arguments, cwd=directory, env=env, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.stdout.strip()


def test_fresh_clone_publication_preserves_remote_history_and_exact_artifact(tmp_path):
    mike = os.getenv("FAXBOT_DOCS_MIKE") or shutil.which("mike")
    if not mike:
        pytest.skip("Install docs/requirements.txt for real Mike publication proof")
    hook()
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    run(tmp_path, "git", "init", "--bare", "-q", str(origin))
    run(tmp_path, "git", "init", "-q", "-b", "main", str(seed))
    run(seed, "git", "config", "user.name", "Synthetic Docs Test")
    run(seed, "git", "config", "user.email", "docs@example.invalid")
    (seed / "docs").mkdir()
    (seed / "docs/index.md").write_text("# Source rebuild\n")
    (seed / "docs/.gitignore").write_text("/generated/\n")
    (seed / "scripts/docs_ai").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/docs_ai/use_built_artifact.py", seed / "scripts/docs_ai/use_built_artifact.py")
    (seed / "mkdocs.yml").write_text("site_name: Synthetic Docs\nplugins: [mike]\nhooks: [scripts/docs_ai/use_built_artifact.py]\n")
    run(seed, "git", "add", ".")
    run(seed, "git", "commit", "-qm", "source")
    revision = run(seed, "git", "rev-parse", "HEAD")
    run(seed, "git", "remote", "add", "origin", str(origin))
    run(seed, "git", "push", "-q", "origin", "main")
    run(seed, "git", "checkout", "-q", "--orphan", "gh-pages")
    run(seed, "git", "rm", "-qrf", ".")
    (seed / "old-version").mkdir()
    (seed / "old-version/index.html").write_text("historical content must survive")
    (seed / "CNAME").write_text("docs.example.invalid\n")
    (seed / "versions.json").write_text(json.dumps([{"version": "old-version", "title": "Old", "aliases": ["latest"]}]))
    (seed / "latest").symlink_to("old-version")
    run(seed, "git", "add", ".")
    run(seed, "git", "commit", "-qm", "existing publication")
    original_tip = run(seed, "git", "rev-parse", "HEAD")
    run(seed, "git", "push", "-q", "origin", "gh-pages")
    checkout = tmp_path / "fresh-checkout"
    run(tmp_path, "git", "clone", "-q", "--branch", "main", str(origin), str(checkout))
    assert not run(checkout, "git", "branch", "--list", "gh-pages")
    artifact = tmp_path / "verified-artifact"
    (artifact / "generated").mkdir(parents=True)
    (artifact / "index.html").write_text("exact verified artifact")
    provenance = {"source_sha": revision, "source_tree_dirty": False}
    (artifact / "generated/provenance.json").write_text(json.dumps(provenance))
    (checkout / "docs/generated").mkdir()
    (checkout / "docs/generated/provenance.json").write_text(json.dumps(provenance))
    config = yaml.load((ROOT / ".github/workflows/mkdocs-deploy.yml").read_text(), Loader=yaml.BaseLoader)
    publication = next(step["run"] for step in config["jobs"]["publish"]["steps"] if step.get("name", "").startswith("Publish exact"))
    environment = {"PATH": str(Path(mike).parent) + os.pathsep + os.environ["PATH"], "SOURCE_SHA": revision,
                   "FAXBOT_DOCS_BUILT_SITE": str(artifact)}
    run(checkout, "bash", "-e", "-c", publication, env=environment)
    run(checkout, "git", "fetch", "-q", "origin", "gh-pages")
    run(checkout, "git", "merge-base", "--is-ancestor", original_tip, "origin/gh-pages")
    assert run(checkout, "git", "show", "origin/gh-pages:old-version/index.html") == "historical content must survive"
    assert run(checkout, "git", "show", f"origin/gh-pages:{revision}/index.html") == "exact verified artifact"
    assert run(checkout, "git", "show", "origin/gh-pages:CNAME") == "docs.example.invalid"
    # A later source version publishes first; a delayed older run must retain it.
    (checkout / "docs/index.md").write_text("# New source\n")
    run(checkout, "git", "add", "docs/index.md")
    run(checkout, "git", "commit", "-qm", "new source")
    newer_revision = run(checkout, "git", "rev-parse", "HEAD")
    run(checkout, "git", "push", "-q", "origin", "main")
    newer_artifact = tmp_path / "newer-artifact"
    (newer_artifact / "generated").mkdir(parents=True)
    (newer_artifact / "index.html").write_text("newer verified artifact")
    newer_provenance = {"source_sha": newer_revision, "source_tree_dirty": False}
    (newer_artifact / "generated/provenance.json").write_text(json.dumps(newer_provenance))
    (checkout / "docs/generated/provenance.json").write_text(json.dumps(newer_provenance))
    run(checkout, "bash", "-e", "-c", publication,
        env={**environment, "SOURCE_SHA": newer_revision, "FAXBOT_DOCS_BUILT_SITE": str(newer_artifact)})
    run(checkout, "git", "checkout", "-q", "--detach", revision)
    (checkout / "docs/generated/provenance.json").write_text(json.dumps(provenance))
    run(checkout, "bash", "-e", "-c", publication, env=environment)
    run(checkout, "git", "fetch", "-q", "origin", "gh-pages")
    versions = json.loads(run(checkout, "git", "show", "origin/gh-pages:versions.json"))
    assert {entry["version"] for entry in versions} == {"old-version", revision, newer_revision}
    assert next(entry for entry in versions if "latest" in entry["aliases"])["version"] == newer_revision
    assert run(checkout, "git", "show", f"origin/gh-pages:{newer_revision}/index.html") == "newer verified artifact"


def test_authoritative_artifact_build_uses_source_version_for_canonical_urls(tmp_path):
    mkdocs = shutil.which('mkdocs')
    if not mkdocs:
        pytest.skip('Install docs/requirements.txt for the real canonical URL build')
    configuration = yaml.load((ROOT / '.github/workflows/api-docs.yml').read_text(), Loader=yaml.BaseLoader)
    step = next(step for step in configuration['jobs']['build']['steps']
                if step.get('name') == 'Build Redocly API reference and strict MkDocs site')
    version = 'a' * 40
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'MIKE_DOCS_VERSION', 'FAXBOT_DOCS_BUILT_SITE'}}
    for name, value in step.get('env', {}).items():
        environment[name] = value.replace('${{ inputs.source_sha }}', version)
    (tmp_path / 'docs/generated').mkdir(parents=True)
    (tmp_path / 'docs/generated/index.md').write_text('# Generated source\n')
    (tmp_path / 'mkdocs.yml').write_text(
        'site_name: Source docs\nsite_url: https://docs.example.invalid/\nplugins: [mike]\n')
    run(tmp_path, mkdocs, 'build', '--strict', env=environment)
    html = (tmp_path / 'site/generated/index.html').read_text()
    sitemap = (tmp_path / 'site/sitemap.xml').read_text()
    expected = f'https://docs.example.invalid/{version}/generated/'
    assert f'href="{expected}"' in html
    assert f'<loc>{expected}</loc>' in sitemap
