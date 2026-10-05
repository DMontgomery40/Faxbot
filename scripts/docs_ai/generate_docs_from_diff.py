#!/usr/bin/env python3
"""
Docs Autopilot: plans and proposes instructional documentation updates from code changes.

Modes:
  - Plan (default): write a markdown checklist of suggested updates using heuristics (no network).
  - Proposal (--llm): ask a model for a unified diff of maintained Markdown under docs/.
      codex       (default, local) runs `codex exec` read-only on the person's own Codex sign-in.
                  No API key is used. Install the Codex CLI and run `codex login` first.
      openrouter  (GitHub Actions) calls OpenRouter's chat completions API with the
                  OPENROUTER_API_KEY repository secret.

Usage:
  python scripts/docs_ai/generate_docs_from_diff.py --base <previous-source-sha> \
    [--llm codex|openrouter] [--apply]
  make docs-propose BASE=<previous-source-sha> [APPLY=1]

Environment:
  DOCS_AI_MODEL              model id (codex default gpt-6-luna; openrouter default openai/gpt-6-luna)
  DOCS_AI_REASONING_EFFORT   reasoning effort (default medium)
  OPENROUTER_API_KEY         openrouter only

Notes:
  - Never includes secrets/PHI in prompts; context is limited to public repository files.
  - A proposal reaches mkdocs-docs-llm.patch only after validate_doc_patch.py accepts it.
  - --apply then stages it; nothing is committed or published automatically.
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import List, Optional
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_URL = 'https://github.com/DMontgomery40/Faxbot'
PATCH_NAME = 'mkdocs-docs-llm.patch'
TIMEOUT_SECONDS = 15 * 60
MAX_TOKENS = 16000
DEFAULT_MODELS = {'codex': 'gpt-6-luna', 'openrouter': 'openai/gpt-6-luna'}
OPENROUTER_MODELS = ('openai/gpt-6-luna', 'anthropic/claude-sonnet-5.5')
# Request shape checked against https://openrouter.ai/docs/use-cases/reasoning-tokens on 2026-10-03:
# reasoning effort is the nested {"reasoning": {"effort": ...}} object (there is no top-level
# reasoning_effort), max_tokens covers reasoning and visible output together, and an exhausted
# budget ends with finish_reason "length" and empty content. No temperature is sent: GPT-6 Luna
# does not list it and Claude Sonnet 5.5 rejects non-default sampling.
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
SYSTEM_PROMPT = 'You are a senior technical writer. Output one unified diff only.'


class ProposalError(Exception):
    """A proposal could not be produced; the message is one sentence for the person running it."""


def run(cmd: str, cwd: Optional[Path] = None, check: bool = True) -> str:
    p = subprocess.run(cmd, cwd=str(cwd or ROOT), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\n{p.stderr}")
    return p.stdout


def git_diff_names(base: str) -> List[str]:
    out = run(f"git diff --name-only {shlex.quote(base)}..HEAD")
    return [line.strip() for line in out.splitlines() if line.strip()]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def resolve_base(base_ref=None):
    base_ref = base_ref or os.getenv("DOCS_BASE_SHA")
    if base_ref and base_ref != "0" * 40:
        return run(f"git rev-parse --verify {shlex.quote(base_ref)}^{{commit}}").strip()
    try:
        return run("git rev-parse --verify HEAD^", check=True).strip()
    except RuntimeError:
        # A first commit has no parent. Store the real empty tree for git diff.
        return run("git hash-object -w -t tree --stdin < /dev/null").strip()


def build_plan(base_ref=None) -> str:
    base_ref = resolve_base(base_ref)
    changed = git_diff_names(base_ref)
    # High-signal inputs
    agents = read_text(ROOT / "AGENTS.md")
    openapi_json = read_text(ROOT / "docs/generated/openapi.json")
    traits = read_text(ROOT / "config" / "provider_traits.json")
    env_example = read_text(ROOT / ".env.example")

    bullets = []
    if any(p.startswith("api/app/") or p == "api/openapi.json" for p in changed):
        bullets.append("Update API usage examples and error mapping in docs, align with OpenAPI.")
    if any(p.startswith("api/admin_ui/") for p in changed):
        bullets.append("Admin Console screenshots/flows may need refresh; verify tooltips and docsBase links.")
    if any(p.startswith("config/") for p in changed):
        bullets.append("Review provider traits and update backend‑specific guidance; avoid backend name gating.")
    if any(p.endswith(".env") or p.endswith(".env.example") for p in changed):
        bullets.append("Reflect new/changed environment variables in deployment docs and security notes.")
    if any("sinch" in p.lower() or "phaxio" in p.lower() for p in changed):
        bullets.append("Re-run inbound/outbound setup steps for the affected provider and update caveats.")
    if not bullets:
        bullets.append("No high‑signal changes detected; run periodic doc hygiene (links, anchors).")

    plan = [
        "# Docs Autopilot Plan (heuristic)",
        f"Base: {base_ref}",
        "", "## Changed files", *[f"- {p}" for p in changed[:200]],
        "", "## Suggested updates", *[f"- {b}" for b in bullets],
        "", "## Context snapshots",
        "### AGENTS.md (excerpt)", agents[:3000],
        "", "### provider_traits.json (excerpt)", (traits or "")[:2000],
        "", "### .env.example (excerpt)", (env_example or "")[:2000],
        "", "### OpenAPI (truncated)", (openapi_json or "Fresh OpenAPI is generated by the source-reference workflow; no stored snapshot is used.")[:2000],
    ]
    return "\n".join(plan)


# -- providers ---------------------------------------------------------------------------

def _model(provider):
    return os.getenv('DOCS_AI_MODEL') or DEFAULT_MODELS[provider]


def _effort():
    effort = os.getenv('DOCS_AI_REASONING_EFFORT') or 'medium'
    if re.fullmatch(r'[a-z]{1,16}', effort) is None:
        raise ProposalError('DOCS_AI_REASONING_EFFORT must be one word, for example low, medium or high.')
    return effort


def call_codex(prompt: str) -> str:
    """Run Codex read-only on the person's own sign-in; return its last message."""
    if shutil.which('codex') is None:
        raise ProposalError('The Codex CLI is not installed; install it and run codex login, then try again.')
    try:
        status = subprocess.run(['codex', 'login', 'status'], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        status = None
    if status is None or status.returncode != 0 or 'Logged in' not in (status.stdout or '') + (status.stderr or ''):
        raise ProposalError('Codex is not signed in; run codex login, then try again.')
    model, effort = _model('codex'), _effort()
    with tempfile.TemporaryDirectory(prefix='faxbot-docs-codex-') as temporary:
        last_message = Path(temporary) / 'last-message.txt'
        command = ['codex', 'exec', '-m', model, '-s', 'read-only', '--ephemeral', '--ignore-user-config',
                   '--skip-git-repo-check', '-C', str(ROOT), '-c', f'model_reasoning_effort="{effort}"',
                   '-o', str(last_message), '-']
        try:
            result = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise ProposalError('Codex did not finish within 15 minutes; nothing was written.') from None
        if result.returncode != 0:
            raise ProposalError(f'Codex stopped with exit status {result.returncode}; nothing was written.')
        return last_message.read_text(encoding='utf-8') if last_message.exists() else ''


def call_openrouter(prompt: str) -> str:
    """Ask OpenRouter's chat completions API; return the message text."""
    key = os.getenv('OPENROUTER_API_KEY')
    if not key:
        raise ProposalError('OPENROUTER_API_KEY is not set; add it as a repository secret, or run locally with '
                            '--llm codex.')
    body = {
        'model': _model('openrouter'),
        'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': prompt}],
        'max_tokens': MAX_TOKENS,
        'reasoning': {'effort': _effort()},
    }
    request = urllib.request.Request(OPENROUTER_URL, data=json.dumps(body).encode('utf-8'), method='POST', headers={
        'Authorization': f'Bearer {key}', 'Content-Type': 'application/json',
        'HTTP-Referer': REPOSITORY_URL, 'X-Title': 'Faxbot Docs Autopilot'})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        raise ProposalError(f'OpenRouter refused the request (HTTP {error.code}); nothing was written.') from None
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        raise ProposalError('OpenRouter could not be reached or did not answer within 15 minutes; nothing was '
                            'written.') from None
    except ValueError:
        raise ProposalError('OpenRouter sent an answer that is not JSON; nothing was written.') from None
    if not isinstance(payload, dict) or payload.get('error'):
        raise ProposalError('OpenRouter reported an error instead of a proposal; nothing was written.')
    choices = payload.get('choices') or [{}]
    choice = choices[0] if isinstance(choices[0], dict) else {}
    finish = choice.get('finish_reason')
    content = (choice.get('message') or {}).get('content')
    if finish in {'content_filter', 'error', 'length'} or not isinstance(content, str) or not content.strip():
        raise ProposalError(f"OpenRouter returned no usable text (finish reason: {finish or 'none'}); nothing was "
                            'written.')
    return content


