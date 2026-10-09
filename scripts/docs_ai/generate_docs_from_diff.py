#!/usr/bin/env python3
"""
Docs Autopilot: an independent reviewer that checks the guides in docs/ against the code.

The model did not write the code. It reads what changed (code first), checks every claim on the
maintained pages that describe it, fixes contradictions, adds brief coverage for user-visible
behavior no page explains, and reports where the code itself looks wrong ("findings").

Modes:
  - Plan (default): write a markdown checklist of suggested updates using heuristics (no network).
  - Review (--llm): review base..HEAD; the reply is a docs patch plus findings.
  - Audit (--audit [--pages <globs>]): check maintained pages against the current code, with no
      base, a few pages per model call; also proposes deleting stale pages and merging duplicates.
  Providers:
      codex       (default, local) runs `codex exec` read-only on the person's own Codex sign-in.
                  No API key is used. Install the Codex CLI and run `codex login` first.
      openrouter  (GitHub Actions) calls OpenRouter's chat completions API with the
                  OPENROUTER_API_KEY repository secret, answering the model's read-only tool calls
                  (read_file, list_dir, git_diff, grep) so it sees what Codex sees.

Outputs: mkdocs-docs-llm.patch (only after validate_doc_patch.py accepts it) and
mkdocs-docs-findings.md (always, also when there is no patch). Findings without a patch exit 0.

Usage:
  python scripts/docs_ai/generate_docs_from_diff.py --base <previous-source-sha> \
    [--llm codex|openrouter] [--apply]
  python scripts/docs_ai/generate_docs_from_diff.py --audit [--pages 'docs/setup/*.md'] [--llm codex]
  make docs-propose BASE=<previous-source-sha> [APPLY=1]
  make docs-audit [PAGES='docs/setup/sip-asterisk.md docs/plugins/*.md']

Environment:
  DOCS_AI_MODEL              model id (codex default gpt-6-luna; openrouter default openai/gpt-6-luna)
  DOCS_AI_REASONING_EFFORT   reasoning effort (default medium)
  DOCS_AI_AUDIT_BATCH        pages per model call in an audit (default 4, at most 10)
  OPENROUTER_API_KEY         openrouter only

Notes:
  - Never includes secrets/PHI in prompts; context is limited to public repository files.
  - A proposal reaches mkdocs-docs-llm.patch only after validate_doc_patch.py accepts it.
  - --apply then stages it; nothing is committed or published automatically.
"""
import fnmatch
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
import time
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

# -- the review: what the model is asked, and the read-only tools it gets ---------------------------------

FINDINGS_NAME = 'mkdocs-docs-findings.md'
# The OpenRouter tool loop: about 60 tool calls, about 40k characters per result, 15 minutes in all, and a
# ceiling on everything the tools return so the conversation stays inside the model's context window.
MAX_TOOL_CALLS = 60
MAX_RESULT_CHARS = 40_000
MAX_TOTAL_TOOL_CHARS = 600_000
# Never shown to the model, whatever path or pattern it asks for.
REFUSED_PARTS = {'.git', '.local-handoff', 'research', 'faxdata', 'node_modules'}
REFUSED_SUFFIXES = {'.key', '.cred'}
REFUSED_PREFIXES = ('.env', 'MAJOR-IMPROVEMENT', 'ENTERPRISE-HANDOFF', '.LEAD-NOTE')
EXCLUDED_PATHSPECS = [':(exclude,glob)**/.env*', ':(exclude,glob)**/*.key', ':(exclude,glob)**/*.cred',
                      *(f':(exclude,glob)**/{part}/**' for part in sorted(REFUSED_PARTS - {'.git'}))]


class Refused(Exception):
    """A tool request the model may not make; the message goes back to the model."""


