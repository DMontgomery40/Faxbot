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


def test_docs_proposals_run_only_by_hand_through_openrouter_and_open_a_pr_only_when_asked():
    text = (ROOT / ".github/workflows/docs-ai.yml").read_text()
    for retired in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_MODEL", "ANTHROPIC_MODEL", "apply-llm"):
        assert retired not in text
    autopilot = workflow("docs-ai.yml")
    assert "pull_request" not in autopilot["on"]
    inputs = autopilot["on"]["workflow_dispatch"]["inputs"]
    assert inputs["model"]["options"] == ["openai/gpt-6-luna", "anthropic/claude-sonnet-5.5"]
    assert inputs["model"]["default"] == "openai/gpt-6-luna"
    assert inputs["apply"]["default"] == "false"
    jobs = autopilot["jobs"]
    propose = jobs["propose-llm"]
    assert propose["if"] == "github.event_name == 'workflow_dispatch'"
    assert "permissions" not in propose  # inherits contents: read
    step = next(s for s in propose["steps"] if s.get("name") == "Produce and validate an instructional proposal")
    assert step["env"] == {"BASE_REF": "${{ inputs.base_ref }}", "DOCS_AI_MODEL": "${{ inputs.model }}",
                           "OPENROUTER_API_KEY": "${{ secrets.OPENROUTER_API_KEY }}"}
    assert "--llm openrouter" in step["run"]
    assert step["run"].index("generate_docs_from_diff.py") < step["run"].index("validate_doc_patch.py")
    upload = propose["steps"][-1]
    assert upload["uses"].startswith("actions/upload-artifact") and upload["with"]["path"] == "mkdocs-docs-llm.patch"
    opened = jobs["open-proposal"]
    assert opened["needs"] == "propose-llm" and "inputs.apply" in opened["if"]
    assert opened["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert not any("secrets." in json_value for s in opened["steps"] for json_value in map(str, s.values()))
    staged = "\n".join(s.get("run", "") for s in opened["steps"])
    assert staged.index("validate_doc_patch.py") < staged.index("git apply --index")
    assert "validate_doc_patch.py --staged" in staged