PROVIDERS = {'codex': call_codex, 'openrouter': call_openrouter}


def call_llm(prompt: str, provider: str) -> str:
    try:
        return PROVIDERS[provider](prompt)
    except KeyError:
        raise ProposalError(f'Unknown proposal provider {provider!r}; use codex or openrouter.') from None


def extract_diff(text: str) -> Optional[str]:
    """The unified diff in a model reply, without Markdown fences; None when there is none."""
    start = re.compile(r'^(diff --git |--- )', re.M)
    for block in re.findall(r'```[A-Za-z]*[ \t]*\n(.*?)```', text or '', re.S):
        if start.search(block):
            text = block
            break
    match = start.search(text or '')
    if match is None:
        return None
    diff = text[match.start():]
    return diff if diff.endswith('\n') else diff + '\n'


def validated_patch(reply: str) -> str:
    """The reply's diff, after the maintained-docs validator accepts it as a whole."""
    patch = extract_diff(reply)
    if patch is None:
        raise ProposalError('The model proposed no documentation changes; nothing was written.')
    with tempfile.TemporaryDirectory(prefix='faxbot-docs-proposal-') as temporary:
        candidate = Path(temporary) / PATCH_NAME
        candidate.write_text(patch, encoding='utf-8')
        checked = subprocess.run([sys.executable, str(Path(__file__).with_name('validate_doc_patch.py')),
                                  str(candidate)], cwd=ROOT, capture_output=True, text=True)
    if checked.returncode != 0:
        # Keep what was refused and why, so a person can see what the model tried.
        rejected = ROOT / 'mkdocs-docs-llm.rejected.patch'
        rejected.write_text(patch, encoding='utf-8')
        reason = (checked.stderr or checked.stdout or '').strip().splitlines()[-1:] or ['no reason given']
        raise ProposalError('The proposed patch does not apply or changes files outside maintained docs '
                            f'Markdown; nothing was written. Validator: {reason[0]} The refused patch is in '
                            f'{rejected.name}.')
    return patch