def guarded_path(path) -> Path:
    """The repository path a tool may read, or Refused."""
    text = str(path or '.').strip() or '.'
    candidate = Path(text)
    target = (ROOT / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    root = ROOT.resolve()
    if target != root and root not in target.parents:
        raise Refused('That path is outside the repository.')
    relative = target.relative_to(root)
    for part in relative.parts:
        if part in REFUSED_PARTS or part.startswith(REFUSED_PREFIXES) or Path(part).suffix in REFUSED_SUFFIXES:
            raise Refused(f'{relative.as_posix()} is not available to Docs Autopilot.')
    return target


def _allowed(relative: str) -> bool:
    try:
        guarded_path(relative)
        return True
    except Refused:
        return False


def _cap(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f'\n[... cut at {MAX_RESULT_CHARS} characters; ask for a narrower range ...]\n'


def tool_read_file(base, path, start=None, end=None):
    target = guarded_path(path)
    if not target.is_file():
        raise Refused(f'{path} is not a file.')
    lines = target.read_text(encoding='utf-8', errors='replace').splitlines()
    first = max(1, int(start or 1))
    last = min(len(lines), int(end or len(lines)))
    numbered = [f'{number:>5}  {lines[number - 1]}' for number in range(first, last + 1)]
    return _cap(f'{path} lines {first}-{last} of {len(lines)}\n' + '\n'.join(numbered))


def tool_list_dir(base, path='.'):
    target = guarded_path(path)
    if not target.is_dir():
        raise Refused(f'{path} is not a folder.')
    root = ROOT.resolve()
    entries = []
    for child in sorted(target.iterdir()):
        relative = child.relative_to(root).as_posix()
        if _allowed(relative):
            entries.append(child.name + ('/' if child.is_dir() else ''))
    return _cap('\n'.join(entries) or '(empty)')


def tool_git_diff(base, paths=None):
    wanted = [str(path) for path in (paths or []) if str(path).strip()]
    for path in wanted:
        guarded_path(path)
    result = subprocess.run(['git', 'diff', f'{base}..HEAD', '--', *(wanted or ['.']), *EXCLUDED_PATHSPECS],
                            cwd=ROOT, capture_output=True, text=True, errors='replace')
    if result.returncode != 0:
        raise Refused('git diff could not read that change.')
    return _cap(result.stdout or '(no changes in these paths)')


def tool_grep(base, pattern, path='.'):
    if not str(pattern or '').strip() or len(str(pattern)) > 200:
        raise Refused('Give a pattern of 1 to 200 characters.')
    guarded_path(path)
    result = subprocess.run(['git', 'grep', '-n', '-I', '-e', str(pattern), '--', str(path or '.'),
                             *EXCLUDED_PATHSPECS], cwd=ROOT, capture_output=True, text=True, errors='replace')
    if result.returncode not in (0, 1):
        raise Refused('That pattern could not be searched.')
    lines = [line for line in result.stdout.splitlines() if _allowed(line.split(':', 1)[0])]
    return _cap('\n'.join(lines) or '(no matches)')


TOOLS = {'read_file': tool_read_file, 'list_dir': tool_list_dir, 'git_diff': tool_git_diff, 'grep': tool_grep}
TOOL_SPECS = [
    {'type': 'function', 'function': {
        'name': 'read_file', 'description': 'Read a repository file with line numbers (optionally lines start..end).',
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': 'Path relative to the repository root.'},
            'start': {'type': 'integer', 'description': 'First line, from 1.'},
            'end': {'type': 'integer', 'description': 'Last line.'}}, 'required': ['path']}}},
    {'type': 'function', 'function': {
        'name': 'list_dir', 'description': 'List a repository folder.',
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': 'Folder relative to the repository root.'}}, 'required': []}}},
    {'type': 'function', 'function': {
        'name': 'git_diff', 'description': 'The change under review (base..HEAD), for some paths or all of it.',
        'parameters': {'type': 'object', 'properties': {
            'paths': {'type': 'array', 'items': {'type': 'string'},
                      'description': 'Files or folders; leave out for the whole change.'}}, 'required': []}}},
    {'type': 'function', 'function': {
        'name': 'grep', 'description': 'Search tracked files with git grep -n.',
        'parameters': {'type': 'object', 'properties': {
            'pattern': {'type': 'string', 'description': 'A git grep pattern.'},
            'path': {'type': 'string', 'description': 'Folder or file to search; the whole repository by default.'}},
            'required': ['pattern']}}},
]


def run_tool(base, name, arguments):
    """One tool call's result text; a refusal or a bad request is explained to the model, never raised."""
    tool = TOOLS.get(name)
    if tool is None:
        return f'Unknown tool {name!r}; use read_file, list_dir, git_diff or grep.'
    try:
        values = json.loads(arguments or '{}')
        if not isinstance(values, dict):
            raise ValueError
        return tool(base, **values)
    except Refused as refused:
        return f'Refused: {refused}'
    except (ValueError, TypeError):
        return f'The arguments for {name} were not understood.'
    except OSError:
        return f'{name} could not read that.'


def review_context(base_ref=None):
    """What the prompt names: the change's two commits, its files and the maintained guide pages."""
    base = resolve_base(base_ref)
    head = run('git rev-parse HEAD').strip()
    stat = run(f'git diff --stat=160 {shlex.quote(base)}..HEAD').splitlines()
    if len(stat) > 300:
        stat = stat[:299] + [f'... and {len(stat) - 299} more lines; use git diff --stat to see them']
    pages = maintained_pages()
    return {'base': base, 'head': head, 'stat': '\n'.join(stat) or '(no changes)', 'pages': '\n'.join(pages)}


# Who reads every Faxbot doc and screen (AGENTS.md "Who reads what Faxbot writes"), stated in the prompts
# themselves so the rule holds even when the model never opens AGENTS.md.
READER_RULE = """Who reads these pages: the administrator who set Faxbot up and handles its exceptions, usually the same person who runs its server, network and provider accounts. Staff at a company running Faxbot never open the docs or the console; they get their faxes by email. So:
- Speak to that administrator directly and tell them exactly what to set and where: the screen and setting, the command, or the value to enter in their phone system or router.
- Never write "ask your administrator", "give this to your (phone system) administrator", "ask your IT team", "ask whoever installed Faxbot" or anything else that treats the reader as someone without access. The reader is that person.
- Name another party only when real companies really have one: the fax carrier or provider, a partner who manages an Avaya or BT phone system, the recipient's fax machine. Even then, give the reader the exact settings to check or pass on.
- Keep developer internals (API internals, revision IDs, plugin manifests, environment-variable plumbing) out of operator guides; they belong on developer reference pages.
- Describe current behavior separately from research possibility. Faxbot targets international companies: scope legal and carrier conditions to the country, activity, account and source date. Do not infer a global prohibition from one jurisdiction, missing local equipment, an unsupported engine or an unmeasured benefit.
- Do not invent owner decisions. The owner permits useful faxbot.net document relay, storage and processing designs; opaque notice IDs and public-demo access limits do not prohibit those architectures. Earlier Phase 1 and four-fix batches are not permanent scope limits.
- Correct unsupported legal or economic absolutes in the affected guides instead of repeating them with an appended disclaimer. Byte savings may affect metered data or storage, but do not count an avoided call twice or claim savings without a cost basis. Recipient agreement and applicable document-handling requirements replace blanket industry exclusions; do not claim automatic compliance."""

# Screen paths in the guides went stale when the console's navigation was reorganized, and
# audits that checked labels alone missed them.
SCREEN_PATH_RULE = (
    "Check every screen path written like **Costs → Prices & plans** against "
    "api/admin_ui/src/navigation.tsx, which lists each top-level area and its pages (their label: values); "
    "a path whose area or page is not there is wrong, so fix it to the page that now holds that setting or button. "
    "A valid page label alone is not enough: follow its mounted component and section filters to confirm "
    "that the specific setting or control actually appears there. Shared settings components can expose "
    "different controls on different pages."
)

GENERATED_CLI_RULE = (
    "Never edit docs/reference/cli.md: api/app/cli/reference.py and make cli-docs regenerate it. "
    "Read it as evidence, and put user-facing explanations in maintained operator guides. "
    "If its generated help is wrong, report the responsible command definition as a code finding."
)

BEHAVIOR_RULE = (
    "For document-handling changes, trace sending, receiving, email delivery and document download "
    "through the code and relevant tests. Check defaults, explicit tools, automatic selection, recipient "
    "agreement, endpoint and decoder compatibility, integrity checks, retained originals and failure "
    "behavior. Check each affected client, including any browser decoder, rather than assuming all clients "
    "support a new format. Explain every changed operator behavior that lacks instructions in maintained "
    "guides; missing instructions are documentation work, not a reason to leave a code finding instead. "
    "Trace value units through their conversion helpers before reporting a numerical mismatch. Before "
    "calling a capability untested, read dated verification recorded in the changed tests and README, "
    "and state the evidence's actual scope. One recorded live route is neither universal interoperability "
    "nor no live validation; inability to repeat a recorded check here does not erase it. Distinguish "
    "supported conditions from unverified limits; a failing example alone does not prove a universal limitation."
)

DIFF_FORMAT = ("Write the diff exactly as `git diff` prints it: a `diff --git a/<path> b/<path>` line, `--- a/<path>` "
               "and `+++ b/<path>` lines, then hunks headed `@@ -<start>,<count> +<start>,<count> @@` with three "
               "unchanged context lines before and after each change. Do not use the apply_patch format or bare `@@` "
               "lines. Read the exact target section before writing each hunk and copy contiguous context from "
               "that section; never combine context from different commands or sections. Delete a whole page "
               "with `deleted file mode 100644` and every line removed.")


def proposal_prompt(context) -> str:
    base, head = context['base'], context['head']
    return f"""You are Docs Autopilot for Faxbot, a self-hosted fax server. You are an independent reviewer and technical writer. You did not write this code, so do not trust any page, comment or commit message to be right: check it against the code.

The change to review is {base}..{head}.

{READER_RULE}

How to work:
1. Read what changed, code first: `git diff {base}..HEAD` for the code (api/, asterisk/, scripts/, docker-compose*.yml, Makefile and similar), then for docs/.
2. Find every maintained page under docs/ that describes the changed behavior (search for setting names, commands, labels and routes). Check each claim on those pages against the code: setting names and defaults, button and screen labels (api/admin_ui/src), CLI commands and options (api/app/cli), API routes, numbers, limits and what the product actually does. {SCREEN_PATH_RULE} {BEHAVIOR_RULE}
3. Fix every contradiction you find. Where user-visible behavior from this change is not explained on any page, add a brief explanation where a reader would look for it.
4. Do not restate, reword or reorganize text that is already correct, and do not add marketing language. Text that addresses the reader as someone other than the administrator is not correct: fix it.
5. Write clear, natural prose. ASD-STE100 Simplified Technical English is loose inspiration only (about 20%): prefer shorter sentences and active voice where they help, use one term for one thing, and never chop explanations into fragments. Say plainly what is unverified.

Scope:
- {GENERATED_CLI_RULE}
- Change only maintained Markdown under docs/. Never change docs/generated/, docs/architecture/, README.md, planning/, AGENTS.md or other agent instructions, .github/ workflows, mkdocs.yml or any code.
- Never open .env files, *.key or *.cred files, .git/, .local-handoff/, research/, faxdata/ or node_modules/.
- Do not run anything that changes the repository.

While you read, note where the CODE itself looks wrong or inconsistent: a message that contradicts what the code does, a dead branch, a setting that has no effect, console text that contradicts the API, or a test that asserts something the code does not do. These are findings for the maintainers, not documentation changes.

Answer with exactly two fenced blocks and nothing else:

```diff
(one unified diff in git apply format, paths relative to the repository root, with context lines; leave this block empty when no page needs a change)
```

```findings
(one line per finding: path:line, then one sentence; or the single word none)
```

{DIFF_FORMAT}

Files changed in {base[:12]}..{head[:12]}:
{context['stat']}

Maintained documentation pages:
{context['pages']}
"""


SYSTEM_PROMPT = ('You are Docs Autopilot, an independent reviewer and technical writer for the Faxbot repository. '
                 'You did not write the code under review. You see the repository only through the read-only tools '
                 'read_file, list_dir, git_diff (the change under review) and grep: use them to read the code and '
                 'the docs before you answer.\n\n' + READER_RULE)


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


# The change under review, for the git_diff tool; None in an audit, which reads the current files only.
REVIEW = {'base': None}


def _post(key, body, timeout):
    request = urllib.request.Request(OPENROUTER_URL, data=json.dumps(body).encode('utf-8'), method='POST', headers={
        'Authorization': f'Bearer {key}', 'Content-Type': 'application/json',
        'HTTP-Referer': REPOSITORY_URL, 'X-Title': 'Faxbot Docs Autopilot'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
    return choices[0] if isinstance(choices[0], dict) else {}


def call_openrouter(prompt: str) -> str:
    """Ask OpenRouter's chat completions API, answering the model's read-only tool calls; return its final text.

    The tools see what Codex sees locally (the files, the change under review and git grep), inside the
    repository and never the refused paths. The loop stops at MAX_TOOL_CALLS, MAX_TOTAL_TOOL_CHARS or 15
    minutes; then the model is asked for its final answer with tools turned off.
    """
    key = os.getenv('OPENROUTER_API_KEY')
    if not key:
        raise ProposalError('OPENROUTER_API_KEY is not set; add it as a repository secret, or run locally with '
                            '--llm codex.')
    deadline = time.monotonic() + TIMEOUT_SECONDS
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': prompt}]
    calls = returned = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProposalError('OpenRouter could not be reached or did not answer within 15 minutes; nothing was '
                                'written.')
        exhausted = calls >= MAX_TOOL_CALLS or returned >= MAX_TOTAL_TOOL_CHARS or remaining < 120
        body = {
            'model': _model('openrouter'),
            'messages': messages,
            'max_tokens': MAX_TOKENS,
            'reasoning': {'effort': _effort()},
            # OpenRouter validates the tool schema on every request, so tools stay listed even when turned off.
            'tools': TOOL_SPECS,
            'tool_choice': 'none' if exhausted else 'auto',
        }
        choice = _post(key, body, remaining)
        finish = choice.get('finish_reason')
        message = choice.get('message') or {}
        requested = message.get('tool_calls') or []
        if requested and not exhausted:
            reply = {'role': 'assistant', 'content': message.get('content') or '', 'tool_calls': requested}
            if message.get('reasoning_details'):
                reply['reasoning_details'] = message['reasoning_details']
            messages.append(reply)
            for call in requested:
                function = call.get('function') or {}
                result = run_tool(REVIEW['base'], function.get('name'), function.get('arguments'))
                calls, returned = calls + 1, returned + len(result)
                messages.append({'role': 'tool', 'tool_call_id': call.get('id'), 'content': result})
            if calls >= MAX_TOOL_CALLS or returned >= MAX_TOTAL_TOOL_CHARS:
                messages.append({'role': 'user', 'content': 'You have used all your tool calls. Write your final '
                                                            'answer now: the diff block, then the findings block.'})
            continue
        content = message.get('content')
        if finish in {'content_filter', 'error', 'length'} or not isinstance(content, str) or not content.strip():
            raise ProposalError(f"OpenRouter returned no usable text (finish reason: {finish or 'none'}); nothing "
                                'was written.')
        return content


PROVIDERS = {'codex': call_codex, 'openrouter': call_openrouter}


def call_llm(prompt: str, provider: str) -> str:
    try:
        return PROVIDERS[provider](prompt)
    except KeyError:
        raise ProposalError(f'Unknown proposal provider {provider!r}; use codex or openrouter.') from None


# -- the reply: a docs patch and findings ------------------------------------------------------------------

# A reply's fenced block ends only at a line that is exactly ``` (column 0). Inside a diff, the docs' own
# code fences always carry a diff prefix (" ```", "+```", "-```"), so they never end the block early.
_BLOCK = re.compile(r'^```([A-Za-z]*)[ \t]*\n(.*?)^```[ \t]*$', re.M | re.S)


def _blocks(text):
    return [(kind.lower(), body) for kind, body in _BLOCK.findall(text or '')]


def extract_diff(text: str) -> Optional[str]:
    """The unified diff in a model reply, without Markdown fences; None when there is none."""
    start = re.compile(r'^(diff --git |--- )', re.M)
    blocks = [body for kind, body in _blocks(text) if kind != 'findings']
    for block in blocks:
        if start.search(block):
            text = block
            break
    else:
        text = _BLOCK.sub('', text or '') if blocks or '```findings' in (text or '') else (text or '')
    match = start.search(text)
    if match is None:
        return None
    diff = text[match.start():]
    return diff if diff.endswith('\n') else diff + '\n'


def extract_findings(text: str):
    """The findings lines in a model reply ([] for "none"), or None when the reply has no findings block."""
    found = [body for kind, body in _blocks(text) if kind == 'findings']
    if not found:
        return None
    lines = [re.sub(r'^[-*]\s+', '', line.strip()) for line in found[0].splitlines() if line.strip()]
    # "none" means no findings, also when a model writes it after real ones.
    return [line for line in lines if line.lower().rstrip('.') != 'none']


_HUNK = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$')


def recount_hunks(patch: str) -> str:
    """The patch with each hunk header's line counts taken from its lines (models often miscount them)."""
    lines = patch.splitlines(keepends=True)
    out, index = [], 0
    while index < len(lines):
        header = _HUNK.match(lines[index].rstrip('\n'))
        if header is None:
            out.append(lines[index])
            index += 1
            continue
        body, index = [], index + 1
        while index < len(lines) and not lines[index].startswith(('@@ ', 'diff --git ')) \
                and not (lines[index].startswith('--- ') and index + 1 < len(lines)
                         and lines[index + 1].startswith('+++ ')):
            body.append(lines[index])
            index += 1
        old = sum(1 for line in body if line[:1] in (' ', '-', '\n'))
        new = sum(1 for line in body if line[:1] in (' ', '+', '\n'))
        out.append(f'@@ -{header.group(1)},{old} +{header.group(2)},{new} @@{header.group(3)}\n')
        out.extend(body)
    return ''.join(out)


def _file_sections(patch):
    """[(path, header lines, body lines)] per file of a unified diff; path is None when it is not a plain edit."""
    sections, current = [], None
    for line in patch.splitlines(keepends=True):
        if line.startswith('diff --git ') or (line.startswith('--- ') and (current is None or current[2])):
            current = [None, [line], []]
            sections.append(current)
            continue
        if current is None:
            continue
        if not current[2] and not line.startswith('@@'):
            current[1].append(line)
            if line.startswith('+++ b/'):
                current[0] = line[6:].strip()
            elif line.startswith(('new file', 'deleted file', 'rename ', 'similarity ')) or '/dev/null' in line:
                current[0] = False
            continue
        current[2].append(line)
    return [(path or None, header, body) for path, header, body in sections]


def _rebuilt(path, body):
    """A fresh diff for one edited file: each hunk's old lines are found in HEAD's copy and replaced by its new
    lines, then the whole file is diffed again. None when a hunk's old lines are not in the file."""
    import difflib
    try:
        original = run(f'git show {shlex.quote("HEAD:" + path)}').splitlines(keepends=True)
    except RuntimeError:
        return None
    updated, cursor, hunks = list(original), 0, []
    for line in body:
        if line.startswith('@@'):  # also Codex's bare '@@' hunks, which carry no line numbers
            hunks.append(([], []))
        elif hunks and not line.startswith('\\'):
            text = line[1:] if line[:1] in (' ', '-', '+') else line
            text = text if text.endswith('\n') else text + '\n'
            if line[:1] != '+':
                hunks[-1][0].append(text)
            if line[:1] != '-':
                hunks[-1][1].append(text)
    for old, new in hunks:
        # A model often ends a hunk with blank lines the file does not have there.
        while old and new and old[-1] == new[-1] == '\n':
            old, new = old[:-1], new[:-1]
        found = next((start for start in range(cursor, len(updated) - len(old) + 1)
                      if updated[start:start + len(old)] == old), None) if old else None
        if found is None:
            return None
        updated[found:found + len(old)] = new
        cursor = found + len(new)
    diff = list(difflib.unified_diff(original, updated, f'a/{path}', f'b/{path}'))
    return f'diff --git a/{path} b/{path}\n' + ''.join(diff) if diff else ''


_BARE_HUNK = re.compile(r'^@@\s*$', re.M)


def _unusable(patch: str):
    """Pages whose hunks still have no line numbers: git apply would skip them without a word."""
    return [path or 'a page' for path, header, body in _file_sections(patch)
            if any(_BARE_HUNK.match(line.rstrip('\n')) for line in body)]


def normalize_patch(patch: str) -> str:
    """A model's diff made exact: edits are rebuilt against HEAD (miscounted hunks, missing context); anything
    else only gets its hunk counts recounted. The validator still decides."""
    pieces = []
    for path, header, body in _file_sections(patch):
        rebuilt = _rebuilt(path, body) if path else None
        pieces.append(rebuilt if rebuilt is not None else recount_hunks(''.join(header + body)))
    return ''.join(pieces) or patch


def _refusal(patch):
    """The validator's last line when it refuses the patch, or None when it accepts it."""
    with tempfile.TemporaryDirectory(prefix='faxbot-docs-proposal-') as temporary:
        candidate = Path(temporary) / PATCH_NAME
        candidate.write_text(patch, encoding='utf-8')
        checked = subprocess.run([sys.executable, str(Path(__file__).with_name('validate_doc_patch.py')),
                                  str(candidate)], cwd=ROOT, capture_output=True, text=True)
    if checked.returncode == 0:
        return None
    return ((checked.stderr or checked.stdout or '').strip().splitlines()[-1:] or ['no reason given'])[0]


def validated_patch(patch: str) -> str:
    """The patch, after the maintained-docs validator accepts it as a whole; ProposalError otherwise.

    A patch that does not apply as written gets one repair: its edits are rebuilt against HEAD
    (normalize_patch), because models often miscount hunk lines or drop trailing context. The repaired
    patch must pass the same validator; nothing outside maintained docs Markdown ever gets through.
    """
    reason = None if _unusable(patch) else _refusal(patch)
    if reason is None and not _unusable(patch):
        return patch
    repaired = normalize_patch(patch)
    unusable = _unusable(repaired)
    if unusable:
        reason = (f'the change to {", ".join(unusable)} does not match the page as it is in the repository, and its '
                  'hunks give no line numbers.')
    elif repaired != patch:
        repaired_reason = _refusal(repaired)
        if repaired_reason is None:
            return repaired
        reason = repaired_reason
    # Keep what was refused and why, so a person can see what the model tried.
    rejected = ROOT / 'mkdocs-docs-llm.rejected.patch'
    rejected.write_text(patch, encoding='utf-8')
    raise ProposalError('The proposed patch does not apply or changes files outside maintained docs '
                        f'Markdown; nothing was written. Validator: {reason} The refused patch is in '
                        f'{rejected.name}.')


def findings_text(lines, heading, note=''):
    body = '\n'.join(f'- {line}' for line in lines) if lines else 'None found.'
    return f'{heading}\n\n{note}\n\n### Possible problems in the code\n\n{body}\n'.replace('\n\n\n', '\n\n')


def _patch_stats(patch):
    files = re.findall(r'^diff --git a/(\S+) ', patch, re.M)
    added = sum(1 for line in patch.splitlines() if line.startswith('+') and not line.startswith('+++'))
    removed = sum(1 for line in patch.splitlines() if line.startswith('-') and not line.startswith('---'))
    return f'{len(files)} files, +{added} -{removed} lines'


# -- audit: every maintained page against the current code, a few pages per model call ---------------------

def maintained_pages():
    return [line for line in run('git ls-files docs').splitlines()
            if line.endswith('.md') and not line.startswith(('docs/generated/', 'docs/architecture/'))
            and line != 'docs/reference/cli.md']


def nav_pages():
    """Every docs page mkdocs.yml's nav names (paths relative to docs/)."""
    text = read_text(ROOT / 'mkdocs.yml')
    nav = re.search(r'^nav:\n(.*?)(?=^\S)', text + '\nend:\n', re.S | re.M)
    return set(re.findall(r'(?:^|\s|:)\s*([\w./-]+\.md)\s*$', nav.group(1), re.M)) if nav else set()


def audit_prompt(pages, everything, missing, head):
    listed = '\n'.join(f'- {page}' + ('  (not in mkdocs.yml nav)' if page in missing else '') for page in pages)
    return f"""You are Docs Autopilot for Faxbot, a self-hosted fax server. You are an independent reviewer and technical writer. You did not write this code or these pages, so do not trust any page, comment or commit message to be right: check it against the code.

This is an audit of the documentation against the CURRENT code at {head}; there is no change under review. Audit these pages:
{listed}

{READER_RULE}

How to work:
1. Read each page in full, then read the code it describes: api/app (settings in api/app/config_values.py, routes, behavior), the console (api/admin_ui/src), the CLI (api/app/cli), asterisk/, the Compose files and scripts/.
2. Check every claim against the code: setting names and defaults, button and screen labels, CLI commands and options, API routes, numbers, limits and what the product actually does. Fix every contradiction. {SCREEN_PATH_RULE} {BEHAVIOR_RULE}
3. Pages or sections that describe features, settings, screens or commands that no longer exist in the code: delete them in the diff (a whole page as a deleted file) and say so in a finding. If mkdocs.yml's nav lists a page you delete, say so too, because mkdocs.yml is changed by hand.
4. Pages that duplicate each other (also with pages outside this batch, listed below): propose merging them. Keep the better page, move anything it lacks into it, and delete the other.
5. Report each page of this batch that is not in mkdocs.yml's nav as a finding.
6. Text that addresses the reader as someone other than the administrator (for example "give your phone system administrator this address") breaks the rule above: rewrite it to tell the administrator what to set and where, and list each place you changed as a finding.
7. Developer internals in an operator guide (revision IDs, manifests, environment-variable plumbing): move them out or cut them, and say so in a finding.
8. Do not restate, reword or reorganize text that is already correct, and do not add marketing language.
9. Write clear, natural prose. ASD-STE100 Simplified Technical English is loose inspiration only (about 20%): prefer shorter sentences and active voice where they help, use one term for one thing, and never chop explanations into fragments. Say plainly what is unverified.

Scope:
- {GENERATED_CLI_RULE}
- Change only maintained Markdown under docs/. Never change docs/generated/, docs/architecture/, README.md, planning/, AGENTS.md or other agent instructions, .github/ workflows, mkdocs.yml or any code.
- Never open .env files, *.key or *.cred files, .git/, .local-handoff/, research/, faxdata/ or node_modules/.
- Do not run anything that changes the repository.

Also note where the CODE itself looks wrong or inconsistent: a message that contradicts what the code does, a dead branch, a setting that has no effect, console text that contradicts the API, or a test that asserts something the code does not do.

Answer with exactly two fenced blocks and nothing else:

```diff
(one unified diff in git apply format, paths relative to the repository root, with context lines; leave this block empty when no page needs a change)
```

```findings
(one line per finding: path:line, then one sentence; or the single word none)
```

{DIFF_FORMAT}

All maintained documentation pages:
{chr(10).join(everything)}
"""


def run_audit(provider, patterns, batch_size):
    pages_all = maintained_pages()
    pages = [page for page in pages_all if not patterns or any(fnmatch.fnmatch(page, pattern) for pattern in patterns)]
    if not pages:
        raise ProposalError('No maintained page under docs/ matches those patterns.')
    head = run('git rev-parse HEAD').strip()
    listed = nav_pages()
    missing = {page for page in pages if page.removeprefix('docs/') not in listed}
    REVIEW['base'] = None
    model = _model(provider)
    findings_path = ROOT / FINDINGS_NAME
    batches = [pages[start:start + batch_size] for start in range(0, len(pages), batch_size)]
    findings_path.write_text(f'## Docs Autopilot audit\n\nDocs Autopilot ({model}) audited {len(pages)} maintained '
                             f'pages against the code at {head[:12]}, {batch_size} pages per model call.\n',
                             encoding='utf-8')
    accepted, failed = [], 0
    for number, batch in enumerate(batches, 1):
        print(f'Batch {number} of {len(batches)}: {", ".join(batch)}', flush=True)
        lines, note = [f'{page}: not in mkdocs.yml nav.' for page in batch if page in missing], 'No page changes proposed.'
        try:
            reply = call_llm(audit_prompt(batch, pages_all, missing, head), provider)
            found = extract_findings(reply)
            patch = extract_diff(reply)
            if found is None and patch is None:
                raise ProposalError('The model returned neither a documentation patch nor findings.')
            lines += [line for line in found or [] if line not in lines]
            if patch:
                try:
                    accepted.append(validated_patch(patch))
                    note = f'Proposed changes: {_patch_stats(accepted[-1])}.'
                except ProposalError as error:
                    failed += 1
                    # Each batch keeps its own refused patch.
                    kept = ROOT / f'mkdocs-docs-llm.rejected.batch-{number}.patch'
                    (ROOT / 'mkdocs-docs-llm.rejected.patch').replace(kept)
                    note = f'The proposed patch for this batch was refused ({kept.name}): {error}'
        except ProposalError as error:
            failed += 1
            note = f'This batch produced no result: {error}'
        with findings_path.open('a', encoding='utf-8') as handle:
            handle.write(f'\n### Batch {number}: {", ".join(batch)}\n\n{note}\n\n'.replace('\n\n\n', '\n\n'))
            handle.write('\n'.join(f'- {line}' for line in lines) + '\n' if lines else 'None found.\n')
    if accepted:
        combined = ''.join(accepted)
        try:
            (ROOT / PATCH_NAME).write_text(validated_patch(combined), encoding='utf-8')
            print(f'Validated documentation patch saved: {ROOT / PATCH_NAME} ({_patch_stats(combined)})')
        except ProposalError as error:
            failed += 1
            print(f'The batches\' patches do not apply together: {error}', file=sys.stderr)
    print(f'Findings saved: {findings_path}')
    return failed


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="Previous source SHA; defaults to DOCS_BASE_SHA or HEAD's parent")
    ap.add_argument("--llm", nargs='?', const='codex', choices=sorted(PROVIDERS), default=None,
                    help="Ask a model for a docs patch: codex (default, local sign-in) or openrouter (CI)")
    ap.add_argument("--apply", action="store_true", help="Stage the validated patch with git apply --index")
    ap.add_argument("--audit", action="store_true",
                    help="Check maintained pages against the current code, a few pages per model call (no base)")
    ap.add_argument("--pages", default='', help="With --audit: page globs, separated by spaces or commas, "
                                                "for example 'docs/setup/*.md'")
    args = ap.parse_args()

    out_patch = ROOT / PATCH_NAME
    if args.audit:
        out_patch.unlink(missing_ok=True)
        batch = int(os.getenv('DOCS_AI_AUDIT_BATCH') or 4)
        try:
            failed = run_audit(args.llm or 'codex', [p for p in re.split(r'[\s,]+', args.pages) if p],
                               max(1, min(batch, 10)))
        except ProposalError as error:
            print(error, file=sys.stderr)
            raise SystemExit(1) from None
        raise SystemExit(1 if failed else 0)

    if not args.llm:
        plan = build_plan(args.base)
        out = ROOT / "mkdocs-docs-plan.md"
        out.write_text(plan, encoding="utf-8")
        print(f"Wrote plan: {out}")
        return

    # A failed run must not leave an earlier proposal or findings looking current.
    out_patch.unlink(missing_ok=True)
    findings_path = ROOT / FINDINGS_NAME
    findings_path.unlink(missing_ok=True)
    context = review_context(args.base)
    REVIEW['base'] = context['base']
    try:
        reply = call_llm(proposal_prompt(context), args.llm)
        found, patch = extract_findings(reply), extract_diff(reply)
        if patch is None and found is None:
            raise ProposalError('The model proposed no documentation changes; nothing was written.')
        heading = (f"## Docs Autopilot findings\n\nDocs Autopilot ({_model(args.llm)}) reviewed "
                   f"{context['base'][:12]}..{context['head'][:12]} against the maintained guides in docs/.")
        note = (f'It proposed documentation changes ({_patch_stats(patch)}).' if patch
                else 'It proposed no documentation changes.')
        if found is None:
            note += ' Its reply had no findings block.'
        findings_path.write_text(findings_text(found or [], heading, note), encoding='utf-8')
        if patch is None:
            print(f'The model proposed no documentation changes; its findings are in {findings_path.name}.')
            return
        patch = validated_patch(patch)
    except ProposalError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
    out_patch.write_text(patch, encoding="utf-8")
    print(f"Validated documentation patch saved: {out_patch}")
    if findings_path.exists():
        print(f"Findings saved: {findings_path}")
    if args.apply:
        try:
            run(f"git apply --index {shlex.quote(str(out_patch))}")
            print("Patch applied to index. Review with git diff --cached and commit.")
        except Exception as e:
            print(f"Failed to apply patch: {e}")
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
