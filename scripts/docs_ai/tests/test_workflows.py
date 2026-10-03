"""Publishing authority stays on main; previews cannot change published latest."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_main_and_refresh_pushes_and_pull_requests_build_without_path_filters():
    config = workflow("mkdocs-deploy.yml")
    assert set(config["on"]["push"]["branches"]) == {"main", "feat/faxbot-refresh"}
    assert "paths" not in config["on"]["push"]
    assert "pull_request" in config["on"]
    assert config["permissions"] == {"contents": "read"}
    publish = config["jobs"]["publish"]
    assert "github.ref == 'refs/heads/main'" in publish["if"]
    assert "github.event_name == 'push'" in publish["if"]
    assert publish["permissions"]["contents"] == "write"
    assert publish["concurrency"]["cancel-in-progress"] == "false"
    script = "\n".join(step.get("run", "") for step in publish["steps"])
    assert '"$SOURCE_SHA"' in script
    assert "git push --force" not in script
    assert "git branch --force gh-pages origin/gh-pages" in script
    assert "refs/heads/main" in script


def test_api_build_is_reusable_read_only_and_autopilot_uses_previous_sha():
    api = workflow("api-docs.yml")
    assert "workflow_call" in api["on"]
    assert api["permissions"] == {"contents": "read"}
    script = "\n".join(step.get("run", "") for step in api["jobs"]["build"]["steps"])
    assert "generate_reference.py" in script
    assert "npm ci --prefix scripts/docs_ai" in script
    assert "build_api.py" in script
    assert "mkdocs build --strict" in script
    assert "https://api.faxbot.net" not in script
    autopilot = workflow("docs-ai.yml")
    assert set(autopilot["on"]["push"]["branches"]) == {"main", "feat/faxbot-refresh"}
    assert "paths" not in autopilot["on"]["push"]
    plan = autopilot["jobs"]["propose-docs"]
    assert plan["steps"][-2]["env"]["DOCS_BASE_SHA"] == "${{ github.event.before }}"
    assert "origin/development" not in (ROOT / ".github/workflows/docs-ai.yml").read_text()