def proposal_prompt(plan: str) -> str:
    return f"""
You are a senior technical writer for Faxbot. Based on the repo context and changes, propose concise updates to the maintained instructional documentation.

Constraints:
- Only modify maintained Markdown files under docs/. Never modify docs/generated/, docs/architecture/, README.md, planning/, agent instructions, workflow files or build configuration.
- Keep copy terse and consistent with existing tone.
- Use provider traits (config/provider_traits.json) as truth. Do not mix backends.
- Link the current source reference with relative generated/ links. Legacy faxbot.net/api compatibility is a separate deployment.
- Use Markdown headings and keep pages short.
- You may read repository files for context. Do not run commands that change anything.
- Output only a single unified diff patch (git apply format, paths relative to the repository root) with context, and nothing else. If no change is needed, output nothing.

=== Plan and context ===
{plan}
"""


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="Previous source SHA; defaults to DOCS_BASE_SHA or HEAD's parent")
    ap.add_argument("--llm", nargs='?', const='codex', choices=sorted(PROVIDERS), default=None,
                    help="Ask a model for a docs patch: codex (default, local sign-in) or openrouter (CI)")
    ap.add_argument("--apply", action="store_true", help="Stage the validated patch with git apply --index")
    args = ap.parse_args()

    plan = build_plan(args.base)
    if not args.llm:
        out = ROOT / "mkdocs-docs-plan.md"
        out.write_text(plan, encoding="utf-8")
        print(f"Wrote plan: {out}")
        return

    out_patch = ROOT / PATCH_NAME
    # A failed run must not leave an earlier proposal looking current.
    out_patch.unlink(missing_ok=True)
    try:
        patch = validated_patch(call_llm(proposal_prompt(plan), args.llm))
    except ProposalError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
    out_patch.write_text(patch, encoding="utf-8")
    print(f"Validated documentation patch saved: {out_patch}")
    if args.apply:
        try:
            run(f"git apply --index {shlex.quote(str(out_patch))}")
            print("Patch applied to index. Review with git diff --cached and commit.")
        except Exception as e:
            print(f"Failed to apply patch: {e}")
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
